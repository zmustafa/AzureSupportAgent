"""Application-class taxonomy for Conditional Access coverage.

Answers one question: **which application classes does this policy actually cover, and which
apps inside each class does it miss?**

Three things make this harder than it looks, and each one is a defect this module exists to
prevent:

1. **`All` is not a blanket.** The previous implementation expanded an `All` target into every
   class unconditionally. A policy targeting `All` while excluding SharePoint therefore rendered
   the collaboration class as *covered* — a false all-clear on the exact surface the reader came
   to check. Here, `All` means "every app in the tenant, minus the excluded ones", and a class is
   only covered when every resolved member of it survives that subtraction.

2. **Coverage has two axes, not one.** A policy can reach 100% of a cohort and only 50% of a
   class's apps (Teams but not SharePoint). Roll that up to a single "covered" and the
   dependency-split defect becomes invisible. Membership resolution therefore returns which
   member apps are hit and which are missed, and the caller keeps both fractions.

3. **Static app ids are hints, not truth.** Microsoft Learn publishes the Office 365 suite BY
   NAME and explicitly tells you to resolve the ids in your own tenant, because the suite changes
   and ids differ by cloud. So membership resolves from the tenant's own service principals
   first; the versioned id list only seeds and labels. Every id carries a `confidence` and, where
   it is not from a Learn page, a `verify_note` that the UI surfaces.

The taxonomy itself is a VERSIONED JSON document, so a snapshot taken months ago analyses into
the same classes it did then rather than silently re-classifying under a newer taxonomy.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent / "data"

DEFAULT_VERSION = "v1"

# Preset target strings Entra uses in `conditions.applications.includeApplications`.
TARGET_ALL = "All"
TARGET_OFFICE365 = "Office365"
TARGET_ADMIN_PORTALS = "MicrosoftAdminPortals"

# Client-app types that only a legacy protocol client uses. Anything reachable by these is the
# legacy auth surface, and it is detected from clientAppTypes rather than from any app id.
LEGACY_CLIENT_APP_TYPES = frozenset({"exchangeActiveSync", "other"})

# The two user actions Conditional Access supports (Learn: targeting page, "User actions").
USER_ACTION_REGISTER_DEVICE = "urn:user:registerdevice"
USER_ACTION_REGISTER_SECURITY_INFO = "urn:user:registersecurityinfo"

_cache: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def load(version: str = DEFAULT_VERSION) -> dict[str, Any]:
    """The versioned taxonomy document. Cached per process; the file never changes at runtime."""
    hit = _cache.get(version)
    if hit is not None:
        return hit
    with _lock:
        hit = _cache.get(version)
        if hit is not None:
            return hit
        path = _DATA_DIR / f"app_classes.{version}.json"
        if not path.is_file():
            raise FileNotFoundError(f"unknown application-class taxonomy version: {version}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        _cache[version] = doc
        return doc


def classes(version: str = DEFAULT_VERSION) -> list[dict[str, Any]]:
    """Class definitions in display order, without the membership internals."""
    doc = load(version)
    out = []
    for c in sorted(doc["classes"], key=lambda c: c.get("order", 999)):
        out.append({
            "id": c["id"],
            "label": c["label"],
            "basis": c["basis"],
            # The UI renders this under the class heading. It was omitted from this projection,
            # so every class description silently rendered as nothing.
            "description": c["basis"],
            "derived": bool(c.get("derived")),
            # Consumed by `ca.class_never_targeted`, which defers to the per-action detectors
            # for these. Omitting it here made that deferral silently ineffective.
            "user_action_based": bool(c.get("user_action_based")),
            # Surfaced so the UI can show a "verify against Microsoft Learn" note rather than
            # presenting an unverified id as fact.
            "unverified_members": [
                {"app_id": m["app_id"], "name": m["name"], "note": m.get("verify_note", "")}
                for m in c.get("members", [])
                if m.get("confidence") != "verified"
            ],
        })
    return out


def class_ids(version: str = DEFAULT_VERSION) -> list[str]:
    return [c["id"] for c in classes(version)]


def applicable_controls(class_id: str, all_controls: list[str], version: str = DEFAULT_VERSION) -> set[str]:
    """Controls Entra actually permits for a class.

    Entra disables every grant except MFA and authentication strength for the
    'Register or join devices' user action. Rendering the others as gaps would invent work that
    cannot be done — an inapplicable cell is `n/a`, never a finding."""
    limit = (load(version).get("control_applicability") or {}).get(class_id)
    return set(limit) & set(all_controls) if limit else set(all_controls)


# ------------------------------------------------------------------ tenant app resolution
def build_app_index(snapshot_data: dict[str, Any], tenant_id: str, version: str = DEFAULT_VERSION) -> dict[str, Any]:
    """Index the tenant's own service principals into the taxonomy.

    This is the authoritative membership source. The static id list seeds well-known apps and
    supplies labels; everything else is whatever the tenant actually has."""
    doc = load(version)
    apps = snapshot_data.get("apps") or {}
    sps = [
        s for s in apps.get("service_principals") or []
        if s.get("app_id") and s.get("sp_type") == "Application"
    ]
    ms_tenants = {t.lower() for t in doc.get("microsoft_publisher_tenants") or []}
    tid = (tenant_id or "").lower()

    by_app_id: dict[str, dict[str, Any]] = {}
    for s in sps:
        app_id = str(s["app_id"]).lower()
        by_app_id[app_id] = {
            "app_id": app_id,
            "name": s.get("display_name") or app_id,
            "enabled": bool(s.get("enabled", True)),
            "owner_tenant": str(s.get("app_owner_tenant_id") or "").lower(),
        }

    members: dict[str, set[str]] = {c["id"]: set() for c in doc["classes"]}
    labels: dict[str, str] = {a["app_id"]: a["name"] for a in by_app_id.values()}

    for cls in doc["classes"]:
        cid = cls["id"]
        # Seeded ids — only counted when the app actually exists in this tenant. A class member
        # the tenant does not have is not a gap; it is not applicable.
        for m in cls.get("members", []):
            aid = str(m["app_id"]).lower()
            labels.setdefault(aid, m["name"])
            if aid in by_app_id:
                members[cid].add(aid)
        # Name matching, which is what Learn prescribes for the Office 365 suite.
        patterns = [p.lower() for p in cls.get("name_patterns") or []]
        if patterns:
            for a in by_app_id.values():
                low = a["name"].lower()
                if any(p in low for p in patterns):
                    members[cid].add(a["app_id"])

    # Ownership-derived classes. `appOwnerOrganizationId` is real tenant evidence, not a guess.
    for a in by_app_id.values():
        owner = a["owner_tenant"]
        if owner and owner == tid:
            members["custom_lob"].add(a["app_id"])
        elif owner and owner not in ms_tenants:
            members["third_party_saas"].add(a["app_id"])
        elif not owner:
            # No owning tenant recorded — a single-tenant registration in this directory.
            members["custom_lob"].add(a["app_id"])

    return {
        "version": version,
        "all_app_ids": set(by_app_id),
        # Every application in the tenant, not only the taxonomy's hard-coded members. The
        # derived-exposure list names applications the taxonomy has never heard of, and without
        # the full map it rendered them as raw GUIDs — which tells a reader that something is
        # wrong but not which application to go and look at.
        "labels": {**{a["app_id"]: a["name"] for a in by_app_id.values()}, **labels},
        "members": {k: v for k, v in members.items()},
        "app_count": len(by_app_id),
    }


# ------------------------------------------------------------------- policy → class coverage
def resolve_policy(
    policy: dict[str, Any],
    index: dict[str, Any],
    version: str = DEFAULT_VERSION,
) -> dict[str, dict[str, Any]]:
    """Which classes this policy touches, and which member apps it hits and misses.

    Returns `{class_id: {"hit": {app ids}, "missed": {app ids}, "basis": str}}`. A class is
    absent when the policy does not touch it at all.

    `basis` records HOW the class was reached (`all`, `preset`, `explicit`, `client_app`,
    `user_action`, `scoped`) because "covered because a policy targets All" and "covered because
    somebody deliberately targeted this class" are different facts to a reader deciding whether
    the coverage is intentional."""
    doc = load(version)
    conditions = policy.get("conditions") or {}
    include = {str(a) for a in conditions.get("include_apps") or []}
    exclude = {str(a).lower() for a in conditions.get("exclude_apps") or []}
    members: dict[str, set[str]] = index["members"]
    out: dict[str, dict[str, Any]] = {}

    def record(cid: str, hit: set[str], basis: str) -> None:
        pool = members.get(cid, set())
        prev = out.get(cid)
        hit = hit & pool if pool else hit
        if prev:
            prev["hit"] |= hit
            prev["missed"] = pool - prev["hit"]
        else:
            out[cid] = {"hit": set(hit), "missed": pool - hit, "basis": basis}

    targets_all = TARGET_ALL in include
    if targets_all:
        # Every app in the tenant, MINUS the exclusions. This subtraction is the whole reason
        # the class can come back partial instead of covered.
        reachable = set(index["all_app_ids"]) - exclude
        for cls in doc["classes"]:
            cid = cls["id"]
            if cls.get("derived") or cls.get("user_action_based") or cls.get("client_app_based"):
                continue
            if cid == "all_cloud_apps":
                # The class IS the target; it is fully covered only when nothing is excluded.
                out[cid] = {
                    "hit": set() if exclude else {"*"},
                    "missed": exclude,
                    "basis": "all",
                }
                continue
            record(cid, reachable, "all")

    explicit = {a.lower() for a in include if a not in (TARGET_ALL, TARGET_OFFICE365, TARGET_ADMIN_PORTALS)}
    if explicit:
        for cls in doc["classes"]:
            cid = cls["id"]
            pool = members.get(cid, set())
            hit = explicit & pool
            if hit:
                record(cid, hit, "explicit")

    presets = doc.get("preset_targets") or {}
    for preset, cid in presets.items():
        if preset == TARGET_ALL or preset not in include:
            continue
        pool = members.get(cid, set())
        # A preset covers its whole grouping by definition, minus any explicit exclusions.
        record(cid, pool - exclude, "preset")
        if preset == TARGET_OFFICE365:
            # The bundle also reaches the collaboration apps inside it. Recording this is what
            # makes bundle_member_divergence detectable: the bundle covers SharePoint, and a
            # separate weaker/absent policy on SharePoint alone becomes visible as divergence.
            record("collaboration_content", members.get("collaboration_content", set()) - exclude, "preset")
        if preset == TARGET_ADMIN_PORTALS:
            # Learn is explicit that this grouping does NOT include the backend services the
            # portals call. So it must NOT mark management_apis as covered.
            pass

    client_types = {str(t) for t in conditions.get("client_app_types") or []}
    if client_types & LEGACY_CLIENT_APP_TYPES:
        out["legacy_protocols"] = {"hit": set(client_types & LEGACY_CLIENT_APP_TYPES), "missed": set(), "basis": "client_app"}

    actions = {str(a).lower() for a in conditions.get("user_actions") or []}
    if actions:
        declared = {a.lower() for a in (
            next((c for c in doc["classes"] if c["id"] == "identity_lifecycle"), {}).get("user_actions") or []
        )}
        out["identity_lifecycle"] = {
            "hit": actions & declared if declared else actions,
            "missed": declared - actions,
            "basis": "user_action",
        }

    scoped = bool(conditions.get("application_filter_rule") or conditions.get("auth_contexts"))
    if scoped:
        out["scoped_constructs"] = {"hit": {"scoped"}, "missed": set(), "basis": "scoped"}

    # Sets are the natural type for the resolution above and cannot survive the response.
    # This dict is attached to every policy, and every policy is returned by /ca/policies and
    # /ca/export — a set reaches the JSON encoder and 500s the endpoint. No unit test catches
    # it, because the tests call the endpoint functions directly and never serialise the result.
    # Sets are the natural type for the resolution above and a poor type to return. This dict
    # is attached to every policy, and every policy is returned by /ca/policies and /ca/export.
    # FastAPI's encoder accepts a set, so this is not a crash — it is worse than that: set
    # iteration order is arbitrary, so the same unchanged tenant exports in a different order
    # each time and every policy-as-code diff shows phantom changes. Sorting makes the artifact
    # comparable.
    return {
        cid: {"hit": sorted(d["hit"]), "missed": sorted(d["missed"]), "basis": d["basis"]}
        for cid, d in out.items()
    }

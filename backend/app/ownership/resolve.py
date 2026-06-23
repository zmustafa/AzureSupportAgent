"""Effective-owner resolution engine — the heart of the ownership feature.

Every consumer (the Ownership UI, Radar, Inventory, Assessments, RBAC, the coverage
screens, chat tools) calls :func:`resolve_owner` instead of re-implementing "who owns
this?". The resolver layers the available signals in a fixed precedence:

    1. **direct**     — an explicit ownership assignment on the subject itself.
    2. **tag**        — an Azure ``owner``-style tag on the resource.
    3. **workload**   — the subject is a resource belonging to a workload that is owned.
    4. **inherited**  — the nearest *ancestor scope* is owned (resource → RG → subscription
                        → management group). Honours decision #2 (inheritance ON by default;
                        a direct/tag/workload owner on the subject overrides it).
    5. *(rbac)*       — Owner/Contributor RBAC principals are surfaced by ``suggest.py`` as
                        AI suggestions, not auto-applied here.
    6. **none**       — unowned / orphaned.

The pure helpers (:func:`owner_from_tags`, :func:`parse_arm_scopes`,
:func:`workload_index`) are unit-testable and have no Azure dependency. ``resolve_owner``
itself is synchronous and reads only the local registries — no network — so it's cheap to
call per-row when annotating big result sets.
"""
from __future__ import annotations

from typing import Any

from app.ownership import registry

# Azure tag keys that conventionally carry an owner, in priority order.
_OWNER_TAG_KEYS = (
    "owner", "Owner", "owner_email", "OwnerEmail", "ownerEmail",
    "team", "Team", "contact", "Contact", "managedBy", "ManagedBy",
)


def owner_from_tags(tags: Any) -> str:
    """Best owner string from an Azure resource's tag dict ("" if none). The canonical
    home for this heuristic (Radar et al. import it from here)."""
    if not isinstance(tags, dict):
        return ""
    for k in _OWNER_TAG_KEYS:
        v = tags.get(k)
        if v:
            return str(v).strip()
    return ""


# --------------------------------------------------------------------- ARM id parsing
def _norm(s: str) -> str:
    return (s or "").strip().lower()


def sub_guid(arm_id: str) -> str:
    """Extract the subscription GUID from any ARM id / scope (lower-cased), else ""."""
    s = _norm(arm_id)
    marker = "/subscriptions/"
    i = s.find(marker)
    if i < 0:
        # A bare GUID (subscription node id) is itself the guid.
        return s if _looks_like_guid(s) else ""
    rest = s[i + len(marker):]
    return rest.split("/", 1)[0]


def _looks_like_guid(s: str) -> bool:
    parts = s.split("-")
    return len(parts) == 5 and all(p and all(c in "0123456789abcdef" for c in p) for p in parts)


def rg_of(arm_id: str) -> str:
    """Extract the resource-group name from an ARM resource/RG id (lower-cased), else ""."""
    s = _norm(arm_id)
    marker = "/resourcegroups/"
    i = s.find(marker)
    if i < 0:
        return ""
    rest = s[i + len(marker):]
    return rest.split("/", 1)[0]


def parse_arm_scopes(arm_id: str) -> list[dict[str, str]]:
    """Ancestor scope chain for a resource/RG/sub ARM id, MOST-SPECIFIC FIRST.

    e.g. a resource id →
        [{kind: resource_group, id: /subscriptions/<s>/resourceGroups/<rg>},
         {kind: subscription,    id: /subscriptions/<s>}]
    Does NOT include the subject itself. Management groups are not derivable from an ARM
    id (no parent pointer), so MG inheritance is handled only when an MG assignment's
    subscription set is known (see ``resolve_owner`` ``mg_subs``)."""
    s = _norm(arm_id)
    out: list[dict[str, str]] = []
    guid = sub_guid(s)
    rg = rg_of(s)
    is_rg_scope = s.endswith(f"/resourcegroups/{rg}") if rg else False
    is_sub_scope = bool(guid) and s.endswith(f"/subscriptions/{guid}")
    # If the subject is itself a RG or subscription scope, the chain starts above it.
    if rg and not is_rg_scope and not is_sub_scope:
        out.append({"kind": "resource_group", "id": f"/subscriptions/{guid}/resourcegroups/{rg}"})
    if guid and not is_sub_scope:
        out.append({"kind": "subscription", "id": f"/subscriptions/{guid}"})
    return out


def _scope_id_variants(kind: str, subject_id: str, subscription_id: str = "") -> set[str]:
    """All normalized id spellings an assignment for this subject might be stored under,
    so a subscription assignment matches whether it was saved as a bare GUID or a
    ``/subscriptions/<guid>`` scope path."""
    out: set[str] = set()
    sid = _norm(subject_id)
    if sid:
        out.add(sid)
    if kind == "subscription":
        guid = sub_guid(subject_id) or _norm(subscription_id)
        if guid:
            out.add(guid)
            out.add(f"/subscriptions/{guid}")
    return out


# --------------------------------------------------------------------- workload index
def workload_index(tenant_id: str) -> dict[str, dict[str, str]]:
    """Lower-cased ARM id → {workload_id, workload_name} for every resource node, PLUS
    (sub, rg) and sub keys so a resource can be matched to a workload via its RG or
    subscription membership. Resource matches win over RG, RG over subscription.

    NOTE: workloads are effectively GLOBAL (the registry stores them under their Azure
    tenant or an empty tenant, while the app principal may be ``default`` in dev) — so we
    do NOT tenant-filter here, mirroring the original Radar owner index. The ``tenant_id``
    arg is accepted for signature symmetry/future use."""
    out: dict[str, dict[str, str]] = {}
    try:
        from app.workloads.registry import list_workloads
    except Exception:  # noqa: BLE001
        return out
    for wl in list_workloads():
        meta = {"workload_id": wl.get("id", ""), "workload_name": wl.get("name", "")}
        for node in wl.get("nodes", []) or []:
            kind = node.get("kind")
            rid = _norm(node.get("id", ""))
            if kind == "resource" and rid:
                out.setdefault(rid, meta)
            elif kind == "resource_group":
                guid = sub_guid(node.get("subscription_id", "")) or sub_guid(node.get("id", ""))
                rg = _norm(node.get("resource_group") or node.get("name", ""))
                if guid and rg:
                    out.setdefault(f"rg::{guid}/{rg}", meta)
            elif kind == "subscription":
                guid = sub_guid(node.get("id", "")) or sub_guid(node.get("subscription_id", ""))
                if guid:
                    out.setdefault(f"sub::{guid}", meta)
    return out


def workload_for_resource(arm_id: str, wl_index: dict[str, dict[str, str]]) -> dict[str, str]:
    """Find the workload a resource belongs to: exact id, then its RG, then its sub."""
    rid = _norm(arm_id)
    if rid in wl_index:
        return wl_index[rid]
    guid = sub_guid(rid)
    rg = rg_of(rid)
    if guid and rg and f"rg::{guid}/{rg}" in wl_index:
        return wl_index[f"rg::{guid}/{rg}"]
    if guid and f"sub::{guid}" in wl_index:
        return wl_index[f"sub::{guid}"]
    return {}


# --------------------------------------------------------------------- the resolver
def _owner_view(owner: dict[str, Any] | None, assignment: dict[str, Any]) -> dict[str, Any]:
    """Shape one resolved owner from an assignment (+ optional owner record)."""
    delegate = _active_delegate(owner)
    return {
        "owner_id": assignment.get("owner_id", ""),
        "display_name": (owner or {}).get("display_name", "") or assignment.get("owner_id", ""),
        "email": (owner or {}).get("email", ""),
        "kind": (owner or {}).get("kind", ""),
        "role": assignment.get("role", "technical"),
        "primary": bool(assignment.get("primary")),
        "source": assignment.get("source", "manual"),
        "confidence": assignment.get("confidence", 1.0),
        "assignment_id": assignment.get("id", ""),
        "attested_at": assignment.get("attested_at", ""),
        "delegate": delegate,
    }


def _active_delegate(owner: dict[str, Any] | None) -> dict[str, Any] | None:
    """An owner's currently-active delegation (until-date in the future), else None."""
    if not owner:
        return None
    d = owner.get("delegate") or {}
    if not isinstance(d, dict) or not d.get("owner_id"):
        return None
    until = str(d.get("until") or "")
    if until:
        from datetime import date, datetime
        try:
            end = datetime.fromisoformat(until).date() if "T" in until or "-" in until else None
        except (ValueError, TypeError):
            end = None
        if end is not None and end < date.today():
            return None  # delegation expired
    return {"owner_id": d.get("owner_id", ""), "until": until, "reason": d.get("reason", "")}


def build_context(tenant_id: str) -> dict[str, Any]:
    """Pre-load the registries ONCE for a batch of resolves (annotating many rows).

    Returns an opaque context dict to pass to :func:`resolve_owner` as ``ctx=``."""
    owners = {o["id"]: o for o in registry.list_owners(tenant_id)}
    assignments = registry.list_assignments(tenant_id)
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for a in assignments:
        for variant in _scope_id_variants(a.get("subject_kind", ""), a.get("subject_id", ""), a.get("subscription_id", "")):
            by_subject.setdefault(variant, []).append(a)
        # Also index workload/architecture subjects by their plain id.
        sid = _norm(a.get("subject_id", ""))
        if sid:
            by_subject.setdefault(sid, []).append(a)
    return {
        "tenant_id": tenant_id or "default",
        "owners": owners,
        "by_subject": by_subject,
        "wl_index": workload_index(tenant_id),
    }


def _assignments_for(ctx: dict[str, Any], *id_variants: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for v in id_variants:
        for a in ctx["by_subject"].get(_norm(v), []):
            aid = a.get("id", "")
            if aid in seen:
                continue
            seen.add(aid)
            out.append(a)
    return out


def resolve_owner(
    tenant_id: str,
    subject_kind: str,
    subject_id: str,
    *,
    tags: Any = None,
    subscription_id: str = "",
    resource_group: str = "",
    ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the effective owner(s) of a subject following the documented precedence.

    Pass a shared ``ctx`` (from :func:`build_context`) when resolving many subjects to
    avoid reloading the registries each call."""
    context = ctx or build_context(tenant_id)
    owners_map: dict[str, Any] = context["owners"]

    def views(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        vs = [_owner_view(owners_map.get(a.get("owner_id", "")), a) for a in assignments]
        # Drop assignments whose owner record is trashed/missing AND has no inline label.
        vs = [v for v in vs if v["display_name"] or v["owner_id"]]
        vs.sort(key=lambda v: (not v["primary"], v["role"]))
        return vs

    # 1. DIRECT — an explicit assignment on the subject itself.
    direct = _assignments_for(context, *_scope_id_variants(subject_kind, subject_id, subscription_id), subject_id)
    if direct:
        return {
            "subject_kind": subject_kind, "subject_id": subject_id,
            "owners": views(direct), "source": "direct",
            "inherited_from": None, "unowned": False,
        }

    # 2. TAG — an owner tag on the resource (raw string, may not be a registered owner).
    tag_owner = owner_from_tags(tags)
    if tag_owner:
        return {
            "subject_kind": subject_kind, "subject_id": subject_id,
            "owners": [{
                "owner_id": "", "display_name": tag_owner,
                "email": tag_owner if "@" in tag_owner else "",
                "kind": "", "role": "technical", "primary": True,
                "source": "tag", "confidence": 1.0, "assignment_id": "", "attested_at": "",
            }],
            "source": "tag", "inherited_from": None, "unowned": False,
        }

    # 3. WORKLOAD — the resource belongs to an owned workload.
    if subject_kind == "resource":
        wl = workload_for_resource(subject_id, context["wl_index"])
        if wl.get("workload_id"):
            wl_assignments = _assignments_for(context, wl["workload_id"])
            if wl_assignments:
                return {
                    "subject_kind": subject_kind, "subject_id": subject_id,
                    "owners": views(wl_assignments), "source": "workload",
                    "inherited_from": {"kind": "workload", "id": wl["workload_id"], "name": wl.get("workload_name", "")},
                    "unowned": False,
                }

    # 4. INHERITED — nearest owned ancestor scope (RG → subscription).
    for anc in parse_arm_scopes(subject_id):
        anc_assignments = _assignments_for(context, *_scope_id_variants(anc["kind"], anc["id"]))
        if anc_assignments:
            label = anc["id"]
            if anc["kind"] == "resource_group":
                label = rg_of(anc["id"])
            elif anc["kind"] == "subscription":
                label = sub_guid(anc["id"])
            return {
                "subject_kind": subject_kind, "subject_id": subject_id,
                "owners": views(anc_assignments), "source": "inherited",
                "inherited_from": {"kind": anc["kind"], "id": anc["id"], "name": label},
                "unowned": False,
            }

    # 5. UNOWNED.
    return {
        "subject_kind": subject_kind, "subject_id": subject_id,
        "owners": [], "source": "none", "inherited_from": None, "unowned": True,
    }


def resolve_label(tenant_id: str, subject_kind: str, subject_id: str, *, tags: Any = None,
                  ctx: dict[str, Any] | None = None) -> str:
    """Convenience: a single display string for the primary owner ("" if unowned)."""
    res = resolve_owner(tenant_id, subject_kind, subject_id, tags=tags, ctx=ctx)
    if res["unowned"] or not res["owners"]:
        return ""
    primary = next((o for o in res["owners"] if o["primary"]), res["owners"][0])
    return primary.get("display_name") or primary.get("email") or ""

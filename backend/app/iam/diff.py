"""Full run diffing — *"what changed since the last scan, and is it worse?"*

`store.save_run` already diffed runs, but only over **privileged** rows and only as added/removed
strings. That answers "did someone become an Owner" and nothing else. This module diffs the whole
access surface and classifies each change, because the interesting movements are the ones that
keep the row count identical: a role widening, a scope broadening, an eligible assignment being
activated, or direct access quietly becoming group-derived.

Two decisions carry the design:

**The key is `effectivePrincipalId`, not `principalId`.** A user who gains access by being added
to a group is the single most common way privilege appears in a tenant. A `principalId`-keyed
diff sees the group's assignment unchanged and reports nothing at all.

**`assignmentState` is part of the key.** That makes an Eligible → Active transition a first-class
change rather than an invisible one, which is precisely the event a reviewer is looking for.

Storage stays bounded on purpose: a run persists a key-set *hash* and the diff against its
predecessor. Full rows are kept only for runs that are pinned — as a campaign baseline or an
evidence snapshot. Thirty runs of a few hundred thousand rows each is not a history feature, it
is an outage.
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.iam import schema

# --------------------------------------------------------------------------- change classes
ADDED = "added"
REMOVED = "removed"
ESCALATED = "escalated"
DE_ESCALATED = "de_escalated"
RE_SCOPED = "re_scoped"
ACTIVATED = "activated"
DEACTIVATED = "deactivated"
PATH_CHANGED = "path_changed"
ORPHANED = "orphaned"

CHANGE_CLASSES = (
    ADDED, REMOVED, ESCALATED, DE_ESCALATED, RE_SCOPED,
    ACTIVATED, DEACTIVATED, PATH_CHANGED, ORPHANED,
)

# Which direction each class moves risk. The UI colours on this and the drift signals only fire
# on the ones that make things worse — a de-escalation is a change worth showing and never worth
# alerting on.
WORSENING = frozenset({ADDED, ESCALATED, RE_SCOPED, ACTIVATED, ORPHANED})

# Privilege tiers, coarse but ordered. Comparing role NAMES cannot tell you whether a change was
# an escalation; comparing tiers can. Anything unrecognised sits at tier 1 (some access) rather
# than 0, so an unknown custom role is never reported as a de-escalation from Reader.
TIER_NONE = 0
TIER_READ = 1
TIER_WRITE = 2
TIER_ADMIN = 3
TIER_OWNER = 4

_READ_ROLES = frozenset({"reader", "global reader", "security reader", "monitoring reader", "cost management reader"})
_OWNER_ROLES = frozenset({
    "owner", "co-administrator", "account administrator", "service administrator",
    "global administrator", "company administrator",
})
_ADMIN_ROLES = frozenset({
    "user access administrator", "role based access control administrator",
    "privileged role administrator", "privileged authentication administrator",
    "key vault administrator",
})


def privilege_tier(row: dict[str, Any]) -> int:
    """Coarse ordered privilege of one access row.

    Deliberately name-and-flag based rather than action-based: this runs over historical
    snapshots where the role definition may no longer exist, so it must not depend on a live
    role index. A tier comparison is only ever used to LABEL a change, never to decide whether
    someone is allowed to do something — that is `effective.evaluate`'s job."""
    if row.get("effect") == schema.EFFECT_DENY:
        return TIER_NONE
    name = str(row.get("roleName", "")).strip().lower()
    if name in _OWNER_ROLES:
        return TIER_OWNER
    if name in _ADMIN_ROLES:
        return TIER_ADMIN
    if name in _READ_ROLES:
        return TIER_READ
    if row.get("roleIsPrivileged"):
        return TIER_ADMIN if "administrator" in name else TIER_WRITE
    if "reader" in name or name.endswith(" read"):
        return TIER_READ
    # Unknown custom role: assume it grants something. Assuming otherwise manufactures
    # de-escalations out of roles nobody has classified.
    return TIER_WRITE


def scope_depth(scope: str) -> int:
    """How specific a scope is. Tenant root is 0; a resource is the deepest.

    Used only to say whether a re-scope went BROADER (worse) or narrower. `/` sorts below a
    management group, which sorts below a subscription, and so on."""
    s = (scope or "").strip().rstrip("/")
    if not s:
        return 0
    parsed = schema.parse_scope(s)
    kind = parsed.get("scopeType", "")
    return {
        schema.SCOPE_TENANT: 0,
        schema.SCOPE_MANAGEMENT_GROUP: 1,
        schema.SCOPE_SUBSCRIPTION: 2,
        schema.SCOPE_RESOURCE_GROUP: 3,
        schema.SCOPE_RESOURCE: 4,
    }.get(kind, 4)


# --------------------------------------------------------------------------- keys
def row_key(row: dict[str, Any]) -> str:
    """Full identity of one grant. Everything in it is part of *what access this is*."""
    who = _principal_of(row)
    return "|".join((
        who,
        str(row.get("roleDefinitionId", "") or row.get("roleName", "")),
        str(row.get("scope", "")),
        str(row.get("surface", "")),
        str(row.get("assignmentState", "")),
    ))


def _principal_of(row: dict[str, Any]) -> str:
    """The person or identity that actually holds this access.

    Falls back through name then principalId: a group-expanded row always has an effective
    principal, but an imported scanner row may only carry a display name, and keying those to
    the empty string would collapse every one of them into a single phantom grant."""
    return str(
        row.get("effectivePrincipalId")
        or row.get("effectivePrincipalName")
        or row.get("principalId")
        or ""
    ).lower()


def key_set_hash(rows: list[dict[str, Any]]) -> str:
    """Cheap identity of a whole run. Equal hashes ⇒ nothing changed, with no diff to compute."""
    keys = sorted({row_key(r) for r in rows})
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _subject(row: dict[str, Any]) -> str:
    """Principal + scope + surface — the axis along which a role or state can move."""
    return "|".join((_principal_of(row), str(row.get("scope", "")), str(row.get("surface", ""))))


def _role_subject(row: dict[str, Any]) -> str:
    """Principal + role + surface — the axis along which a SCOPE can move."""
    return "|".join((
        _principal_of(row),
        str(row.get("roleDefinitionId", "") or row.get("roleName", "")),
        str(row.get("surface", "")),
    ))


# --------------------------------------------------------------------------- diff
MAX_CHANGES = 2000


def compute(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    max_changes: int = MAX_CHANGES,
) -> dict[str, Any]:
    """Classify every difference between two composed access snapshots.

    A raw key diff produces two entries — one `removed`, one `added` — for what a human would
    call a single change. This pairs those up along three axes (same subject different role,
    same subject different state, same principal+role different scope) so the reader sees
    "Alice went from Reader to Owner", not two unrelated lines they have to correlate by eye."""
    before_by_key = {row_key(r): r for r in before}
    after_by_key = {row_key(r): r for r in after}

    gone = {k: v for k, v in before_by_key.items() if k not in after_by_key}
    new = {k: v for k, v in after_by_key.items() if k not in before_by_key}

    changes: list[dict[str, Any]] = []
    consumed_old: set[str] = set()
    consumed_new: set[str] = set()

    # 1. Same principal+scope+surface, different STATE — the PIM activation signal.
    for nk, nrow in new.items():
        if nk in consumed_new:
            continue
        for ok, orow in gone.items():
            if ok in consumed_old:
                continue
            if _subject(orow) != _subject(nrow):
                continue
            if orow.get("roleName") != nrow.get("roleName"):
                continue
            if orow.get("assignmentState") == nrow.get("assignmentState"):
                continue
            activated = nrow.get("assignmentState") == schema.STATE_ACTIVE
            changes.append(_entry(ACTIVATED if activated else DEACTIVATED, orow, nrow))
            consumed_old.add(ok)
            consumed_new.add(nk)
            break

    # 2. Same principal+scope+surface, different ROLE — escalation or de-escalation.
    for nk, nrow in new.items():
        if nk in consumed_new:
            continue
        for ok, orow in gone.items():
            if ok in consumed_old or _subject(orow) != _subject(nrow):
                continue
            old_tier, new_tier = privilege_tier(orow), privilege_tier(nrow)
            if old_tier == new_tier:
                continue
            changes.append(_entry(ESCALATED if new_tier > old_tier else DE_ESCALATED, orow, nrow))
            consumed_old.add(ok)
            consumed_new.add(nk)
            break

    # 3. Same principal+role+surface, different SCOPE. Broader is worse and is the one that gets
    #    missed by eye: /subscriptions/x -> / is one character of diff and a tenant-wide grant.
    for nk, nrow in new.items():
        if nk in consumed_new:
            continue
        for ok, orow in gone.items():
            if ok in consumed_old or _role_subject(orow) != _role_subject(nrow):
                continue
            if orow.get("scope") == nrow.get("scope"):
                continue
            e = _entry(RE_SCOPED, orow, nrow)
            e["broader"] = scope_depth(str(nrow.get("scope", ""))) < scope_depth(str(orow.get("scope", "")))
            changes.append(e)
            consumed_old.add(ok)
            consumed_new.add(nk)
            break

    # 4. Whatever is left really is an addition or a removal.
    for ok, orow in gone.items():
        if ok not in consumed_old:
            changes.append(_entry(REMOVED, orow, None))
    for nk, nrow in new.items():
        if nk not in consumed_new:
            changes.append(_entry(ADDED, None, nrow))

    # 5. Two classes are properties of a SURVIVING row rather than of a key difference, so they
    #    have to be looked for separately. `path_changed` is the subtle one: `accessPath` is
    #    deliberately NOT part of the key — the access is identical, only its governance moved —
    #    which means the pairing above can never see it. Detecting it there was dead code that
    #    type-checked, ran, and produced nothing.
    for key, nrow in after_by_key.items():
        orow = before_by_key.get(key)
        if not orow:
            continue
        if nrow.get("principalExists") == schema.EXISTS_FALSE and orow.get("principalExists") != schema.EXISTS_FALSE:
            changes.append(_entry(ORPHANED, orow, nrow))
        if orow.get("accessPath") != nrow.get("accessPath"):
            changes.append(_entry(PATH_CHANGED, orow, nrow))

    changes.sort(key=lambda c: (CHANGE_CLASSES.index(c["class"]), c["principalName"], c["scope"]))
    counts = {cls: sum(1 for c in changes if c["class"] == cls) for cls in CHANGE_CLASSES}
    total = len(changes)
    return {
        "changes": changes[:max_changes],
        "counts_by_class": counts,
        "total": total,
        "truncated": total > max_changes,
        "worsening": sum(counts[c] for c in WORSENING),
    }


def _entry(cls: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """One diff row, carrying enough of both sides to be read without re-fetching either run."""
    ref = after or before or {}
    out: dict[str, Any] = {
        "class": cls,
        "key": row_key(ref),
        "principalId": _principal_of(ref),
        "principalName": str(ref.get("effectivePrincipalName") or ref.get("principalDisplayName") or ""),
        "principalType": str(ref.get("effectivePrincipalType") or ref.get("principalType") or ""),
        "scope": str(ref.get("scope", "")),
        "scopeName": str(ref.get("scopeDisplayName", "")),
        "surface": str(ref.get("surface", "")),
        "roleName": str(ref.get("roleName", "")),
        "assignmentId": str(ref.get("assignmentId", "")),
        "privileged": bool(ref.get("roleIsPrivileged")),
        "worsens": cls in WORSENING,
        # Attribution is joined in later and is `unknown` until it is. It is never blank and
        # never guessed — see `attribution.py`.
        "actor": None,
    }
    if before is not None and after is not None:
        out["from"] = {
            "roleName": str(before.get("roleName", "")),
            "scope": str(before.get("scope", "")),
            "assignmentState": str(before.get("assignmentState", "")),
            "accessPath": str(before.get("accessPath", "")),
            "tier": privilege_tier(before),
        }
        out["to"] = {
            "roleName": str(after.get("roleName", "")),
            "scope": str(after.get("scope", "")),
            "assignmentState": str(after.get("assignmentState", "")),
            "accessPath": str(after.get("accessPath", "")),
            "tier": privilege_tier(after),
        }
    return out


def timeline_for(principal_id: str, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chronological access events for one principal, newest first.

    Reads the stored per-run diffs rather than recomputing, so a timeline costs one query. Runs
    that predate full diffing simply contribute nothing — an empty stretch of history is honest;
    back-filling it from the privileged-only diff would invent events."""
    pid = (principal_id or "").lower()
    out: list[dict[str, Any]] = []
    for run in runs:
        diff = run.get("diff") or {}
        for change in diff.get("changes", []) or []:
            if change.get("principalId", "").lower() != pid:
                continue
            out.append({
                **change,
                "run_id": run.get("id", ""),
                "at": run.get("started_at", ""),
            })
    out.sort(key=lambda e: e["at"], reverse=True)
    return out

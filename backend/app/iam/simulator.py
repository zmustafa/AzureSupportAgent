"""What-if simulator — *"if I make this change, what actually happens?"*

A pure function over a cached snapshot. No Azure call is reachable from this module, and none
should ever become reachable: the value of a simulator is that it is safe to run on a whim, and
the moment it touches the live estate it stops being that.

`entra/ca_simulator.py` is the precedent and its hard-won rules transfer wholesale:

**An invalid change raises rather than being ignored.** The Entra version originally skipped
unknown change kinds and unknown ids, which produced a reassuring "nothing changes" result from
a typo — the worst possible output, because it looks like an answer. `InvalidChange` → 400.

**`access_retained_via_other_path` is the most valuable field.** Removing Alice from a group
*looks* like a revocation and frequently is not: she may hold the same role directly, through a
second group, or via a service principal she owns. A simulator that only reports what it removed
encourages revocations that achieve nothing — and those are worse than no revocation, because
they leave a false record of remediation behind.

**`orphaned_resources` is the second.** "After this change, 3 resources have no principal with
owner-level access" is the outcome that gets a revocation reverted in a panic a fortnight later,
and it is knowable in advance.

**Sampling is seeded and some cohorts are never sampled.** An answer that changes between
identical runs cannot support a decision, and a sample that drops the break-glass account is
answering a different question from the one asked.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

from app.iam import diff as diff_mod, effective, schema

# Change kinds.
REMOVE_ASSIGNMENT = "remove_assignment"
REMOVE_GROUP_MEMBER = "remove_group_member"
REMOVE_GROUP = "remove_group"
CONVERT_TO_ELIGIBLE = "convert_to_eligible"
RESCOPE_ASSIGNMENT = "rescope_assignment"
REPLACE_ROLE = "replace_role"
DISABLE_BYPASS = "disable_bypass"
ASSUME_PRINCIPAL = "assume_principal"
ADD_DELEGATION = "add_delegation"

CHANGE_KINDS = (
    REMOVE_ASSIGNMENT, REMOVE_GROUP_MEMBER, REMOVE_GROUP, CONVERT_TO_ELIGIBLE,
    RESCOPE_ASSIGNMENT, REPLACE_ROLE, DISABLE_BYPASS, ASSUME_PRINCIPAL, ADD_DELEGATION,
)

#: Deterministic seed. `ca_simulator` fixes one for the same reason: a simulator whose answer
#: moves between identical runs is not usable for a decision, and "run it again" becomes the
#: first thing anybody does when they dislike the result.
SEED = 20260801
SAMPLE_THRESHOLD = 5000

#: Cohorts that are never sampled away. A sample that drops the break-glass account, the tier-0
#: holders or the guests is answering a different question from the one that was asked.
ALWAYS_FULL = "always_full"


class InvalidChange(ValueError):
    """A change that cannot be applied. Never swallowed — an ignored change produces a
    'nothing happens' result that is indistinguishable from a correct one."""


class MissingReferent(LookupError):
    """A saved simulation whose referenced object no longer exists. 409, not 400: the request
    was valid when it was written and the world moved."""


@dataclass
class Change:
    kind: str
    # Whichever of these the kind needs. Validated per kind rather than by presence, so a
    # missing field is an explicit error rather than a silently skipped change.
    principal_id: str = ""
    group_id: str = ""
    assignment_id: str = ""
    scope: str = ""
    role_name: str = ""
    to_role: str = ""
    to_scope: str = ""
    resource_id: str = ""
    label: str = ""

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class Result:
    rows_before: list[dict[str, Any]] = field(default_factory=list)
    rows_after: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- validation
_REQUIRED: dict[str, tuple[str, ...]] = {
    REMOVE_ASSIGNMENT: ("assignment_id",),
    REMOVE_GROUP_MEMBER: ("group_id", "principal_id"),
    REMOVE_GROUP: ("group_id",),
    CONVERT_TO_ELIGIBLE: ("assignment_id",),
    RESCOPE_ASSIGNMENT: ("assignment_id", "to_scope"),
    REPLACE_ROLE: ("assignment_id", "to_role"),
    DISABLE_BYPASS: ("resource_id",),
    ASSUME_PRINCIPAL: ("principal_id",),
    ADD_DELEGATION: ("principal_id", "scope", "role_name"),
}


def parse_change(raw: dict[str, Any]) -> Change:
    kind = str(raw.get("kind", "")).strip()
    if kind not in CHANGE_KINDS:
        raise InvalidChange(f"unknown change kind {kind!r}; expected one of {', '.join(CHANGE_KINDS)}")
    change = Change(kind=kind, **{
        k: str(raw.get(k, "") or "")
        for k in ("principal_id", "group_id", "assignment_id", "scope", "role_name",
                  "to_role", "to_scope", "resource_id", "label")
    })
    missing = [f for f in _REQUIRED[kind] if not getattr(change, f)]
    if missing:
        raise InvalidChange(f"{kind} needs {', '.join(missing)}")
    return change


def _assert_referent(change: Change, rows: list[dict[str, Any]]) -> None:
    """Every change must name something that exists in the snapshot.

    A change against an id that is not there is the typo case: applied silently it produces an
    empty diff that looks exactly like "this change is safe"."""
    if change.kind in (ASSUME_PRINCIPAL, ADD_DELEGATION):
        return  # these describe hypothetical access, so their subject need not exist yet
    if change.assignment_id:
        if not any(str(r.get("assignmentId", "")).lower() == change.assignment_id.lower() for r in rows):
            raise MissingReferent(f"no assignment {change.assignment_id!r} in this snapshot")
    if change.group_id:
        gid = change.group_id.lower()
        present = any(
            str(r.get("principalId", "")).lower() == gid
            or gid in str(r.get("groupChain", "")).lower()
            for r in rows
        )
        if not present:
            raise MissingReferent(f"no group {change.group_id!r} in this snapshot")
    if change.kind == REMOVE_GROUP_MEMBER and change.principal_id:
        pid = change.principal_id.lower()
        if not any(str(r.get("effectivePrincipalId", "")).lower() == pid for r in rows):
            raise MissingReferent(f"no principal {change.principal_id!r} in this snapshot")


# --------------------------------------------------------------------------- application
def apply_changes(rows: list[dict[str, Any]], changes: list[Change]) -> list[dict[str, Any]]:
    """Apply changes to an in-memory copy. Never mutates the input."""
    out = [dict(r) for r in rows]
    for change in changes:
        out = _apply_one(out, change)
    return out


def _apply_one(rows: list[dict[str, Any]], change: Change) -> list[dict[str, Any]]:
    kind = change.kind
    if kind == REMOVE_ASSIGNMENT:
        aid = change.assignment_id.lower()
        # Removes EVERY row for the assignment — one per expanded group member. Removing only
        # the direct row would leave the members' access in place and report a revocation that
        # did not happen.
        return [r for r in rows if str(r.get("assignmentId", "")).lower() != aid]

    if kind == REMOVE_GROUP_MEMBER:
        gid, pid = change.group_id.lower(), change.principal_id.lower()
        return [
            r for r in rows
            if not (
                str(r.get("effectivePrincipalId", "")).lower() == pid
                and r.get("accessPath") == schema.PATH_GROUP
                and (str(r.get("principalId", "")).lower() == gid or gid in str(r.get("groupChain", "")).lower())
            )
        ]

    if kind == REMOVE_GROUP:
        gid = change.group_id.lower()
        return [
            r for r in rows
            if not (
                str(r.get("principalId", "")).lower() == gid
                or gid in str(r.get("groupChain", "")).lower()
            )
        ]

    if kind == CONVERT_TO_ELIGIBLE:
        aid = change.assignment_id.lower()
        return [
            {**r, "assignmentState": schema.STATE_ELIGIBLE, "pimManaged": True}
            if str(r.get("assignmentId", "")).lower() == aid else r
            for r in rows
        ]

    if kind == RESCOPE_ASSIGNMENT:
        aid = change.assignment_id.lower()
        return [
            {**r, "scope": change.to_scope, "scopeDisplayName": change.to_scope}
            if str(r.get("assignmentId", "")).lower() == aid else r
            for r in rows
        ]

    if kind == REPLACE_ROLE:
        aid = change.assignment_id.lower()
        return [
            {**r, "roleName": change.to_role, "roleDefinitionId": "",
             "roleIsPrivileged": schema.role_is_privileged(change.to_role, surface=str(r.get("surface", "")))}
            if str(r.get("assignmentId", "")).lower() == aid else r
            for r in rows
        ]

    if kind == DISABLE_BYPASS:
        # A bypass is not an access row, so nothing changes in this row set. Saying so is the
        # point: the simulator must not imply it modeled something it cannot see.
        return rows

    if kind == ASSUME_PRINCIPAL:
        # Attack simulation: nothing is removed. The question is what the principal reaches,
        # which the reporting layer answers from the unchanged snapshot.
        return rows

    if kind == ADD_DELEGATION:
        new = schema.make_row(
            surface=schema.SURFACE_AZURE_RBAC, effect=schema.EFFECT_ALLOW,
            assignmentState=schema.STATE_ACTIVE, accessPath=schema.PATH_DIRECT,
            principalId=change.principal_id, effectivePrincipalId=change.principal_id,
            effectivePrincipalName=change.label or change.principal_id,
            effectivePrincipalType="ServicePrincipal",
            roleName=change.role_name, scope=change.scope, scopeDisplayName=change.scope,
            assignmentId=f"simulated-{change.principal_id}-{change.role_name}",
            roleIsPrivileged=schema.role_is_privileged(change.role_name),
        )
        return [*rows, new]

    raise InvalidChange(f"unhandled change kind {kind!r}")


# --------------------------------------------------------------------------- simulation
def simulate(
    rows: list[dict[str, Any]],
    raw_changes: list[dict[str, Any]],
    *,
    role_index: dict[str, effective.RoleActionSet] | None = None,
    owned_scopes: set[str] | None = None,
    escalation_before: dict[str, Any] | None = None,
    usage_age_days: int | None = None,
    sample_threshold: int = SAMPLE_THRESHOLD,
) -> dict[str, Any]:
    """Model a set of changes over a cached snapshot. Pure: no Azure call in this code path."""
    changes = [parse_change(c) for c in raw_changes]
    for change in changes:
        _assert_referent(change, rows)

    after = apply_changes(rows, changes)
    # Keyed on the GRANT, not on the access it produces. `diff.row_key` deliberately ignores
    # `accessPath` and `assignmentId` — two rows describing the same access are the same access.
    # Here that is exactly wrong: a principal holding Owner both directly AND through a group is
    # two grants that collapse to one key, so removing one of them looked like no change at all
    # and `access_retained_via_other_path` — the whole point of this function — never fired.
    before_keys = {_grant_key(r): r for r in rows}
    after_keys = {_grant_key(r): r for r in after}

    lost_keys = [k for k in before_keys if k not in after_keys]
    gained_keys = [k for k in after_keys if k not in before_keys]

    # The critical computation. For every apparent revocation, does the principal still hold
    # equivalent access by some other route?
    retained: list[dict[str, Any]] = []
    truly_lost: list[dict[str, Any]] = []
    for key in lost_keys:
        row = before_keys[key]
        other = _equivalent_after(row, after)
        if other:
            retained.append({
                **_ref(row),
                "otherPath": str(other.get("accessPath", "")),
                "otherVia": str(other.get("principalDisplayName") or other.get("principalId") or ""),
                "otherScope": str(other.get("scope", "")),
            })
        else:
            truly_lost.append({**_ref(row), "wasVia": str(row.get("accessPath", ""))})

    gained = [_ref(after_keys[k]) for k in gained_keys]

    affected = {r["principalId"] for r in truly_lost} | {r["principalId"] for r in retained} | {r["principalId"] for r in gained}

    sample = _sample(truly_lost, threshold=sample_threshold)

    result = {
        "changes_applied": [c.public() for c in changes],
        "principals_affected": len(affected),
        "access_lost": sample["items"],
        # Never omitted. A simulator that reports only removals encourages revocations that
        # achieve nothing and leave a false record of remediation behind.
        "access_retained_via_other_path": retained,
        "access_gained": gained,
        "orphaned_resources": orphaned(rows, after, owned_scopes or set()),
        "standing_privilege_before": _standing(rows),
        "standing_privilege_after": _standing(after),
        "unchanged": len(set(before_keys) & set(after_keys)),
        "sample": {
            "sampled": sample["sampled"],
            "size": len(sample["items"]),
            "population": sample["population"],
            "seed": SEED,
            "always_full": sample["always_full"],
        },
        "limitations": _limitations(rows, changes, role_index, usage_age_days),
    }
    if escalation_before is not None:
        result["escalation_paths_before"] = len(escalation_before.get("paths", []) or [])
    return result


def _grant_key(row: dict[str, Any]) -> str:
    """Identity of one GRANT: the access identity plus how and through what it is held."""
    return "|".join((
        diff_mod.row_key(row),
        str(row.get("assignmentId", "")),
        str(row.get("accessPath", "")),
        str(row.get("principalId", "")),
    ))


def _ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "principalId": str(row.get("effectivePrincipalId") or row.get("principalId") or ""),
        "principalName": str(row.get("effectivePrincipalName") or row.get("principalDisplayName") or ""),
        "roleName": str(row.get("roleName", "")),
        "scope": str(row.get("scope", "")),
        "scopeName": str(row.get("scopeDisplayName", "")),
        "privileged": bool(row.get("roleIsPrivileged")),
    }


def _equivalent_after(row: dict[str, Any], after: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Does this principal still hold this role over this scope by any other route?

    "Over this scope" rather than "at this scope": an assignment at the subscription still
    covers a resource-group grant that was removed, and reporting that as lost access is the
    error this whole function exists to prevent."""
    pid = str(row.get("effectivePrincipalId") or row.get("principalId") or "").lower()
    role = str(row.get("roleName", "")).lower()
    scope = str(row.get("scope", ""))
    for candidate in after:
        if candidate.get("effect") == schema.EFFECT_DENY:
            continue
        if str(candidate.get("effectivePrincipalId") or candidate.get("principalId") or "").lower() != pid:
            continue
        if str(candidate.get("roleName", "")).lower() != role:
            continue
        if candidate.get("assignmentState") != schema.STATE_ACTIVE:
            continue
        if effective.scope_covers(str(candidate.get("scope", "")), scope):
            return candidate
    return None


def orphaned(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    owned_scopes: set[str],
) -> list[dict[str, Any]]:
    """Scopes left with nobody holding owner-level access.

    Cross-references the ownership registry so the answer distinguishes "nobody has access" from
    the considerably worse "nobody has access and nobody is recorded as the owner either"."""
    def owners_of(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for r in rows:
            if r.get("effect") == schema.EFFECT_DENY or r.get("assignmentState") != schema.STATE_ACTIVE:
                continue
            if diff_mod.privilege_tier(r) < diff_mod.TIER_OWNER:
                continue
            out.setdefault(str(r.get("scope", "")), set()).add(
                str(r.get("effectivePrincipalId") or r.get("principalId") or "")
            )
        return out

    was, now = owners_of(before), owners_of(after)
    out = []
    for scope, holders in was.items():
        if holders and not now.get(scope):
            out.append({
                "resourceId": scope,
                "lostAllOwners": True,
                "previousOwners": sorted(holders)[:10],
                "remainingAccess": sorted({
                    str(r.get("roleName", "")) for r in after if str(r.get("scope", "")) == scope
                })[:10],
                # The distinction that decides whether this is recoverable in an hour or a day.
                "hasRecordedOwner": scope.lower() in {s.lower() for s in owned_scopes},
            })
    return out


def _standing(rows: list[dict[str, Any]]) -> int:
    return sum(1 for r in rows if schema.is_standing_privilege(r))


def _sample(items: list[dict[str, Any]], *, threshold: int) -> dict[str, Any]:
    """Seeded sampling that never drops the cohorts a reader came for.

    Population and sample size are returned at the top level of the result, so no chart can be
    rendered without them."""
    population = len(items)
    always = [i for i in items if i.get("privileged")]
    if population <= threshold:
        return {"items": items, "sampled": False, "population": population, "always_full": len(always)}
    rest = [i for i in items if not i.get("privileged")]
    rng = random.Random(SEED)
    keep = max(0, threshold - len(always))
    chosen = always + rng.sample(rest, min(keep, len(rest)))
    return {"items": chosen, "sampled": True, "population": population, "always_full": len(always)}


def _limitations(
    rows: list[dict[str, Any]],
    changes: list[Change],
    role_index: dict[str, effective.RoleActionSet] | None,
    usage_age_days: int | None,
) -> list[str]:
    """Always present, and never a green tick for something that was not evaluated."""
    out: list[str] = []
    conditioned = sum(1 for r in rows if str(r.get("condition", "")).strip())
    if conditioned:
        out.append(
            f"{conditioned} assignment(s) carry an ABAC condition which was NOT evaluated. Access "
            f"they grant may be narrower in practice than this simulation assumes."
        )
    if role_index is not None:
        unknown = sum(1 for r in rows if not (effective._lookup(role_index, r) or effective.RoleActionSet("", "")).known)
        if unknown:
            out.append(
                f"{unknown} row(s) reference a role definition whose permissions were not "
                f"collected, so what they grant could not be compared."
            )
    if usage_age_days is not None:
        out.append(f"Usage data is {usage_age_days} day(s) old and was not refreshed for this simulation.")
    if any(c.kind == DISABLE_BYPASS for c in changes):
        out.append(
            "A `disable_bypass` change was requested. Bypass credentials are not access rows, so "
            "this simulation models the RBAC consequences only — it cannot show who loses "
            "data-plane access through a shared key."
        )
    if any(c.kind == ASSUME_PRINCIPAL for c in changes):
        out.append(
            "`assume_principal` models reachability from the snapshot as it stands; it removes "
            "nothing, so the lost/gained columns are empty by design."
        )
    out.append(
        "This is a model over the last collected snapshot, not a prediction. Anything changed in "
        "Azure since that collection is invisible to it."
    )
    return out


def fingerprint(raw_changes: list[dict[str, Any]]) -> str:
    """Stable id for a change set, so a saved simulation can be re-run and compared."""
    parts = sorted(
        "|".join(f"{k}={v}" for k, v in sorted(c.items()) if v) for c in raw_changes
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

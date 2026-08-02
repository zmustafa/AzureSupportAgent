"""Action-level effective permissions: *can principal P perform action A on resource R?*

The rest of this feature answers "who holds role X, where". This module answers the question
people actually ask, and it is the one no Azure-native screen answers.

The evaluation order below **must** match ARM's, because a plausible-looking answer that is
wrong is worse than no answer:

1. a matching **deny assignment** at or above the scope wins outright — deny is evaluated first
   and cannot be overridden, not even by Owner;
2. otherwise any role assignment at or above the scope whose role grants the action and does not
   subtract it via ``notActions`` allows it;
3. otherwise the action is simply not granted.

Four rules sit on top, each of which is a real support ticket:

* **Control plane and data plane are separate universes.** ``Reader`` does not grant
  ``…/blobs/read``, and a role with ``actions: ["*"]`` and no ``dataActions`` grants no data
  action at all. The engine classifies the requested action and only consults the matching plane.
* **``notActions`` is a subtraction, not a deny.** It removes actions from *its own* role. A
  second role without that ``notAction`` still allows — treating it as a deny reports people as
  blocked when they are not.
* **Scope is directional.** An assignment above the target applies; one below does not.
* **An unevaluated ABAC condition means the answer is `indeterminate`, never `allowed`.** A
  confident yes that turns out to be conditional is exactly the answer that gets someone locked
  out at 2am, or worse, believed when it says access exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from app.iam import schema

# Verdicts. `indeterminate` is a first-class answer, not an error state.
ALLOWED = "allowed"
DENIED = "denied"
NOT_GRANTED = "not_granted"
INDETERMINATE = "indeterminate"

PLANE_CONTROL = "control"
PLANE_DATA = "data"

# Azure marks data-plane actions by embedding a provider sub-path; there is no flag on the action
# string itself. The only reliable classifier is the role definition that carries it, so a
# caller-supplied plane always wins and this is the fallback heuristic for a bare action string.
_DATA_ACTION_HINTS = (
    "/blobs/",
    "/containers/",
    "/queues/",
    "/tables/",
    "/fileservices/",
    "/secrets/",
    "/keys/",
    "/certificates/",
    "/messages/",
    "/entities/",
    "/topics/",
    "/subscriptions/messages/",
    "/items/",
)


def classify_plane(action: str) -> str:
    """Best-effort plane for a bare action string.

    Deliberately a *hint*: callers that know the plane should pass it. Guessing wrong in the
    permissive direction would let a control-plane wildcard answer a data-plane question."""
    a = (action or "").lower()
    if any(h in a for h in _DATA_ACTION_HINTS):
        return PLANE_DATA
    return PLANE_CONTROL


# --------------------------------------------------------------------------- wildcard matching
@lru_cache(maxsize=8192)
def _compile(pattern: str) -> re.Pattern[str]:
    """Compile one Azure action pattern.

    Azure's ``*`` spans segments: ``Microsoft.Compute/*/read`` matches
    ``Microsoft.Compute/virtualMachines/extensions/read``. Every other regex metacharacter in the
    pattern (notably ``.``, which appears in every provider namespace) must be escaped, or
    ``Microsoft.Compute/*`` would also match ``MicrosoftXCompute/…``."""
    parts = (pattern or "").split("*")
    return re.compile("^" + ".*".join(re.escape(p) for p in parts) + "$", re.IGNORECASE)


def action_matches(pattern: str, action: str) -> bool:
    """Does ``pattern`` (which may contain ``*``) cover ``action``? Case-insensitive."""
    if not pattern or not action:
        return False
    if "*" not in pattern:
        return pattern.lower() == action.lower()
    return _compile(pattern).match(action) is not None


def any_matches(patterns: Iterable[str], action: str) -> str:
    """The first pattern covering ``action``, or ``""``. Returning the *pattern* rather than a
    bool is what lets the UI say **which** wildcard granted the access."""
    for p in patterns:
        if action_matches(p, action):
            return p
    return ""


# --------------------------------------------------------------------------- scope arithmetic
def normalize_scope(scope: str) -> str:
    s = (scope or "").strip().rstrip("/")
    return s or "/"


# The scope arithmetic below is the hot loop of the whole feature: the escalation graph runs
# (principals x primitives x scopes) evaluations, so on a real tenant `scope_covers` is called
# tens of millions of times over a set of scope strings numbering in the thousands. Both
# functions are pure, so the repeated work is pure waste — profiling a 45-scope tenant showed
# 109M `normalize_scope` calls costing ~37s of a 41s request, purely re-lowercasing the same
# few thousand strings. Memoizing turns that into a dict lookup and changes no answer.
@lru_cache(maxsize=200_000)
def _scope_key(scope: str) -> str:
    """Comparison form of a scope: trimmed, no trailing slash, lower-cased."""
    return normalize_scope(scope).lower()


@lru_cache(maxsize=1_000_000)
def scope_covers(assignment_scope: str, target_scope: str) -> bool:
    """Does an assignment at ``assignment_scope`` reach ``target_scope``?

    True when the assignment is at the target or ABOVE it. Compared segment-wise rather than by
    raw string prefix: ``/subscriptions/abc`` must not be treated as covering
    ``/subscriptions/abcdef``, which a naive ``startswith`` does."""
    a = _scope_key(assignment_scope)
    t = _scope_key(target_scope)
    if a == "/":  # tenant root covers everything
        return True
    if a == t:
        return True
    return t.startswith(a + "/")


def _scope_depth(scope: str) -> int:
    s = normalize_scope(scope)
    return 0 if s == "/" else s.count("/")


# --------------------------------------------------------------------------- role action sets
@dataclass(frozen=True)
class RoleActionSet:
    role_definition_id: str
    role_name: str
    actions: tuple[str, ...] = ()
    not_actions: tuple[str, ...] = ()
    data_actions: tuple[str, ...] = ()
    not_data_actions: tuple[str, ...] = ()
    is_custom: bool = False
    assignable_scopes: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """Whether this role's permissions were actually collected.

        A role definition we never fetched has empty action lists, which is indistinguishable
        from a role that grants nothing — and would make the engine report "not granted" for an
        Owner. Callers must check this before trusting a negative answer."""
        return bool(self.actions or self.data_actions)

    def grants(self, action: str, plane: str) -> tuple[str, str]:
        """``(granting_pattern, excluding_not_action)`` for this role alone.

        A non-empty second element means the role would have granted the action but subtracts it
        via ``notActions`` — which is why it is returned rather than folded into a bool: the UI
        needs to be able to say "Contributor grants this, but its notActions exclude it"."""
        if plane == PLANE_DATA:
            # Data actions come ONLY from dataActions. A control-plane `*` grants none of them.
            granted = any_matches(self.data_actions, action)
            excluded = any_matches(self.not_data_actions, action) if granted else ""
        else:
            granted = any_matches(self.actions, action)
            excluded = any_matches(self.not_actions, action) if granted else ""
        return granted, excluded


def role_action_set(role_def: dict[str, Any] | None, role_definition_id: str = "") -> RoleActionSet:
    rd = role_def or {}
    return RoleActionSet(
        role_definition_id=str(rd.get("roleDefinitionId") or role_definition_id),
        role_name=str(rd.get("roleName") or ""),
        actions=tuple(str(a) for a in (rd.get("actions") or [])),
        not_actions=tuple(str(a) for a in (rd.get("notActions") or [])),
        data_actions=tuple(str(a) for a in (rd.get("dataActions") or [])),
        not_data_actions=tuple(str(a) for a in (rd.get("notDataActions") or [])),
        is_custom=str(rd.get("roleType", "")).lower() == "customrole",
        assignable_scopes=tuple(str(s) for s in (rd.get("assignableScopes") or [])),
    )


def build_role_index(role_defs: Iterable[dict[str, Any]]) -> dict[str, RoleActionSet]:
    """Lookup key -> RoleActionSet, built once per refresh and reused across every evaluation.

    Keyed by BOTH the definition GUID and the lower-cased role name. The name is a legitimate
    second key, not a fuzzy fallback: Azure enforces uniqueness of role names within a tenant,
    and built-in names are globally unique. Without it, rows that carry a role name but no full
    ``roleDefinitionId`` — imported scanner runs, and the demo dataset — resolve to nothing, and
    the engine answers `indeterminate` for every question about them."""
    out: dict[str, RoleActionSet] = {}
    for rd in role_defs or []:
        rid = str(rd.get("roleDefinitionId") or "")
        rset = role_action_set(rd, rid)
        guid = rid.rstrip("/").split("/")[-1] if rid else ""
        if guid:
            out[guid.lower()] = rset
        name = str(rd.get("roleName", "")).strip().lower()
        # A GUID key never collides with a name key, so this cannot shadow a real definition.
        if name and name not in out:
            out[name] = rset
    return out


def _guid_of(role_definition_id: str) -> str:
    return (role_definition_id or "").rstrip("/").split("/")[-1].lower()


def _lookup(role_index: dict[str, RoleActionSet], row: dict[str, Any]) -> RoleActionSet | None:
    """Resolve a row's role, by definition id first and role name second."""
    guid = _guid_of(str(row.get("roleDefinitionId", "")))
    if guid:
        hit = role_index.get(guid)
        if hit is not None:
            return hit
    name = str(row.get("roleName", "")).strip().lower()
    return role_index.get(name) if name else None


# --------------------------------------------------------------------------- evaluation
@dataclass
class Decision:
    verdict: str = NOT_GRANTED
    action: str = ""
    plane: str = PLANE_CONTROL
    scope: str = ""
    principal_id: str = ""
    decided_by: dict[str, Any] | None = None
    granting: list[dict[str, Any]] = field(default_factory=list)
    denying: list[dict[str, Any]] = field(default_factory=list)
    not_action_exclusions: list[dict[str, Any]] = field(default_factory=list)
    condition_unevaluated: list[dict[str, Any]] = field(default_factory=list)
    via_groups: list[dict[str, Any]] = field(default_factory=list)
    unknown_roles: list[str] = field(default_factory=list)
    reason: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "action": self.action,
            "plane": self.plane,
            "scope": self.scope,
            "principalId": self.principal_id,
            "decidedBy": self.decided_by,
            "grantingAssignments": self.granting,
            "denyingAssignments": self.denying,
            "notActionExclusions": self.not_action_exclusions,
            "conditionUnevaluated": self.condition_unevaluated,
            "viaGroups": self.via_groups,
            "unknownRoles": self.unknown_roles,
            "reason": self.reason,
        }


def _row_ref(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A compact, self-explaining reference to an assignment row."""
    ref = {
        "assignmentId": row.get("assignmentId", ""),
        "roleName": row.get("roleName", ""),
        "roleDefinitionId": row.get("roleDefinitionId", ""),
        "scope": row.get("scope", ""),
        "scopeDisplayName": row.get("scopeDisplayName", "") or row.get("scope", ""),
        "principalId": row.get("effectivePrincipalId", "") or row.get("principalId", ""),
        "principalName": row.get("effectivePrincipalName", "") or row.get("principalDisplayName", ""),
        "accessPath": row.get("accessPath", ""),
        "sourceGroupId": row.get("sourceGroupId", ""),
        "sourceGroupName": row.get("sourceGroupName", ""),
        "surface": row.get("surface", ""),
        "assignmentState": row.get("assignmentState", ""),
        "condition": row.get("condition", ""),
    }
    ref.update(extra)
    return ref


def _deny_applies(row: dict[str, Any], target_scope: str) -> bool:
    """Does this deny row reach ``target_scope``?

    ``doNotApplyToChildScopes`` makes a deny apply at its own scope ONLY. Ignoring it reports
    people as blocked on resources the deny never touched."""
    deny_scope = str(row.get("scope", ""))
    if not scope_covers(deny_scope, target_scope):
        return False
    if _truthy(row.get("doNotApplyToChildScopes")):
        return normalize_scope(deny_scope).lower() == normalize_scope(target_scope).lower()
    return True


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes")


def _principal_rows(rows: Iterable[dict[str, Any]], principal_id: str) -> list[dict[str, Any]]:
    """Rows whose EFFECTIVE principal is this one.

    Group membership is already resolved into ``effectivePrincipalId`` by ``compose``, so a
    user's group-derived grants are included without walking the graph again here."""
    pid = (principal_id or "").strip().lower()
    if not pid:
        return []
    return [
        r for r in rows
        if str(r.get("effectivePrincipalId", "") or r.get("principalId", "")).lower() == pid
    ]


# Classic administrator roles that carry Owner-equivalent ARM control-plane access. These are
# real grants and must not be skipped just because they have no role definition.
#
# Compared after stripping non-alphanumerics, because ARM returns them unseparated
# ("CoAdministrator") while every human writes them hyphenated ("Co-Administrator") — matching
# on the literal string silently classified every classic admin as granting nothing.
_CLASSIC_OWNER_EQUIVALENT = ("coadministrator", "serviceadministrator", "accountadministrator")


def _squash(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _surface_verdict(row: dict[str, Any], action: str, plane: str) -> str:
    """How to treat a row whose surface is not Azure RBAC.

    Returns ``"skip"`` (irrelevant to this question), ``"grant"`` (a real grant), or
    ``"resolve"`` (an ordinary Azure RBAC row — look up its role definition).

    This exists because of a bug found on a live tenant: an Entra directory role and a Key Vault
    access policy have no ARM role definition **by design**, and treating that as an unresolved
    role made the engine answer "cannot be determined whether they can delete a VM, because the
    permissions of Global Reader were never collected". That is not an honest uncertainty — it
    is a category error, and it turned 5 of 12 answers into non-answers."""
    surface = str(row.get("surface", ""))

    if surface == schema.SURFACE_ENTRA:
        # A directory role is not an ARM role assignment and grants no ARM action. (Global
        # Administrator *can* elevate to User Access Administrator, but only via an explicit
        # elevateAccess call — that is an escalation PATH, not a current grant, and it belongs
        # to the escalation graph rather than here.)
        return "skip"

    if surface == schema.SURFACE_KEY_VAULT:
        # An access policy grants Key Vault DATA-plane operations on its own vault, and nothing
        # on the control plane. The row carries the granted families in its label rather than a
        # resolvable definition, so it is honoured as a data-plane grant at the vault and
        # ignored elsewhere.
        return "grant" if plane == PLANE_DATA else "skip"

    if surface == schema.SURFACE_CLASSIC:
        name = _squash(str(row.get("roleName", "")))
        if any(c in name for c in _CLASSIC_OWNER_EQUIVALENT):
            return "grant" if plane == PLANE_CONTROL else "skip"
        return "skip"

    return "resolve"


def evaluate(
    rows: list[dict[str, Any]],
    role_index: dict[str, RoleActionSet],
    *,
    principal_id: str,
    scope: str,
    action: str,
    plane: str = "",
) -> Decision:
    """Resolve one (principal, scope, action) question against composed access rows."""
    plane = plane or classify_plane(action)
    dec = Decision(action=action, plane=plane, scope=scope, principal_id=principal_id)

    mine = _principal_rows(rows, principal_id)
    if not mine:
        dec.reason = "This principal holds no access anywhere in the collected estate."
        return dec

    # --- 1. deny assignments ------------------------------------------------------------
    # Denies are evaluated first and are absolute. A deny row carries no action list of its own
    # in the normalized schema, so a deny that reaches the scope denies the action outright;
    # over-reporting a deny is the safe direction (it never invents access).
    for row in mine:
        if row.get("effect") != schema.EFFECT_DENY:
            continue
        if _deny_applies(row, scope):
            dec.denying.append(_row_ref(row))
    if dec.denying:
        dec.verdict = DENIED
        dec.decided_by = dec.denying[0]
        dec.reason = (
            f"A deny assignment at {dec.denying[0]['scopeDisplayName']} blocks this. "
            "Deny assignments are evaluated before role assignments and cannot be overridden, "
            "not even by Owner."
        )
        return dec

    # --- 2. role assignments --------------------------------------------------------------
    conditioned: list[dict[str, Any]] = []
    for row in mine:
        if row.get("effect") == schema.EFFECT_DENY:
            continue
        if not scope_covers(str(row.get("scope", "")), scope):
            continue
        # Eligible-but-not-active PIM access is not access right now. Reporting it as allowed
        # answers "could they activate" when the question was "can they do it".
        if row.get("assignmentState") == schema.STATE_ELIGIBLE:
            continue

        rset = _lookup(role_index, row)
        if rset is None or not rset.known:
            handling = _surface_verdict(row, action, plane)
            if handling == "skip":
                continue
            if handling == "grant":
                dec.granting.append(_row_ref(row, matchedBy=str(row.get("surface", ""))))
                continue
            # An uncollected Azure RBAC role definition genuinely cannot be evaluated. Silently
            # skipping it would turn "we don't know what Owner grants" into "Owner grants
            # nothing", which is the one thing worse than admitting the gap.
            name = str(row.get("roleName", "")) or str(row.get("roleDefinitionId", ""))
            if name and name not in dec.unknown_roles:
                dec.unknown_roles.append(name)
            continue

        granted, excluded = rset.grants(action, plane)
        if not granted:
            continue
        if excluded:
            # notActions is a subtraction from THIS role only — it does not veto another role.
            dec.not_action_exclusions.append(
                _row_ref(row, notAction=excluded, matchedBy=granted)
            )
            continue
        ref = _row_ref(row, matchedBy=granted)
        if str(row.get("condition", "")).strip():
            conditioned.append(ref)
            continue
        dec.granting.append(ref)

    dec.condition_unevaluated = conditioned

    if dec.granting:
        # Report the narrowest (deepest) granting scope as the decider: it is the assignment an
        # operator would actually edit, and the broadest one is rarely the interesting answer.
        # Ties are broken by role name so the same question always returns the same decider —
        # an arbitrary winner makes two identical queries disagree and destroys trust in both.
        dec.granting.sort(key=lambda r: (-_scope_depth(str(r.get("scope", ""))), str(r.get("roleName", ""))))
        dec.decided_by = dec.granting[0]
        dec.verdict = ALLOWED
        dec.via_groups = _via_groups(dec.granting)
        dec.reason = _allow_reason(dec.decided_by, dec)
        return dec

    if conditioned:
        # An assignment that WOULD grant, gated by a condition this phase does not evaluate.
        dec.verdict = INDETERMINATE
        dec.decided_by = conditioned[0]
        dec.via_groups = _via_groups(conditioned)
        dec.reason = (
            f"{conditioned[0]['roleName']} at {conditioned[0]['scopeDisplayName']} would grant "
            "this, but the assignment carries an ABAC condition that is not evaluated here. "
            "The answer depends on the target resource and the request."
        )
        return dec

    if dec.unknown_roles:
        dec.verdict = INDETERMINATE
        dec.reason = (
            "No collected role grants this action, but the permissions of "
            + ", ".join(dec.unknown_roles[:3])
            + " were never collected, so this cannot be answered definitively."
        )
        return dec

    dec.verdict = NOT_GRANTED
    if dec.not_action_exclusions:
        ex = dec.not_action_exclusions[0]
        dec.reason = (
            f"{ex['roleName']} covers this action via {ex['matchedBy']} but subtracts it with "
            f"notActions {ex['notAction']}, and no other assignment grants it."
        )
    else:
        dec.reason = (
            f"No assignment at or above {normalize_scope(scope)} grants {action}"
            + (" (data plane)." if plane == PLANE_DATA else ".")
        )
    return dec


def _via_groups(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in refs:
        gid = str(r.get("sourceGroupId", ""))
        if gid and gid not in seen:
            seen[gid] = {
                "groupId": gid,
                "groupName": r.get("sourceGroupName", "") or gid,
                "assignmentId": r.get("assignmentId", ""),
            }
    return list(seen.values())


def _allow_reason(ref: dict[str, Any], dec: Decision) -> str:
    bits = [f"{ref['roleName']} at {ref['scopeDisplayName']}"]
    if ref.get("sourceGroupName") or ref.get("sourceGroupId"):
        bits.append(f"received via group {ref.get('sourceGroupName') or ref.get('sourceGroupId')}")
    text = "Granted by " + ", ".join(bits) + "."
    if dec.condition_unevaluated:
        text += (
            f" {len(dec.condition_unevaluated)} competing assignment(s) carry an ABAC condition "
            "that was not evaluated."
        )
    if dec.unknown_roles:
        text += f" {len(dec.unknown_roles)} role definition(s) in the path could not be resolved."
    return text


# --------------------------------------------------------------------------- grant sets
def effective_actions(
    rows: list[dict[str, Any]],
    role_index: dict[str, RoleActionSet],
    *,
    principal_id: str,
    scope: str = "/",
) -> dict[str, Any]:
    """Role-level grant set for a principal at (or under) a scope.

    Returns ROLES, not the expanded action strings. A tenant-wide expansion is tens of thousands
    of strings that nobody reads, and the plan is explicit that expansion happens lazily."""
    mine = _principal_rows(rows, principal_id)
    control: list[dict[str, Any]] = []
    data: list[dict[str, Any]] = []
    denies: list[dict[str, Any]] = []
    unknown: list[str] = []

    for row in mine:
        if row.get("effect") == schema.EFFECT_DENY:
            if scope_covers(str(row.get("scope", "")), scope) or scope_covers(scope, str(row.get("scope", ""))):
                denies.append(_row_ref(row))
            continue
        rscope = str(row.get("scope", ""))
        # Both directions: assignments above the scope apply to it, and assignments below it are
        # part of "what can this principal reach from here".
        if not (scope_covers(rscope, scope) or scope_covers(scope, rscope)):
            continue
        rset = _lookup(role_index, row)
        if rset is None or not rset.known:
            # Same category rule as `evaluate`: a directory role or an access policy is not an
            # unresolved ARM role, so it must not be reported as one.
            if _surface_verdict(row, "", PLANE_CONTROL) == "resolve":
                name = str(row.get("roleName", ""))
                if name and name not in unknown:
                    unknown.append(name)
            elif str(row.get("surface", "")) == schema.SURFACE_KEY_VAULT:
                data.append(_row_ref(row))
            elif str(row.get("surface", "")) == schema.SURFACE_CLASSIC:
                control.append(_row_ref(row))
            continue
        ref = _row_ref(
            row,
            actionCount=len(rset.actions),
            dataActionCount=len(rset.data_actions),
            notActions=list(rset.not_actions),
            notDataActions=list(rset.not_data_actions),
        )
        if rset.data_actions:
            data.append(ref)
        if rset.actions:
            control.append(ref)

    return {
        "principalId": principal_id,
        "scope": scope,
        "control": control,
        "data": data,
        "denies": denies,
        "unknownRoles": unknown,
    }


def who_can(
    rows: list[dict[str, Any]],
    role_index: dict[str, RoleActionSet],
    *,
    scope: str,
    action: str,
    plane: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    """The inverse pivot: every principal that can perform ``action`` on ``scope``.

    Evaluates each candidate principal through the same :func:`evaluate`, so a principal denied
    by a deny assignment does not appear as able — which a naive "who holds a matching role"
    query would report, and which is the whole reason this function exists."""
    plane = plane or classify_plane(action)
    candidates: dict[str, str] = {}
    for row in rows:
        pid = str(row.get("effectivePrincipalId", "") or row.get("principalId", ""))
        if not pid:
            continue
        if not (scope_covers(str(row.get("scope", "")), scope) or row.get("effect") == schema.EFFECT_DENY):
            continue
        candidates.setdefault(pid, str(row.get("effectivePrincipalName", "") or row.get("principalDisplayName", "")))

    allowed: list[dict[str, Any]] = []
    indeterminate: list[dict[str, Any]] = []
    for pid, name in candidates.items():
        dec = evaluate(rows, role_index, principal_id=pid, scope=scope, action=action, plane=plane)
        entry = {
            "principalId": pid,
            "principalName": name,
            "verdict": dec.verdict,
            "decidedBy": dec.decided_by,
            "reason": dec.reason,
        }
        if dec.verdict == ALLOWED:
            allowed.append(entry)
        elif dec.verdict == INDETERMINATE:
            indeterminate.append(entry)
        if len(allowed) + len(indeterminate) >= limit:
            break

    return {
        "scope": scope,
        "action": action,
        "plane": plane,
        "allowed": allowed,
        # Kept separate rather than merged into `allowed`: an unevaluated condition is not a yes,
        # and a reader scanning a list of names will not notice a per-row qualifier.
        "indeterminate": indeterminate,
        "candidates": len(candidates),
    }

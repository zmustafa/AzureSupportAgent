"""Custom role hygiene and assignment-limit headroom (least-privilege pillar).

Both families are entirely offline over data already collected, and both answer questions that
nothing in the Azure portal asks.

Role hygiene is about the definitions themselves: a custom role with `*`, six near-identical
copies of the same role, a role that duplicates a built-in, a role nobody is assigned. The
duplicate and built-in-equivalent checks are the valuable ones because their remediation is
concrete — "consolidate these into one" with the resulting definition generated.

Limits are the checks nobody runs until a deployment fails at 2am with an opaque error. Azure
caps role assignments per subscription at 4,000 and per management group at 500, and the failure
mode when you hit them is not a warning, it is a broken deploy.

`notActions` deserves its own note. It is a SUBTRACTION from the role that declares it, not a
deny. A second assignment that grants the same action re-grants it, and the person who built the
role believes they restricted something they did not. That is `lp.role_notactions_illusion`, and
it is the check most likely to surprise the tenant's own platform team.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import effective, schema
from app.iam.signals import Finding, SignalContext, SignalSpec

# Documented Azure limits. Exceeding them fails the deployment, it does not degrade.
LIMIT_ASSIGNMENTS_PER_SUBSCRIPTION = 4000
LIMIT_ASSIGNMENTS_PER_MG = 500
LIMIT_CUSTOM_ROLES_PER_TENANT = 5000
LIMIT_DENY_PER_SUBSCRIPTION = 500

WARN_AT = 0.80
ERROR_AT = 0.95

#: How similar two roles must be to call them duplicates. High on purpose: two roles that
#: overlap 80% usually differ deliberately, and telling a platform team to merge them is how
#: this feature gets switched off.
DUPLICATE_SIMILARITY = 0.95
BUILTIN_EQUIVALENT_SIMILARITY = 0.98

#: Below this a cluster is not worth a group. Default from the plan.
DIRECT_CLUSTER_MIN = 5


def _custom_roles(ctx: SignalContext) -> list[dict[str, Any]]:
    defs = ctx.directory.get("role_defs", []) or []
    ctx.require(
        bool(defs),
        "Role definitions have not been collected, so custom-role hygiene cannot be assessed. "
        "No findings here does not mean the custom roles are clean.",
    )
    return [rd for rd in defs if str(rd.get("roleType", "")).lower() == "customrole"]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _actions_of(rd: dict[str, Any]) -> set[str]:
    return {str(a).lower() for a in (rd.get("actions") or [])} | {
        f"data:{str(a).lower()}" for a in (rd.get("dataActions") or [])
    }


# --------------------------------------------------------------------------- role hygiene
def _wildcard_action(ctx: SignalContext) -> list[Finding]:
    """A custom role granting `*` is Owner wearing a different name — and it evades every
    report that looks for the Owner role by name."""
    out = []
    for rd in _custom_roles(ctx):
        actions = [str(a) for a in (rd.get("actions") or [])]
        if "*" not in actions:
            continue
        out.append(Finding(
            signal_id="lp.role_wildcard_action",
            title="Custom role grants every action",
            severity="error", pillar="lp", object_kind="role_definition",
            subject=str(rd.get("roleDefinitionId") or rd.get("roleName", "")),
            subject_label=str(rd.get("roleName", "")),
            detail=(
                f"{rd.get('roleName')} declares `*` in its actions, so it grants everything Owner "
                f"does while appearing in reports as a bespoke role. Anything that searches for "
                f"'Owner' by name will not find the people holding it."
            ),
            evidence={"role": rd.get("roleName"), "notActions": rd.get("notActions") or []},
            remediation="Replace `*` with the specific actions the role needs, or use Contributor.",
            frameworks=("NIST:AC-6", "MCSB:PA-7", "CIS-Azure:1.23"),
        ))
    return out


def _authorization_write(ctx: SignalContext) -> list[Finding]:
    """A role that can write role assignments can grant itself anything else. That is
    self-elevation by construction, not a risk that depends on who holds it."""
    out = []
    for rd in _custom_roles(ctx):
        hits = [
            str(a) for a in (rd.get("actions") or [])
            if effective.action_matches(str(a), "Microsoft.Authorization/roleAssignments/write")
        ]
        if not hits:
            continue
        out.append(Finding(
            signal_id="lp.role_authorization_write",
            title="Custom role can grant access",
            severity="critical", pillar="lp", object_kind="role_definition",
            subject=str(rd.get("roleDefinitionId") or rd.get("roleName", "")),
            subject_label=str(rd.get("roleName", "")),
            detail=(
                f"{rd.get('roleName')} grants {hits[0]}, so anyone holding it can assign themselves "
                f"any other role. Whatever else it appears to be limited to is advisory."
            ),
            evidence={"role": rd.get("roleName"), "actions": hits[:5]},
            remediation="Remove the roleAssignments/write action, or treat this role as tier-0.",
            frameworks=("NIST:AC-6", "MCSB:PA-1"),
        ))
    return out


def _unused_roles(ctx: SignalContext) -> list[Finding]:
    """Custom roles nobody is assigned. Harmless individually; collectively they are the reason
    nobody can answer "what does this role do" about the ones that matter."""
    customs = _custom_roles(ctx)
    assigned = {str(r.get("roleName", "")).strip().lower() for r in ctx.grants}
    orphans = [rd for rd in customs if str(rd.get("roleName", "")).strip().lower() not in assigned]
    if not orphans:
        return []
    return [Finding(
        signal_id="lp.role_unused",
        title="Custom roles with no assignments",
        severity="info", pillar="lp", object_kind="tenant",
        subject=ctx.tenant_id, subject_label="This tenant",
        detail=(
            f"{len(orphans)} of {len(customs)} custom role(s) are not assigned anywhere in what "
            f"was collected. Each one still has to be understood by whoever audits the tenant next."
        ),
        count=len(orphans),
        evidence={"roles": [rd.get("roleName") for rd in orphans][:20], "total_custom": len(customs)},
        remediation="Delete the ones that are genuinely dead; document the ones kept deliberately.",
        frameworks=("NIST:AC-2",),
    )]


def _duplicate_roles(ctx: SignalContext) -> list[Finding]:
    """Near-identical custom roles. The remediation — consolidate N into 1 — is concrete, and
    it also buys back assignment headroom."""
    customs = _custom_roles(ctx)
    # Built once per role, not once per PAIR. The comparison below is quadratic, so rebuilding
    # the action set inside it made the set construction quadratic too.
    action_sets = [_actions_of(rd) for rd in customs]
    clusters: list[list[dict[str, Any]]] = []
    used: set[int] = set()
    for i, a in enumerate(customs):
        if i in used:
            continue
        group = [a]
        used.add(i)
        for j, b in enumerate(customs):
            if j in used:
                continue
            if _jaccard(action_sets[i], action_sets[j]) >= DUPLICATE_SIMILARITY:
                group.append(b)
                used.add(j)
        if len(group) > 1:
            clusters.append(group)
    return [
        Finding(
            signal_id="lp.role_duplicate",
            title="Near-identical custom roles",
            severity="warning", pillar="lp", object_kind="role_definition",
            subject="|".join(sorted(str(rd.get("roleName", "")) for rd in group)),
            subject_label=str(group[0].get("roleName", "")),
            detail=(
                f"{len(group)} custom roles share at least {int(DUPLICATE_SIMILARITY * 100)}% of "
                f"their actions: {', '.join(str(rd.get('roleName')) for rd in group[:5])}. Each one "
                f"is a separate thing to review, and they drift apart over time."
            ),
            count=len(group),
            evidence={"roles": [rd.get("roleName") for rd in group]},
            remediation=(
                f"Consolidate into one role and repoint the assignments. That also frees "
                f"{len(group) - 1} custom-role slot(s)."
            ),
            frameworks=("NIST:AC-6",),
        )
        for group in clusters
    ]


def _builtin_equivalent(ctx: SignalContext) -> list[Finding]:
    """A custom role whose actions are a subset of a built-in. Microsoft maintains the built-in
    as Azure grows new resource types; the custom copy silently stops covering them."""
    customs = _custom_roles(ctx)
    builtins = [
        rd for rd in (ctx.directory.get("role_defs", []) or [])
        if str(rd.get("roleType", "")).lower() != "customrole" and (rd.get("actions") or [])
    ]
    ctx.require(bool(builtins), "The built-in role catalogue was not collected, so custom roles "
                                "cannot be compared against it.")
    # Every custom role is compared against every built-in. Building the built-in action sets
    # inside that loop rebuilt the same ~1,800 sets for each custom role — 48,532 set
    # constructions on a real tenant, and the single hottest thing in the findings endpoint.
    builtin_sets = [(rd, _actions_of(rd)) for rd in builtins]
    out = []
    for rd in customs:
        mine = _actions_of(rd)
        if not mine:
            continue
        for b, theirs in builtin_sets:
            if not theirs or not mine <= theirs:
                continue
            if _jaccard(mine, theirs) < BUILTIN_EQUIVALENT_SIMILARITY and len(theirs) > len(mine) * 3:
                # A subset of a much larger role is not an "equivalent" — Reader is a subset of
                # Owner. Only flag it when the built-in is genuinely close in size.
                continue
            out.append(Finding(
                signal_id="lp.role_builtin_equivalent",
                title="Custom role duplicates a built-in",
                severity="info", pillar="lp", object_kind="role_definition",
                subject=str(rd.get("roleDefinitionId") or rd.get("roleName", "")),
                subject_label=str(rd.get("roleName", "")),
                detail=(
                    f"{rd.get('roleName')}'s actions are contained in the built-in "
                    f"'{b.get('roleName')}'. Microsoft extends built-ins as Azure grows new "
                    f"resource types; this copy will not follow."
                ),
                evidence={"custom": rd.get("roleName"), "builtin": b.get("roleName"),
                          "similarity": round(_jaccard(mine, theirs), 3)},
                remediation=f"Replace assignments of {rd.get('roleName')} with {b.get('roleName')}.",
                frameworks=("NIST:AC-6",),
            ))
            break
    return out


def _assignable_root(ctx: SignalContext) -> list[Finding]:
    """A role assignable at the tenant root that is only ever used in one subscription. The
    assignable scope is the blast radius of a future mistake, not of a current one."""
    customs = _custom_roles(ctx)
    scopes_by_role: dict[str, set[str]] = defaultdict(set)
    for r in ctx.grants:
        scopes_by_role[str(r.get("roleName", "")).strip().lower()].add(str(r.get("scope", "")))
    out = []
    for rd in customs:
        assignable = [str(s) for s in (rd.get("assignableScopes") or [])]
        broad = [s for s in assignable if s == "/" or "/managementgroups/" in s.lower()]
        if not broad:
            continue
        used = scopes_by_role.get(str(rd.get("roleName", "")).strip().lower(), set())
        subs = {schema.parse_scope(s).get("subscriptionId", "") for s in used} - {""}
        if len(subs) > 1 or not used:
            continue
        out.append(Finding(
            signal_id="lp.role_assignable_root",
            title="Custom role assignable far wider than it is used",
            severity="warning", pillar="lp", object_kind="role_definition",
            subject=str(rd.get("roleDefinitionId") or rd.get("roleName", "")),
            subject_label=str(rd.get("roleName", "")),
            detail=(
                f"{rd.get('roleName')} is assignable at {broad[0]} but every assignment of it is "
                f"inside one subscription. The assignable scope is the blast radius of the next "
                f"mistake, not of the current state."
            ),
            evidence={"role": rd.get("roleName"), "assignableScopes": assignable[:5],
                      "used_in_subscriptions": sorted(subs)},
            remediation="Narrow assignableScopes to the subscription(s) the role is actually used in.",
            frameworks=("NIST:AC-6", "MCSB:PA-7"),
        ))
    return out


def _notactions_illusion(ctx: SignalContext) -> list[Finding]:
    """`notActions` is a subtraction from ITS OWN role, never a deny.

    A principal holding the restricted role AND another role that grants the same action has the
    action. The person who wrote the restriction believes it holds. This is the check most
    likely to surprise a tenant's own platform team."""
    role_index = effective.build_role_index(ctx.directory.get("role_defs", []) or [])
    ctx.require(bool(role_index), "Role definitions were not collected, so notActions cannot be "
                                  "checked against what other roles re-grant.")
    by_principal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        pid = str(r.get("effectivePrincipalId") or "")
        if pid:
            by_principal[pid].append(r)

    out = []
    for pid, rows in by_principal.items():
        sets = [(r, effective._lookup(role_index, r)) for r in rows]
        # Aggregated to ONE finding per principal, not one per re-granted restriction. The
        # subject feeds the fingerprint, which keys the suppression table — emitting several
        # findings that all say `subject=pid` gave them identical fingerprints, so suppressing
        # one silently suppressed the rest and the scanner's own counts stopped adding up
        # (11 findings, 4 fingerprints on a live tenant).
        hits: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
        for row, rset in sets:
            if not rset or not rset.not_actions:
                continue
            hit = _regranted_by(row, rset, sets)
            if not hit:
                continue
            regrant, excluded, action = hit
            hits.append((row, regrant, excluded, action))
        if not hits:
            continue

        row, regrant, excluded, action = hits[0]
        who = str(row.get("effectivePrincipalName") or pid)
        extra = (
            f" {len(hits) - 1} further restriction(s) on this principal are re-granted the same "
            f"way." if len(hits) > 1 else ""
        )
        out.append(Finding(
            signal_id="lp.role_notactions_illusion",
            title="notActions restriction is re-granted by another role",
            severity="warning", pillar="lp", object_kind="principal",
            subject=pid,
            subject_label=who,
            count=len(hits),
            detail=(
                f"{who} holds {row.get('roleName')}, which excludes {excluded} via notActions — "
                f"but also holds {regrant.get('roleName')}, which grants {action}. notActions "
                f"subtracts from its own role; it is not a deny, and only a deny assignment "
                f"stops this.{extra}"
            ),
            evidence={
                "restrictedRole": row.get("roleName"), "notAction": excluded,
                "regrantedBy": regrant.get("roleName"), "regrantedAction": action,
                "scope": row.get("scope"),
                "allRestrictions": [
                    {"restrictedRole": h[0].get("roleName"), "notAction": h[2],
                     "regrantedBy": h[1].get("roleName"), "regrantedAction": h[3]}
                    for h in hits[:10]
                ],
            },
            remediation=(
                "Use a deny assignment if the restriction must hold, or remove the role "
                "that re-grants the action."
            ),
            frameworks=("NIST:AC-6", "MCSB:PA-7"),
        ))
    return out


def _regranted_by(
    row: dict[str, Any],
    rset: effective.RoleActionSet,
    sets: list[tuple[dict[str, Any], effective.RoleActionSet | None]],
) -> tuple[dict[str, Any], str, str] | None:
    """Find another role of the same principal that grants something this role excludes.

    Compares the notActions PATTERN against the other role's declared actions directly. The first
    version probed with a synthesised action string (`Microsoft.Authorization/*/write` with `*`
    replaced by a literal) which matched nothing real, so the check silently never fired — a
    detector that cannot detect is worse than no detector, because its absence reads as a pass."""
    for excluded in rset.not_actions:
        pattern = str(excluded)
        for other, oset in sets:
            if oset is None or oset is rset or not oset.known:
                continue
            if not effective.scope_covers(str(other.get("scope", "")), str(row.get("scope", ""))):
                continue
            for candidate in oset.actions:
                action = str(candidate)
                # The other role grants `action`; does the restriction claim to remove it?
                if not (effective.action_matches(pattern, action) or effective.action_matches(action, pattern)):
                    continue
                if oset.grants(action, effective.PLANE_CONTROL)[1]:
                    continue  # the other role subtracts it too, so nothing is re-granted
                return other, pattern, action
    return None


# --------------------------------------------------------------------------- limits
def _limit_pressure(ctx: SignalContext) -> list[Finding]:
    """Assignment counts against the documented caps. Exceeding one fails a deployment."""
    per_scope: dict[str, int] = defaultdict(int)
    for r in ctx.grants:
        if r.get("accessPath") == schema.PATH_GROUP:
            # Group-expanded rows are not separate assignments in Azure's accounting. Counting
            # them would report a tenant as being at its cap when it is nowhere near it.
            continue
        scope = str(r.get("scope", ""))
        parsed = schema.parse_scope(scope)
        if parsed.get("scopeType") == schema.SCOPE_SUBSCRIPTION:
            per_scope[scope] += 1
        elif parsed.get("scopeType") == schema.SCOPE_MANAGEMENT_GROUP:
            per_scope[scope] += 1

    out = []
    for scope, used in per_scope.items():
        parsed = schema.parse_scope(scope)
        limit = (
            LIMIT_ASSIGNMENTS_PER_MG
            if parsed.get("scopeType") == schema.SCOPE_MANAGEMENT_GROUP
            else LIMIT_ASSIGNMENTS_PER_SUBSCRIPTION
        )
        pct = used / limit
        if pct < WARN_AT:
            continue
        out.append(Finding(
            signal_id="lp.assignment_limit_pressure",
            title="Role assignments approaching the Azure limit",
            severity="error" if pct >= ERROR_AT else "warning",
            pillar="lp", object_kind="scope",
            subject=scope,
            subject_label=scope,
            detail=(
                f"{used} of {limit} role assignments used ({round(pct * 100)}%). Azure does not "
                f"warn at this point — the next deployment past the cap fails with an opaque error."
            ),
            count=used,
            evidence={"used": used, "limit": limit, "percent": round(pct * 100, 1)},
            remediation=(
                "Replace direct assignments with group-based ones: N principals holding the same "
                "role at the same scope need one assignment, not N."
            ),
            frameworks=("MCSB:PA-7",),
        ))
    return out


def _direct_assignment_clusters_ALREADY_EXISTS() -> None:
    """Not implemented here on purpose.

    `lp.direct_assignment_cluster` already ships in `signal_defs/lp.py` from P2, complete with
    the service-principal exclusion (grouping SPs for unrelated workloads couples them, and the
    next person to edit the group breaks two systems). Adding a second copy under the same id
    would have moved weight around silently — the duplicate-id invariant test caught it.
    """


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="lp.role_wildcard_action",
        title="Custom role grants every action",
        pillar="lp", severity="error", weight=8, object_kind="role_definition",
        why="A custom role granting `*` is Owner under another name and evades every Owner report.",
        remediation="Replace `*` with the specific actions needed.",
        frameworks=("NIST:AC-6", "MCSB:PA-7", "CIS-Azure:1.23"),
        evaluate=_wildcard_action,
    ),
    SignalSpec(
        id="lp.role_authorization_write",
        title="Custom role can grant access",
        pillar="lp", severity="critical", weight=9, object_kind="role_definition",
        why="A role that can write role assignments can grant itself everything else.",
        remediation="Remove roleAssignments/write, or treat the role as tier-0.",
        frameworks=("NIST:AC-6", "MCSB:PA-1"),
        evaluate=_authorization_write,
    ),
    SignalSpec(
        id="lp.role_unused",
        title="Custom roles with no assignments",
        pillar="lp", severity="info", weight=2, object_kind="tenant",
        why="Every unassigned role is still something the next auditor has to understand.",
        remediation="Delete the dead ones; document the deliberate ones.",
        frameworks=("NIST:AC-2",),
        evaluate=_unused_roles,
    ),
    SignalSpec(
        id="lp.role_duplicate",
        title="Near-identical custom roles",
        pillar="lp", severity="warning", weight=4, object_kind="role_definition",
        why="Duplicated roles drift apart and multiply the review surface.",
        remediation="Consolidate into one role and repoint the assignments.",
        frameworks=("NIST:AC-6",),
        evaluate=_duplicate_roles,
    ),
    SignalSpec(
        id="lp.role_builtin_equivalent",
        title="Custom role duplicates a built-in",
        pillar="lp", severity="info", weight=3, object_kind="role_definition",
        why="Microsoft extends built-ins as Azure grows; a hand-copied role does not follow.",
        remediation="Replace the custom role with the built-in.",
        frameworks=("NIST:AC-6",),
        evaluate=_builtin_equivalent,
    ),
    SignalSpec(
        id="lp.role_assignable_root",
        title="Custom role assignable far wider than it is used",
        pillar="lp", severity="warning", weight=5, object_kind="role_definition",
        why="assignableScopes is the blast radius of the next mistake.",
        remediation="Narrow assignableScopes to where the role is actually used.",
        frameworks=("NIST:AC-6", "MCSB:PA-7"),
        evaluate=_assignable_root,
    ),
    SignalSpec(
        id="lp.role_notactions_illusion",
        title="notActions restriction is re-granted by another role",
        pillar="lp", severity="warning", weight=7, object_kind="principal",
        why="notActions subtracts from its own role; another role re-grants the action.",
        remediation="Use a deny assignment, or remove the role that re-grants it.",
        frameworks=("NIST:AC-6", "MCSB:PA-7"),
        evaluate=_notactions_illusion,
    ),
    SignalSpec(
        id="lp.assignment_limit_pressure",
        title="Role assignments approaching the Azure limit",
        pillar="lp", severity="warning", weight=6, object_kind="scope",
        why="Azure does not warn; the deployment past the cap simply fails.",
        remediation="Replace direct assignments with group-based ones.",
        frameworks=("MCSB:PA-7",),
        evaluate=_limit_pressure,
    ),
]

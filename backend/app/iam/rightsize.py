"""Right-sizing — *"what is the narrowest set of roles that still does the job?"*

Two pieces of reference data and one search.

**The narrowest-role index** (`action -> roles granting it, narrowest first`) is built once per
tenant from the built-in catalogue. The plan lists four consumers for it and this is the third
to arrive; it was deferred out of P4 because nothing needed it yet.

**Set cover.** Given the actions a principal actually exercised, find the smallest set of
built-in roles whose combined actions cover them. Set cover is NP-hard, so this is the standard
greedy approximation — which is fine, because the answer is a *proposal a human reviews*, not an
optimum anybody will verify. What matters far more than optimality is that the proposal is
honest about what it drops.

The honesty rules from `usage.py` are enforced here, at the point recommendations are built:

- a break-glass principal never gets a removal recommendation, by construction;
- a data-plane role is never right-sized when data-plane logging is unavailable — the Activity
  Log cannot see that activity, so an "unused" verdict is drawn from a source that has nothing
  to say about it;
- every figure carries its denominator, its window and its confidence;
- `residual_risk` names what the narrower proposal gives up, because "covers everything you did
  last quarter" and "safe" are different claims.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any
from collections.abc import Callable

from app.iam import diff as diff_mod, effective, usage

log = logging.getLogger("app.iam.rightsize")

#: Right-sizing memo: (tenant, cache fingerprint) -> analysis. Bounded so an estate with many
#: connections cannot pin every tenant's analysis in memory at once.
_ANALYSIS_CACHE: OrderedDict[tuple[str, tuple[str, int]], dict[str, Any]] = OrderedDict()
MAX_MEMO_ENTRIES = 6

#: Roles never proposed as a replacement. Owner and the assignment-granting roles are the thing
#: right-sizing exists to move people OFF; proposing one as the "narrower" option is how a
#: greedy search that only counts actions produces an absurd answer.
NEVER_PROPOSE = frozenset({
    "owner", "user access administrator", "role based access control administrator",
    "co-administrator", "account administrator", "service administrator",
})

#: Cap the search. A principal with thousands of distinct exercised actions is not a
#: right-sizing candidate, it is a platform identity, and the greedy loop should not spend
#: minutes discovering that.
MAX_ACTIONS = 500
MAX_ROLES_IN_PROPOSAL = 4

#: Below this, "over-privileged" is noise — every role grants more than anyone uses.
OVERPRIVILEGE_THRESHOLD = 0.90


def _breadth_memo(universe: tuple[str, ...]):
    """Breadth per role, computed once.

    `sorted(key=...)` calls the key once per element, but `cover()` re-sorts its candidates on
    every greedy step — without this the same 1,800 roles are re-measured against the same
    universe on each iteration."""
    cache: dict[str, int] = {}

    def measure(role: effective.RoleActionSet) -> int:
        key = role.role_definition_id or role.role_name.lower()
        if key not in cache:
            cache[key] = usage.breadth(role, universe)
        return cache[key]

    return measure


def build_narrowest_index(
    role_index: dict[str, effective.RoleActionSet],
    universe: tuple[str, ...],
) -> list[effective.RoleActionSet]:
    """Built-in roles ordered narrowest-first, deduplicated.

    `role_index` is keyed by both GUID and name, so the same role appears twice; iterating it
    directly would double-count every role and make the greedy search prefer whichever spelling
    it happened to see first.

    "Narrow" is measured against the observable action universe, not against how many patterns
    the role declares — by pattern count `Owner` (`*`) and `Reader` (`*/read`) are equally narrow,
    and a set-cover search would happily propose Owner as the tighter option."""
    seen: set[str] = set()
    out: list[effective.RoleActionSet] = []
    for role in role_index.values():
        key = role.role_definition_id or role.role_name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if not role.known:
            # A role whose permissions were never collected has empty action lists, which is
            # indistinguishable from one that grants nothing — and would win every set-cover
            # search by being the "narrowest" role in the tenant while covering nothing.
            continue
        out.append(role)
    measure = _breadth_memo(universe)
    out.sort(key=lambda r: (measure(r), r.role_name.lower()))
    return out


def narrowest_granting(
    action: str,
    catalogue: list[effective.RoleActionSet],
    *,
    plane: str = effective.PLANE_CONTROL,
    include_custom: bool = False,
) -> list[effective.RoleActionSet]:
    """Every role granting `action`, narrowest first."""
    return [
        r for r in catalogue
        if (include_custom or not r.is_custom) and usage.covers(r, action, plane=plane)
    ]


def cover(
    actions: set[str],
    catalogue: list[effective.RoleActionSet],
    universe: tuple[str, ...],
    *,
    plane: str = effective.PLANE_CONTROL,
    max_roles: int = MAX_ROLES_IN_PROPOSAL,
    measure: Callable[[effective.RoleActionSet], int] | None = None,
) -> tuple[list[effective.RoleActionSet], set[str]]:
    """Greedy set cover. Returns ``(chosen_roles, actions_left_uncovered)``.

    Preference order, applied by sorting the candidates at each step: covers more of what is
    left, then grants LESS of the observable universe overall, then alphabetical so the answer
    is deterministic. A recommendation whose output changes between identical runs is not usable
    for a decision.

    ``universe`` is REQUIRED rather than defaulted. With an empty universe every role has a
    breadth of zero, the tie-break falls through to alphabetical order, and the search cheerfully
    proposes Contributor over Reader — which is the exact bug this argument exists to prevent.

    ``measure`` is the breadth memo. Pass the caller's when covering repeatedly against one
    universe: breadth is by far the most expensive thing here, it depends only on (role,
    universe), and a per-call memo re-measures the same roles for every recommendation. On a
    5,506-grant tenant this ran 2,185 times and breadth dominated the profile."""
    remaining = {a for a in actions if a}
    chosen: list[effective.RoleActionSet] = []
    usable = [r for r in catalogue if not r.is_custom and r.role_name.lower() not in NEVER_PROPOSE]
    breadth_of = measure or _breadth_memo(universe)
    while remaining and len(chosen) < max_roles:
        scored = [
            (len({a for a in remaining if usage.covers(r, a, plane=plane)}), r)
            for r in usable
            if r not in chosen
        ]
        scored = [(n, r) for n, r in scored if n > 0]
        if not scored:
            break
        scored.sort(key=lambda t: (-t[0], breadth_of(t[1]), t[1].role_name.lower()))
        best = scored[0][1]
        chosen.append(best)
        remaining -= {a for a in remaining if usage.covers(best, a, plane=plane)}

    return chosen, remaining


def narrowest_scope(exercised_scopes: set[str], current_scope: str) -> str:
    """The tightest scope that still contains everything the principal touched.

    Returns the current scope unchanged when the exercised scopes span more than one branch —
    proposing a narrower scope that does not contain all the activity is proposing an outage."""
    if not exercised_scopes:
        return current_scope
    candidates = [s for s in exercised_scopes if effective.scope_covers(current_scope, s)]
    if not candidates or len(candidates) < len(exercised_scopes):
        # Some activity was outside the current scope (inherited from higher up, or the join is
        # incomplete). Narrowing on that evidence would be guessing.
        return current_scope
    common = _common_prefix(candidates)
    return common if common and diff_mod.scope_depth(common) > diff_mod.scope_depth(current_scope) else current_scope


def _common_prefix(scopes: list[str]) -> str:
    """Deepest ADDRESSABLE ARM scope containing every input, computed SEGMENT-wise.

    A character-wise prefix of `/subscriptions/abc` and `/subscriptions/abd` is
    `/subscriptions/ab`, which is not a scope at all and covers neither.

    Segment-wise is necessary but not sufficient: the shared prefix of two VMs in one resource
    group runs through `/providers/Microsoft.Compute/virtualMachines`, and none of those are
    scopes you can assign a role at. The result is truncated back to the deepest ADDRESSABLE
    boundary — tenant, management group, subscription, resource group or a whole resource."""
    parts = [s.strip("/").split("/") for s in scopes if s]
    if not parts:
        return ""
    shared: list[str] = []
    for chunk in zip(*parts):
        if len({c.lower() for c in chunk}) != 1:
            break
        shared.append(chunk[0])
    return _truncate_to_scope(shared)


def _truncate_to_scope(segments: list[str]) -> str:
    """Cut a segment list back to the deepest scope Azure will accept a role assignment at."""
    lowered = [s.lower() for s in segments]
    # A full resource: /subscriptions/x/resourceGroups/y/providers/NS/type/name
    if "providers" in lowered:
        idx = lowered.index("providers")
        if len(segments) >= idx + 4:
            return "/" + "/".join(segments[: idx + 4])
        # Inside the provider path but short of a named resource — fall back to the RG.
        segments = segments[:idx]
        lowered = lowered[:idx]
    if "resourcegroups" in lowered:
        idx = lowered.index("resourcegroups")
        if len(segments) >= idx + 2:
            return "/" + "/".join(segments[: idx + 2])
        segments = segments[:idx]
        lowered = lowered[:idx]
    if "subscriptions" in lowered:
        idx = lowered.index("subscriptions")
        if len(segments) >= idx + 2:
            return "/" + "/".join(segments[: idx + 2])
    if "managementgroups" in lowered:
        idx = lowered.index("managementgroups")
        if len(segments) >= idx + 2:
            return "/" + "/".join(segments[: idx + 2])
    return ""


# --------------------------------------------------------------------------- recommendations
def analyse(
    rows: list[dict[str, Any]],
    role_index: dict[str, effective.RoleActionSet],
    usage_payload: dict[str, Any],
    *,
    data_plane_logged: bool = False,
    break_glass: set[str] | None = None,
    threshold: float = OVERPRIVILEGE_THRESHOLD,
) -> dict[str, Any]:
    """Granted-vs-used per (principal, scope), with a narrower proposal where one is defensible.

    Returns an explicitly-unmeasured payload when usage was not collected, rather than an empty
    recommendation list — which would read as "nothing is over-privileged"."""
    window = int(usage_payload.get("window_days") or 0)
    exclusions: list[str] = []

    if not usage.is_measured(usage_payload):
        # `excluded` is populated here too. A field whose meaning changes between branches —
        # "roles left out of the analysis" in one and "empty because there was no analysis" in
        # the other — is a trap for the next consumer, and the data-plane caveat is true either
        # way: nothing on this screen will ever judge a data-plane role while the log cannot see
        # data-plane activity.
        if not data_plane_logged:
            exclusions.append(
                "Data-plane roles were excluded: the Activity Log does not record data-plane "
                "operations, so it cannot show them as used or unused."
            )
        return {
            "measured": False,
            "recommendations": [],
            "window_days": window,
            "assessed": 0,
            "unresolved_roles": 0,
            "action_universe_size": 0,
            "excluded": exclusions,
            "limitations": (usage_payload.get("limitations") or usage.LIMITATIONS) + [
                "Usage has not been collected for this tenant, so nothing here is a claim about "
                "what is unused. Run a usage scan first."
            ],
            "notes": usage_payload.get("notes") or [],
        }

    used = usage.used_actions(usage_payload)
    events = usage.event_counts(usage_payload)
    # The denominator, built once. Every ratio below is "of the actions this tenant's roles can
    # actually speak about", which is a number a reader can be shown rather than a percentage of
    # something unstated.
    all_used: set[str] = set()
    for actions in used.values():
        all_used |= actions
    catalogue = build_narrowest_index(role_index, ())
    universe = usage.action_universe(catalogue, all_used)
    catalogue = build_narrowest_index(role_index, universe)
    measure = _breadth_memo(universe)

    if not data_plane_logged:
        exclusions.append(
            "Data-plane roles were excluded: the Activity Log does not record data-plane "
            "operations, so it cannot show them as used or unused."
        )

    out: list[dict[str, Any]] = []
    assessed = 0
    skipped_break_glass = 0
    # Rows dropped because their role's actions were never collected. Counted rather than
    # silently skipped: when the role catalogue is missing, EVERY row takes this branch and the
    # function returns an empty recommendation list that is indistinguishable from "this tenant
    # is clean". That is the exact shape of the bug this counter exists to make visible.
    unresolved = 0

    for group in _by_principal_scope(rows):
        row = group["row"]
        pid = str(row.get("effectivePrincipalId") or row.get("principalId") or "").lower()
        if not pid:
            continue
        role = effective._lookup(role_index, row)
        if not role or not role.known:
            # A role whose actions were never collected cannot be compared against anything.
            unresolved += 1
            continue
        # Data-plane roles are out of scope when the log cannot see data-plane activity. This is
        # a `continue`, not a filter on the output, so it cannot be undone downstream.
        if role.data_actions and not data_plane_logged:
            continue

        assessed += 1
        granted = measure(role)
        principal_used = used.get(pid, set())
        covered = {a for a in principal_used if usage.covers(role, a)}
        unused_ratio = 1.0 - (len(covered) / granted) if granted else 0.0
        if unused_ratio < threshold:
            continue

        conf, why = usage.confidence_for(window, events=events.get(pid, 0))

        if usage.is_break_glass(row, break_glass):
            # Reported, never recommended. Showing it keeps the reader honest about why the
            # number in the header does not match the list length.
            skipped_break_glass += 1
            out.append(_entry(row, role, granted, covered, unused_ratio, window, conf, why,
                              proposal=None,
                              note="Break-glass account: excluded from removal recommendations "
                                   "by construction. It is SUPPOSED to look unused."))
            continue

        chosen, uncovered = cover(covered, catalogue, universe, measure=measure)
        proposal = None
        if chosen and not uncovered:
            proposal = {
                "roles": [r.role_name for r in chosen],
                "scope": narrowest_scope(set(group["scopes"]), str(row.get("scope", ""))),
                "coversUsedActions": True,
                "residualRisk": _residual(role, chosen, universe),
            }
        out.append(_entry(row, role, granted, covered, unused_ratio, window, conf, why,
                          proposal=proposal, note=_note_for(proposal, covered)))

    out.sort(key=lambda r: (-r["unusedRatio"], r["principalName"]))
    if unresolved:
        exclusions.append(
            f"{unresolved} assignment(s) could not be assessed because the actions of the role "
            f"they grant were never collected. They are absent from the count below, not clean."
        )
    # Nothing at all was assessable. The recommendation list is empty because the analysis could
    # not run, which must never be presented as "nothing is over-privileged".
    blind = assessed == 0 and unresolved > 0
    return {
        "measured": not blind,
        "recommendations": out,
        "assessed": assessed,
        "unresolved_roles": unresolved,
        "window_days": window,
        "source": usage_payload.get("source", ""),
        "break_glass_excluded": skipped_break_glass,
        # Published so the ratio is legible: "of the N distinct actions this tenant's roles can
        # grant". A percentage whose denominator is unstated is a number designed to be quoted.
        "action_universe_size": len(universe),
        "excluded": exclusions,
        "notes": usage_payload.get("notes") or [],
        "limitations": (usage_payload.get("limitations") or []) + exclusions + ([
            "No role definition in this tenant could be resolved to its actions, so NOTHING was "
            "assessed. This is not a clean result — run a full access refresh to re-collect the "
            "role catalogue, then re-run the usage scan."
        ] if blind else []),
    }


def _remember(key: tuple[str, tuple[str, int]], payload: dict[str, Any]) -> None:
    _ANALYSIS_CACHE[key] = payload
    _ANALYSIS_CACHE.move_to_end(key)
    while len(_ANALYSIS_CACHE) > MAX_MEMO_ENTRIES:
        _ANALYSIS_CACHE.popitem(last=False)


def analyse_for_tenant(tenant_id: str, *, force: bool = False) -> dict[str, Any]:
    """The granted-vs-used analysis for a tenant's current snapshot, memoised and PERSISTED.

    :func:`analyse` is pure CPU over (assignments x role catalogue): **two seconds** on a
    realistic 5,506-grant tenant even after the breadth memo, and it was previously re-run on
    every single request to `/iam/rightsizing` — cold AND warm, because the endpoint computed
    from scratch while a perfectly good cached copy sat on disk unread.

    Freshness is the cache version, never a TTL. Rows, the directory and the usage sweep all
    bump it, and persisting this analysis does not — so the stamp means exactly "the inputs this
    was derived from are still the current ones". ``force=True`` is what the refresh path uses.

    Callers holding rows that did not come from the tenant cache must call :func:`analyse`."""
    from app.iam import cache, compose

    version = cache.cache_version()
    key = (tenant_id, cache.cache_fingerprint())

    if not force:
        hit = _ANALYSIS_CACHE.get(key)
        if hit is not None:
            _ANALYSIS_CACHE.move_to_end(key)
            return hit
        meta = cache.read_rightsizing_meta(tenant_id)
        if meta.get("cache_version") == version:
            stored = cache.read_rightsizing(tenant_id)
            if stored:
                _remember(key, stored)
                return stored

    started = time.monotonic()
    payload = analyse(
        compose.build_master_rows(tenant_id),
        effective.build_role_index(cache.read_directory(tenant_id).get("role_defs", [])),
        cache.read_usage(tenant_id),
        data_plane_logged=False,
    )
    duration = time.monotonic() - started
    _remember(key, payload)
    try:
        cache.write_rightsizing(
            tenant_id, payload, cache_version=version, duration_seconds=duration,
        )
    except Exception:  # noqa: BLE001 - a cache write must never fail the request
        log.warning("iam rightsizing: could not persist the analysis", exc_info=True)
    return payload


def _note_for(proposal: dict[str, Any] | None, covered: set[str]) -> str:
    """Why there is no proposal — and the two reasons are completely different.

    Nothing recorded at all is the common case on a real tenant, and saying "no combination of
    roles covers everything this principal did" about somebody who did *nothing* implies they did
    something unusual. It also invites the obvious wrong inference — that the answer is to remove
    all their access — from ninety days of silence that may simply mean their activity is not
    logged."""
    if proposal:
        return ""
    if not covered:
        return (
            "No operation by this principal was recorded in the window at all. That is consistent "
            "with unused access AND with an identity whose activity the Activity Log does not "
            "capture, so no narrower role is proposed — there is nothing to size against."
        )
    return (
        "No combination of built-in roles covers everything this principal did, so no narrower "
        "proposal is offered."
    )


def _entry(
    row: dict[str, Any],
    role: effective.RoleActionSet,
    granted: int,
    covered: set[str],
    ratio: float,
    window: int,
    confidence: str,
    why: str,
    *,
    proposal: dict[str, Any] | None,
    note: str,
) -> dict[str, Any]:
    return {
        # The identity this recommendation is ABOUT — the same (principal, role, scope, surface,
        # state) key `_by_principal_scope` grouped on. Published because the grouping happens
        # here: a principal holding two over-privileged roles at one scope produces two distinct
        # recommendations, and a consumer that keys them on (principal, scope) alone silently
        # collapses them. On a real tenant that hid 243 of 2185 rows from the reviewer while the
        # header above the list still counted all 2185.
        "id": diff_mod.row_key(row),
        "principalId": str(row.get("effectivePrincipalId") or row.get("principalId") or ""),
        "principalName": str(row.get("effectivePrincipalName") or row.get("principalDisplayName") or ""),
        "principalType": str(row.get("effectivePrincipalType") or row.get("principalType") or ""),
        "scope": str(row.get("scope", "")),
        "scopeName": str(row.get("scopeDisplayName", "")),
        "currentRoles": [str(row.get("roleName", ""))],
        # Both numbers, always, never just the ratio. "99.8% over-privileged" on its own is a
        # figure designed to be quoted out of context.
        "usedActionCount": len(covered),
        "grantedActionCount": granted,
        "unusedRatio": round(ratio, 4),
        "usedActions": sorted(covered)[:25],
        "window": {"days": window, "clamped": window < usage.DEFAULT_WINDOW_DAYS},
        "confidence": confidence,
        "confidenceWhy": why,
        "recommendation": proposal,
        "note": note,
    }


def _residual(
    current: effective.RoleActionSet,
    chosen: list[effective.RoleActionSet],
    universe: tuple[str, ...],
) -> str:
    """What the narrower proposal gives up. Named, not implied.

    "Covers everything you did last quarter" and "safe" are different claims, and the gap
    between them is exactly where a right-sizing recommendation causes an incident."""
    lost = sorted(
        usage.granted_actions(current, universe)
        - set().union(*(usage.granted_actions(r, universe) for r in chosen)) if chosen
        else usage.granted_actions(current, universe)
    )
    if not lost:
        return "No observed action granted today is lost."
    notable = [a for a in lost if "authorization" in a]
    head = notable[:3] or lost[:3]
    return (
        f"Loses {len(lost)} granted action(s) not exercised in the window, including "
        + ", ".join(head)
        + ". None were used, which is not the same as none being needed."
    )


def _by_principal_scope(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One group per (principal, role, scope) grant that actually grants something."""
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("effect") == "Deny":
            continue
        if r.get("assignmentState") == "Eligible":
            # Eligible access is not held; right-sizing it is answering a question nobody asked.
            continue
        key = diff_mod.row_key(r)
        groups.setdefault(key, {"row": r, "scopes": set()})["scopes"].add(str(r.get("scope", "")))
    return list(groups.values())

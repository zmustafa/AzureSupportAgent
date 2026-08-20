"""Usage collection — the *used* half of "granted vs used".

Everything hard about CIEM is in what this module refuses to conclude. Comparing granted actions
against used actions is arithmetic; deciding that an unused permission is an unnecessary one is
a judgment, and it is wrong often enough to cause outages.

Four rules, all of them load-bearing:

**Absence of use is not proof of non-need.** A break-glass account is *supposed* to be unused.
`BREAK_GLASS_MARKERS` and any principal a human has flagged are excluded from removal
recommendations by construction — not filtered out of the UI afterwards, where a future refactor
could drop the filter and nobody would notice.

**Confidence reflects the window, not the sample size.** Ninety days does not cover an annual DR
test, a quarterly close or a yearly certificate rotation. A 30-day window over a workload with a
quarterly cadence is `low` confidence no matter how much data it gathered.

**Read operations are under-logged, and data-plane operations are absent entirely** unless
diagnostic settings ship them somewhere. Recommending the removal of a data-plane role on the
strength of an Activity Log that never records data-plane activity is drawing a conclusion from
a source that cannot speak to it. Data-plane roles are excluded from the analysis and the
exclusion is stated.

**Publish the denominator.** "998 of 8213 granted actions unused" is a fact. "99.8%
over-privileged" is a number designed to be quoted out of context.

Usage runs as its own schedulable job with its own cache slice and its own freshness, because
the Activity Log is per-subscription and slow — a 26-subscription tenant cannot do this inside
an access refresh. The UI therefore shows access as fresh and usage as whatever age it is.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.iam import attribution, effective, schema

log = logging.getLogger("app.iam.usage")

#: Same platform limit the attribution join hits. Asking for more silently returns nothing
#: rather than erroring, so the window is clamped before it is asked for.
MAX_WINDOW_DAYS = attribution.ACTIVITY_LOG_RETENTION_DAYS
DEFAULT_WINDOW_DAYS = 90

SOURCE_ACTIVITY_LOG = "ActivityLog"
SOURCE_WORKSPACE = "AzureActivity"

# The exact phrase the Activity Log collector emits when a subscription trips the 6 MB cap
# ("...returned more than 6 MB and was truncated; showing the N event(s) received"). Matching
# this rather than the bare word "truncated" keeps a note that merely mentions truncation —
# or denies it — from being read as a partial sweep.
TRUNCATION_MARKER = "and was truncated"

# Confidence in a "this is unused" claim.
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

#: A window shorter than this cannot speak to a quarterly process, let alone an annual one.
QUARTERLY_DAYS = 90
MONTHLY_DAYS = 31

#: Naming conventions that mean "this account exists to be unused". Matching is substring-based
#: and deliberately generous: a false positive costs one missed recommendation, a false negative
#: recommends revoking the account that gets you back in when everything else is broken.
BREAK_GLASS_MARKERS = (
    "break-glass", "breakglass", "break_glass", "emergency", "glassbreak",
    "firecall", "fire-call", "backdoor-admin", "eba-", "emergencyaccess",
)


def is_break_glass(row: dict[str, Any], flagged: set[str] | None = None) -> bool:
    """Whether this principal is exempt from removal recommendations.

    Checked at the point recommendations are BUILT rather than filtered out of the response,
    so a future change to the presentation layer cannot re-expose it."""
    pid = str(row.get("effectivePrincipalId") or row.get("principalId") or "").lower()
    if flagged and pid in flagged:
        return True
    haystack = " ".join(
        str(row.get(k, "")).lower()
        for k in ("effectivePrincipalName", "principalDisplayName",
                  "effectivePrincipalUserPrincipalName", "principalUserPrincipalName")
    )
    return any(marker in haystack for marker in BREAK_GLASS_MARKERS)


def clamp_window(days: int, *, now: datetime | None = None) -> tuple[str, str, int, str]:
    """``(start_iso, end_iso, effective_days, note)``.

    The note is non-empty exactly when the request was cut down, and it travels into every
    figure computed from the window. A silently shortened window makes "never used" mean two
    different things."""
    now = now or datetime.now(timezone.utc)
    asked = max(1, int(days))
    allowed = min(asked, MAX_WINDOW_DAYS)
    note = ""
    if allowed < asked:
        note = (
            f"Requested {asked} days but the Azure Activity Log retains {MAX_WINDOW_DAYS}. "
            f"Usage covers {allowed} days; anything exercised only before that looks unused here."
        )
    return (now - timedelta(days=allowed)).isoformat(), now.isoformat(), allowed, note


def confidence_for(window_days: int, *, cadence_days: int = 0, events: int = 0) -> tuple[str, str]:
    """``(confidence, why)`` for a claim that something is unused.

    Driven by the WINDOW, not by how many events were collected. A month of dense data still
    cannot tell you whether a quarterly job needs its permissions."""
    if cadence_days and window_days < cadence_days:
        return LOW, (
            f"The window is {window_days} days but this workload has a known {cadence_days}-day "
            f"cadence, so a process that runs on that cadence may not appear at all."
        )
    if window_days < MONTHLY_DAYS:
        return LOW, f"A {window_days}-day window cannot speak to anything monthly or rarer."
    if window_days < QUARTERLY_DAYS:
        return MEDIUM, (
            f"A {window_days}-day window covers monthly work but not a quarterly close, an "
            f"annual DR test or a yearly certificate rotation."
        )
    if events == 0:
        return MEDIUM, (
            f"No activity at all was recorded in {window_days} days. That is consistent with "
            f"unused access and also with an identity whose activity is not logged."
        )
    return HIGH, f"A {window_days}-day window with {events} recorded operation(s)."


# --------------------------------------------------------------------------- collection
async def collect(
    subscriptions: list[str],
    connection: dict[str, Any] | None,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Gather management-plane operations per principal for the window.

    Reuses Change Explorer's Activity Log collector, which already handles the
    service-principal-vs-pasted-token split, 429 backoff, the capture cap and salvage on
    truncation. Reimplementing any of that would be relearning it."""
    from app.changeexplorer.collectors import collect_activity_log

    start_iso, end_iso, window_days, clamp_note = clamp_window(days)
    notes = [n for n in (clamp_note,) if n]
    events: list[dict[str, Any]] = []
    status = schema.STATUS_SUCCEEDED
    if not subscriptions:
        return _empty(window_days, "No subscriptions in scope, so no usage could be collected.",
                      status=schema.STATUS_SKIPPED)
    try:
        events, note = await collect_activity_log(subscriptions, start_iso, end_iso, connection)
        if note:
            notes.append(note)
    except Exception as exc:  # noqa: BLE001 — usage is additive; losing it must not lose access data
        log.warning("iam usage: activity log unavailable", exc_info=True)
        return _empty(window_days, f"Usage collection failed: {exc}", status=schema.STATUS_FAILED)

    by_principal: dict[str, dict[str, Any]] = {}
    for e in events:
        pid = str(e.get("actorObjectId", "") or "").lower()
        if not pid:
            # An event with no resolvable actor cannot be attributed to a principal. Counting it
            # against nobody is right; counting it against everybody would manufacture usage.
            continue
        entry = by_principal.setdefault(pid, {"principalId": pid, "actions": set(), "events": 0,
                                              "displayName": str(e.get("actor", "")), "scopes": set(),
                                              "lastSeen": ""})
        op = str(e.get("operation", "")).strip()
        if op:
            entry["actions"].add(op.lower())
        entry["events"] += 1
        # The most recent operation this principal was seen performing. "Granted Owner in 2019,
        # last actually did anything in 2021, account disabled in 2024" is a far stronger case
        # for removal than any role name, and the timestamp was already in every event and
        # being discarded. ISO-8601 UTC strings compare correctly as strings.
        when = str(e.get("eventTime", "") or "")
        if when > entry["lastSeen"]:
            entry["lastSeen"] = when
        rid = str(e.get("resourceId", ""))
        if rid:
            entry["scopes"].add(rid.lower())

    return {
        "window_days": window_days,
        "start": start_iso,
        "end": end_iso,
        "source": SOURCE_ACTIVITY_LOG,
        "status": status,
        "subscriptions": len(subscriptions),
        "event_count": len(events),
        # The Activity Log query caps at 6 MB per subscription and the collector says so in a
        # note when it trips. Measured on a real tenant: ELEVEN subscriptions truncated in one
        # 90-day sweep. A truncated sweep holds a PREFIX of the activity, so the absence of an
        # operation proves nothing at all — and "this principal never used their access" is an
        # argument for deleting it. The flag has to travel with the data, not sit in a note
        # nobody parses.
        # Anchored on the phrase the collector actually emits, not the bare word, so a future
        # note reading "not truncated" cannot flip this to the alarming value.
        "truncated": any(TRUNCATION_MARKER in str(n).lower() for n in notes),
        "principals": [
            {
                "principalId": v["principalId"],
                "displayName": v["displayName"],
                "actions": sorted(v["actions"]),
                "events": v["events"],
                "lastSeen": v["lastSeen"],
                "scopes": sorted(v["scopes"])[:200],
            }
            for v in by_principal.values()
        ],
        "notes": notes,
        # Stated on every response, because it is the single biggest reason a usage-based
        # conclusion can be wrong.
        "limitations": LIMITATIONS,
    }


LIMITATIONS = [
    "The Azure Activity Log records management-plane writes well and reads poorly. An action "
    "absent from this data was not necessarily unused.",
    "Data-plane operations (blob reads, key retrievals, SQL queries) are NOT in the Activity "
    "Log at all unless diagnostic settings ship them elsewhere. Data-plane roles are therefore "
    "excluded from right-sizing rather than recommended for removal on no evidence.",
    "Absence of use is not proof of absence of need. A break-glass account is supposed to look "
    "unused, and is excluded from recommendations by construction.",
]


def _empty(window_days: int, note: str, *, status: str) -> dict[str, Any]:
    """A usage payload that says nothing was gathered, in a shape every consumer can read.

    An empty dict here would make `used_actions` a `KeyError` or, worse, an empty set that reads
    as "this principal used nothing"."""
    return {
        "window_days": window_days,
        "start": "", "end": "", "source": SOURCE_ACTIVITY_LOG,
        "status": status,
        "subscriptions": 0,
        "event_count": 0,
        "principals": [],
        "notes": [note],
        "limitations": LIMITATIONS,
        # The gate every consumer must check. False means "we have not measured usage", which is
        # NOT the same as "nothing was used" — and the difference is somebody's access.
        "measured": False,
    }


def is_measured(payload: dict[str, Any]) -> bool:
    """True only when usage was actually gathered and can be reasoned from."""
    if not payload:
        return False
    if payload.get("measured") is False:
        return False
    return payload.get("status") not in schema.UNTRUSTWORTHY_STATUSES


def used_actions(payload: dict[str, Any]) -> dict[str, set[str]]:
    """``principalId -> {action}``, lower-cased."""
    return {
        str(p["principalId"]).lower(): {str(a).lower() for a in (p.get("actions") or [])}
        for p in (payload.get("principals") or [])
    }


def event_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {str(p["principalId"]).lower(): int(p.get("events") or 0) for p in (payload.get("principals") or [])}


# --------------------------------------------------------------------------- granted vs used
def action_universe(
    roles: list[effective.RoleActionSet],
    extra: set[str] | None = None,
) -> tuple[str, ...]:
    """The concrete actions this tenant's roles can speak about.

    The obvious denominator — "how many action patterns does the role declare" — is worse than
    useless: `Owner` declares exactly one (`*`), so the most over-privileged role in Azure scores
    an unused ratio of **zero** and never appears in a report about over-privilege. Reader
    declares one too. The measure inverts precisely where it matters.

    Expanding against the full Azure action catalog is not available offline, so the universe
    is built from what is actually observable: every LITERAL action any collected role declares,
    plus every action anybody was seen to use. It is concrete, it is derived from real data, and
    it can be published alongside the ratio so a reader can see what the percentage is *of*."""
    out: set[str] = set(extra or ())
    for role in roles:
        for action in (*role.actions, *role.data_actions):
            text = str(action).strip().lower()
            # A wildcard is not a member of the universe; it is a claim over the universe.
            if text and "*" not in text:
                out.add(text)
    return tuple(sorted(out))


_NS_CACHE: dict[int, dict[str, tuple[str, ...]]] = {}


def _namespaces_for(universe: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Universe actions bucketed by resource-provider namespace, memoised per universe.

    An Azure action is `<Namespace>/<path>/<verb>`, and almost every wildcard pattern names a
    concrete namespace (`Microsoft.Compute/*/read`). Scanning only that namespace turns a
    wildcard match from "the whole universe" into "a few dozen strings"."""
    key = id(universe)
    cached = _NS_CACHE.get(key)
    if cached is None:
        buckets: dict[str, list[str]] = {}
        for action in universe:
            buckets.setdefault(action.split("/", 1)[0], []).append(action)
        cached = {k: tuple(v) for k, v in buckets.items()}
        if len(_NS_CACHE) > 8:  # bounded; one analysis holds one universe
            _NS_CACHE.clear()
        _NS_CACHE[key] = cached
    return cached


def _candidates(pattern: str, universe: tuple[str, ...]) -> tuple[str, ...]:
    """The subset of the universe a wildcard pattern could possibly match."""
    namespace = pattern.split("/", 1)[0]
    if "*" in namespace:
        return universe
    return _namespaces_for(universe).get(namespace, ())


def granted_actions(
    role: effective.RoleActionSet | None,
    universe: tuple[str, ...],
    *,
    plane: str = effective.PLANE_CONTROL,
) -> set[str]:
    """Which actions in the universe this role actually grants.

    Split by pattern SHAPE rather than testing every action against every pattern. Nearly all
    role patterns are literal, and a literal pattern is either in the universe or it is not — an
    O(1) set lookup. Wildcards are narrowed to their resource-provider namespace first.

    The naive version tested 3,947 universe actions against every pattern of all 1,848 roles:
    tens of millions of regex matches. It measured **40 seconds** on a live tenant and, being
    synchronous inside an async handler, stalled the whole application until SQLite began
    reporting "database is locked" on unrelated session writes."""
    if not role:
        return set()
    patterns = role.data_actions if plane == effective.PLANE_DATA else role.actions
    excludes = role.not_data_actions if plane == effective.PLANE_DATA else role.not_actions
    members = set(universe)

    granted: set[str] = set()
    for raw in patterns:
        pattern = str(raw).strip().lower()
        if not pattern:
            continue
        if "*" not in pattern:
            if pattern in members:
                granted.add(pattern)
            continue
        if pattern == "*":
            granted = members
            break
        granted |= {
            a for a in _candidates(pattern, universe)
            if a not in granted and effective.action_matches(pattern, a)
        }

    return _subtract(granted, excludes) if excludes and granted else granted


def _subtract(granted: set[str], excludes: tuple[str, ...]) -> set[str]:
    """Apply a role's own notActions. A subtraction from ITS OWN grant, never a deny."""
    out = set(granted)
    for raw in excludes:
        pattern = str(raw).strip().lower()
        if not pattern:
            continue
        if "*" not in pattern:
            out.discard(pattern)
        else:
            out -= {a for a in out if effective.action_matches(pattern, a)}
    return out


def covers(role: effective.RoleActionSet, action: str, *, plane: str = effective.PLANE_CONTROL) -> bool:
    granted, excluded = role.grants(action, plane)
    return bool(granted) and not excluded


def breadth(role: effective.RoleActionSet | None, universe: tuple[str, ...]) -> int:
    """How much of the observable universe a role grants — the denominator for the ratio."""
    if not role:
        return 0
    control = len(granted_actions(role, universe))
    data = len(granted_actions(role, universe, plane=effective.PLANE_DATA))
    return control + data

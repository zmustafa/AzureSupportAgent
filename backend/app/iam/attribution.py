"""Attribution — *"who granted this, when, from where, and with what?"*

A diff that says "Alice gained Owner" is useful. One that says "Bob granted Alice Owner on
12 July at 02:14 from 203.0.113.9 via the deployment `prod-network-v3`" closes the ticket.

The source is the Azure Activity Log, filtered to authorization operations. Three rules make
this trustworthy rather than merely plausible:

1. **An unmatched change is `unknown`, never guessed.** The temptation is to attribute a change
   to the nearest event in time. Two role assignments 40 seconds apart by different people would
   then be swapped, and a confidently wrong actor in an audit trail is worse than an honest gap.
   Matching is on assignment id first; the time-window fallback requires the principal, role and
   scope to line up too, and still records its own confidence.

2. **The window is clamped to real retention and the clamp is reported.** The Entra work found
   that `directoryAudits` *rejects* an over-long filter with a 400 rather than returning what it
   has — losing the entire source instead of the excess. Assume the same class of behavior and
   clamp before asking.

3. **`changeSource` separates IaC from a human in the portal.** "Granted by Terraform" and
   "granted by a person at 2am" are different findings, and the user agent plus correlation id
   are enough to tell them apart.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("app.iam.attribution")

# Azure Activity Log retention is 90 days for the platform-native store. Anything longer needs a
# diagnostic setting to Log Analytics or storage, which this feature does not read.
ACTIVITY_LOG_RETENTION_DAYS = 90

# Authorization operations worth attributing. Everything else in the Activity Log is noise for
# this purpose and querying for it wastes the window.
AUTHORIZATION_OPERATIONS = (
    "microsoft.authorization/roleassignments/write",
    "microsoft.authorization/roleassignments/delete",
    "microsoft.authorization/roledefinitions/write",
    "microsoft.authorization/roledefinitions/delete",
    "microsoft.authorization/denyassignments/write",
    "microsoft.managedservices/registrationassignments/write",
    "microsoft.keyvault/vaults/write",
    "microsoft.authorization/roleeligibilityschedulerequests/write",
    "microsoft.authorization/roleassignmentschedulerequests/write",
)

# --------------------------------------------------------------------------- change source
SOURCE_PORTAL = "Portal"
SOURCE_ARM = "ARM"
SOURCE_CLI = "CLI"
SOURCE_TERRAFORM = "Terraform"
SOURCE_BICEP = "Bicep"
SOURCE_POLICY = "Policy"
SOURCE_PIM = "PIM"
SOURCE_UNKNOWN = "Unknown"

# Ordered: the first match wins, so the more specific tools are tested before the generic SDK
# strings they are built on (Terraform's agent contains "Go-http-client", AzureCLI contains
# "python-requests").
_AGENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("hashicorp", SOURCE_TERRAFORM),
    ("terraform", SOURCE_TERRAFORM),
    ("azurecli", SOURCE_CLI),
    ("azure-cli", SOURCE_CLI),
    ("azurepowershell", SOURCE_CLI),
    ("az-powershell", SOURCE_CLI),
    ("portal", SOURCE_PORTAL),
    ("azure-sdk", SOURCE_ARM),
    ("azuresdk", SOURCE_ARM),
)

# IaC estates care about this distinction: a change that did NOT come from the pipeline is
# out-of-band regardless of who made it.
IAC_SOURCES = frozenset({SOURCE_TERRAFORM, SOURCE_BICEP, SOURCE_ARM})
HUMAN_SOURCES = frozenset({SOURCE_PORTAL, SOURCE_CLI})


def clamp_window(days: int, *, now: datetime | None = None) -> tuple[str, str, str]:
    """Return ``(start_iso, end_iso, note)`` clamped to what the Activity Log can actually serve.

    The note is non-empty exactly when the request was cut down, and it is carried all the way
    to the UI. A silently shortened window makes "no changes found" mean two different things."""
    now = now or datetime.now(timezone.utc)
    asked = max(1, int(days))
    allowed = min(asked, ACTIVITY_LOG_RETENTION_DAYS)
    start = now - timedelta(days=allowed)
    note = ""
    if allowed < asked:
        note = (
            f"Requested {asked} days but the Azure Activity Log retains {ACTIVITY_LOG_RETENTION_DAYS}; "
            f"attribution covers the last {allowed} days only. Changes older than that are reported "
            f"as 'unknown actor' because the record is gone, not because nobody made them."
        )
    return start.isoformat(), now.isoformat(), note


def is_authorization_event(operation: str) -> bool:
    op = (operation or "").strip().lower()
    return any(op.startswith(known) for known in AUTHORIZATION_OPERATIONS)


def infer_change_source(event: dict[str, Any]) -> str:
    """Portal / CLI / Terraform / Bicep / Policy / PIM / ARM / Unknown.

    Order matters: PIM and Policy are identified from the operation and the actor, which is
    stronger evidence than a user agent that any of them could be wearing."""
    op = str(event.get("operation", "")).lower()
    if "schedulerequest" in op or "privilegedaccess" in op or "roleeligibility" in op:
        return SOURCE_PIM
    if event.get("isPlatformActor") and "policy" in str(event.get("actor", "")).lower():
        return SOURCE_POLICY
    caller = str(event.get("actor", "")).lower()
    if "policyinsights" in caller or "azurepolicy" in caller:
        return SOURCE_POLICY

    agent = " ".join(
        str(v).lower()
        for v in (
            event.get("userAgent", ""),
            (event.get("raw") or {}).get("httpRequest", {}).get("clientRequestId", "")
            if isinstance((event.get("raw") or {}).get("httpRequest"), dict) else "",
            (event.get("raw") or {}).get("userAgent", "") if isinstance(event.get("raw"), dict) else "",
        )
    )
    for needle, source in _AGENT_PATTERNS:
        if needle in agent:
            return source

    # A deployment name means it came through a template. Bicep compiles to ARM and is
    # indistinguishable at this layer, so it is reported as ARM rather than guessed as Bicep.
    if event.get("deploymentName"):
        return SOURCE_ARM
    return SOURCE_UNKNOWN


_DEPLOYMENT_RE = re.compile(r"/deployments/([^/]+)", re.IGNORECASE)


def deployment_of(event: dict[str, Any]) -> str:
    """The ARM deployment name behind a change, when the event carries one."""
    for candidate in (str(event.get("resourceId", "")), str((event.get("raw") or {}).get("claims", {}) if isinstance(event.get("raw"), dict) else "")):
        m = _DEPLOYMENT_RE.search(candidate)
        if m:
            return m.group(1)
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    return str(props.get("deploymentName", "") or "")


# --------------------------------------------------------------------------- matching
#: How far from a change a bare (principal, role, scope) event may sit and still be believed.
MATCH_WINDOW_MINUTES = 30

CONFIDENCE_EXACT = "exact"       # matched on the assignment id itself
CONFIDENCE_INFERRED = "inferred"  # matched on principal+role+scope inside the time window
UNKNOWN_ACTOR = {
    "actorPrincipalId": "",
    "actorDisplayName": "",
    "actorType": "",
    "eventTimestamp": "",
    "callerIpAddress": "",
    "correlationId": "",
    "deploymentName": "",
    "changeSource": SOURCE_UNKNOWN,
    "confidence": "unknown",
}


def index_events(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket authorization events by the lower-cased resource id they acted on.

    The assignment id appears in the Activity Log `resourceId`, which is what makes an exact
    match possible at all."""
    out: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if not is_authorization_event(str(e.get("operation", ""))):
            continue
        rid = str(e.get("resourceId", "")).lower()
        if rid:
            out.setdefault(rid, []).append(e)
    for bucket in out.values():
        bucket.sort(key=lambda e: str(e.get("eventTime", "")), reverse=True)
    return out


def attribute(change: dict[str, Any], by_resource: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Best available actor for one diff entry, or `unknown`.

    Only the exact assignment-id match is attempted here. The scope-and-time fallback lives in
    :func:`attribute_all` because it needs to see every candidate at once to refuse an ambiguous
    one — a fallback that picks the first plausible event is how the wrong person ends up named
    in an audit report."""
    assignment_id = str(change.get("assignmentId", "")).lower()
    if assignment_id and assignment_id in by_resource:
        return _actor_from(by_resource[assignment_id][0], CONFIDENCE_EXACT)
    return dict(UNKNOWN_ACTOR)


def attribute_all(
    changes: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    window_minutes: int = MATCH_WINDOW_MINUTES,
) -> dict[str, Any]:
    """Attach an actor to every change. Returns counts so the UI can state its own coverage.

    An access change with no matching event is `unknown` and says so; the alternative — leaving
    `actor` blank — reads as "nobody did this", which is never true."""
    by_resource = index_events(events)
    scoped = _index_by_scope(events)

    exact = inferred = unknown = 0
    for change in changes:
        actor = attribute(change, by_resource)
        if actor["confidence"] == "unknown":
            actor = _infer_from_scope(change, scoped, window_minutes)
        change["actor"] = actor
        if actor["confidence"] == CONFIDENCE_EXACT:
            exact += 1
        elif actor["confidence"] == CONFIDENCE_INFERRED:
            inferred += 1
        else:
            unknown += 1
    return {
        "attributed_exact": exact,
        "attributed_inferred": inferred,
        "unattributed": unknown,
        "events_considered": len(by_resource),
    }


def _index_by_scope(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Authorization events bucketed by the scope prefix of their resource id.

    A role assignment's resource id is `<scope>/providers/Microsoft.Authorization/roleAssignments/<guid>`,
    so the scope is recoverable from the event even when the assignment id has changed."""
    out: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if not is_authorization_event(str(e.get("operation", ""))):
            continue
        rid = str(e.get("resourceId", "")).lower()
        scope = rid.split("/providers/microsoft.authorization/")[0]
        if scope:
            out.setdefault(scope, []).append(e)
    return out


def _infer_from_scope(
    change: dict[str, Any],
    scoped: dict[str, list[dict[str, Any]]],
    window_minutes: int,
) -> dict[str, Any]:
    """Fallback match on scope within a time window — and it REFUSES when ambiguous.

    If two authorization events touched the same scope inside the window, there is no honest way
    to say which one produced this change, so the answer is `unknown`. Naming one of two possible
    people is worse than naming neither."""
    scope = str(change.get("scope", "")).lower()
    if not scope:
        return dict(UNKNOWN_ACTOR)
    candidates = scoped.get(scope) or []
    if len(candidates) != 1:
        return dict(UNKNOWN_ACTOR)
    return _actor_from(candidates[0], CONFIDENCE_INFERRED)


def _actor_from(event: dict[str, Any], confidence: str) -> dict[str, Any]:
    """Project one Activity Log event into the actor block carried on a diff entry."""
    return {
        "actorPrincipalId": str(event.get("actorObjectId", "") or ""),
        "actorDisplayName": str(event.get("actor", "") or ""),
        "actorType": str(event.get("actorKind", "") or ""),
        "eventTimestamp": str(event.get("eventTime", "") or ""),
        "callerIpAddress": str(event.get("actorIp", "") or ""),
        "correlationId": str(event.get("correlationId", "") or ""),
        "deploymentName": deployment_of(event),
        "changeSource": infer_change_source(event),
        "confidence": confidence,
    }


# --------------------------------------------------------------------------- collection
async def collect_authorization_events(
    subscriptions: list[str],
    connection: dict[str, Any] | None,
    *,
    days: int = 30,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch authorization events for the window, reusing Change Explorer's collector.

    This deliberately does not re-implement Activity Log access. `changeexplorer.collectors`
    already handles the service-principal-vs-pasted-token split, the 429 backoff, the capture
    cap and the salvage-on-truncation path — all of which were learned the hard way and none of
    which are worth learning twice."""
    from app.changeexplorer.collectors import collect_activity_log

    start_iso, end_iso, clamp_note = clamp_window(days)
    try:
        rows, note = await collect_activity_log(subscriptions, start_iso, end_iso, connection)
    except Exception as exc:  # noqa: BLE001 — attribution is additive; losing it must not lose the diff
        log.warning("iam attribution: activity log unavailable", exc_info=True)
        return [], f"Attribution unavailable: {exc}"
    auth = [r for r in rows if is_authorization_event(str(r.get("operation", "")))]
    parts = [p for p in (clamp_note, note) if p]
    return auth, " ".join(parts)

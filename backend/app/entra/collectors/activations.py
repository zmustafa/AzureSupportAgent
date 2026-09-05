"""Privileged activation sessions — who turned on which role, when, and under what terms.

An *activation session* is one elevation: a principal taking a role they are eligible for,
for a bounded window. This collector reconciles four sources into that single shape, two
per plane, because on most tenants only some of them are readable:

  Entra ID
    ``/roleManagement/directory/roleAssignmentScheduleRequests``
        The rich record — justification, ticket, requestor, approval. Needs
        ``RoleAssignmentSchedule.Read.Directory``, which is separate from the
        ``RoleManagement.Read.Directory`` most tenants grant, so it 403s far more often
        than people expect.
    ``/roleManagement/directory/roleAssignmentScheduleInstances``
        Readable with the ordinary role-management scope. Rows with
        ``assignmentType == "Activated"`` ARE activations — no justification or ticket,
        but the principal, role and window are all there. This is what keeps the feature
        working on a tenant that has not granted the scope above.

  Azure resources
    ``/providers/Microsoft.Authorization/roleAssignmentScheduleRequests`` per subscription
        The Azure equivalent of the rich record, and it needs no Graph scope at all — only
        Azure RBAC on the subscription. On the tenant this was built against it read fine
        while the Entra one was still forbidden.
    ``…/roleAssignmentScheduleInstances`` — same fallback role as its Entra counterpart.

Nothing here reads what the principal DID with the elevation. That is deliberate: the
Activity Log is per-subscription and slow enough that folding 26 subscriptions into every
refresh would add tens of minutes. Actions are fetched per session, on demand, by
``activation_actions.py``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.entra import model
from app.entra.collectors import CollectContext, as_dict, clip, guarded
from app.entra.collectors.pim import parse_duration_hours
from app.entra.collectors.roles import tier_of
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

DOMAIN = "activations"

log = logging.getLogger("app.entra.collectors.activations")

RM = "/roleManagement/directory"

# The one scope that turns an activation list into an activation RECORD (justification,
# ticket, requestor). Named once so the note, the blocker and the pending-propagation branch
# cannot drift apart.
DETAIL_SCOPE = "RoleAssignmentSchedule.Read.Directory"

# Microsoft's retention for directory audits. Justification older than this is gone from the
# source, which is the whole reason the ledger persists what it has already seen.
AUDIT_RETENTION_DAYS = 30

# Audit `category` for Entra DIRECTORY role activations. The PIM service also logs
# ResourceManagement (Azure roles, read from ARM instead) and GroupManagement (PIM for
# Groups) under the same loggedByService.
_AUDIT_DIRECTORY_CATEGORY = "RoleManagement"

# Azure PIM lives at the subscription scope and answers on this api-version; verified live.
AZ_PIM_API = "2020-10-01"

# Subscriptions queried concurrently for Azure PIM. These are small, cheap reads (unlike the
# Activity Log), but a tenant can easily have dozens, so keep a lid on it.
AZ_CONCURRENCY = 8

# Ceilings. Both are generous; both are reported when they bite, because a silently trimmed
# activation history is indistinguishable from a quiet tenant.
MAX_REQUESTS = 20_000
MAX_INSTANCES = 20_000

_ACTIVATED = "activated"

# Statuses that mean privilege was actually issued. Everything else — Failed, Denied,
# PendingApproval, Canceled — is an ATTEMPT: worth seeing (a run of failures is a probing
# signal), but it granted nothing, so it must not be counted as time spent elevated or
# searched for actions.
_GRANTED_STATUSES = {"provisioned", "granted", "accepted", "succeeded", "active"}


def is_granted(status: str) -> bool:
    text = (status or "").strip().lower()
    if not text:
        return True          # sources that report no status only ever return real grants
    return text in _GRANTED_STATUSES


def _hours(start: str, end: str) -> float | None:
    """Length of an activation window in hours, or None if either end is unusable."""
    a, b = parse_time(start), parse_time(end)
    if not a or not b or b < a:
        return None
    return round((b - a).total_seconds() / 3600.0, 2)


def parse_time(value: str) -> datetime | None:
    """Parse the several ISO-8601 spellings Graph and ARM mix within one payload."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    # ARM sometimes emits more than six fractional digits, which fromisoformat rejects.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        rest = tail[len(digits):] if not digits else tail.lstrip("0123456789")
        text = f"{head}.{digits or '0'}{rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope_kind(scope: str) -> str:
    """Classify an ARM scope string. Breadth is what makes an Azure activation dangerous."""
    s = (scope or "").strip("/").lower()
    if not s:
        return "unknown"
    if s.startswith("providers/microsoft.management/managementgroups"):
        return "managementGroup"
    parts = s.split("/")
    if parts[0] == "subscriptions":
        if len(parts) <= 2:
            return "subscription"
        if len(parts) <= 4:
            return "resourceGroup"
        return "resource"
    return "unknown"


def _scope_label(scope: str) -> str:
    return (scope or "").rstrip("/").rsplit("/", 1)[-1] or "/"


def session(
    *,
    sid: str,
    plane: str,
    source: str,
    principal_id: str,
    role_id: str,
    role_name: str = "",
    principal_name: str = "",
    principal_upn: str = "",
    principal_type: str = "",
    scope_type: str = "directory",
    scope_id: str = "",
    scope_name: str = "",
    subscription_id: str = "",
    action: str = "",
    status: str = "",
    requested_at: str = "",
    start: str = "",
    end: str = "",
    justification: str = "",
    ticket_number: str = "",
    ticket_system: str = "",
    requestor_id: str = "",
    detail_known: bool = True,
) -> dict[str, Any]:
    """One activation, in the single shape the API, signals and ledger all consume."""
    return {
        "id": sid,
        "plane": plane,
        "source": source,
        "principal_id": principal_id,
        "principal_name": principal_name,
        "principal_upn": principal_upn,
        "principal_type": principal_type,
        "role_id": role_id,
        "role_name": role_name,
        "tier": tier_of(role_name) if role_name else "tier2",
        "scope_type": scope_type,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "subscription_id": subscription_id,
        "action": action,
        "status": status,
        "requested_at": requested_at,
        "start": start,
        "end": end,
        "granted_hours": _hours(start, end),
        "justification": justification,
        "ticket_number": ticket_number,
        "ticket_system": ticket_system,
        "requestor_id": requestor_id,
        # A principal activating for itself is normal. A DIFFERENT requestor means someone
        # granted the elevation, which is a separation-of-duties event worth seeing.
        "self_service": bool(requestor_id) and requestor_id == principal_id,
        # Did privilege actually get issued? A failed or pending request is an attempt.
        "granted": is_granted(status),
        # False when the source cannot carry justification/ticket at all, so the UI can say
        # "not recorded by this source" instead of implying the operator omitted it.
        "detail_known": detail_known,
    }


def _is_activation_action(action: str, request_type: str) -> bool:
    text = f"{action} {request_type}".lower()
    return "activat" in text or "extend" in text or "renew" in text


async def _entra_audits(client: GraphClient, ctx: CollectContext,
                        since: str) -> tuple[list[dict[str, Any]], bool, str]:
    """Entra activation detail from the PIM audit log. Returns (sessions, available, note).

    This is the ONLY read-only source of activation justification for an app-only
    connection. ``roleAssignmentScheduleRequests`` looks like the obvious answer, but Graph
    answers it with:

        missing permission scope RoleAssignmentSchedule.ReadWrite.Directory,
        RoleManagement.ReadWrite.Directory, RoleAssignmentSchedule.Remove.Directory

    Every one of those is a WRITE scope. There is no read-only scope that opens it to an
    app-only token, and this product does not ask for write access to a directory in order
    to read it.

    The audit log carries strictly more than the requests API would have: justification,
    whether MFA was satisfied, and whether approval was required. It is bounded by the
    30-day directory-audit retention, which is exactly what the durable ledger exists to
    outlive.
    """
    # directoryAudits refuses a filter older than its retention outright — 400 "Minimum
    # allowed time for activityDateTime is ..." — rather than returning an empty page. The
    # activation lookback is 90 days by default, so asking for the same window here loses
    # the whole source. Clamp to retention and let the ledger hold anything older.
    floor = _iso(datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS - 1))
    audit_since = max(since, floor)
    try:
        rows, _ = await client.get_all(
            "/auditLogs/directoryAudits",
            filter=f"loggedByService eq 'PIM' and activityDateTime ge {audit_since}",
            max_items=MAX_REQUESTS,
        )
    except GraphPermissionError:
        return [], False, ("Activation justification unavailable: grant AuditLog.Read.All to "
                           "read the PIM audit log.")
    except GraphError as exc:
        return [], False, f"PIM audit log: {clip(exc, 140)}"

    out: list[dict[str, Any]] = []
    for raw in rows:
        row = as_dict(raw)
        # `loggedByService eq 'PIM'` spans BOTH planes plus PIM for Groups. `category` is the
        # discriminator: RoleManagement = Entra directory roles, ResourceManagement = Azure
        # resource roles (already read from ARM, with better scope data), GroupManagement =
        # PIM for Groups. Taking them all as directory activations both duplicated the Azure
        # half and left those rows with an unresolvable role id, which grades every one of
        # them tier-2 and silences the tier-0 signals.
        if str(row.get("category") or "") != _AUDIT_DIRECTORY_CATEGORY:
            continue
        detail = {str(d.get("key") or ""): str(d.get("value") or "")
                  for d in (row.get("additionalDetails") or []) if isinstance(d, dict)}
        # ActivateRole is the completed grant. The matching "requested" row carries the same
        # justification, so counting both would double every activation.
        if detail.get("AuditType") != "ActivateRole":
            continue
        target = _audit_principal(row)
        role_id = detail.get("RoleDefinitionOriginId") or detail.get("TemplateId") or ""
        request_id = detail.get("RoleAssignmentRequestId") or str(row.get("id") or "")
        out.append(session(
            sid=f"entra:audit:{request_id}",
            plane="entra", source="entra_audit",
            principal_id=target.get("id", ""),
            principal_name=target.get("name", ""),
            principal_upn=target.get("upn", ""),
            principal_type=target.get("type", ""),
            role_id=role_id,
            scope_type="directory",
            scope_id=detail.get("ResourceOriginId") or "/",
            action="activated",
            status=str(row.get("result") or ""),
            requested_at=str(row.get("activityDateTime") or ""),
            start=detail.get("StartTime") or str(row.get("activityDateTime") or ""),
            end=detail.get("ExpirationTime") or "",
            justification=detail.get("Justification") or "",
            # The audit log records the actor, and a PIM self-activation is initiated by the
            # same principal it targets. Anything else is someone elevating another account.
            requestor_id=_audit_actor(row) or target.get("id", ""),
            detail_known=True,
        ))
    note = ""
    if out:
        note = (f"Activation justification is read from the PIM audit log, which Microsoft "
                f"retains for {AUDIT_RETENTION_DAYS} days. Older activations keep their "
                "window but not their reason; the ledger preserves what was already seen.")
    return out, True, note


def _audit_principal(row: dict[str, Any]) -> dict[str, str]:
    """The account whose privilege was raised (the audit target)."""
    for raw in (row.get("targetResources") or []):
        target = as_dict(raw)
        if str(target.get("type") or "").lower() in {"user", "serviceprincipal", "group"}:
            return {
                "id": str(target.get("id") or ""),
                "name": str(target.get("displayName") or ""),
                "upn": str(target.get("userPrincipalName") or ""),
                "type": str(target.get("type") or ""),
            }
    return {}


def _audit_actor(row: dict[str, Any]) -> str:
    initiated = as_dict(row.get("initiatedBy"))
    user = as_dict(initiated.get("user"))
    app = as_dict(initiated.get("app"))
    return str(user.get("id") or app.get("servicePrincipalId") or "")


async def _entra_requests(client: GraphClient, ctx: CollectContext,
                          since: str) -> tuple[list[dict[str, Any]], bool, str, bool]:
    """The rich Entra record. Returns (sessions, available, note, needs_consent)."""
    try:
        rows, truncated = await client.get_all(
            f"{RM}/roleAssignmentScheduleRequests",
            filter=f"createdDateTime ge {since}",
            max_items=MAX_REQUESTS,
        )
    except GraphPermissionError:
        # Deliberately NOT echoing exc.message: Graph answers this one with a JSON body, and
        # clipping it dropped a truncated `{"errorCode":"PermissionScopeNotGranted"...` into
        # the coverage banner.
        #
        # The untruncated message names the scopes that WOULD open it:
        #   RoleAssignmentSchedule.ReadWrite.Directory, RoleManagement.ReadWrite.Directory,
        #   RoleAssignmentSchedule.Remove.Directory
        # All three are WRITE scopes. There is no read-only scope for an app-only token, so
        # this collection is unreachable by a read-only product and is NOT worth asking the
        # operator to grant. `_entra_audits` gets the same facts from the audit log instead.
        return [], False, "", False
    except GraphError as exc:
        return [], False, f"Entra activation requests: {clip(exc, 140)}", False

    out: list[dict[str, Any]] = []
    for raw in rows:
        row = as_dict(raw)
        action = str(row.get("action") or "")
        if not _is_activation_action(action, ""):
            continue                       # adminAssign/adminRemove are not elevations
        schedule = as_dict(row.get("scheduleInfo"))
        expiration = as_dict(schedule.get("expiration"))
        ticket = as_dict(row.get("ticketInfo"))
        out.append(session(
            sid=f"entra:req:{row.get('id') or ''}",
            plane="entra", source="entra_request",
            principal_id=str(row.get("principalId") or ""),
            role_id=str(row.get("roleDefinitionId") or ""),
            scope_type="directory",
            scope_id=str(row.get("directoryScopeId") or "/"),
            action=action,
            status=str(row.get("status") or ""),
            requested_at=str(row.get("createdDateTime") or ""),
            start=str(schedule.get("startDateTime") or ""),
            end=str(expiration.get("endDateTime") or ""),
            justification=str(row.get("justification") or ""),
            ticket_number=str(ticket.get("ticketNumber") or ""),
            ticket_system=str(ticket.get("ticketSystem") or ""),
            requestor_id=str(row.get("createdBy", {}).get("user", {}).get("id") or "")
            if isinstance(row.get("createdBy"), dict) else "",
        ))
    note = (f"Entra activation history capped at {MAX_REQUESTS:,} requests." if truncated else "")
    return out, True, note


async def _entra_instances(client: GraphClient, ctx: CollectContext,
                           ) -> tuple[list[dict[str, Any]], bool, str]:
    """Activations visible without the request scope.

    ``assignmentType == "Activated"`` on a schedule instance means the principal elevated
    into the role. There is no justification here — the source does not carry one — but the
    who, the role and the window are exact.
    """
    try:
        rows, truncated = await client.get_all(
            f"{RM}/roleAssignmentScheduleInstances", max_items=MAX_INSTANCES)
    except GraphPermissionError:
        # Same service, same JSON-body answer — name the scope ourselves. Unlike the
        # requests collection, this one IS reachable with an ordinary read scope.
        return [], False, ("Entra activations not readable: grant "
                           "RoleManagement.Read.Directory.")
    except GraphError as exc:
        return [], False, f"Entra activation instances: {clip(exc, 140)}"

    out: list[dict[str, Any]] = []
    for raw in rows:
        row = as_dict(raw)
        if str(row.get("assignmentType") or "").lower() != _ACTIVATED:
            continue
        out.append(session(
            sid=f"entra:inst:{row.get('id') or ''}",
            plane="entra", source="entra_instance",
            principal_id=str(row.get("principalId") or ""),
            role_id=str(row.get("roleDefinitionId") or ""),
            scope_type="directory",
            scope_id=str(row.get("directoryScopeId") or "/"),
            action="activated",
            status="Provisioned",
            start=str(row.get("startDateTime") or ""),
            end=str(row.get("endDateTime") or ""),
            requested_at=str(row.get("startDateTime") or ""),
            detail_known=False,
        ))
    note = (f"Entra activation instances capped at {MAX_INSTANCES:,}." if truncated else "")
    return out, True, note

def _az_expanded(props: dict[str, Any]) -> tuple[str, str, str, str]:
    """(principal_name, principal_type, role_name, scope_name) from ARM expandedProperties.

    ARM returns these inline, which saves a directory lookup per row — worth using, but it
    is optional in the schema so every field is treated as absent-by-default.
    """
    exp = as_dict(props.get("expandedProperties"))
    principal = as_dict(exp.get("principal"))
    role = as_dict(exp.get("roleDefinition"))
    scope = as_dict(exp.get("scope"))
    return (
        str(principal.get("displayName") or ""),
        str(principal.get("type") or ""),
        str(role.get("displayName") or ""),
        str(scope.get("displayName") or ""),
    )


async def _azure_requests(connection: dict[str, Any] | None, ctx: CollectContext,
                          since: str) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Azure PIM activations across every subscription the connection can see."""
    notes: list[str] = []
    caps = {"available": False, "subscriptions": 0, "reason": ""}
    if connection is None:
        caps["reason"] = "No Azure connection is attached to this Entra connection."
        return [], caps, notes

    from app.azure.arm import list_subscriptions
    from app.azure.credentials import get_arm_token

    token, terr = await get_arm_token(connection)
    if not token:
        # Graph app permissions say nothing about Azure RBAC — a tenant can be fully
        # readable in Entra and completely closed in Azure. Say which one is missing.
        caps["reason"] = (
            "Azure activations need an Azure Resource Manager token for this connection "
            f"({clip(terr or 'no token', 100)}). Graph permissions do not grant it.")
        notes.append(caps["reason"])
        return [], caps, notes

    subs, serr = await list_subscriptions(token)
    # arm.list_subscriptions projects the subscription GUID onto "id", not "subscriptionId".
    # Reading the raw ARM field name here silently produced an empty list on a tenant with
    # 26 readable subscriptions, and reported it as "no subscriptions are visible" — a
    # permissions story for what was actually a key-name bug.
    sub_ids = [str(s.get("id") or "") for s in subs if s.get("id")]
    sub_names = {str(s.get("id") or ""): str(s.get("name") or "") for s in subs}
    caps["subscriptions"] = len(sub_ids)
    if not sub_ids:
        caps["reason"] = (f"No subscriptions are visible to this connection"
                          f"{': ' + clip(serr, 90) if serr else '.'}")
        notes.append(caps["reason"])
        return [], caps, notes

    import httpx

    await ctx.say("info", f"Activations: reading Azure PIM across {len(sub_ids)} subscription(s)…")
    sem = asyncio.Semaphore(AZ_CONCURRENCY)
    # (subscription id, why) — the id is kept whole so the note can name the subscription.
    # "1 of 26 subscription(s)" told the reader a number they could do nothing with.
    failures: list[tuple[str, str]] = []

    async def _one(sub: str) -> list[dict[str, Any]]:
        url = (f"https://management.azure.com/subscriptions/{sub}"
               f"/providers/Microsoft.Authorization/roleAssignmentScheduleRequests")
        try:
            async with sem:
                async with httpx.AsyncClient(timeout=60) as http:
                    resp = await http.get(url, headers={"Authorization": f"Bearer {token}"},
                                          params={"api-version": AZ_PIM_API})
        except httpx.HTTPError as exc:
            failures.append((sub, type(exc).__name__))
            return []
        if resp.status_code != 200:
            # 403 here is ordinary: the connection may read some subscriptions and not others.
            failures.append((sub, f"HTTP {resp.status_code}"))
            return []
        rows: list[dict[str, Any]] = []
        for raw in (resp.json().get("value") or []):
            item = as_dict(raw)
            props = as_dict(item.get("properties"))
            request_type = str(props.get("requestType") or "")
            if not _is_activation_action("", request_type):
                continue
            created = str(props.get("createdOn") or "")
            if created and created < since:
                continue
            schedule = as_dict(props.get("scheduleInfo"))
            expiration = as_dict(schedule.get("expiration"))
            ticket = as_dict(props.get("ticketInfo"))
            pname, ptype, rname, sname = _az_expanded(props)
            scope = str(props.get("scope") or "")
            started = str(schedule.get("startDateTime") or created)
            # Azure PIM states the window as a DURATION ("PT8H") and usually omits
            # endDateTime entirely, where Entra gives an explicit end. Without deriving it
            # every Azure session showed a blank length, which is also what the
            # long-window signal reads.
            ends = str(expiration.get("endDateTime") or "")
            if not ends:
                hours = parse_duration_hours(str(expiration.get("duration") or ""))
                begin = parse_time(started)
                if hours is not None and begin is not None:
                    ends = _iso(begin + timedelta(hours=hours))
            rows.append(session(
                sid=f"azure:req:{item.get('name') or item.get('id') or ''}",
                plane="azure", source="azure_request",
                principal_id=str(props.get("principalId") or ""),
                principal_name=pname,
                principal_type=ptype or str(props.get("principalType") or ""),
                role_id=str(props.get("roleDefinitionId") or "").rsplit("/", 1)[-1],
                role_name=rname,
                scope_type=_scope_kind(scope),
                scope_id=scope,
                scope_name=sname or sub_names.get(sub) or _scope_label(scope),
                subscription_id=sub,
                action=request_type,
                status=str(props.get("status") or ""),
                requested_at=created,
                start=started,
                end=ends,
                justification=str(props.get("justification") or ""),
                ticket_number=str(ticket.get("ticketNumber") or ""),
                ticket_system=str(ticket.get("ticketSystem") or ""),
                requestor_id=str(props.get("requestorId") or ""),
            ))
        return rows

    gathered = await asyncio.gather(*(_one(s) for s in sub_ids))
    out = [row for chunk in gathered for row in chunk]
    caps["available"] = len(failures) < len(sub_ids)
    if failures:
        caps["failed_subscriptions"] = [
            {"id": sub, "name": sub_names.get(sub) or "", "reason": why} for sub, why in failures]
        notes.append(
            f"Azure PIM unreadable in {_name_subscriptions(failures, sub_names)} \u2014 those "
            "activations are missing from this view.")
    return out, caps, notes


# How many subscriptions to name before summarising. Enough to act on, short enough that a
# connection which can read none of 200 does not produce a paragraph.
_MAX_NAMED_SUBSCRIPTIONS = 3

# The Azure built-in roles that can actually read PIM role-management data. Reader cannot:
# it omits Microsoft.Authorization/roleManagement*/read, which is the single most common
# reason this fails on a subscription the operator believes is fully readable.
AZURE_PIM_ROLES = "User Access Administrator, Role Based Access Control Administrator or Owner"


def _name_subscriptions(failures: list[tuple[str, str]], names: dict[str, str]) -> str:
    """Name the subscriptions that failed, not just how many.

    A count alone ("1 of 26") is unactionable: the reader cannot tell which subscription to
    go and fix, and on a large tenant will not find it by hand.
    """
    labelled = [f"{names.get(sub) or sub} ({why})" for sub, why in failures]
    shown = labelled[:_MAX_NAMED_SUBSCRIPTIONS]
    rest = len(labelled) - len(shown)
    text = ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")
    return f"{len(failures)} subscription(s): {text}"


def _dedupe(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer the richest record when two sources describe the same elevation.

    A request, an audit entry and an instance for the same principal+role+window are one
    activation seen three times. Whichever carries justification wins; without this the UI
    would show every Entra activation once per readable source.
    """
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in sessions:
        key = (row["plane"], row["principal_id"] + "|" + row["role_id"],
               (row.get("start") or "")[:16])
        current = best.get(key)
        if current is None or (not current["detail_known"] and row["detail_known"]):
            best[key] = row
    ordered = sorted(best.values(), key=lambda r: r.get("start") or "", reverse=True)
    return _merge_overlapping(ordered)


# Two records of the same elevation can disagree on the start by seconds: the audit log
# stamps when PIM completed the grant, the schedule instance when the window opened.
_MERGE_MINUTES = 10


def _merge_overlapping(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse records of the SAME elevation that a minute-precision key missed.

    One principal cannot hold two overlapping activations of one role — so when two rows
    for the same principal and role start within minutes of each other, they are the same
    event described by different sources. Left unmerged the activation appears twice, once
    with its justification and once without, which reads as two elevations.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in sessions:
        groups.setdefault(
            (row["plane"], row["principal_id"], row["role_id"]), []).append(row)

    out: list[dict[str, Any]] = []
    for rows in groups.values():
        rows.sort(key=lambda r: r.get("start") or "")
        current: dict[str, Any] | None = None
        for row in rows:
            if current is None:
                current = row
                continue
            if _within(current.get("start"), row.get("start"), _MERGE_MINUTES):
                current = _richer(current, row)
            else:
                out.append(current)
                current = row
        if current is not None:
            out.append(current)
    return sorted(out, key=lambda r: r.get("start") or "", reverse=True)


def _within(a: str | None, b: str | None, minutes: int) -> bool:
    first, second = parse_time(a or ""), parse_time(b or "")
    if first is None or second is None:
        return False
    return abs((second - first).total_seconds()) <= minutes * 60


def _richer(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Keep the record that knows more, then backfill anything it left blank.

    Neither source is a superset: the audit log has the justification, the schedule instance
    has the authoritative window. Taking one wholesale would throw away the other's facts.
    """
    winner, loser = (a, b) if a.get("detail_known") and not b.get("detail_known") else (
        (b, a) if b.get("detail_known") and not a.get("detail_known") else (a, b))
    merged = dict(winner)
    for field, value in loser.items():
        if not merged.get(field) and value:
            merged[field] = value
    merged["detail_known"] = bool(a.get("detail_known") or b.get("detail_known"))
    merged["granted_hours"] = _hours(merged.get("start", ""), merged.get("end", ""))
    return merged


async def _role_names(client: GraphClient) -> dict[str, str]:
    """Directory role id -> display name.

    The schedule endpoints return only a roleDefinitionId. Without this every Entra session
    is stored with an empty role name, which also means ``tier_of("")`` grades Global
    Administrator as tier-2 and the tier-0 signals never fire. Resolving it here rather than
    reading the roles collector's output keeps collectors independent, and it is one cheap
    call against an endpoint this connection already reads.
    """
    try:
        rows, _ = await client.get_all(
            f"{RM}/roleDefinitions", select=["id", "displayName"], top=999)
    except GraphError:
        return {}
    return {str(as_dict(r).get("id") or ""): str(as_dict(r).get("displayName") or "")
            for r in rows}


def _apply_role_names(sessions: list[dict[str, Any]], names: dict[str, str]) -> None:
    for row in sessions:
        if row.get("role_name") or not names:
            continue
        name = names.get(str(row.get("role_id") or ""), "")
        if name:
            row["role_name"] = name
            row["tier"] = tier_of(name)


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        blockers: list[dict[str, Any]] = []
        since_dt = datetime.now(timezone.utc) - timedelta(days=ctx.activation_lookback_days)
        since = _iso(since_dt)
        await ctx.say("info", f"Activations: collecting since {since[:10]}…")

        requests, req_ok, req_note, _unused = await _entra_requests(client, ctx, since)
        if req_note:
            notes.append(req_note)
        # The audit log is the read-only route to justification. It is tried regardless of
        # whether the requests API worked, because it also carries MFA and approval facts
        # that the requests API does not.
        audits, audit_ok, audit_note = await _entra_audits(client, ctx, since)
        if audit_note:
            notes.append(audit_note)
        if not audit_ok:
            blockers.append(model.blocker(
                model.BLOCKER_CONSENT,
                "Activation justification is not readable.",
                scope="AuditLog.Read.All",
                impact="Activations are still listed, but not the reason each was requested.",
            ))
        instances, inst_ok, inst_note = await _entra_instances(client, ctx)
        if inst_note:
            notes.append(inst_note)

        connection = None
        if ctx.connection_id:
            try:
                from app.core.azure_connections import resolve_connection

                connection = resolve_connection(ctx.connection_id)
            except Exception as exc:  # noqa: BLE001 - the Azure half is always optional
                log.debug("activations: no connection resolved: %s", exc)
        azure, az_caps, az_notes = await _azure_requests(connection, ctx, since)
        notes.extend(az_notes)
        for failed in az_caps.get("failed_subscriptions") or []:
            blockers.append(model.blocker(
                model.BLOCKER_AZURE_ROLE,
                f"Azure PIM activations cannot be read in this subscription ({failed['reason']}).",
                scope=AZURE_PIM_ROLES,
                subject=failed["name"] or failed["id"],
                impact="Elevations performed in it are missing from this view.",
            ))

        sessions = _dedupe([*requests, *audits, *instances, *azure])
        _apply_role_names(sessions, await _role_names(client))

        detail_ok = req_ok or audit_ok
        if not detail_ok and inst_ok:
            notes.append(
                "Activation windows are exact, but justification and approver are blank "
                "because no source that carries them is readable.")

        await ctx.say(
            "ok",
            f"Activations: {len(sessions):,} session(s) "
            f"({sum(1 for s in sessions if s['plane'] == 'entra'):,} Entra, "
            f"{sum(1 for s in sessions if s['plane'] == 'azure'):,} Azure)")

        data = {
            "sessions": sessions,
            "lookback_days": ctx.activation_lookback_days,
            "capabilities": {
                "entra_requests": req_ok,
                "entra_audits": audit_ok,
                "entra_instances": inst_ok,
                "azure_requests": bool(az_caps.get("available")),
                "azure_subscriptions": int(az_caps.get("subscriptions") or 0),
                "azure_reason": str(az_caps.get("reason") or ""),
                "detail": detail_ok or bool(az_caps.get("available")),
            },
            "counts": {
                "sessions": len(sessions),
                "granted": sum(1 for s in sessions if s["granted"]),
                "attempts": sum(1 for s in sessions if not s["granted"]),
                "entra": sum(1 for s in sessions if s["plane"] == "entra"),
                "azure": sum(1 for s in sessions if s["plane"] == "azure"),
                "tier0": sum(1 for s in sessions if s["tier"] == "tier0"),
                "self_service": sum(1 for s in sessions if s["self_service"]),
                "no_justification": sum(
                    1 for s in sessions if s["detail_known"] and not s["justification"]),
            },
        }
        status = model.STATUS_OK
        if not detail_ok or not az_caps.get("available"):
            status = model.STATUS_PARTIAL
        if not detail_ok and not inst_ok and not az_caps.get("available"):
            return model.blind_payload(
                DOMAIN, "No activation source is readable on this connection.",
                ["AuditLog.Read.All", "RoleManagement.Read.Directory"])
        return model.domain_payload(DOMAIN, data, status=status,
                                    item_count=len(sessions), notes=notes, blockers=blockers)

    return await guarded(DOMAIN, ctx, _run)

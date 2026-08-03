"""What a principal actually did while a privileged role was activated.

This is the half of the activations feature that no Microsoft screen gives you: PIM tells
you an elevation happened, the audit logs tell you what changed, and nobody joins them.

Two sources, one per plane, both already proven in Change Explorer:
  * Entra   — ``auditLogs/directoryAudits`` filtered to the actor and the window.
  * Azure   — the Activity Log per subscription, with the actor recovered from the event's
              ``objectidentifier`` claim by ``changeexplorer.identity.extract_actor_meta``.
              That claim is the only reliable join back to a directory principal; the
              ``caller`` field is a UPN for humans, absent for platform events, and an
              appId for service principals.

Attribution is the hard part, and getting it wrong would be worse than not shipping it.
A user who holds a standing Global Administrator assignment can do everything the activated
role allows WITHOUT activating anything, so "they activated at 09:00 and deleted a group at
09:05" does not mean the deletion needed the elevation. Every action is therefore classified
rather than blamed:

    required_activation  the principal holds no standing privileged role that covers it
    possible_without     a standing role already allowed it — the elevation is incidental
    unclassified         the standing picture is unreadable, so no claim is made

Cost: the Activity Log is per-subscription and slow (~30s a page on a busy subscription),
and a tenant here has 26 subscriptions. So this runs on demand for ONE session and its
result is cached, never as part of a refresh.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from app.entra import cache
from app.entra.collectors.activations import parse_time

log = logging.getLogger("app.entra.activation_actions")

# Cached enrichment per session. Actions inside a window that has already closed cannot
# change, so this is a permanent answer for expired sessions and a short-lived one for live.
STATE_NAME = "activation_actions"
CACHE_MAX_SESSIONS = 500

# A window is padded before/after because clocks and audit ingestion are not instantaneous:
# the elevation is usable the moment it is granted, and audit events land seconds to a couple
# of minutes late. Too small and real actions are missed; too large and unrelated work is
# swept in, so this stays deliberately tight and is stated in the UI.
WINDOW_PAD_MINUTES = 2

MAX_ENTRA_ACTIONS = 500
MAX_AZURE_ACTIONS = 500

REQUIRED = "required_activation"
POSSIBLE = "possible_without"
UNKNOWN = "unclassified"


def _standing_privileged(data: dict[str, Any], principal_id: str) -> list[str]:
    """Privileged directory roles the principal holds WITHOUT activating anything."""
    roles = (data.get("roles") or {})
    out: list[str] = []
    for bucket in ("assignments", "group_derived"):
        for row in roles.get(bucket) or []:
            if str(row.get("principal_id") or "") != principal_id:
                continue
            if row.get("activated"):
                continue                  # that IS an activation, not standing power
            if row.get("role_privileged") or row.get("role_tier") in ("tier0", "tier1"):
                name = str(row.get("role_name") or "")
                if name and name not in out:
                    out.append(name)
    return out


def _standing_azure(data: dict[str, Any], principal_id: str) -> list[str]:
    """Powerful Azure roles the principal holds standing, via the RBAC join."""
    link = data.get("_azure_link") or {}
    if not link.get("available"):
        return []
    principal = (link.get("principals") or {}).get(principal_id) or {}
    return [str(r) for r in (principal.get("powerful_roles") or [])]


def classify(plane: str, standing_entra: list[str], standing_azure: list[str],
             azure_link_available: bool) -> str:
    """Would this action have been possible without the elevation?"""
    if plane == "entra":
        return POSSIBLE if standing_entra else REQUIRED
    if not azure_link_available:
        return UNKNOWN                    # no standing picture — refuse to guess
    return POSSIBLE if standing_azure else REQUIRED


async def _entra_actions(connection: dict[str, Any], principal_id: str,
                         start_iso: str, end_iso: str) -> tuple[list[dict[str, Any]], str]:
    """Directory changes this principal made in the window."""
    from app.azure.credentials import get_graph_token

    token, terr = await get_graph_token(connection)
    if not token:
        return [], f"Entra actions unavailable: {terr or 'no Graph token'}"

    import httpx

    flt = (f"activityDateTime ge {start_iso} and activityDateTime le {end_iso} "
           f"and initiatedBy/user/id eq '{principal_id}'")
    url = "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits"
    params: dict[str, str] | None = {"$filter": flt, "$top": "200"}
    headers = {"Authorization": f"Bearer {token}"}
    rows: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            for _page in range(10):
                resp = await http.get(url, headers=headers, params=params)
                params = None
                if resp.status_code in (401, 403):
                    return [], ("Entra actions unavailable — Graph denied the audit log "
                                "(needs AuditLog.Read.All).")
                if resp.status_code != 200:
                    return rows, f"Entra audit query failed ({resp.status_code})."
                body = resp.json()
                for raw in body.get("value") or []:
                    targets = raw.get("targetResources") or []
                    first = targets[0] if targets else {}
                    rows.append({
                        "plane": "entra",
                        "at": str(raw.get("activityDateTime") or ""),
                        "operation": str(raw.get("activityDisplayName") or ""),
                        "category": str(raw.get("category") or ""),
                        "result": str(raw.get("result") or ""),
                        "target": str(first.get("displayName")
                                      or first.get("userPrincipalName") or first.get("id") or ""),
                        "target_type": str(first.get("type") or ""),
                        "correlation_id": str(raw.get("correlationId") or ""),
                    })
                    if len(rows) >= MAX_ENTRA_ACTIONS:
                        return rows, ""
                url = body.get("@odata.nextLink") or ""
                if not url:
                    break
    except httpx.HTTPError as exc:
        return rows, f"Entra audit query error: {type(exc).__name__}"
    return rows, ""


async def _azure_actions(connection: dict[str, Any], principal_id: str, start_iso: str,
                         end_iso: str, subscriptions: list[str]) -> tuple[list[dict[str, Any]], str]:
    """Azure control-plane operations this principal performed in the window."""
    from app.azure.arm import list_activity_log_events
    from app.azure.credentials import get_arm_token
    from app.changeexplorer.identity import extract_actor_meta

    token, terr = await get_arm_token(connection)
    if not token:
        return [], f"Azure actions unavailable: {terr or 'no ARM token'}"
    if not subscriptions:
        return [], "No subscription is associated with this activation."

    sem = asyncio.Semaphore(4)
    notes: list[str] = []

    async def _one(sub: str) -> list[dict[str, Any]]:
        async with sem:
            events, err = await list_activity_log_events(
                token, sub, start_iso, end_iso, max_events=MAX_AZURE_ACTIONS)
        if err:
            notes.append(f"{sub[:8]}…: {err[:120]}")
        rows: list[dict[str, Any]] = []
        for raw in events:
            meta = extract_actor_meta(raw.get("caller", ""), raw.get("claims") or {},
                                      raw.get("correlationId", ""))
            if (meta.get("object_id") or "").lower() != principal_id.lower():
                continue
            op = raw.get("operationName") or {}
            status = raw.get("status") or {}
            rows.append({
                "plane": "azure",
                "at": str(raw.get("eventTimestamp") or ""),
                "operation": str(op.get("value") if isinstance(op, dict) else op or ""),
                "category": "AzureResource",
                "result": str(status.get("value") if isinstance(status, dict) else status or ""),
                "target": str(raw.get("resourceId") or "").rsplit("/", 1)[-1],
                "target_type": str(raw.get("resourceId") or ""),
                "subscription_id": sub,
                "correlation_id": str(raw.get("correlationId") or ""),
                "actor_ip": meta.get("ip", ""),
            })
        return rows

    gathered = await asyncio.gather(*(_one(s) for s in subscriptions))
    rows = [r for chunk in gathered for r in chunk]
    return rows, ("; ".join(notes) if notes else "")


def _cache_key(session_id: str) -> str:
    return session_id


def cached(tenant_id: str, session_id: str) -> dict[str, Any] | None:
    store = cache.read_state(tenant_id, STATE_NAME, default=None) or {}
    if not isinstance(store, dict):
        return None
    return (store.get("sessions") or {}).get(_cache_key(session_id))


def _remember(tenant_id: str, session_id: str, payload: dict[str, Any]) -> None:
    store = cache.read_state(tenant_id, STATE_NAME, default=None) or {}
    if not isinstance(store, dict):
        store = {}
    sessions = store.setdefault("sessions", {})
    sessions[_cache_key(session_id)] = payload
    if len(sessions) > CACHE_MAX_SESSIONS:
        for key in list(sessions)[: len(sessions) - CACHE_MAX_SESSIONS]:
            del sessions[key]
    cache.write_state(tenant_id, STATE_NAME, store)


async def actions_in_window(
    connection: dict[str, Any] | None,
    principal_id: str,
    start_iso: str,
    end_iso: str,
    snapshot_data: dict[str, Any],
    *,
    subscriptions: list[str] | None = None,
    planes: tuple[str, ...] = ("entra", "azure"),
) -> dict[str, Any]:
    """What one principal did between two instants, on either plane, classified.

    Extracted from :func:`collect_actions` so the Investigate screen can ask the same
    question over an arbitrary window ("the last three days") instead of only over a PIM
    activation. The classification is the whole point and is unchanged: an action is
    *never* blamed on an elevation the principal did not need.

    Returns the body of an actions payload — the caller owns windowing and caching.
    """
    notes: list[str] = []
    entra_rows: list[dict[str, Any]] = []
    azure_rows: list[dict[str, Any]] = []

    if connection is None:
        notes.append("No connection is attached, so no actions could be read.")
    elif not principal_id:
        notes.append("No principal id, so actions cannot be traced.")
    else:
        if "entra" in planes:
            entra_rows, e_note = await _entra_actions(connection, principal_id, start_iso, end_iso)
            if e_note:
                notes.append(e_note)
        if "azure" in planes:
            if subscriptions:
                azure_rows, a_note = await _azure_actions(
                    connection, principal_id, start_iso, end_iso, subscriptions)
                if a_note:
                    notes.append(a_note)
            else:
                notes.append("No subscription in scope, so resource operations cannot be read.")

    link = snapshot_data.get("_azure_link") or {}
    standing_entra = _standing_privileged(snapshot_data, principal_id)
    standing_azure = _standing_azure(snapshot_data, principal_id)
    link_ok = bool(link.get("available"))

    actions = [
        {**row, "attribution": classify(row["plane"], standing_entra, standing_azure, link_ok)}
        for row in [*entra_rows, *azure_rows]
    ]
    actions.sort(key=lambda r: r.get("at") or "")

    return {
        "actions": actions,
        "counts": {
            "total": len(actions),
            "entra": len(entra_rows),
            "azure": len(azure_rows),
            REQUIRED: sum(1 for a in actions if a["attribution"] == REQUIRED),
            POSSIBLE: sum(1 for a in actions if a["attribution"] == POSSIBLE),
            UNKNOWN: sum(1 for a in actions if a["attribution"] == UNKNOWN),
        },
        "standing_entra_roles": standing_entra,
        "standing_azure_roles": standing_azure,
        "azure_link_available": link_ok,
        "truncated": len(entra_rows) >= MAX_ENTRA_ACTIONS or len(azure_rows) >= MAX_AZURE_ACTIONS,
        "notes": notes,
    }


async def collect_actions(tenant_id: str, connection: dict[str, Any] | None,
                          session: dict[str, Any], snapshot_data: dict[str, Any],
                          *, refresh: bool = False) -> dict[str, Any]:
    """Everything the principal did during one activation, classified.

    Cached: a closed window's answer cannot change, and re-reading 26 subscriptions of
    Activity Log on every drawer open would make the screen unusable.
    """
    sid = str(session.get("id") or "")
    if not refresh:
        hit = cached(tenant_id, sid)
        if hit is not None:
            return {**hit, "cached": True}

    notes: list[str] = []
    start = parse_time(session.get("start") or "")
    end = parse_time(session.get("end") or "")
    if not start:
        return {"actions": [], "notes": ["This activation has no start time recorded."],
                "counts": {}, "cached": False, "window": {}}
    if not end:
        # Still live, or the source never recorded an end. Read up to now.
        from datetime import datetime, timezone

        end = datetime.now(timezone.utc)
        notes.append("This activation has no recorded end — actions are shown up to now.")

    pad = timedelta(minutes=WINDOW_PAD_MINUTES)
    start_iso = (start - pad).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = (end + pad).strftime("%Y-%m-%dT%H:%M:%SZ")
    principal_id = str(session.get("principal_id") or "")

    # An activation reads the Azure plane only when it IS an Azure activation, and only
    # against its own subscription — widening that here would attribute unrelated work.
    subs: list[str] = []
    planes: tuple[str, ...] = ("entra",)
    if session.get("plane") == "azure":
        planes = ("entra", "azure")
        if session.get("subscription_id"):
            subs = [session["subscription_id"]]

    body = await actions_in_window(
        connection, principal_id, start_iso, end_iso, snapshot_data,
        subscriptions=subs, planes=planes,
    )
    # Preserve the original wording for the one case this endpoint phrases differently.
    body_notes = [
        ("This Azure activation records no subscription, so resource operations cannot be "
         "scoped to it.") if n == "No subscription in scope, so resource operations cannot be read."
        else n
        for n in body["notes"]
    ]

    payload = {
        **{k: v for k, v in body.items() if k not in ("notes", "truncated")},
        "window": {"start": start_iso, "end": end_iso, "pad_minutes": WINDOW_PAD_MINUTES},
        "notes": [*notes, *body_notes],
        "collected_at": cache.now_iso(),
        "cached": False,
    }
    _remember(tenant_id, sid, payload)
    return payload

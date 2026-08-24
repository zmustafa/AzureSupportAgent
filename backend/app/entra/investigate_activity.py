"""Behavioral history for ONE principal — the activity half of Investigate.

Separate from ``collectors/risk.py`` on purpose. That module reads sign-ins tenant-wide and
folds every page into an aggregator, keeping no raw row: the right shape for a posture
score, useless for "what did this account do on Tuesday". Here the filter is a single
principal, so the query is cheap and the rows are kept.

Four sources, three cheap and one not:

  signins                 Graph /auditLogs/signIns            one filtered call
  signins_noninteractive  Graph /auditLogs/signIns (beta)     one filtered call
  audit            Graph /auditLogs/directoryAudits    one filtered call (via activation_actions)
  risk             Graph /identityProtection/riskDetections   one filtered call
  azure_activity   Azure Activity Log, PER SUBSCRIPTION       ~30s a page, never implicit

The two sign-in reads are deliberately separate calls rather than one widened filter. Graph
returns interactive sign-ins and nothing else unless the event type is named explicitly, and
non-interactive volume is far higher — one shared row budget would let a day of token
refreshes push every interactive sign-in off the end of the page.

Reading this is a sensitive act — it is behavioral data about a named person. The route
that calls this sits behind its own permission and records who asked, about whom, and why.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("app.entra.investigate_activity")

# Microsoft Graph keeps sign-in and directory-audit data for ~30 days (license dependent).
# Asking for more does not fail gracefully: directoryAudits rejects an over-long filter with
# a 400 rather than returning what it has, so the window is clamped BEFORE the call and the
# clamp is reported rather than silently applied.
GRAPH_RETENTION_DAYS = 30
AZURE_RETENTION_DAYS = 90

# `signInEventTypes` exists only on the beta endpoint — it is absent from the v1.0 signIn
# resource entirely, and v1.0 `isInteractive` is not filterable. Non-interactive sign-ins are
# therefore unreachable without beta, which is a setting the operator controls.
GRAPH_V1 = "v1.0"
GRAPH_BETA = "beta"

MAX_SIGNIN_ROWS = 500
MAX_NONINTERACTIVE_SIGNIN_ROWS = 500
MAX_RISK_ROWS = 200

TYPE_SIGNINS = "signins"
TYPE_SIGNINS_NONINTERACTIVE = "signins_noninteractive"
TYPE_AUDIT = "audit"
TYPE_RISK = "risk"
TYPE_AZURE = "azure_activity"
ALL_TYPES = (TYPE_SIGNINS, TYPE_SIGNINS_NONINTERACTIVE, TYPE_AUDIT, TYPE_RISK, TYPE_AZURE)

# Types cheap enough to run without asking. The Azure Activity Log is deliberately absent.
EAGER_TYPES = (TYPE_SIGNINS, TYPE_SIGNINS_NONINTERACTIVE, TYPE_AUDIT, TYPE_RISK)


def beta_endpoints_enabled() -> bool:
    from app.core.app_settings import load_settings

    return bool(load_settings().get("entra_enable_beta_endpoints", True))


def clamp_days(asked: int, *, azure: bool = False) -> tuple[int, str]:
    """Clamp a requested window to what the source actually retains.

    Returns ``(days, note)``; the note is empty when nothing was clamped. Reporting the
    clamp matters: a reader who asked for 90 days and silently got 30 would read the
    result as "nothing happened before that", which is the opposite of the truth."""
    limit = AZURE_RETENTION_DAYS if azure else GRAPH_RETENTION_DAYS
    asked = max(1, int(asked))
    if asked <= limit:
        return asked, ""
    source = "The Azure Activity Log retains" if azure else "Microsoft Graph retains"
    return limit, (
        f"Asked for {asked} days; {source} about {limit}. Showing {limit} days — "
        "older activity was not withheld, it no longer exists at the source."
    )


def window(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, int(days)))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), now.strftime(fmt)


async def _graph_pages(connection: dict[str, Any], path: str, params: dict[str, str],
                       cap: int, version: str = GRAPH_V1) -> tuple[list[dict[str, Any]], str]:
    """Page a filtered Graph query, stopping at ``cap``. Fail-soft with a stated reason."""
    from app.azure.credentials import get_graph_token

    token, terr = await get_graph_token(connection)
    if not token:
        return [], f"unavailable: {terr or 'no Graph token'}"

    import httpx

    url = f"https://graph.microsoft.com/{version}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    rows: list[dict[str, Any]] = []
    query: dict[str, str] | None = params
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            for _page in range(10):
                resp = await http.get(url, headers=headers, params=query)
                query = None
                if resp.status_code in (401, 403):
                    return [], ("denied by Graph — this needs AuditLog.Read.All "
                                "(and IdentityRiskEvent.Read.All for risk detections).")
                if resp.status_code == 400:
                    return rows, f"rejected the query ({resp.text[:120]})"
                if resp.status_code != 200:
                    return rows, f"failed ({resp.status_code})"
                body = resp.json()
                rows.extend(body.get("value") or [])
                if len(rows) >= cap:
                    return rows[:cap], ""
                url = body.get("@odata.nextLink") or ""
                if not url:
                    break
    except httpx.HTTPError as exc:
        return rows, f"error: {type(exc).__name__}"
    return rows, ""


def _shape_signin(raw: dict[str, Any]) -> dict[str, Any]:
    status = raw.get("status") or {}
    loc = raw.get("location") or {}
    return {
        "at": str(raw.get("createdDateTime") or ""),
        "app": str(raw.get("appDisplayName") or raw.get("resourceDisplayName") or ""),
        "client_app": str(raw.get("clientAppUsed") or ""),
        "ip": str(raw.get("ipAddress") or ""),
        "city": str(loc.get("city") or ""),
        "country": str(loc.get("countryOrRegion") or ""),
        "failure_code": status.get("errorCode"),
        "failure_reason": str(status.get("failureReason") or ""),
        "success": (status.get("errorCode") in (0, None)),
        "interactive": raw.get("isInteractive"),
        "ca_status": str(raw.get("conditionalAccessStatus") or ""),
        "risk_level": str(raw.get("riskLevelDuringSignIn") or ""),
    }


async def signins(connection: dict[str, Any], principal: dict[str, Any],
                  start_iso: str, end_iso: str) -> tuple[list[dict[str, Any]], str]:
    """Interactive sign-ins by this principal.

    Interactive only, because that is what Graph returns when no event type is named. The
    non-interactive half is a separate read; see ``noninteractive_signins``.

    A user is filtered by ``userId``; a workload identity signs in as itself and is filtered
    by ``appId`` — the object id would match nothing, which reads as "never signed in"."""
    from app.entra.investigate import KIND_MI, KIND_SP

    kind = principal.get("kind")
    if kind in (KIND_SP, KIND_MI):
        app_id = str(principal.get("app_id") or "")
        if not app_id:
            return [], "this workload identity has no appId recorded, so its sign-ins cannot be found."
        clause = f"appId eq '{app_id}'"
    else:
        clause = f"userId eq '{principal.get('id')}'"

    rows, err = await _graph_pages(
        connection, "/auditLogs/signIns",
        {"$filter": f"createdDateTime ge {start_iso} and createdDateTime le {end_iso} and {clause}",
         "$top": "200"},
        MAX_SIGNIN_ROWS,
    )
    return [_shape_signin(raw) for raw in rows], err


async def noninteractive_signins(connection: dict[str, Any], principal: dict[str, Any],
                                 start_iso: str, end_iso: str) -> tuple[list[dict[str, Any]], str]:
    """Non-interactive sign-ins by this user — auth a client performed on their behalf.

    The user supplied no factor here: a token was redeemed or refreshed in the background.
    That makes these the rows that answer "was this account still being used after the person
    left", which an interactive-only list can answer wrongly — a mailbox client refreshing
    tokens daily produces no interactive sign-in at all.

    Unreachable without the beta endpoint, and that is reported rather than returned as an
    empty list: nothing-found and could-not-look render identically and mean the opposite."""
    if not beta_endpoints_enabled():
        return [], ("needs the Microsoft Graph beta endpoints, which are switched off for this "
                    "tenant (Settings → Entra). signInEventTypes does not exist on v1.0, so "
                    "non-interactive sign-ins cannot be read at all without it.")

    rows, err = await _graph_pages(
        connection, "/auditLogs/signIns",
        {"$filter": (f"createdDateTime ge {start_iso} and createdDateTime le {end_iso} "
                     f"and userId eq '{principal.get('id')}' "
                     "and signInEventTypes/any(t: t eq 'nonInteractiveUser')"),
         "$top": "200"},
        MAX_NONINTERACTIVE_SIGNIN_ROWS, version=GRAPH_BETA,
    )
    return [_shape_signin(raw) for raw in rows], err


async def risk_detections(connection: dict[str, Any], principal: dict[str, Any],
                          start_iso: str, end_iso: str) -> tuple[list[dict[str, Any]], str]:
    """Identity Protection detections for this principal. Users only — see capabilities."""
    rows, err = await _graph_pages(
        connection, "/identityProtection/riskDetections",
        {"$filter": f"userId eq '{principal.get('id')}' and detectedDateTime ge {start_iso}",
         "$top": "100"},
        MAX_RISK_ROWS,
    )
    out = []
    for raw in rows:
        out.append({
            "at": str(raw.get("detectedDateTime") or raw.get("activityDateTime") or ""),
            "type": str(raw.get("riskEventType") or ""),
            "level": str(raw.get("riskLevel") or ""),
            "state": str(raw.get("riskState") or ""),
            "detail": str(raw.get("riskDetail") or ""),
            "ip": str(raw.get("ipAddress") or ""),
            "source": str(raw.get("source") or ""),
        })
    return out, err


def subscriptions_for(rows: list[dict[str, Any]], principal_id: str) -> list[str]:
    """Subscriptions where this principal currently holds access.

    Used to scope the Activity Log read, because sweeping every subscription in a large
    tenant costs minutes. The trade is stated to the reader rather than hidden: access
    removed since the action was taken would put that subscription out of scope, so this
    narrows where we look, and the caller says so."""
    out: list[str] = []
    needle = (principal_id or "").lower()
    for row in rows:
        ids = {str(row.get("principalId") or "").lower(),
               str(row.get("effectivePrincipalId") or "").lower()}
        if needle not in ids:
            continue
        sub = str(row.get("subscriptionId") or "")
        if sub and sub not in out:
            out.append(sub)
    return out

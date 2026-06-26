"""Entra ID (Azure AD) directory-audit + PIM source for the Change Explorer (features B1/B2).

ARM control-plane (Resource Graph + Activity Log) misses the highest-risk identity events — role
assignments, app credential/secret changes, conditional-access edits, PIM activations. Those live
in Microsoft Graph ``auditLogs/directoryAudits``. This collector pulls them with the connection's
Graph token and maps each into the common *raw change* row shape the normalizer consumes.

Best-effort + fail-open: no Graph token / 403 -> ``([], note)`` so the rest of the run is unaffected.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("app.changeexplorer.entra")

# Graph directoryAudits categories worth surfacing as "changes" (skip pure sign-in noise).
_INTERESTING_CATEGORIES = {
    "rolemanagement", "applicationmanagement", "usermanagement", "groupmanagement",
    "policy", "directorymanagement", "authorization", "servicePrincipalManagement",
}


def _map_category(activity: str, category: str) -> str:
    a = (activity or "").lower()
    c = (category or "").lower()
    if "pim" in a or "eligib" in a or ("role" in a and "activat" in a):
        return "PIM"
    if "role" in a or "rolemanagement" in c:
        return "RBAC"
    if "application" in c or "app role" in a or "credential" in a or "password" in a or "secret" in a:
        return "AppRegistration"
    if "serviceprincipal" in c.replace(" ", ""):
        return "ServicePrincipal"
    if "conditional access" in a or "policy" in c:
        return "Policy"
    if "user" in c:
        return "Identity"
    if "group" in c:
        return "Identity"
    return "Identity"


def _actor_from_audit(initiated_by: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return (caller, kind, object_id, ip) from a directoryAudit initiatedBy block."""
    user = (initiated_by or {}).get("user") or {}
    app = (initiated_by or {}).get("app") or {}
    if user and (user.get("userPrincipalName") or user.get("displayName")):
        return (user.get("userPrincipalName") or user.get("displayName", ""), "User",
                user.get("id", ""), user.get("ipAddress", ""))
    if app and (app.get("displayName") or app.get("servicePrincipalId")):
        return (app.get("displayName") or app.get("servicePrincipalId", ""), "ServicePrincipal",
                app.get("servicePrincipalId", ""), "")
    return ("", "Unknown", "", "")


async def collect_entra_audits(connection: dict[str, Any] | None, start_iso: str, end_iso: str,
                               max_events: int = 1000) -> tuple[list[dict[str, Any]], str]:
    """Pull Entra directory audit events in the window via Graph. Returns (raw_rows, note)."""
    if connection is None:
        return [], ""
    from app.azure.credentials import get_graph_token

    token, terr = await get_graph_token(connection)
    if not token:
        # Soft note only — Entra audits are a bonus source, not required.
        return [], ("Entra ID audit events not included — this connection has no Microsoft Graph "
                    "token (paste a Graph token or use an SP with AuditLog.Read.All / Directory.Read.All).")

    import httpx

    flt = f"activityDateTime ge {start_iso} and activityDateTime le {end_iso}"
    url = "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits"
    params: dict[str, str] | None = {"$filter": flt, "$top": "200", "$orderby": "activityDateTime desc"}
    headers = {"Authorization": f"Bearer {token}"}
    rows: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            for _page in range(20):
                resp = await client.get(url, headers=headers, params=params)
                params = None
                if resp.status_code != 200:
                    if resp.status_code in (401, 403):
                        return [], ("Entra ID audit events not included — Graph denied access "
                                    f"({resp.status_code}); grant AuditLog.Read.All to include identity changes.")
                    return rows, f"Entra audit query failed ({resp.status_code})."
                data = resp.json()
                for a in data.get("value", []) or []:
                    cat = (a.get("category", "") or "")
                    if cat.lower().replace(" ", "") not in {c.replace(" ", "") for c in _INTERESTING_CATEGORIES}:
                        # Keep role/app/policy even if category label differs; else skip noise.
                        act = (a.get("activityDisplayName", "") or "").lower()
                        if not any(t in act for t in ("role", "application", "credential", "password",
                                                      "conditional access", "policy", "member", "secret")):
                            continue
                    caller, kind, oid, ip = _actor_from_audit(a.get("initiatedBy") or {})
                    targets = a.get("targetResources", []) or []
                    target_name = ""
                    target_id = ""
                    if targets:
                        t0 = targets[0]
                        target_name = t0.get("displayName") or t0.get("userPrincipalName") or t0.get("id", "")
                        target_id = t0.get("id", "")
                    activity = a.get("activityDisplayName", "") or "Directory change"
                    rows.append({
                        "source": "EntraAudit",
                        "resourceId": f"entra://{cat}/{target_id}" if target_id else f"entra://{cat}",
                        "resourceName": target_name or activity,
                        "resourceType": f"microsoft.graph/{cat.lower().replace(' ', '')}",
                        "resourceGroup": "", "subscriptionId": "", "location": "Entra ID",
                        "eventTime": a.get("activityDateTime", ""),
                        "operation": activity, "changeType": "Update",
                        "actor": caller, "actorType": kind, "actorKind": kind,
                        "actorObjectId": oid, "actorIp": ip,
                        "correlationId": a.get("correlationId", ""),
                        "category_hint": _map_category(activity, cat),
                        "changes": [], "raw": a,
                    })
                    if len(rows) >= max_events:
                        return rows, ""
                nxt = data.get("@odata.nextLink") or ""
                if not nxt:
                    break
                url = nxt
        return rows, ""
    except httpx.HTTPError as e:  # noqa: BLE001
        return rows, f"Entra audit query error: {e}"

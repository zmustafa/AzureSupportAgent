"""Admin endpoints for Azure connections (multi-tenant).

CRUD over the connection registry plus a live ``/test`` (acquire an ARM token and read
the tenant's subscriptions) and ``/discover`` (subscriptions + management groups for the
selected connection). All endpoints require the admin role; secrets are never returned.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.azure.arm import list_management_groups, list_subscriptions
from app.azure.credentials import get_arm_token
from app.core.azure_connections import (
    AUTH_METHODS,
    delete_connection,
    get_connection,
    public_connection,
    public_connections,
    resolve_connection,
    set_default,
    update_status,
    upsert_connection,
)
from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.models import AuditLog

router = APIRouter(prefix="/admin/connections", tags=["connections"])


class ConnectionUpsert(BaseModel):
    id: str | None = None
    display_name: str
    tenant_id: str
    auth_method: str
    default_subscription: str | None = None
    log_analytics_workspace_id: str | None = None
    read_only: bool | None = None
    auto_execute_writes: bool | None = None
    disabled: bool | None = None
    is_default: bool | None = None
    client_id: str | None = None
    # Secrets — blank on update means "keep the stored value".
    client_secret: str | None = None
    certificate_pem: str | None = None
    # For az_cli_token: either paste the full `az account get-access-token` JSON in
    # access_token_json, or supply access_token (+ optional token_expires_on) directly.
    access_token: str | None = None
    access_token_json: str | None = None
    token_expires_on: str | None = None
    # Optional Microsoft Graph token for az_cli_token connections (resolves principal names).
    # Paste the JSON from `az account get-access-token --resource-type ms-graph` in
    # graph_access_token_json, or supply graph_access_token (+ optional expiry) directly.
    graph_access_token: str | None = None
    graph_access_token_json: str | None = None
    graph_token_expires_on: str | None = None


def _parse_token_json(raw: str) -> dict[str, str]:
    """Parse the JSON emitted by `az account get-access-token` into our fields."""
    import json

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="Pasted token is not valid JSON.")
    token = data.get("accessToken") or data.get("access_token")
    if not token:
        raise HTTPException(
            status_code=400, detail="Pasted JSON has no 'accessToken' field."
        )
    # Prefer the UTC epoch (`expires_on`) over the human-readable `expiresOn`, which is
    # LOCAL time and would be mis-compared against the server's UTC clock (a container
    # runs in UTC) — making a still-valid token look expired.
    expires = data.get("expires_on")
    if expires in (None, ""):
        expires = data.get("expiresOn", "")
    return {
        "access_token": token,
        "token_expires_on": str(expires),
        "default_subscription": data.get("subscription", ""),
        "tenant_id": data.get("tenant", ""),
    }


@router.get("")
async def list_connections_endpoint(_: Principal = Depends(require_admin)):
    return {"connections": public_connections(), "auth_methods": list(AUTH_METHODS)}


@router.put("")
async def upsert_connection_endpoint(
    payload: ConnectionUpsert,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if payload.auth_method not in AUTH_METHODS:
        raise HTTPException(status_code=400, detail=f"Unknown auth_method '{payload.auth_method}'.")

    data = payload.model_dump(exclude_none=True)
    # If a full az token JSON was pasted, expand it into the credential fields.
    raw_json = data.pop("access_token_json", None)
    if raw_json:
        parsed = _parse_token_json(raw_json)
        for k, v in parsed.items():
            # Don't overwrite an explicitly-provided tenant/subscription with blanks.
            if v or k not in data:
                data[k] = v

    # Optional Microsoft Graph token JSON (az account get-access-token --resource-type ms-graph)
    # -> graph_access_token + graph_token_expires_on. The ARM-scope token can't query Graph.
    graph_json = data.pop("graph_access_token_json", None)
    if graph_json:
        gparsed = _parse_token_json(graph_json)
        data["graph_access_token"] = gparsed["access_token"]
        data["graph_token_expires_on"] = gparsed["token_expires_on"]

    saved = upsert_connection(data)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="connection.upsert",
            target=saved["id"],
            metadata_json={
                "display_name": saved.get("display_name"),
                "tenant_id": saved.get("tenant_id"),
                "auth_method": saved.get("auth_method"),
            },
        )
    )
    await db.commit()
    return {"connection": public_connection(saved)}


@router.delete("/{connection_id}")
async def delete_connection_endpoint(
    connection_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not delete_connection(connection_id):
        raise HTTPException(status_code=404, detail="Connection not found.")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="connection.delete",
            target=connection_id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.post("/{connection_id}/default")
async def set_default_endpoint(
    connection_id: str, _: Principal = Depends(require_admin)
):
    if not set_default(connection_id):
        raise HTTPException(status_code=404, detail="Connection not found.")
    return {"ok": True, "connections": public_connections()}


@router.post("/{connection_id}/test")
async def test_connection_endpoint(
    connection_id: str, _: Principal = Depends(require_admin)
):
    conn = get_connection(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found.")
    token, err = await get_arm_token(conn)
    if err or not token:
        update_status(connection_id, "error", err or "No token")
        return {"ok": False, "detail": err or "Could not acquire a token."}
    subs, sub_err = await list_subscriptions(token)
    if sub_err:
        update_status(connection_id, "error", sub_err)
        return {"ok": False, "detail": sub_err}
    update_status(connection_id, "ok", f"{len(subs)} subscription(s) visible")
    return {
        "ok": True,
        "subscription_count": len(subs),
        "subscriptions": subs[:50],
        "connection": public_connection(get_connection(connection_id) or conn),
    }


@router.get("/{connection_id}/discover")
async def discover_endpoint(
    connection_id: str, _: Principal = Depends(require_admin)
):
    conn = resolve_connection(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found.")
    token, err = await get_arm_token(conn)
    if err or not token:
        return {"ok": False, "detail": err or "No token", "subscriptions": [], "management_groups": []}
    subs, _se = await list_subscriptions(token)
    mgs, _me = await list_management_groups(token)
    return {
        "ok": True,
        "subscriptions": subs,
        "management_groups": mgs,
    }


@router.post("/{connection_id}/validate-entra")
async def validate_entra_endpoint(
    connection_id: str, _: Principal = Depends(require_admin)
):
    """Validate that this connection's app has the Microsoft Graph application
    permissions required for the EntraID MCP server. Spawns the EntraID server with the
    connection's service-principal identity and reads its actual granted app-roles.
    """
    import json as _json

    from app.core.config import get_settings
    from app.mcp.client import build_entra_mcp_client

    # Use an EXACT lookup (not resolve_connection, which falls back to the default for a
    # missing/disabled connection) so we validate the connection the user actually
    # clicked — never silently report another tenant's app as passing.
    conn = get_connection(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found.")

    if conn.get("auth_method") not in ("service_principal", "service_principal_cert"):
        return {
            "ok": False,
            "detail": (
                "EntraID validation requires a service-principal connection (client id + "
                "secret or certificate). This connection uses "
                f"'{conn.get('auth_method')}', which can't authenticate to Microsoft Graph "
                "as an application."
            ),
        }

    client_id = conn.get("client_id", "")
    if not client_id:
        return {
            "ok": False,
            "detail": (
                "This connection has no client (application) id. EntraID validation needs a "
                "service-principal connection (client id + secret/certificate)."
            ),
        }
    if not (conn.get("client_secret") or conn.get("certificate_pem")):
        return {
            "ok": False,
            "detail": (
                "This connection has no client secret or certificate. EntraID validation "
                "requires app credentials to authenticate to Microsoft Graph."
            ),
        }

    settings = get_settings()
    client = build_entra_mcp_client(settings, connection=conn)
    try:
        result = await client.call_tool("validate_app_permissions", {"client_id": client_id})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"EntraID MCP server error: {exc}"}
    finally:
        client.close()

    if result.get("isError"):
        body = "\n".join(str(p) for p in (result.get("content") or []))
        return {"ok": False, "detail": body[:600] or "Validation failed."}

    # The tool returns a single JSON object as text content.
    report: dict = {}
    for part in (result.get("content") or []):
        try:
            parsed = _json.loads(part) if isinstance(part, str) else part
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            report = parsed
            break

    if report.get("status") == "error":
        return {"ok": False, "detail": report.get("message", "Validation error."), "report": report}
    if not report:
        return {"ok": False, "detail": "Could not parse the validation result."}

    return {"ok": True, "report": report}


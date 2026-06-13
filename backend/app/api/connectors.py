"""Admin endpoints for connectors (Teams, Outlook/Email, Jira, Grafana).

CRUD over the encrypted connector registry plus a live ``/test`` that exercises a
lightweight read/no-op for the connector. Secrets are never returned; the UI receives
masked ``{field}_set`` flags. Admin role required.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.registry import (
    CONNECTOR_TYPES,
    connector_types_public,
    delete_connector,
    get_connector,
    public_connector,
    public_connectors,
    update_status,
    upsert_connector,
)
from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.models import AuditLog

router = APIRouter(prefix="/admin/connectors", tags=["connectors"])


class ConnectorUpsert(BaseModel):
    id: str | None = None
    name: str
    type: str
    mode: str
    disabled: bool | None = None
    config: dict = {}


@router.get("")
async def list_connectors_endpoint(_: Principal = Depends(require_admin)):
    return {
        "connectors": public_connectors(),
        "types": connector_types_public(),
    }


@router.put("")
async def upsert_connector_endpoint(
    payload: ConnectorUpsert,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if payload.type not in CONNECTOR_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown connector type '{payload.type}'.")
    ct = CONNECTOR_TYPES[payload.type]
    if payload.mode not in ct.modes:
        raise HTTPException(status_code=400, detail=f"Unknown mode '{payload.mode}' for {payload.type}.")
    saved = upsert_connector(payload.model_dump(exclude_none=True))
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="connector.upsert",
            target=saved["id"],
            metadata_json={"type": saved.get("type"), "name": saved.get("name")},
        )
    )
    await db.commit()
    return {"connector": public_connector(saved)}


@router.delete("/{connector_id}")
async def delete_connector_endpoint(
    connector_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not delete_connector(connector_id):
        raise HTTPException(status_code=404, detail="Connector not found.")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="connector.delete",
            target=connector_id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.post("/{connector_id}/test")
async def test_connector_endpoint(
    connector_id: str, _: Principal = Depends(require_admin)
):
    conn = get_connector(connector_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connector not found.")
    ct = CONNECTOR_TYPES.get(conn.get("type", ""))
    if not ct:
        raise HTTPException(status_code=400, detail="Unknown connector type.")
    # Lightweight, side-effect-free probe per connector type.
    detail = ""
    ok = True
    try:
        ctype = conn.get("type")
        if ctype == "jira":
            from app.connectors.jira import _client

            client, cerr = _client(conn)
            if cerr or client is None:
                ok, detail = False, cerr or "Missing config."
            else:
                async with client:
                    resp = await client.get("/rest/api/3/myself")
                ok = resp.status_code < 300
                detail = "Authenticated" if ok else f"{resp.status_code}: {resp.text[:160]}"
        elif ctype == "grafana":
            from app.connectors.grafana import _client

            client, cerr = _client(conn)
            if cerr or client is None:
                ok, detail = False, cerr or "Missing config."
            else:
                async with client:
                    resp = await client.get("/api/health")
                ok = resp.status_code < 300
                detail = "Reachable" if ok else f"{resp.status_code}: {resp.text[:160]}"
        elif ctype == "servicenow":
            from app.connectors.servicenow import _client

            client, cerr = _client(conn)
            if cerr or client is None:
                ok, detail = False, cerr or "Missing config."
            else:
                async with client:
                    # Side-effect-free probe: read a single incident row.
                    resp = await client.get(
                        "/api/now/table/incident",
                        params={"sysparm_limit": 1, "sysparm_fields": "number"},
                    )
                ok = resp.status_code < 300
                detail = "Authenticated" if ok else f"{resp.status_code}: {resp.text[:160]}"
        elif ctype == "teams":
            if conn.get("mode") == "graph":
                from app.connectors.teams import _graph_token

                token, terr = await _graph_token(conn)
                ok, detail = bool(token), terr or "Graph token acquired"
            else:
                ok = bool(conn.get("webhook_url"))
                detail = "Webhook configured" if ok else "No webhook URL set."
        elif ctype == "outlook":
            if conn.get("mode") in ("graph", "office365"):
                from app.connectors.outlook import _graph_token

                token, terr = await _graph_token(conn)
                ok, detail = bool(token), terr or "Graph token acquired"
            else:
                ok, detail = False, "Unknown Outlook mode."
        elif ctype == "email":
            ok = bool(conn.get("smtp_host"))
            detail = "SMTP host configured" if ok else "No SMTP host set."
        elif ctype == "slack":
            if conn.get("mode") == "token":
                import httpx

                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        "https://slack.com/api/auth.test",
                        headers={"Authorization": f"Bearer {conn.get('bot_token', '')}"},
                    )
                data = resp.json() if resp.content else {}
                ok = bool(data.get("ok"))
                detail = f"Authenticated as {data.get('user', '')}" if ok else f"{data.get('error', 'auth failed')}"
            else:
                ok = bool(conn.get("webhook_url"))
                detail = "Webhook configured" if ok else "No webhook URL set."
        elif ctype == "webhook":
            ok = bool(conn.get("url"))
            detail = "URL configured" if ok else "No URL set."
        elif ctype == "pagerduty":
            ok = bool(conn.get("routing_key"))
            detail = "Routing key configured" if ok else "No routing key set."
        elif ctype == "splunk":
            ok = bool(conn.get("hec_url") and conn.get("hec_token"))
            detail = "HEC URL + token configured" if ok else "Missing HEC URL or token."
        elif ctype == "xsoar":
            from app.connectors.xsoar import _client

            client, cerr = _client(conn)
            if cerr or client is None:
                ok, detail = False, cerr or "Missing config."
            else:
                async with client:
                    resp = await client.get("/user")
                ok = resp.status_code < 400
                detail = "Authenticated" if ok else f"{resp.status_code}: {resp.text[:160]}"
        elif ctype in ("sqs", "s3", "securityhub"):
            from app.connectors.aws_common import aws_client, aws_config_valid, run_aws

            cerr = aws_config_valid(conn)
            if cerr:
                ok, detail = False, cerr
            else:
                ident = await run_aws(lambda: aws_client(conn, "sts").get_caller_identity())
                ok = True
                detail = f"Authenticated as {ident.get('Arn', '')}"
        elif ctype == "servicebus":
            if conn.get("mode") == "sas":
                ok = bool(conn.get("namespace") and conn.get("sas_key_name") and conn.get("sas_key"))
            else:
                ok = bool(conn.get("connection_string"))
            detail = "Configured" if ok else "Missing Service Bus credentials."
        else:
            ok, detail = False, "No test available."
    except Exception as exc:  # noqa: BLE001
        from app.core.utils import format_error

        ok, detail = False, format_error(exc)
    update_status(connector_id, "ok" if ok else "error", detail)
    return {"ok": ok, "detail": detail, "connector": public_connector(get_connector(connector_id) or conn)}

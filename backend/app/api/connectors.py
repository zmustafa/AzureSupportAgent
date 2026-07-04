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
from app.core.security import Principal, require_permission
from app.models import AuditLog

router = APIRouter(prefix="/admin/connectors", tags=["connectors"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("connectors.manage")


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
        elif ctype == "logicapp":
            ok = bool(conn.get("trigger_url"))
            detail = "Trigger URL configured" if ok else "No HTTP trigger URL set."
        elif ctype == "sumologic":
            ok = bool(conn.get("source_url"))
            detail = "Source URL configured" if ok else "No HTTP source URL set."
        elif ctype == "crowdstrike_ngsiem":
            ok = bool(conn.get("ingest_url") and conn.get("ingest_token"))
            detail = "HEC URL + token configured" if ok else "Missing HEC ingest URL or token."
        else:
            ok, detail = False, "No test available."
    except Exception as exc:  # noqa: BLE001
        from app.core.utils import format_error

        ok, detail = False, format_error(exc)
    update_status(connector_id, "ok" if ok else "error", detail)
    return {"ok": ok, "detail": detail, "connector": public_connector(get_connector(connector_id) or conn)}


# Connector types where "send test" delivers a harmless message/alert rather than
# creating a real ticket or storage object. Kept deliberately narrow so a test can't
# open an incident or write to a bucket/queue.
TEST_MESSAGE_TYPES = {"teams", "slack", "email", "outlook", "webhook", "pagerduty", "splunk", "grafana", "logicapp", "sumologic", "crowdstrike_ngsiem"}


@router.post("/{connector_id}/send-test")
async def send_test_message_endpoint(
    connector_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Deliver a real test message through a messaging connector so admins can confirm
    end-to-end delivery (not just that config is present)."""
    conn = get_connector(connector_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connector not found.")
    ctype = conn.get("type", "")
    if ctype not in TEST_MESSAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Send test isn't available for '{ctype}' connectors — it would create a real record.",
        )
    from app.connectors.notify import deliver_to_connector

    ok_sent, detail = await deliver_to_connector(
        connector_id,
        title="Test message from Azure Support Agent",
        message=(
            "This is a test message confirming your connector is wired up correctly. "
            "If you can read this, the agent can reach this destination."
        ),
        severity="info",
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="connector.send_test",
            target=connector_id,
            metadata_json={"type": ctype, "ok": ok_sent},
        )
    )
    await db.commit()
    update_status(connector_id, "ok" if ok_sent else "error", detail)
    return {"ok": ok_sent, "detail": detail, "connector": public_connector(get_connector(connector_id) or conn)}

"""Grafana connector: query datasources, list alerts, and add annotations.

Auth: base URL + API token (service-account token or API key). Grafana is both a data
source (read for investigation) and a notify/record target (annotations).
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok


def _client(config: dict[str, Any]) -> tuple[httpx.AsyncClient | None, str | None]:
    base = (config.get("base_url", "") or "").rstrip("/")
    token = config.get("api_token", "")
    if not (base and token):
        return None, "Grafana needs base URL and API token."
    return (
        httpx.AsyncClient(
            base_url=base,
            timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        ),
        None,
    )


async def _list_alerts(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "Grafana not configured.")
    async with client:
        # Prometheus-style alerting API (Grafana-managed + datasource alerts).
        resp = await client.get("/api/prometheus/grafana/api/v1/alerts")
    if resp.status_code >= 300:
        return err(f"Grafana alerts failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    alerts = (data.get("data") or {}).get("alerts", []) if isinstance(data, dict) else []
    summary = [
        {
            "state": a.get("state"),
            "name": (a.get("labels") or {}).get("alertname"),
            "severity": (a.get("labels") or {}).get("severity"),
        }
        for a in alerts
    ]
    firing = [a for a in summary if a.get("state") == "firing"]
    return ok(json.dumps({"total": len(summary), "firing": len(firing), "alerts": summary[:50]}))


async def _query(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "Grafana not configured.")
    datasource_uid = args.get("datasource_uid") or config.get("default_datasource_uid", "")
    expr = args.get("query", "")
    if not (datasource_uid and expr):
        return err("datasource_uid and query are required.")
    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"uid": datasource_uid},
                "expr": expr,
                "instant": True,
            }
        ],
        "from": "now-1h",
        "to": "now",
    }
    async with client:
        resp = await client.post("/api/ds/query", json=payload)
    if resp.status_code >= 300:
        return err(f"Grafana query failed ({resp.status_code}): {resp.text[:300]}")
    return ok(json.dumps(resp.json())[:6000])


async def _annotate(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "Grafana not configured.")
    text = args.get("text", "")
    if not text:
        return err("text is required.")
    payload: dict[str, Any] = {"text": text, "tags": args.get("tags") or ["sre-agent"]}
    if args.get("dashboard_uid"):
        payload["dashboardUID"] = args["dashboard_uid"]
    async with client:
        resp = await client.post("/api/annotations", json=payload)
    if resp.status_code >= 300:
        return err(f"Grafana annotate failed ({resp.status_code}): {resp.text[:300]}")
    return ok("Added Grafana annotation.")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="grafana_list_alerts",
            description="List current Grafana alerts (with firing count).",
            parameters={"type": "object", "properties": {}},
            kind="read",
            handler=_list_alerts,
        ),
        ConnectorTool(
            name="grafana_query",
            description="Run a query against a Grafana datasource (e.g. PromQL).",
            parameters={
                "type": "object",
                "properties": {
                    "datasource_uid": {"type": "string"},
                    "query": {"type": "string", "description": "Datasource query, e.g. a PromQL expression."},
                },
                "required": ["query"],
            },
            kind="read",
            handler=_query,
        ),
        ConnectorTool(
            name="grafana_annotate",
            description="Create a Grafana annotation (record an event).",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "dashboard_uid": {"type": "string"},
                },
                "required": ["text"],
            },
            kind="write",
            handler=_annotate,
        ),
    ]


CONNECTOR = ConnectorType(
    id="grafana",
    label="Grafana",
    description="Query datasources, list alerts, and add annotations via the Grafana API.",
    modes={
        "token": [
            FieldSpec(key="base_url", label="Grafana base URL", type="url", placeholder="https://grafana.contoso.com"),
            FieldSpec(key="api_token", label="API token / service-account token", type="password", secret=True),
            FieldSpec(key="default_datasource_uid", label="Default datasource UID", optional=True),
        ],
    },
    build_tools=_build_tools,
)

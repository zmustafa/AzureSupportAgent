"""Cortex XSOAR (Palo Alto / Demisto) connector: create incidents and add entries.

Auth: server URL + API key. Cortex XSOAR v8+ also requires an API key *id* sent in the
``x-xdr-auth-id`` header; v6 uses only the ``Authorization`` key. Both are supported.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

# Map free-form severities onto XSOAR's numeric scale.
_SEVERITY_MAP = {
    "info": 0.5,
    "low": 1,
    "warning": 1,
    "warn": 1,
    "medium": 2,
    "error": 2,
    "high": 3,
    "critical": 4,
    "failed": 2,
}


def _client(config: dict[str, Any]) -> tuple[httpx.AsyncClient | None, str | None]:
    base = (config.get("base_url", "") or "").rstrip("/")
    api_key = config.get("api_key", "")
    if not (base and api_key):
        return None, "Cortex XSOAR needs the server URL and API key."
    headers = {"Authorization": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    key_id = config.get("api_key_id", "")
    if key_id:
        headers["x-xdr-auth-id"] = str(key_id)
    verify = bool(config.get("verify_tls", True))
    return httpx.AsyncClient(base_url=base, timeout=30, headers=headers, verify=verify), None


async def _create_incident(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "Cortex XSOAR not configured.")
    name = args.get("name") or args.get("title")
    if not name:
        return err("name is required.")
    payload: dict[str, Any] = {
        "name": str(name)[:256],
        "type": args.get("type") or config.get("default_type") or "Unclassified",
        "severity": _SEVERITY_MAP.get(str(args.get("severity") or "medium").lower(), 2),
        "details": args.get("details") or args.get("message") or "",
        "createInvestigation": True,
    }
    if isinstance(args.get("labels"), list):
        payload["labels"] = [
            {"type": str(lbl.get("type", "")), "value": str(lbl.get("value", ""))}
            for lbl in args["labels"]
            if isinstance(lbl, dict)
        ]
    if isinstance(args.get("custom_fields"), dict):
        payload["CustomFields"] = args["custom_fields"]
    async with client:
        resp = await client.post("/incident", content=json.dumps(payload))
    if resp.status_code >= 300:
        return err(f"XSOAR create failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json() if resp.content else {}
    return ok(f"Created XSOAR incident {data.get('id', '')} ('{name}').")


async def _add_entry(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "Cortex XSOAR not configured.")
    incident_id = args.get("incident_id", "")
    note = args.get("note") or args.get("message", "")
    if not (incident_id and note):
        return err("incident_id and note are required.")
    payload = {"investigationId": str(incident_id), "data": str(note), "markdown": True}
    async with client:
        resp = await client.post("/entry", content=json.dumps(payload))
    if resp.status_code >= 300:
        return err(f"XSOAR add-entry failed ({resp.status_code}): {resp.text[:300]}")
    return ok(f"Added note to XSOAR incident {incident_id}.")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="xsoar_create_incident",
            description="Create a Cortex XSOAR incident.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "description": "XSOAR incident type."},
                    "severity": {"type": "string", "description": "info | low | medium | high | critical"},
                    "details": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "object"}},
                    "custom_fields": {"type": "object"},
                },
                "required": ["name"],
            },
            kind="write",
            handler=_create_incident,
        ),
        ConnectorTool(
            name="xsoar_add_entry",
            description="Add a note/entry to an existing XSOAR investigation.",
            parameters={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["incident_id", "note"],
            },
            kind="write",
            handler=_add_entry,
        ),
    ]


CONNECTOR = ConnectorType(
    id="xsoar",
    label="Cortex XSOAR",
    description="Create incidents and add entries in Palo Alto Cortex XSOAR (Demisto).",
    modes={
        "api_key": [
            FieldSpec(key="base_url", label="XSOAR server URL", type="url", placeholder="https://xsoar.contoso.com"),
            FieldSpec(key="api_key", label="API key", type="password", secret=True),
            FieldSpec(
                key="api_key_id",
                label="API key ID (XSOAR 8+)",
                optional=True,
                help="Required for Cortex XSOAR 8 / XSIAM; leave blank for v6.",
            ),
            FieldSpec(key="default_type", label="Default incident type", optional=True, placeholder="Unclassified"),
        ],
    },
    build_tools=_build_tools,
)

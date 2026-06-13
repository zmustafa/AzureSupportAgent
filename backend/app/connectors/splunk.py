"""Splunk connector: send events to the HTTP Event Collector (HEC).

Auth: HEC URL + token. Events are posted to ``/services/collector/event`` with the
standard ``Authorization: Splunk <token>`` header.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok


def _collector_url(base: str) -> str:
    base = (base or "").rstrip("/")
    if not base:
        return ""
    if not base.startswith("http"):
        base = f"https://{base}"
    # Accept either the HEC base host or a full collector path.
    if "/services/collector" in base:
        return base
    return f"{base}/services/collector/event"


async def _send_event(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = _collector_url(config.get("hec_url", ""))
    token = config.get("hec_token", "")
    if not (url and token):
        return err("Splunk needs the HEC URL and token.")
    event: Any = args.get("event")
    if event is None:
        event = {
            "title": args.get("title", ""),
            "message": args.get("message", ""),
            "severity": args.get("severity", "info"),
            "facts": args.get("facts") or {},
        }
    body: dict[str, Any] = {
        "event": event,
        "sourcetype": args.get("sourcetype") or config.get("default_sourcetype") or "azsupagent",
        "source": args.get("source") or "azsupagent",
    }
    index = args.get("index") or config.get("default_index")
    if index:
        body["index"] = index
    # verify is configurable: many HEC endpoints use self-signed certs.
    verify = bool(config.get("verify_tls", True))
    async with httpx.AsyncClient(timeout=20, verify=verify) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Splunk {token}", "Content-Type": "application/json"},
            content=json.dumps(body),
        )
    if resp.status_code >= 300:
        return err(f"Splunk HEC failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json() if resp.content else {}
    if data.get("code", 0) not in (0, None):
        return err(f"Splunk HEC error: {data}")
    return ok("Sent event to Splunk HEC.")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="splunk_send_event",
            description="Send an event to Splunk via the HTTP Event Collector (HEC).",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "severity": {"type": "string"},
                    "facts": {"type": "object"},
                    "event": {"type": "object", "description": "Optional explicit event object (overrides envelope)."},
                    "index": {"type": "string"},
                    "sourcetype": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": [],
            },
            kind="write",
            handler=_send_event,
        ),
    ]


CONNECTOR = ConnectorType(
    id="splunk",
    label="Splunk",
    description="Send events to Splunk via the HTTP Event Collector (HEC).",
    modes={
        "hec": [
            FieldSpec(
                key="hec_url",
                label="HEC URL",
                type="url",
                placeholder="https://splunk.contoso.com:8088",
            ),
            FieldSpec(key="hec_token", label="HEC token", type="password", secret=True),
            FieldSpec(key="default_index", label="Default index", optional=True, placeholder="main"),
            FieldSpec(key="default_sourcetype", label="Default sourcetype", optional=True, placeholder="azsupagent"),
        ],
    },
    build_tools=_build_tools,
)

"""PagerDuty connector: trigger, acknowledge, and resolve incidents.

Uses the Events API v2 (https://events.pagerduty.com/v2/enqueue) with an integration
*routing key* — the standard way services raise alerts in PagerDuty.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

_ENQUEUE = "https://events.pagerduty.com/v2/enqueue"

# Map free-form severities onto PagerDuty's allowed set.
_SEVERITY_MAP = {
    "info": "info",
    "warning": "warning",
    "warn": "warning",
    "error": "error",
    "critical": "critical",
    "high": "error",
    "failed": "error",
}


async def _enqueue(routing_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(_ENQUEUE, json={"routing_key": routing_key, **payload})
    if resp.status_code >= 300:
        return err(f"PagerDuty failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json() if resp.content else {}
    return ok(
        f"PagerDuty {payload.get('event_action', 'event')} accepted "
        f"(dedup_key {data.get('dedup_key', '')})."
    )


async def _trigger(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    routing_key = config.get("routing_key", "")
    if not routing_key:
        return err("PagerDuty routing key is not configured.")
    summary = args.get("summary") or args.get("title") or args.get("message")
    if not summary:
        return err("summary is required.")
    severity = _SEVERITY_MAP.get(str(args.get("severity") or "error").lower(), "error")
    body: dict[str, Any] = {
        "event_action": "trigger",
        "payload": {
            "summary": str(summary)[:1024],
            "source": args.get("source") or config.get("default_source") or "azsupagent",
            "severity": severity,
        },
    }
    if args.get("dedup_key"):
        body["dedup_key"] = args["dedup_key"]
    if isinstance(args.get("custom_details"), dict):
        body["payload"]["custom_details"] = args["custom_details"]
    return await _enqueue(routing_key, body)


async def _acknowledge(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    routing_key = config.get("routing_key", "")
    if not routing_key:
        return err("PagerDuty routing key is not configured.")
    if not args.get("dedup_key"):
        return err("dedup_key is required to acknowledge.")
    return await _enqueue(routing_key, {"event_action": "acknowledge", "dedup_key": args["dedup_key"]})


async def _resolve(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    routing_key = config.get("routing_key", "")
    if not routing_key:
        return err("PagerDuty routing key is not configured.")
    if not args.get("dedup_key"):
        return err("dedup_key is required to resolve.")
    return await _enqueue(routing_key, {"event_action": "resolve", "dedup_key": args["dedup_key"]})


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="pagerduty_trigger_incident",
            description="Trigger a PagerDuty incident/alert.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "One-line incident summary."},
                    "severity": {"type": "string", "description": "info | warning | error | critical"},
                    "source": {"type": "string", "description": "Originating component/host."},
                    "dedup_key": {"type": "string", "description": "Optional key to dedupe/correlate."},
                    "custom_details": {"type": "object", "description": "Structured context."},
                },
                "required": ["summary"],
            },
            kind="write",
            handler=_trigger,
        ),
        ConnectorTool(
            name="pagerduty_acknowledge",
            description="Acknowledge a PagerDuty incident by dedup_key.",
            parameters={
                "type": "object",
                "properties": {"dedup_key": {"type": "string"}},
                "required": ["dedup_key"],
            },
            kind="write",
            handler=_acknowledge,
        ),
        ConnectorTool(
            name="pagerduty_resolve",
            description="Resolve a PagerDuty incident by dedup_key.",
            parameters={
                "type": "object",
                "properties": {"dedup_key": {"type": "string"}},
                "required": ["dedup_key"],
            },
            kind="write",
            handler=_resolve,
        ),
    ]


CONNECTOR = ConnectorType(
    id="pagerduty",
    label="PagerDuty",
    description="Trigger, acknowledge, and resolve incidents via the PagerDuty Events API v2.",
    modes={
        "events_v2": [
            FieldSpec(
                key="routing_key",
                label="Integration routing key",
                type="password",
                secret=True,
                help="From a PagerDuty service's Events API v2 integration.",
            ),
            FieldSpec(key="default_source", label="Default source", optional=True, placeholder="azsupagent"),
        ],
    },
    build_tools=_build_tools,
)

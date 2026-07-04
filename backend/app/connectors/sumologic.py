"""Sumo Logic connector: send events to an HTTP Logs & Metrics Source.

A Sumo Logic Hosted Collector exposes an HTTP Source with a unique, SAS-style
collection URL (``https://endpoint<N>.collection.sumologic.com/receiver/v1/http/<token>``).
Posting JSON to that URL ingests it. The URL embeds an auth token, so it's a secret,
and the host is pinned to ``*.sumologic.com``.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

_ALLOWED_HOST_SUFFIX = ".sumologic.com"


def _valid_source_url(url: str) -> str | None:
    """Return None if the URL is an acceptable Sumo Logic HTTPS source, else an error."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return "Invalid source URL."
    if parsed.scheme != "https":
        return "Source URL must use HTTPS."
    host = (parsed.hostname or "").lower()
    if not (host == "sumologic.com" or host.endswith(_ALLOWED_HOST_SUFFIX)):
        return "Source URL host must be a *.sumologic.com collection endpoint."
    return None


def _body(args: dict[str, Any]) -> bytes:
    """Explicit ``event`` (object or list of objects) wins; else the standard envelope.
    A list is sent as newline-delimited JSON — Sumo's batching format."""
    event = args.get("event")
    if isinstance(event, list):
        return "\n".join(json.dumps(e) for e in event).encode("utf-8")
    if isinstance(event, dict):
        return json.dumps(event).encode("utf-8")
    envelope = {
        "title": args.get("title", ""),
        "message": args.get("message", ""),
        "severity": args.get("severity", "info"),
        "facts": args.get("facts") or {},
    }
    return json.dumps(envelope).encode("utf-8")


async def _send_event(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = config.get("source_url", "")
    if not url:
        return err("Sumo Logic source URL is not configured.")
    url_err = _valid_source_url(url)
    if url_err:
        return err(url_err)
    from app.core.ssrf import check_url

    blocked = check_url(url, require_https=True)
    if blocked:
        return err(blocked)
    body = _body(args)
    headers = {"Content-Type": "application/json"}
    # Optional Sumo metadata headers (searchable as _sourceCategory / _sourceHost / _sourceName).
    category = args.get("source_category") or config.get("source_category")
    if category:
        headers["X-Sumo-Category"] = str(category)
    if config.get("source_host"):
        headers["X-Sumo-Host"] = str(config["source_host"])
    if config.get("source_name"):
        headers["X-Sumo-Name"] = str(config["source_name"])
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, content=body, headers=headers)
    if resp.status_code >= 300:
        return err(f"Sumo Logic ingest failed ({resp.status_code}): {resp.text[:300]}")
    return ok(f"Sent event(s) to Sumo Logic ({resp.status_code}).")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="sumologic_send_event",
            description=(
                "Send an event to Sumo Logic via an HTTP Logs & Metrics Source. Send a "
                "{title, message, severity, facts} envelope, or an explicit event object "
                "(or a list of objects for batching)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "severity": {"type": "string", "description": "info | warning | error | critical"},
                    "facts": {"type": "object", "description": "Key/value context."},
                    "event": {
                        "type": "object",
                        "description": "Optional explicit JSON event (overrides the envelope).",
                    },
                    "source_category": {"type": "string", "description": "Overrides _sourceCategory for this event."},
                },
                "required": [],
            },
            kind="write",
            handler=_send_event,
        ),
    ]


CONNECTOR = ConnectorType(
    id="sumologic",
    label="Sumo Logic",
    description="Send events to Sumo Logic via an HTTP Logs & Metrics Source.",
    modes={
        "http_source": [
            FieldSpec(
                key="source_url",
                label="HTTP source URL",
                type="url",
                secret=True,
                placeholder="https://endpoint4.collection.sumologic.com/receiver/v1/http/…",
                help=(
                    "Sumo Logic → Collection → your Hosted Collector → HTTP Logs & Metrics Source → "
                    "copy the URL. It embeds an auth token, so it's stored as a secret."
                ),
            ),
            FieldSpec(
                key="source_category",
                label="Default source category",
                optional=True,
                placeholder="azure/support-agent",
                help="Sets _sourceCategory (searchable) on ingested events.",
            ),
        ],
    },
    build_tools=_build_tools,
)

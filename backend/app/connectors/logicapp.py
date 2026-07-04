"""Azure Logic Apps connector: trigger a workflow via its HTTP request trigger.

A Consumption Logic App's "When an HTTP request is received" trigger exposes a callback
URL that carries a SAS signature (``…&sig=…``). Posting JSON to that URL starts a run —
so this is a hardened, Azure-specific webhook. The trigger URL is a secret (it grants
the ability to start the workflow), and the host is restricted to ``*.logic.azure.com``.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

# Consumption Logic App HTTP triggers are always served from *.logic.azure.com
# (e.g. prod-12.westeurope.logic.azure.com). Pinning the host stops a stored trigger
# URL from being repurposed to POST elsewhere.
_ALLOWED_HOST_SUFFIX = ".logic.azure.com"


def _valid_trigger_url(url: str) -> str | None:
    """Return None if the URL is an acceptable Logic App HTTPS trigger, else an error."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return "Invalid trigger URL."
    if parsed.scheme != "https":
        return "Trigger URL must use HTTPS."
    host = (parsed.hostname or "").lower()
    if not (host == "logic.azure.com" or host.endswith(_ALLOWED_HOST_SUFFIX)):
        return "Trigger URL host must be a *.logic.azure.com Logic App endpoint."
    return None


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse newline- or comma-separated ``Key: Value`` header lines."""
    headers: dict[str, str] = {}
    for line in (raw or "").replace(",", "\n").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip():
                headers[k.strip()] = v.strip()
    return headers


def _parse_kv(raw: str) -> dict[str, str]:
    """Parse ``key=value`` lines into a dict (static payload additions)."""
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def _payload(config: dict[str, Any], args: dict[str, Any]) -> Any:
    """Explicit ``payload`` object wins; otherwise build the standard envelope. Static
    additions from the connector config are merged into a dict body (never a list)."""
    payload = args.get("payload")
    if not isinstance(payload, (dict, list)):
        payload = {
            "title": args.get("title", ""),
            "message": args.get("message", ""),
            "severity": args.get("severity", "info"),
            "facts": args.get("facts") or {},
        }
    extra = _parse_kv(config.get("static_payload", ""))
    if extra and isinstance(payload, dict):
        # Caller-provided keys win over static defaults.
        payload = {**extra, **payload}
    return payload


async def _trigger_http(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = config.get("trigger_url", "")
    if not url:
        return err("Logic App trigger URL is not configured.")
    url_err = _valid_trigger_url(url)
    if url_err:
        return err(url_err)
    from app.core.ssrf import check_url

    blocked = check_url(url, require_https=True)
    if blocked:
        return err(blocked)
    body = json.dumps(_payload(config, args)).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(_parse_headers(config.get("headers", "")))
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, content=body, headers=headers)
    if resp.status_code >= 300:
        return err(f"Logic App trigger failed ({resp.status_code}): {resp.text[:300]}")
    # 202 Accepted is the usual async response; 200 means the flow returned synchronously.
    reply = (resp.text or "").strip()
    if reply:
        return ok(f"Triggered Logic App workflow ({resp.status_code}). Response: {reply[:300]}")
    return ok(f"Triggered Logic App workflow ({resp.status_code}).")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="logicapp_trigger",
            description=(
                "Start an Azure Logic App workflow by posting JSON to its HTTP request trigger. "
                "Send a {title, message, severity, facts} envelope, or an explicit payload object."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "severity": {"type": "string", "description": "info | warning | error | critical"},
                    "facts": {"type": "object", "description": "Key/value context."},
                    "payload": {
                        "type": "object",
                        "description": "Optional explicit JSON body (overrides the title/message envelope).",
                    },
                },
                "required": [],
            },
            kind="write",
            handler=_trigger_http,
        ),
    ]


CONNECTOR = ConnectorType(
    id="logicapp",
    label="Azure Logic Apps",
    description="Trigger a Logic App workflow via its HTTP request trigger (SAS-signed URL).",
    modes={
        "http": [
            FieldSpec(
                key="trigger_url",
                label="HTTP trigger URL",
                type="url",
                secret=True,
                placeholder="https://prod-00.<region>.logic.azure.com/workflows/…/triggers/manual/paths/invoke?…&sig=…",
                help=(
                    "Logic App Designer → 'When an HTTP request is received' → copy the trigger URL. "
                    "It contains a SAS signature, so it's stored as a secret."
                ),
            ),
            FieldSpec(
                key="headers",
                label="Custom headers",
                type="textarea",
                optional=True,
                placeholder="Authorization: Bearer xyz\nX-Source: azsupagent",
                help="One 'Key: Value' per line — for auth or routing into the flow.",
            ),
            FieldSpec(
                key="static_payload",
                label="Static payload additions",
                type="textarea",
                optional=True,
                placeholder="source=azure-support-agent\nenv=prod",
                help="One 'key=value' per line, merged into every trigger body. Call-time values win.",
            ),
        ],
    },
    build_tools=_build_tools,
)

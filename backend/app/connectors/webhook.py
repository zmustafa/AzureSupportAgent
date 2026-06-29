"""Generic Webhook connector: POST a JSON payload to an arbitrary HTTPS endpoint.

Optional HMAC-SHA256 request signing (``X-Signature: sha256=<hex>``) and custom headers
let it integrate with internal services and SIEMs that expect signed webhooks.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse newline- or comma-separated ``Key: Value`` header lines."""
    headers: dict[str, str] = {}
    for line in (raw or "").replace(",", "\n").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip():
                headers[k.strip()] = v.strip()
    return headers


async def _send(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = config.get("url", "")
    if not url:
        return err("Webhook URL is not configured.")
    from app.core.ssrf import check_url

    blocked = check_url(url, require_https=True)
    if blocked:
        return err(blocked)
    # The payload is either an explicit object, or a {title, message, severity, facts} envelope.
    payload: Any = args.get("payload")
    if not isinstance(payload, (dict, list)):
        payload = {
            "title": args.get("title", ""),
            "message": args.get("message", ""),
            "severity": args.get("severity", "info"),
            "facts": args.get("facts") or {},
        }
    body = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    headers.update(_parse_headers(config.get("headers", "")))
    secret = config.get("signing_secret", "")
    if secret:
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers[config.get("signature_header") or "X-Signature"] = f"sha256={sig}"

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, content=body, headers=headers)
    if resp.status_code >= 300:
        return err(f"Webhook failed ({resp.status_code}): {resp.text[:300]}")
    return ok(f"Delivered webhook ({resp.status_code}).")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="webhook_send",
            description="POST a JSON payload to the configured webhook endpoint.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "severity": {"type": "string", "description": "info | warning | error | critical"},
                    "facts": {"type": "object", "description": "Key/value context."},
                    "payload": {
                        "type": "object",
                        "description": "Optional explicit JSON body (overrides title/message envelope).",
                    },
                },
                "required": [],
            },
            kind="write",
            handler=_send,
        ),
    ]


CONNECTOR = ConnectorType(
    id="webhook",
    label="Webhook",
    description="POST JSON payloads to any HTTPS endpoint, with optional HMAC signing.",
    modes={
        "http": [
            FieldSpec(key="url", label="Webhook URL", type="url", placeholder="https://example.com/hooks/agent"),
            FieldSpec(
                key="headers",
                label="Custom headers",
                type="textarea",
                optional=True,
                placeholder="Authorization: Bearer xyz\nX-Source: azsupagent",
                help="One 'Key: Value' per line. Used for auth or routing.",
            ),
            FieldSpec(
                key="signing_secret",
                label="HMAC signing secret",
                type="password",
                secret=True,
                optional=True,
                help="If set, requests include X-Signature: sha256=<hmac>.",
            ),
            FieldSpec(
                key="signature_header",
                label="Signature header name",
                optional=True,
                placeholder="X-Signature",
            ),
        ],
    },
    build_tools=_build_tools,
)

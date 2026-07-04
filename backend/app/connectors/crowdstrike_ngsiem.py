"""CrowdStrike Falcon Next-Gen SIEM connector: send events via the HEC ingest endpoint.

Falcon Next-Gen SIEM is built on LogScale. A "HEC / third-party data" connector created
in the Falcon console gives you an ingest URL plus an API key (token). Posting HEC-format
JSON to that URL ingests events. The token is a secret; the host is pinned to CrowdStrike
/ LogScale domains.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

# Next-Gen SIEM / LogScale HEC endpoints are served from CrowdStrike- or Humio-hosted
# domains. Pinning the host stops a stored ingest URL from being repurposed elsewhere.
_ALLOWED_HOST_SUFFIXES = (".crowdstrike.com", ".humio.com", ".logscale.com")


def _valid_ingest_url(url: str) -> str | None:
    """Return None if the URL is an acceptable NG-SIEM HTTPS ingest endpoint, else an error."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return "Invalid ingest URL."
    if parsed.scheme != "https":
        return "Ingest URL must use HTTPS."
    host = (parsed.hostname or "").lower()
    if not any(host == s.lstrip(".") or host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES):
        return "Ingest URL host must be a CrowdStrike / LogScale endpoint (*.crowdstrike.com, *.humio.com)."
    return None


def _payload(args: dict[str, Any]) -> dict[str, Any]:
    """Build a LogScale HEC body: {"event": <event>, "fields": {...}}. An explicit
    ``event`` (object) wins; otherwise the standard envelope is used."""
    event = args.get("event")
    if not isinstance(event, (dict, list)):
        event = {
            "title": args.get("title", ""),
            "message": args.get("message", ""),
            "severity": args.get("severity", "info"),
            "facts": args.get("facts") or {},
        }
    body: dict[str, Any] = {"event": event}
    if isinstance(args.get("fields"), dict):
        body["fields"] = args["fields"]
    return body


async def _send_event(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = config.get("ingest_url", "")
    token = config.get("ingest_token", "")
    if not (url and token):
        return err("CrowdStrike Next-Gen SIEM needs the HEC ingest URL and token.")
    url_err = _valid_ingest_url(url)
    if url_err:
        return err(url_err)
    from app.core.ssrf import check_url

    blocked = check_url(url, require_https=True)
    if blocked:
        return err(blocked)
    body = json.dumps(_payload(args)).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, content=body, headers=headers)
    if resp.status_code >= 300:
        return err(f"CrowdStrike NG-SIEM ingest failed ({resp.status_code}): {resp.text[:300]}")
    return ok(f"Sent event to CrowdStrike Next-Gen SIEM ({resp.status_code}).")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="crowdstrike_ngsiem_send_event",
            description=(
                "Send an event to CrowdStrike Falcon Next-Gen SIEM via its HEC ingest endpoint. "
                "Send a {title, message, severity, facts} envelope, or an explicit event object."
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
                    "fields": {
                        "type": "object",
                        "description": "Optional HEC 'fields' metadata attached to the event.",
                    },
                },
                "required": [],
            },
            kind="write",
            handler=_send_event,
        ),
    ]


CONNECTOR = ConnectorType(
    id="crowdstrike_ngsiem",
    label="CrowdStrike Next-Gen SIEM",
    description="Send events to CrowdStrike Falcon Next-Gen SIEM via the HEC ingest endpoint.",
    modes={
        "hec": [
            FieldSpec(
                key="ingest_url",
                label="HEC ingest URL",
                type="url",
                placeholder="https://<instance>.crowdstrike.com/api/v1/ingest/hec",
                help=(
                    "Falcon → Next-Gen SIEM → Data onboarding → HEC / third-party connector → "
                    "copy the API URL."
                ),
            ),
            FieldSpec(
                key="ingest_token",
                label="HEC API key",
                type="password",
                secret=True,
                help="The API key from the same HEC connector — sent as a bearer token.",
            ),
        ],
    },
    build_tools=_build_tools,
)

"""Microsoft Teams connector: post messages to a channel.

Two configuration modes:
- ``webhook``: an Incoming Webhook URL (no OAuth, simplest enterprise setup).
- ``graph``:   Microsoft Graph via the team/channel and an Azure connection identity
               (client-credentials token), posting as the app.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

_GRAPH = "https://graph.microsoft.com/v1.0"


# Severity → (Adaptive Card container style, accent color, emoji).
_SEVERITY = {
    "info": ("accent", "0078D4", "ℹ️"),
    "success": ("good", "2EB67D", "✅"),
    "good": ("good", "2EB67D", "✅"),
    "ok": ("good", "2EB67D", "✅"),
    "healthy": ("good", "2EB67D", "✅"),
    "warning": ("warning", "ECB22E", "⚠️"),
    "warn": ("warning", "ECB22E", "⚠️"),
    "error": ("attention", "E01E5A", "🔴"),
    "critical": ("attention", "E01E5A", "🚨"),
    "high": ("attention", "E01E5A", "🔴"),
    "failed": ("attention", "E01E5A", "❌"),
}


def _normalize_facts(facts: Any) -> list[dict[str, str]]:
    """Accept a dict {k: v} or a list of {title/name, value} and return AdaptiveCard facts."""
    out: list[dict[str, str]] = []
    if isinstance(facts, dict):
        out = [{"title": str(k), "value": str(v)} for k, v in facts.items()]
    elif isinstance(facts, list):
        for f in facts:
            if isinstance(f, dict):
                title = f.get("title") or f.get("name") or f.get("key") or ""
                value = f.get("value") or f.get("val") or ""
                if title or value:
                    out.append({"title": str(title), "value": str(value)})
    return out


def _normalize_actions(actions: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(actions, list):
        for a in actions:
            if isinstance(a, dict):
                t = a.get("title") or a.get("label") or a.get("text") or "Open"
                u = a.get("url") or a.get("href") or a.get("link")
                if u:
                    out.append({"type": "Action.OpenUrl", "title": str(t), "url": str(u)})
    return out


def _build_adaptive_card(args: dict[str, Any]) -> dict[str, Any]:
    title = (args.get("title") or "").strip()
    text = (args.get("message") or "").strip()
    subtitle = (args.get("subtitle") or args.get("source") or "").strip()
    severity = str(args.get("severity") or "info").lower()
    style, accent, emoji = _SEVERITY.get(severity, _SEVERITY["info"])
    facts = _normalize_facts(args.get("facts"))
    actions = _normalize_actions(args.get("actions"))

    body: list[dict[str, Any]] = []

    # Colored header bar with severity emoji + title.
    header_items: list[dict[str, Any]] = []
    if title:
        header_items.append(
            {
                "type": "TextBlock",
                "text": f"{emoji} {title}",
                "weight": "Bolder",
                "size": "Large",
                "wrap": True,
                "color": "Light" if style in ("attention", "accent") else "Default",
            }
        )
    if subtitle:
        header_items.append(
            {
                "type": "TextBlock",
                "text": subtitle,
                "isSubtle": True,
                "spacing": "None",
                "wrap": True,
                "color": "Light" if style in ("attention", "accent") else "Default",
            }
        )
    if header_items:
        body.append(
            {
                "type": "Container",
                "style": style,
                "bleed": True,
                "items": header_items,
            }
        )

    if text:
        body.append({"type": "TextBlock", "text": text, "wrap": True, "spacing": "Medium"})

    if facts:
        body.append({"type": "FactSet", "facts": facts, "spacing": "Medium"})

    # Footer with a timestamp Teams renders in the viewer's locale.
    body.append(
        {
            "type": "TextBlock",
            "text": "{{DATE(" + _now_iso() + ", SHORT)}} {{TIME(" + _now_iso() + ")}}",
            "wrap": True,
            "isSubtle": True,
            "size": "Small",
            "spacing": "Medium",
        }
    )

    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    if actions:
        card["actions"] = actions
    return card


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _post_webhook(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = config.get("webhook_url", "")
    if not url:
        return err("Teams webhook URL is not configured.")
    from app.core.ssrf import check_url

    blocked = check_url(url, require_https=True)
    if blocked:
        return err(blocked)
    title = args.get("title") or ""
    text = args.get("message") or ""
    if not text and not title:
        return err("message is required.")
    # Power Automate Workflows + Teams render Adaptive Cards wrapped as an attachment.
    card = _build_adaptive_card(args)
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code >= 300:
        return err(f"Teams webhook returned {resp.status_code}: {resp.text[:200]}")
    return ok(f"Posted Adaptive Card to Teams channel{f' (title: {title})' if title else ''}.")


async def _graph_token(config: dict[str, Any]) -> tuple[str | None, str | None]:
    """Acquire a Graph token via the configured Azure connection (client credentials)."""
    conn_id = config.get("connection_id", "")
    if not conn_id:
        return None, "No Azure connection selected for Graph mode."
    from app.core.azure_connections import get_connection

    azure = get_connection(conn_id)
    if not azure:
        return None, "Selected Azure connection not found."
    tenant = azure.get("tenant_id", "")
    cid = azure.get("client_id", "")
    secret = azure.get("client_secret", "")
    if not (tenant and cid and secret):
        return None, "Graph mode needs a service-principal Azure connection (tenant/client/secret)."
    # Swap the ARM scope for Graph.
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = {
        "client_id": cid,
        "client_secret": secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data=data)
        if resp.status_code != 200:
            return None, f"Graph token failed ({resp.status_code}): {resp.text[:200]}"
        return resp.json().get("access_token"), None
    except httpx.HTTPError as e:  # noqa: BLE001
        return None, f"Graph token error: {e}"


async def _post_graph(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    team_id = config.get("team_id", "")
    channel_id = config.get("channel_id", "")
    if not (team_id and channel_id):
        return err("Graph mode needs both team_id and channel_id.")
    text = args.get("message") or ""
    if not text:
        return err("message is required.")
    token, terr = await _graph_token(config)
    if terr or not token:
        return err(terr or "Could not get a Graph token.")
    body = {"body": {"contentType": "html", "content": text}}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_GRAPH}/teams/{team_id}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    if resp.status_code >= 300:
        return err(f"Graph post failed ({resp.status_code}): {resp.text[:200]}")
    return ok("Posted message to the Teams channel via Graph.")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    mode = config.get("mode", "webhook")
    handler = _post_graph if mode == "graph" else _post_webhook
    return [
        ConnectorTool(
            name="teams_post_message",
            description=(
                "Post a message to the configured Microsoft Teams channel. In webhook mode this "
                "renders a rich Adaptive Card with a severity-colored header, optional facts, and "
                "action buttons."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Main body text. Supports markdown (bold, lists, links) in webhook mode; HTML in Graph mode.",
                    },
                    "title": {"type": "string", "description": "Card heading / title."},
                    "subtitle": {
                        "type": "string",
                        "description": "Optional subtitle/source shown under the title (e.g. resource or subscription name).",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "success", "warning", "error", "critical"],
                        "description": "Controls the header color and icon. Defaults to 'info'.",
                    },
                    "facts": {
                        "type": "object",
                        "description": "Key/value pairs rendered as a fact table (e.g. {\"Resource\": \"vm-prod-01\", \"Status\": \"Degraded\"}).",
                        "additionalProperties": {"type": "string"},
                    },
                    "actions": {
                        "type": "array",
                        "description": "Optional buttons linking out (e.g. to the Azure portal or a runbook).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                            },
                            "required": ["title", "url"],
                        },
                    },
                },
                "required": ["message"],
            },
            kind="write",
            handler=handler,
        )
    ]


CONNECTOR = ConnectorType(
    id="teams",
    label="Microsoft Teams",
    description="Post messages to a Teams channel via Incoming Webhook or Microsoft Graph.",
    modes={
        "webhook": [
            FieldSpec(
                key="webhook_url",
                label="Incoming webhook URL",
                type="url",
                secret=True,
                placeholder="https://outlook.office.com/webhook/…",
                help="Teams channel → Connectors → Incoming Webhook → copy the URL.",
            ),
        ],
        "graph": [
            FieldSpec(
                key="connection_id",
                label="Azure connection (service principal)",
                type="select",
                help="Reuses a service-principal Azure tenant connection to authenticate to Graph.",
            ),
            FieldSpec(key="team_id", label="Team ID", placeholder="GUID of the team"),
            FieldSpec(key="channel_id", label="Channel ID", placeholder="19:...@thread.tacv2"),
        ],
    },
    build_tools=_build_tools,
)

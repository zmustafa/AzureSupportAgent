"""Slack connector: post messages and alerts to a channel.

Two configuration modes:
- ``webhook``: an Incoming Webhook URL (simplest; posts to the webhook's channel).
- ``token``:   a Bot/User OAuth token (chat.postMessage) — lets the agent choose the
               channel per message and post as the app.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

# Severity → Slack attachment color + emoji.
_SEVERITY = {
    "info": ("#0078D4", "ℹ️"),
    "success": ("#2EB67D", "✅"),
    "good": ("#2EB67D", "✅"),
    "ok": ("#2EB67D", "✅"),
    "healthy": ("#2EB67D", "✅"),
    "warning": ("#ECB22E", "⚠️"),
    "warn": ("#ECB22E", "⚠️"),
    "error": ("#E01E5A", "🔴"),
    "critical": ("#E01E5A", "🚨"),
    "high": ("#E01E5A", "🔴"),
    "failed": ("#E01E5A", "❌"),
}


def _blocks(args: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """Build Slack Block Kit blocks + a plain-text fallback from a notification payload."""
    title = (args.get("title") or "").strip()
    text = (args.get("message") or args.get("text") or "").strip()
    severity = str(args.get("severity") or "info").lower()
    color, emoji = _SEVERITY.get(severity, _SEVERITY["info"])
    fallback = " — ".join(p for p in [title, text] if p) or "Notification"

    blocks: list[dict[str, Any]] = []
    if title:
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"[:150]}}
        )
    if text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}})
    facts = args.get("facts")
    fields: list[dict[str, str]] = []
    if isinstance(facts, dict):
        fields = [{"type": "mrkdwn", "text": f"*{k}:*\n{v}"} for k, v in facts.items()]
    elif isinstance(facts, list):
        for f in facts:
            if isinstance(f, dict):
                t = f.get("title") or f.get("name") or ""
                v = f.get("value") or ""
                if t or v:
                    fields.append({"type": "mrkdwn", "text": f"*{t}:*\n{v}"})
    # Slack allows max 10 fields per section.
    for i in range(0, len(fields), 10):
        blocks.append({"type": "section", "fields": fields[i : i + 10]})
    return fallback, blocks, color


async def _post_webhook(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = config.get("webhook_url", "")
    if not url:
        return err("Slack webhook URL is not configured.")
    fallback, blocks, color = _blocks(args)
    payload: dict[str, Any] = {"text": fallback}
    if blocks:
        payload["attachments"] = [{"color": color, "blocks": blocks}]
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code >= 300:
        return err(f"Slack webhook failed ({resp.status_code}): {resp.text[:300]}")
    return ok("Posted message to Slack.")


async def _post_token(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    token = config.get("bot_token", "")
    if not token:
        return err("Slack bot token is not configured.")
    channel = args.get("channel") or config.get("default_channel", "")
    if not channel:
        return err("A channel is required (e.g. #alerts or a channel id).")
    fallback, blocks, color = _blocks(args)
    payload: dict[str, Any] = {"channel": channel, "text": fallback}
    if blocks:
        payload["attachments"] = [{"color": color, "blocks": blocks}]
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 300 or not data.get("ok"):
        return err(f"Slack post failed: {data.get('error') or resp.text[:200]}")
    return ok(f"Posted message to Slack channel {channel}.")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    mode = config.get("mode", "webhook")
    handler = _post_token if mode == "token" else _post_webhook
    props: dict[str, Any] = {
        "title": {"type": "string"},
        "message": {"type": "string", "description": "Message body (Slack mrkdwn supported)."},
        "severity": {"type": "string", "description": "info | success | warning | error | critical"},
        "facts": {"type": "object", "description": "Key/value pairs shown as fields."},
    }
    required = ["message"]
    if mode == "token":
        props["channel"] = {"type": "string", "description": "Channel id or #name (optional if a default is set)."}
    return [
        ConnectorTool(
            name="slack_post_message",
            description="Post a message / alert to Slack.",
            parameters={"type": "object", "properties": props, "required": required},
            kind="write",
            handler=handler,
        ),
    ]


_AUTH_FIELDS_WEBHOOK = [
    FieldSpec(
        key="webhook_url",
        label="Incoming Webhook URL",
        type="password",
        secret=True,
        placeholder="https://hooks.slack.com/services/T000/B000/XXXX",
    ),
]
_AUTH_FIELDS_TOKEN = [
    FieldSpec(key="bot_token", label="Bot/User OAuth token", type="password", secret=True, placeholder="xoxb-…"),
    FieldSpec(key="default_channel", label="Default channel", optional=True, placeholder="#alerts"),
]

CONNECTOR = ConnectorType(
    id="slack",
    label="Slack",
    description="Post messages and alerts to Slack via Incoming Webhook or a bot token.",
    modes={"webhook": _AUTH_FIELDS_WEBHOOK, "token": _AUTH_FIELDS_TOKEN},
    build_tools=_build_tools,
)

"""Email (SMTP) connector: send email via any SMTP server.

Auth: host/port + optional username/password + a from address. STARTTLS (587/25) and
implicit SSL (465) are both supported. smtplib is blocking, so the send runs off the
event loop via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import Any

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

_SEND_PARAMS = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Recipient email address(es), comma-separated.",
        },
        "subject": {"type": "string"},
        "body": {"type": "string", "description": "Email body. HTML is supported."},
    },
    "required": ["to", "subject", "body"],
}


def _recipients(to: str) -> list[str]:
    return [a.strip() for a in (to or "").replace(";", ",").split(",") if a.strip()]


def _smtp_send_sync(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    host = config.get("smtp_host", "")
    port = int(config.get("smtp_port") or 587)
    user = config.get("smtp_username", "")
    password = config.get("smtp_password", "")
    sender = config.get("from_address", "") or user
    to = _recipients(args.get("to", ""))
    if not host:
        return err("SMTP host is not configured.")
    if not to:
        return err("At least one recipient is required.")
    msg = EmailMessage()
    msg["Subject"] = args.get("subject", "")
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    body = args.get("body", "")
    msg.set_content("This message requires an HTML-capable client.")
    msg.add_alternative(body, subtype="html")
    # Port 465 is implicit SSL; 587/25 use STARTTLS.
    use_ssl = bool(config.get("smtp_ssl")) or port == 465
    try:
        if use_ssl:
            server_cm = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server_cm = smtplib.SMTP(host, port, timeout=30)
        with server_cm as server:
            server.ehlo()
            if not use_ssl and config.get("smtp_starttls", True):
                server.starttls()
                server.ehlo()
            if user:
                server.login(user, password)
            server.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        from app.core.utils import format_error

        return err(f"SMTP send failed: {format_error(exc)}")
    return ok(f"Email sent to {', '.join(to)} via SMTP.")


async def _send_smtp(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    # smtplib is blocking; run it off the event loop.
    return await asyncio.to_thread(_smtp_send_sync, config, args)


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="email_send",
            description="Send an email (HTML supported) to one or more recipients via SMTP.",
            parameters=_SEND_PARAMS,
            kind="write",
            handler=_send_smtp,
        )
    ]


CONNECTOR = ConnectorType(
    id="email",
    label="Email",
    description="Send email through any SMTP server (STARTTLS or SSL).",
    modes={
        "smtp": [
            FieldSpec(key="smtp_host", label="SMTP host", placeholder="smtp.contoso.com"),
            FieldSpec(key="smtp_port", label="SMTP port", placeholder="587"),
            FieldSpec(key="from_address", label="From address", placeholder="alerts@contoso.com"),
            FieldSpec(key="smtp_username", label="Username", optional=True),
            FieldSpec(key="smtp_password", label="Password", type="password", secret=True, optional=True),
        ],
    },
    build_tools=_build_tools,
)

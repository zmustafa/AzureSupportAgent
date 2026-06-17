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


# Header-injection guard. Python's stdlib EmailMessage already rejects raw newlines
# in header values, but a tool result could still contain CR/LF that would raise an
# error mid-send and leak partial context. We strip them up-front and cap the
# subject length so a tool result can't smuggle BCC/Reply-To rows into the message
# via a crafted subject string. Recipient addresses are validated via the
# stdlib email.utils.parseaddr below.
_HEADER_CRLF = ("\r", "\n")
_MAX_SUBJECT_LEN = 998  # RFC 5322 max line length


def _sanitize_header_value(value: str, *, max_len: int = _MAX_SUBJECT_LEN) -> str:
    """Strip CR/LF and clamp length so a tool result can't inject extra headers."""
    if not value:
        return ""
    cleaned = value
    for ch in _HEADER_CRLF:
        cleaned = cleaned.replace(ch, " ")
    # Also reject NUL and other control chars under 0x20 except TAB.
    cleaned = "".join(c for c in cleaned if c == "\t" or ord(c) >= 0x20)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def _sanitize_recipient(addr: str) -> str | None:
    """Return the normalized address (`name@host`) if valid, else None.

    Uses ``email.utils.parseaddr`` which already rejects CR/LF in addresses and
    pulls a clean `<addr>` out of a `Name <addr>` form. Returns None for empty
    or syntactically invalid entries so the caller can fail closed.
    """
    from email.utils import parseaddr

    _name, parsed = parseaddr(addr or "")
    parsed = (parsed or "").strip()
    if not parsed or "\r" in parsed or "\n" in parsed or "@" not in parsed:
        return None
    # Local-part / domain sanity: no spaces, at least one dot in domain.
    if " " in parsed:
        return None
    domain = parsed.split("@", 1)[1]
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return parsed


def _smtp_send_sync(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    host = config.get("smtp_host", "")
    port = int(config.get("smtp_port") or 587)
    user = config.get("smtp_username", "")
    password = config.get("smtp_password", "")
    sender = config.get("from_address", "") or user
    # Validate sender + each recipient. An invalid address would otherwise raise
    # deep inside smtplib (or worse, allow header injection if the format is
    # `valid@x.com\r\nBcc: attacker@evil.com`).
    sender_clean = _sanitize_recipient(sender) if sender else ""
    if sender and not sender_clean:
        return err("Configured 'from_address' is invalid.")
    raw_to = _recipients(args.get("to", ""))
    to = [addr for addr in (_sanitize_recipient(a) for a in raw_to) if addr]
    if not host:
        return err("SMTP host is not configured.")
    if not to:
        return err("At least one valid recipient is required.")
    msg = EmailMessage()
    # Sanitize header values — strip CR/LF so a crafted subject can't inject Bcc
    # / Reply-To rows. EmailMessage would also raise on raw newlines, but failing
    # silently with a normalized header is friendlier to callers (and matches the
    # behavior most SMTP gateways already enforce).
    msg["Subject"] = _sanitize_header_value(args.get("subject", ""))
    if sender_clean:
        msg["From"] = sender_clean
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

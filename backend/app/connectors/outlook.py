"""Microsoft Outlook connector: send, reply, and read email via Microsoft Graph.

Two configuration modes:
- ``office365``: Office 365 Outlook (OAuth + managed identity) — send, reply, and read
                 the inbox of a connected mailbox.
- ``graph``:     Microsoft Graph ``sendMail`` using a service-principal Azure connection.

For plain SMTP servers, use the separate "Email" connector.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

_GRAPH = "https://graph.microsoft.com/v1.0"

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

_REPLY_PARAMS = {
    "type": "object",
    "properties": {
        "message_id": {
            "type": "string",
            "description": "The id of the Outlook message to reply to (from email_read).",
        },
        "body": {
            "type": "string",
            "description": "Reply body to add to the thread. HTML is supported.",
        },
        "reply_all": {
            "type": "boolean",
            "description": "Reply to all recipients instead of just the sender. Default false.",
        },
    },
    "required": ["message_id", "body"],
}

_READ_PARAMS = {
    "type": "object",
    "properties": {
        "folder": {
            "type": "string",
            "description": "Mail folder to read (e.g. inbox, sentitems). Default inbox.",
        },
        "top": {
            "type": "integer",
            "description": "How many recent messages to return (1-25). Default 10.",
        },
        "search": {
            "type": "string",
            "description": "Optional keyword to filter messages (subject/body).",
        },
    },
}


def _recipients(to: str) -> list[str]:
    return [a.strip() for a in (to or "").replace(";", ",").split(",") if a.strip()]


async def _graph_token(config: dict[str, Any]) -> tuple[str | None, str | None]:
    from app.core.azure_connections import get_connection

    azure = get_connection(config.get("connection_id", ""))
    if not azure:
        return None, "Selected Azure connection not found."
    tenant = azure.get("tenant_id", "")
    cid = azure.get("client_id", "")
    secret = azure.get("client_secret", "")
    if not (tenant and cid and secret):
        return None, "Graph mode needs a service-principal Azure connection."
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


async def _send_graph(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    sender = config.get("from_address", "")
    if not sender:
        return err("Graph mode needs a 'from address' (the mailbox to send as).")
    to = _recipients(args.get("to", ""))
    if not to:
        return err("At least one recipient is required.")
    token, terr = await _graph_token(config)
    if terr or not token:
        return err(terr or "Could not get a Graph token.")
    message = {
        "message": {
            "subject": args.get("subject", ""),
            "body": {"contentType": "HTML", "content": args.get("body", "")},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        },
        "saveToSentItems": True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_GRAPH}/users/{sender}/sendMail",
            headers={"Authorization": f"Bearer {token}"},
            json=message,
        )
    if resp.status_code >= 300:
        return err(f"Graph sendMail failed ({resp.status_code}): {resp.text[:200]}")
    return ok(f"Email sent to {', '.join(to)} via Microsoft Graph.")


async def _reply_graph(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Reply to an existing Outlook thread (Graph /messages/{id}/reply[All])."""
    mailbox = config.get("from_address", "")
    if not mailbox:
        return err("Office 365 mode needs a connected mailbox (from address).")
    message_id = (args.get("message_id") or "").strip()
    if not message_id:
        return err("A message_id is required (get one from email_read).")
    token, terr = await _graph_token(config)
    if terr or not token:
        return err(terr or "Could not get a Graph token.")
    action = "replyAll" if args.get("reply_all") else "reply"
    payload = {
        "message": {"body": {"contentType": "HTML", "content": args.get("body", "")}}
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_GRAPH}/users/{mailbox}/messages/{message_id}/{action}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
    if resp.status_code >= 300:
        return err(f"Graph reply failed ({resp.status_code}): {resp.text[:200]}")
    return ok(f"Replied to message {message_id} ({action}).")


async def _read_graph(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Read recent messages from the connected mailbox (Graph list messages)."""
    mailbox = config.get("from_address", "")
    if not mailbox:
        return err("Office 365 mode needs a connected mailbox (from address).")
    token, terr = await _graph_token(config)
    if terr or not token:
        return err(terr or "Could not get a Graph token.")
    folder = (args.get("folder") or "inbox").strip() or "inbox"
    try:
        top = max(1, min(25, int(args.get("top") or 10)))
    except (TypeError, ValueError):
        top = 10
    params: dict[str, Any] = {
        "$top": top,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead",
        "$orderby": "receivedDateTime DESC",
    }
    search = (args.get("search") or "").strip()
    if search:
        # $search can't be combined with $orderby in Graph; drop the ordering.
        params.pop("$orderby", None)
    headers = {"Authorization": f"Bearer {token}"}
    if search:
        headers["ConsistencyLevel"] = "eventual"
        params["$search"] = f'"{search}"'
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_GRAPH}/users/{mailbox}/mailFolders/{folder}/messages",
            headers=headers,
            params=params,
        )
    if resp.status_code >= 300:
        return err(f"Graph read failed ({resp.status_code}): {resp.text[:200]}")
    items = resp.json().get("value", [])
    if not items:
        return ok(f"No messages found in {folder}.")
    lines = [f"{len(items)} message(s) in {folder} for {mailbox}:"]
    for m in items:
        frm = (m.get("from", {}).get("emailAddress", {}) or {}).get("address", "?")
        unread = "" if m.get("isRead", True) else "● "
        lines.append(
            f"- {unread}[{m.get('id', '')[:16]}…] {m.get('subject', '(no subject)')} "
            f"— from {frm} @ {m.get('receivedDateTime', '')}\n    {m.get('bodyPreview', '')[:160]}"
        )
    return ok("\n".join(lines))


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    mode = config.get("mode", "graph")
    if mode == "office365":
        # Office 365 Outlook (OAuth + managed identity) — like Azure SRE Agent's
        # Outlook Tools: send emails, reply to threads, and read the inbox.
        return [
            ConnectorTool(
                name="email_send",
                description="Send an email (HTML supported) to one or more recipients via Outlook.",
                parameters=_SEND_PARAMS,
                kind="write",
                handler=_send_graph,
            ),
            ConnectorTool(
                name="email_reply",
                description="Reply (or reply-all) to an existing Outlook email thread.",
                parameters=_REPLY_PARAMS,
                kind="write",
                handler=_reply_graph,
            ),
            ConnectorTool(
                name="email_read",
                description="Read recent messages from the connected Outlook mailbox (inbox by default).",
                parameters=_READ_PARAMS,
                kind="read",
                handler=_read_graph,
            ),
        ]
    # App-only Graph mode (service principal sendMail).
    return [
        ConnectorTool(
            name="email_send",
            description="Send an email (HTML supported) to one or more recipients via Outlook.",
            parameters=_SEND_PARAMS,
            kind="write",
            handler=_send_graph,
        )
    ]


CONNECTOR = ConnectorType(
    id="outlook",
    label="Microsoft Outlook",
    description="Send, reply, and read email via Office 365 Outlook (OAuth + managed identity) or Microsoft Graph.",
    modes={
        "office365": [
            FieldSpec(
                key="connection_id",
                label="Managed identity (Azure connection)",
                type="select",
                help=(
                    "The Azure connection providing the identity the agent uses to reach "
                    "Microsoft Graph. Choose the managed identity below."
                ),
            ),
            FieldSpec(
                key="managed_identity",
                label="Managed identity type",
                type="select",
                options=["System assigned", "User assigned"],
                help="System assigned is tied to the agent's lifecycle; User assigned is shared/independent.",
            ),
            FieldSpec(
                key="from_address",
                label="Connected mailbox",
                placeholder="you@contoso.com",
                help="The Office 365 mailbox the agent is signed in as (sends, replies, and reads from here).",
            ),
        ],
        "graph": [
            FieldSpec(
                key="connection_id",
                label="Azure connection (service principal)",
                type="select",
                help="Reuses a service-principal Azure tenant connection to authenticate to Graph.",
            ),
            FieldSpec(
                key="from_address",
                label="From mailbox",
                placeholder="alerts@contoso.com",
                help="The mailbox the agent sends as (Graph /users/{address}/sendMail).",
            ),
        ],
    },
    build_tools=_build_tools,
)

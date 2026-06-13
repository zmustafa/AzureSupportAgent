"""Azure Service Bus connector: send a message to a queue.

Two configuration modes:
- ``connection_string``: a namespace connection string (with SAS key).
- ``sas``: namespace fully-qualified name + SAS policy name + key.

The azure-servicebus SDK is synchronous here, run off the event loop via to_thread.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok


def _send_sync(config: dict[str, Any], queue: str, body: str, subject: str | None) -> None:
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    mode = config.get("mode", "connection_string")
    if mode == "sas":
        from azure.servicebus import ServiceBusSharedKeyCredential

        namespace = config.get("namespace", "")
        cred = ServiceBusSharedKeyCredential(
            config.get("sas_key_name", ""), config.get("sas_key", "")
        )
        client = ServiceBusClient(fully_qualified_namespace=namespace, credential=cred)
    else:
        client = ServiceBusClient.from_connection_string(config.get("connection_string", ""))
    with client:
        sender = client.get_queue_sender(queue_name=queue)
        with sender:
            msg = ServiceBusMessage(body)
            if subject:
                msg.subject = subject
            sender.send_messages(msg)


async def _send_message(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    mode = config.get("mode", "connection_string")
    if mode == "sas":
        if not (config.get("namespace") and config.get("sas_key_name") and config.get("sas_key")):
            return err("Service Bus needs the namespace, SAS policy name, and key.")
    elif not config.get("connection_string"):
        return err("Service Bus connection string is not configured.")
    queue = args.get("queue") or config.get("queue", "")
    if not queue:
        return err("queue is required (or set a default).")
    body = args.get("message")
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    elif body is None:
        body = json.dumps(
            {"title": args.get("title", ""), "message": args.get("text", ""), "facts": args.get("facts") or {}}
        )
    subject = args.get("subject") or args.get("title")
    try:
        await asyncio.to_thread(_send_sync, config, queue, str(body), subject)
    except Exception as exc:  # noqa: BLE001
        return err(f"Service Bus send failed: {exc}")
    return ok(f"Sent message to Service Bus queue '{queue}'.")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="servicebus_send_message",
            description="Send a message to an Azure Service Bus queue.",
            parameters={
                "type": "object",
                "properties": {
                    "queue": {"type": "string", "description": "Queue name (optional if a default is set)."},
                    "message": {"type": "string", "description": "Message body (string or JSON object)."},
                    "subject": {"type": "string", "description": "Optional message subject/label."},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "facts": {"type": "object"},
                },
                "required": [],
            },
            kind="write",
            handler=_send_message,
        ),
    ]


CONNECTOR = ConnectorType(
    id="servicebus",
    label="Azure Service Bus Queue",
    description="Send messages to an Azure Service Bus queue.",
    modes={
        "connection_string": [
            FieldSpec(
                key="connection_string",
                label="Connection string",
                type="password",
                secret=True,
                placeholder="Endpoint=sb://<ns>.servicebus.windows.net/;SharedAccessKeyName=…;SharedAccessKey=…",
            ),
            FieldSpec(key="queue", label="Default queue", optional=True, placeholder="alerts"),
        ],
        "sas": [
            FieldSpec(
                key="namespace",
                label="Namespace (FQDN)",
                placeholder="<namespace>.servicebus.windows.net",
            ),
            FieldSpec(key="sas_key_name", label="SAS policy name", placeholder="RootManageSharedAccessKey"),
            FieldSpec(key="sas_key", label="SAS key", type="password", secret=True),
            FieldSpec(key="queue", label="Default queue", optional=True, placeholder="alerts"),
        ],
    },
    build_tools=_build_tools,
)

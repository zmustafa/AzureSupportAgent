"""Amazon SQS connector: send a message to a queue."""
from __future__ import annotations

import json
from typing import Any

from app.connectors.aws_common import (
    AWS_AUTH_FIELDS_KEYS,
    AWS_AUTH_FIELDS_ROLE,
    aws_client,
    aws_config_valid,
    run_aws,
)
from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok


async def _send_message(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    cerr = aws_config_valid(config)
    if cerr:
        return err(cerr)
    queue_url = args.get("queue_url") or config.get("queue_url", "")
    if not queue_url:
        return err("queue_url is required (or set a default).")
    body = args.get("message")
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    elif body is None:
        body = json.dumps(
            {"title": args.get("title", ""), "message": args.get("text", ""), "facts": args.get("facts") or {}}
        )

    def _call() -> dict[str, Any]:
        client = aws_client(config, "sqs")
        kwargs: dict[str, Any] = {"QueueUrl": queue_url, "MessageBody": str(body)}
        # FIFO queues require a group id.
        if queue_url.endswith(".fifo"):
            kwargs["MessageGroupId"] = args.get("group_id") or "azsupagent"
            if args.get("dedup_id"):
                kwargs["MessageDeduplicationId"] = args["dedup_id"]
        return client.send_message(**kwargs)

    try:
        resp = await run_aws(_call)
    except Exception as exc:  # noqa: BLE001
        return err(f"SQS send failed: {exc}")
    return ok(f"Sent SQS message (id {resp.get('MessageId', '')}).")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="sqs_send_message",
            description="Send a message to an Amazon SQS queue.",
            parameters={
                "type": "object",
                "properties": {
                    "queue_url": {"type": "string", "description": "Queue URL (optional if a default is set)."},
                    "message": {"type": "string", "description": "Message body (string or JSON object)."},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "facts": {"type": "object"},
                    "group_id": {"type": "string", "description": "FIFO message group id."},
                    "dedup_id": {"type": "string", "description": "FIFO dedup id."},
                },
                "required": [],
            },
            kind="write",
            handler=_send_message,
        ),
    ]


_QUEUE_FIELD = FieldSpec(
    key="queue_url",
    label="Default queue URL",
    optional=True,
    placeholder="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue",
)

CONNECTOR = ConnectorType(
    id="sqs",
    label="Amazon SQS",
    description="Send messages to an Amazon SQS queue.",
    modes={
        "keys": [*AWS_AUTH_FIELDS_KEYS, _QUEUE_FIELD],
        "role": [*AWS_AUTH_FIELDS_ROLE, _QUEUE_FIELD],
    },
    build_tools=_build_tools,
)

"""Amazon S3 connector: write an object (e.g. a report) to a bucket."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.connectors.aws_common import (
    AWS_AUTH_FIELDS_KEYS,
    AWS_AUTH_FIELDS_ROLE,
    aws_client,
    aws_config_valid,
    run_aws,
)
from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok


async def _put_object(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    cerr = aws_config_valid(config)
    if cerr:
        return err(cerr)
    bucket = args.get("bucket") or config.get("bucket", "")
    if not bucket:
        return err("bucket is required (or set a default).")
    key = args.get("key")
    if not key:
        prefix = (config.get("key_prefix") or "").strip("/")
        ts = datetime.now(timezone.utc).strftime("%Y/%m/%d/%H%M%S")
        key = f"{prefix + '/' if prefix else ''}azsupagent-{ts}.json"
    content = args.get("content")
    if isinstance(content, (dict, list)):
        body = json.dumps(content, indent=2).encode("utf-8")
        content_type = "application/json"
    else:
        body = str(content if content is not None else "").encode("utf-8")
        content_type = args.get("content_type") or "text/plain"

    def _call() -> dict[str, Any]:
        client = aws_client(config, "s3")
        return client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)

    try:
        await run_aws(_call)
    except Exception as exc:  # noqa: BLE001
        return err(f"S3 put failed: {exc}")
    return ok(f"Wrote s3://{bucket}/{key} ({len(body)} bytes).")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="s3_put_object",
            description="Write an object (report/JSON/text) to an Amazon S3 bucket.",
            parameters={
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "description": "Bucket name (optional if a default is set)."},
                    "key": {"type": "string", "description": "Object key (auto-generated if omitted)."},
                    "content": {"type": "string", "description": "Object body (string or JSON object)."},
                    "content_type": {"type": "string"},
                },
                "required": ["content"],
            },
            kind="write",
            handler=_put_object,
        ),
    ]


_BUCKET_FIELDS = [
    FieldSpec(key="bucket", label="Default bucket", optional=True, placeholder="my-reports-bucket"),
    FieldSpec(key="key_prefix", label="Default key prefix", optional=True, placeholder="azsupagent/"),
]

CONNECTOR = ConnectorType(
    id="s3",
    label="Amazon S3",
    description="Write objects (reports, findings) to an Amazon S3 bucket.",
    modes={
        "keys": [*AWS_AUTH_FIELDS_KEYS, *_BUCKET_FIELDS],
        "role": [*AWS_AUTH_FIELDS_ROLE, *_BUCKET_FIELDS],
    },
    build_tools=_build_tools,
)

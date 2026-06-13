"""AWS Security Hub connector: import a finding in ASFF format."""
from __future__ import annotations

import uuid
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

# Free-form severity → ASFF SeverityLabel.
_SEVERITY_MAP = {
    "info": "INFORMATIONAL",
    "informational": "INFORMATIONAL",
    "low": "LOW",
    "warning": "LOW",
    "warn": "LOW",
    "medium": "MEDIUM",
    "error": "MEDIUM",
    "high": "HIGH",
    "critical": "CRITICAL",
    "failed": "MEDIUM",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def _import_finding(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    cerr = aws_config_valid(config)
    if cerr:
        return err(cerr)
    account_id = config.get("account_id", "")
    region = config.get("region", "")
    if not account_id:
        return err("AWS account ID is required to build the ASFF finding.")
    title = args.get("title")
    description = args.get("description") or args.get("message")
    if not (title and description):
        return err("title and description are required.")
    severity = _SEVERITY_MAP.get(str(args.get("severity") or "medium").lower(), "MEDIUM")
    finding_id = args.get("id") or str(uuid.uuid4())
    now = _now_iso()
    product_arn = f"arn:aws:securityhub:{region}:{account_id}:product/{account_id}/default"
    finding: dict[str, Any] = {
        "SchemaVersion": "2018-10-08",
        "Id": finding_id,
        "ProductArn": product_arn,
        "GeneratorId": args.get("generator_id") or "azsupagent",
        "AwsAccountId": account_id,
        "Types": args.get("types") or ["Software and Configuration Checks"],
        "CreatedAt": now,
        "UpdatedAt": now,
        "Severity": {"Label": severity},
        "Title": str(title)[:256],
        "Description": str(description)[:1024],
        "Resources": args.get("resources")
        or [{"Type": "Other", "Id": args.get("resource_id") or "azsupagent", "Region": region}],
    }

    def _call() -> dict[str, Any]:
        client = aws_client(config, "securityhub")
        return client.batch_import_findings(Findings=[finding])

    try:
        resp = await run_aws(_call)
    except Exception as exc:  # noqa: BLE001
        return err(f"Security Hub import failed: {exc}")
    failed = resp.get("FailedCount", 0)
    if failed:
        return err(f"Security Hub rejected the finding: {resp.get('FailedFindings')}")
    return ok(f"Imported Security Hub finding {finding_id}.")


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="securityhub_import_finding",
            description="Import a finding into AWS Security Hub (ASFF).",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"type": "string", "description": "info | low | medium | high | critical"},
                    "resource_id": {"type": "string", "description": "Affected resource ARN/id."},
                    "types": {"type": "array", "items": {"type": "string"}},
                    "id": {"type": "string", "description": "Stable finding id (for updates)."},
                },
                "required": ["title", "description"],
            },
            kind="write",
            handler=_import_finding,
        ),
    ]


_ACCOUNT_FIELD = FieldSpec(
    key="account_id",
    label="AWS account ID",
    placeholder="123456789012",
    help="Used to build the ASFF ProductArn and AwsAccountId.",
)

CONNECTOR = ConnectorType(
    id="securityhub",
    label="AWS Security Hub",
    description="Import findings into AWS Security Hub (ASFF format).",
    modes={
        "keys": [*AWS_AUTH_FIELDS_KEYS, _ACCOUNT_FIELD],
        "role": [*AWS_AUTH_FIELDS_ROLE, _ACCOUNT_FIELD],
    },
    build_tools=_build_tools,
)

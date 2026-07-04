"""Deliver a scheduled-task result to selected connectors as a notification.

Each connector type has a primary "notify" tool that accepts the common
{title, message, severity, facts} envelope. After a task run, the result summary is
pushed to every connector the task selected as a notification target.
"""
from __future__ import annotations

from typing import Any

from app.connectors.registry import CONNECTOR_TYPES, get_connector

# connector type -> the tool used to deliver a notification.
PRIMARY_NOTIFY_TOOL: dict[str, str] = {
    "teams": "teams_post_message",
    "slack": "slack_post_message",
    "outlook": "email_send",
    "email": "email_send",
    "jira": "jira_create_issue",
    "servicenow": "servicenow_create_incident",
    "grafana": "grafana_annotate",
    "pagerduty": "pagerduty_trigger_incident",
    "splunk": "splunk_send_event",
    "xsoar": "xsoar_create_incident",
    "webhook": "webhook_send",
    "sqs": "sqs_send_message",
    "s3": "s3_put_object",
    "securityhub": "securityhub_import_finding",
    "servicebus": "servicebus_send_message",
    "logicapp": "logicapp_trigger",
    "sumologic": "sumologic_send_event",
    "crowdstrike_ngsiem": "crowdstrike_ngsiem_send_event",
}


def _notify_args(conn_type: str, title: str, message: str, severity: str) -> dict[str, Any]:
    """Map the standard envelope onto the per-connector tool's required args."""
    base = {"title": title, "message": message, "severity": severity, "text": message}
    if conn_type == "grafana":
        return {"text": f"{title}: {message}"[:1000], "tags": ["azsupagent", "scheduled-task"]}
    if conn_type == "jira":
        return {"summary": title, "description": message}
    if conn_type == "servicenow":
        return {"short_description": title, "description": message}
    if conn_type == "pagerduty":
        return {"summary": f"{title}: {message}"[:1024], "severity": severity}
    if conn_type == "xsoar":
        return {"name": title, "details": message, "severity": severity}
    if conn_type == "securityhub":
        return {"title": title, "description": message, "severity": severity}
    if conn_type == "s3":
        return {"content": {"title": title, "message": message, "severity": severity}}
    return base


async def deliver_to_connector(connector_id: str, title: str, message: str, severity: str) -> tuple[bool, str]:
    """Send a notification to one connector. Returns (ok, detail)."""
    conn = get_connector(connector_id)
    if conn is None:
        return False, "connector not found"
    if conn.get("disabled"):
        return False, "connector disabled"
    type_id = conn.get("type", "")
    ct = CONNECTOR_TYPES.get(type_id)
    tool_name = PRIMARY_NOTIFY_TOOL.get(type_id)
    if not ct or not tool_name:
        return False, "no notify tool for connector"
    tools = {t.name: t for t in ct.build_tools(conn)}
    tool = tools.get(tool_name)
    if tool is None:
        return False, f"tool {tool_name} unavailable"
    args = _notify_args(type_id, title, message, severity)
    try:
        result = await tool.handler(conn, args)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    is_err = bool(result.get("isError"))
    detail = (result.get("content") or [""])[0]
    return (not is_err), str(detail)[:200]


async def deliver_task_result(
    connector_ids: list[str], title: str, message: str, failed: bool
) -> list[dict[str, Any]]:
    """Deliver a task result to all selected connectors; returns per-target outcomes."""
    severity = "error" if failed else "info"
    out: list[dict[str, Any]] = []
    for cid in connector_ids or []:
        ok, detail = await deliver_to_connector(cid, title, message, severity)
        out.append({"connector_id": cid, "ok": ok, "detail": detail})
    return out

"""Create a remediation ticket from an assessment finding via a configured connector
(Jira / ServiceNow). Builds a single-connector toolset and invokes its create tool."""
from __future__ import annotations

import re
from typing import Any


def _build_single_toolset(connector_id: str):
    from app.connectors.base import ConnectorToolset
    from app.connectors.registry import CONNECTOR_TYPES, get_connector

    conn = get_connector(connector_id)
    if conn is None or conn.get("disabled"):
        return None, None
    ct = CONNECTOR_TYPES.get(conn.get("type", ""))
    if ct is None:
        return None, None
    ts = ConnectorToolset()
    ts.add_connector(conn, ct.build_tools(conn))
    return ts, conn


def _ticket_text(finding: dict[str, Any], workload_name: str) -> tuple[str, str]:
    title = f"[{(finding.get('severity') or 'warning').upper()}] {finding.get('title', 'Assessment finding')} — {workload_name}"
    lines = [
        f"Assessment finding for workload: {workload_name}",
        f"Control: {finding.get('check_id')} ({finding.get('pillar')})",
        f"Severity: {finding.get('severity')}",
        "",
        finding.get("description", ""),
    ]
    if finding.get("ai_rationale"):
        lines += ["", f"Impact: {finding['ai_rationale']}"]
    if finding.get("remediation"):
        lines += ["", f"Remediation: {finding['remediation']}"]
    if finding.get("remediation_command"):
        lines += ["", f"Command: {finding['remediation_command']}"]
    flagged = finding.get("flagged_resources") or []
    if flagged:
        lines += ["", f"Flagged resources ({finding.get('flagged_count', len(flagged))}):"]
        for r in flagged[:20]:
            lines.append(f"- {r.get('name')} ({r.get('resource_group')})")
    fw = finding.get("frameworks") or {}
    fw_parts = []
    for k in ("cis", "nist", "iso"):
        if fw.get(k):
            fw_parts.append(f"{k.upper()}: {', '.join(fw[k])}")
    if fw_parts:
        lines += ["", "Compliance: " + " | ".join(fw_parts)]
    return title, "\n".join(lines)


async def create_ticket(
    *, connector_id: str, finding: dict[str, Any], workload_name: str
) -> dict[str, Any]:
    """Create a ticket; returns {ok, connector_type, ticket_id, ticket_url, detail}."""
    ts, conn = _build_single_toolset(connector_id)
    if ts is None or conn is None:
        return {"ok": False, "detail": "Connector not found or disabled."}
    ctype = conn.get("type", "")
    title, body = _ticket_text(finding, workload_name)

    if ctype == "jira":
        tool, args = "jira_create_issue", {"summary": title, "description": body}
    elif ctype == "servicenow":
        tool, args = "servicenow_create_incident", {"short_description": title, "description": body}
    else:
        # Generic fall-through: any connector exposing a *_create* write tool, else a
        # message/post tool so Teams/Slack/webhook can at least receive the finding.
        names = ts.tool_names()
        tool = next((n for n in names if "create" in n), None) or next(
            (n for n in names if any(k in n for k in ("send", "post", "message", "notify"))), None
        )
        if not tool:
            return {"ok": False, "detail": f"Connector type '{ctype}' has no create/notify tool."}
        args = {"summary": title, "title": title, "message": body, "description": body, "text": body}

    if not ts.has(tool):
        return {"ok": False, "detail": f"Connector does not expose '{tool}'."}
    result = await ts.call(tool, args)
    text = "\n".join(str(p) for p in (result.get("content") or []))
    if result.get("isError"):
        return {"ok": False, "detail": text[:500] or "Ticket creation failed.", "connector_type": ctype}

    url_match = re.search(r"https?://[^\s'\"]+", text)
    key_match = re.search(r"\b([A-Z][A-Z0-9]+-\d+|INC\d+)\b", text)
    return {
        "ok": True,
        "connector_type": ctype,
        "ticket_url": url_match.group(0) if url_match else "",
        "ticket_id": key_match.group(0) if key_match else "",
        "detail": text[:500],
    }

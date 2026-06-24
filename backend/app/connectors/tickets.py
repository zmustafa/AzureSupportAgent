"""Ticketing helpers: turn the configured connectors into a "create a ticket" capability.

The chat "Send to →" action and any future ticket integrations route through here so there is one
place that knows which connector types are ticketing systems, which tool each one uses to open a
ticket, and how to parse the resulting ticket number + deep link out of the tool's text result.

Read paths (``ticket_connectors``) are safe to expose to any signed-in user — they return only
non-secret metadata (id / name / type / label). The write path (``create_ticket``) calls the
connector's existing create tool with the decrypted config the registry already manages.
"""
from __future__ import annotations

import re
from typing import Any

# Connector types that can open a ticket/incident, mapped to the tool that creates one.
TICKET_CREATE_TOOL: dict[str, str] = {
    "servicenow": "servicenow_create_incident",
    "jira": "jira_create_issue",
    "xsoar": "xsoar_create_incident",
}


def ticket_connectors() -> list[dict[str, Any]]:
    """Enabled connectors that can create a ticket — non-secret metadata only."""
    from app.connectors.registry import CONNECTOR_TYPES, enabled_connectors

    out: list[dict[str, Any]] = []
    for c in enabled_connectors():
        t = c.get("type", "")
        if t in TICKET_CREATE_TOOL:
            ct = CONNECTOR_TYPES.get(t)
            out.append({
                "id": c.get("id", ""),
                "name": c.get("name", ""),
                "type": t,
                "label": ct.label if ct else t,
            })
    out.sort(key=lambda c: c["name"].lower())
    return out


def _create_args(conn_type: str, title: str, body: str) -> dict[str, Any]:
    """Map the (title, body) envelope onto the per-connector create tool's args."""
    if conn_type == "servicenow":
        return {"short_description": title[:160], "description": body}
    if conn_type == "jira":
        return {"summary": title[:255], "description": body}
    if conn_type == "xsoar":
        return {"name": title[:255], "details": body, "severity": "info"}
    return {"title": title, "message": body}


def _parse_ticket(conn: dict[str, Any], conn_type: str, detail: str) -> tuple[str, str]:
    """Extract (ticket_number, deep_link) from a create tool's text result + connector config."""
    number = ""
    url = ""
    if conn_type == "servicenow":
        m = re.search(r"incident\s+([A-Za-z0-9_-]+)", detail)
        number = m.group(1) if m else ""
        sid = re.search(r"sys_id\s+([0-9a-fA-F]+)", detail)
        base = (conn.get("instance_url", "") or "").rstrip("/")
        if base and not base.startswith("http"):
            base = f"https://{base}"
        if base and sid:
            url = f"{base}/nav_to.do?uri=incident.do?sys_id={sid.group(1)}"
        elif base and number:
            url = f"{base}/incident_list.do?sysparm_query=number={number}"
    elif conn_type == "jira":
        m = re.search(r"issue\s+([A-Z][A-Z0-9]+-\d+)", detail)
        number = m.group(1) if m else ""
        base = (conn.get("base_url", "") or "").rstrip("/")
        if base and number:
            url = f"{base}/browse/{number}"
    elif conn_type == "xsoar":
        m = re.search(r"incident\s+([A-Za-z0-9_-]+)", detail)
        number = m.group(1) if m else ""
    return number, url


async def _attach_pdf_servicenow(conn: dict[str, Any], detail: str, pdf: bytes, name: str) -> str:
    """Attach a PDF to a just-created ServiceNow incident. Returns "" on success, else an error."""
    import base64
    import re as _re

    import httpx

    sid = _re.search(r"sys_id\s+([0-9a-fA-F]+)", detail)
    if not sid:
        return "could not resolve incident sys_id for attachment"
    base = (conn.get("instance_url", "") or "").rstrip("/")
    if base and not base.startswith("http"):
        base = f"https://{base}"
    user, password = conn.get("username", ""), conn.get("password", "")
    if not (base and user and password):
        return "ServiceNow not fully configured for attachment"
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    url = f"{base}/api/now/attachment/file"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            params={"table_name": "incident", "table_sys_id": sid.group(1), "file_name": name},
            content=pdf,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/pdf",
                     "Accept": "application/json"},
        )
    return "" if resp.status_code < 300 else f"attachment failed ({resp.status_code})"


async def _attach_pdf_jira(conn: dict[str, Any], key: str, pdf: bytes, name: str) -> str:
    """Attach a PDF to a just-created Jira issue. Returns "" on success, else an error."""
    import base64

    import httpx

    base = (conn.get("base_url", "") or "").rstrip("/")
    email, token = conn.get("email", ""), conn.get("api_token", "")
    if not (base and email and token and key):
        return "Jira not fully configured for attachment"
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{base}/rest/api/3/issue/{key}/attachments",
            files={"file": (name, pdf, "application/pdf")},
            headers={"Authorization": f"Basic {auth}", "X-Atlassian-Token": "no-check",
                     "Accept": "application/json"},
        )
    return "" if resp.status_code < 300 else f"attachment failed ({resp.status_code})"


async def _attach_pdf(conn: dict[str, Any], conn_type: str, detail: str, number: str,
                      pdf: bytes, name: str) -> str:
    """Dispatch a PDF attachment to the just-created ticket. Returns "" on success / unsupported."""
    try:
        if conn_type == "servicenow":
            return await _attach_pdf_servicenow(conn, detail, pdf, name)
        if conn_type == "jira":
            return await _attach_pdf_jira(conn, number, pdf, name)
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:200]
    return ""  # type doesn't support attachments — skip silently


async def create_ticket(
    connector_id: str, title: str, body: str,
    *, pdf_bytes: bytes | None = None, pdf_name: str = "conversation.pdf",
) -> dict[str, Any]:
    """Create a ticket via a ticketing connector. When ``pdf_bytes`` is supplied, the PDF is
    attached to the created ticket (ServiceNow / Jira). Returns
    ``{ok, number, url, detail, connector_type, attached, attach_error, error}``."""
    from app.connectors.registry import CONNECTOR_TYPES, get_connector

    conn = get_connector(connector_id)
    if conn is None:
        return {"ok": False, "error": "Connector not found."}
    if conn.get("disabled"):
        return {"ok": False, "error": "Connector is disabled."}
    conn_type = conn.get("type", "")
    tool_name = TICKET_CREATE_TOOL.get(conn_type)
    if not tool_name:
        return {"ok": False, "error": f"'{conn_type}' is not a ticketing connector."}
    ct = CONNECTOR_TYPES.get(conn_type)
    if ct is None:
        return {"ok": False, "error": "Connector type unavailable."}
    tools = {t.name: t for t in ct.build_tools(conn)}
    tool = tools.get(tool_name)
    if tool is None:
        return {"ok": False, "error": f"'{tool_name}' is unavailable for this connector."}

    try:
        result = await tool.handler(conn, _create_args(conn_type, title, body))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}

    detail = str((result.get("content") or [""])[0])
    if result.get("isError"):
        return {"ok": False, "error": detail[:300], "connector_type": conn_type}
    number, url = _parse_ticket(conn, conn_type, detail)

    attached = False
    attach_error = ""
    if pdf_bytes:
        attach_error = await _attach_pdf(conn, conn_type, detail, number, pdf_bytes, pdf_name)
        attached = not attach_error and conn_type in ("servicenow", "jira")

    return {
        "ok": True, "number": number, "url": url, "detail": detail[:300],
        "connector_type": conn_type, "attached": attached, "attach_error": attach_error,
    }


"""Ticketing helpers (app.connectors.tickets) — connector filtering + create/parse.

Pins that: only ticketing connector types are listed; create_ticket maps to the right tool with
the right args, parses the ticket number + builds a deep link from the connector config, and
fails gracefully for non-ticketing / disabled / missing connectors.
"""
import asyncio

from app.connectors import tickets
from app.connectors.base import ConnectorTool, ConnectorType, err, ok


def _fake_ct(tool_name: str, capture: dict):
    async def _handler(config, args):
        capture["config"] = config
        capture["args"] = args
        if config.get("type") == "servicenow":
            return ok("Created ServiceNow incident INC0012345 (sys_id abc123def456).")
        return ok("Created Jira issue OPS-42 (id 10001).")

    tool = ConnectorTool(name=tool_name, description="", parameters={}, kind="write", handler=_handler)
    return ConnectorType(id="x", label="X", description="", modes={}, build_tools=lambda conn: [tool])


def test_ticket_connectors_filters_to_ticketing(monkeypatch):
    conns = [
        {"id": "a", "name": "ITSM", "type": "servicenow", "disabled": False},
        {"id": "b", "name": "Board", "type": "jira", "disabled": False},
        {"id": "c", "name": "Chat", "type": "teams", "disabled": False},   # not ticketing
    ]
    monkeypatch.setattr("app.connectors.registry.enabled_connectors", lambda: conns)
    out = tickets.ticket_connectors()
    types = {c["type"] for c in out}
    assert types == {"servicenow", "jira"}
    assert all("password" not in c and "username" not in c for c in out)  # metadata only


def test_create_ticket_servicenow_parses_number_and_url(monkeypatch):
    cap: dict = {}
    conn = {"id": "a", "type": "servicenow", "disabled": False,
            "instance_url": "https://contoso.service-now.com", "username": "u", "password": "p"}
    monkeypatch.setattr("app.connectors.registry.get_connector", lambda cid: conn)
    monkeypatch.setattr("app.connectors.registry.CONNECTOR_TYPES",
                        {"servicenow": _fake_ct("servicenow_create_incident", cap)})
    res = asyncio.run(tickets.create_ticket("a", "VM down", "Full conversation body…"))
    assert res["ok"] is True
    assert res["number"] == "INC0012345"
    assert "sys_id=abc123def456" in res["url"]
    # The whole body was passed as the incident description; title truncated into short_description.
    assert cap["args"]["short_description"] == "VM down"
    assert cap["args"]["description"] == "Full conversation body…"


def test_create_ticket_jira_parses_key_and_url(monkeypatch):
    cap: dict = {}
    conn = {"id": "b", "type": "jira", "disabled": False, "base_url": "https://contoso.atlassian.net"}
    monkeypatch.setattr("app.connectors.registry.get_connector", lambda cid: conn)
    monkeypatch.setattr("app.connectors.registry.CONNECTOR_TYPES",
                        {"jira": _fake_ct("jira_create_issue", cap)})
    res = asyncio.run(tickets.create_ticket("b", "Title", "Body"))
    assert res["ok"] is True and res["number"] == "OPS-42"
    assert res["url"] == "https://contoso.atlassian.net/browse/OPS-42"
    assert cap["args"]["summary"] == "Title"


def test_create_ticket_rejects_non_ticketing(monkeypatch):
    monkeypatch.setattr("app.connectors.registry.get_connector",
                        lambda cid: {"id": "c", "type": "teams", "disabled": False})
    res = asyncio.run(tickets.create_ticket("c", "t", "b"))
    assert res["ok"] is False and "ticketing" in res["error"].lower()


def test_create_ticket_missing_and_disabled(monkeypatch):
    monkeypatch.setattr("app.connectors.registry.get_connector", lambda cid: None)
    assert asyncio.run(tickets.create_ticket("x", "t", "b"))["ok"] is False

    monkeypatch.setattr("app.connectors.registry.get_connector",
                        lambda cid: {"id": "d", "type": "servicenow", "disabled": True})
    res = asyncio.run(tickets.create_ticket("d", "t", "b"))
    assert res["ok"] is False and "disabled" in res["error"].lower()


def test_create_ticket_surfaces_tool_error(monkeypatch):
    conn = {"id": "a", "type": "servicenow", "disabled": False}

    async def _boom(config, args):
        return err("ServiceNow create failed (401): unauthorized")

    ct = ConnectorType(id="x", label="X", description="", modes={},
                       build_tools=lambda c: [ConnectorTool("servicenow_create_incident", "", {}, "write", _boom)])
    monkeypatch.setattr("app.connectors.registry.get_connector", lambda cid: conn)
    monkeypatch.setattr("app.connectors.registry.CONNECTOR_TYPES", {"servicenow": ct})
    res = asyncio.run(tickets.create_ticket("a", "t", "b"))
    assert res["ok"] is False and "401" in res["error"]


def test_create_ticket_attaches_pdf_servicenow(monkeypatch):
    cap: dict = {}
    conn = {"id": "a", "type": "servicenow", "disabled": False,
            "instance_url": "https://contoso.service-now.com", "username": "u", "password": "p"}
    monkeypatch.setattr("app.connectors.registry.get_connector", lambda cid: conn)
    monkeypatch.setattr("app.connectors.registry.CONNECTOR_TYPES",
                        {"servicenow": _fake_ct("servicenow_create_incident", cap)})

    seen = {}

    async def _fake_attach(c, ct_, detail, number, pdf, name):
        seen["type"], seen["detail"], seen["number"], seen["pdf"], seen["name"] = ct_, detail, number, pdf, name
        return ""

    monkeypatch.setattr(tickets, "_attach_pdf", _fake_attach)
    res = asyncio.run(tickets.create_ticket("a", "VM down", "body", pdf_bytes=b"%PDF-xyz", pdf_name="chat.pdf"))
    assert res["ok"] is True and res["attached"] is True and res["attach_error"] == ""
    assert seen["pdf"] == b"%PDF-xyz" and seen["name"] == "chat.pdf"
    assert "sys_id" in seen["detail"]  # the create detail is passed through for sys_id resolution


def test_create_ticket_attach_failure_does_not_fail_ticket(monkeypatch):
    cap: dict = {}
    conn = {"id": "b", "type": "jira", "disabled": False, "base_url": "https://x.atlassian.net"}
    monkeypatch.setattr("app.connectors.registry.get_connector", lambda cid: conn)
    monkeypatch.setattr("app.connectors.registry.CONNECTOR_TYPES",
                        {"jira": _fake_ct("jira_create_issue", cap)})

    async def _fail_attach(c, ct_, detail, number, pdf, name):
        return "attachment failed (413)"

    monkeypatch.setattr(tickets, "_attach_pdf", _fail_attach)
    res = asyncio.run(tickets.create_ticket("b", "T", "B", pdf_bytes=b"%PDF-"))
    # Ticket still created OK; the attach error is surfaced but non-fatal.
    assert res["ok"] is True and res["number"] == "OPS-42"
    assert res["attached"] is False and "413" in res["attach_error"]


def test_create_ticket_no_pdf_skips_attach(monkeypatch):
    cap: dict = {}
    conn = {"id": "b", "type": "jira", "disabled": False, "base_url": "https://x.atlassian.net"}
    monkeypatch.setattr("app.connectors.registry.get_connector", lambda cid: conn)
    monkeypatch.setattr("app.connectors.registry.CONNECTOR_TYPES",
                        {"jira": _fake_ct("jira_create_issue", cap)})

    called = {"n": 0}

    async def _attach(*a, **k):
        called["n"] += 1
        return ""

    monkeypatch.setattr(tickets, "_attach_pdf", _attach)
    res = asyncio.run(tickets.create_ticket("b", "T", "B"))  # no pdf_bytes
    assert res["ok"] is True and res["attached"] is False and called["n"] == 0


def test_build_chat_pdf_renders():
    from datetime import datetime, timezone

    from app.connectors import chat_pdf

    pdf = chat_pdf.build_chat_pdf(
        "Audit secrets",
        [{"role": "user", "content": "list expiring secrets", "created_at": datetime.now(timezone.utc)},
         {"role": "assistant", "content": "Here:\n\n- one\n- two\n\n```\ncode\n```",
          "created_at": datetime.now(timezone.utc), "model": "gpt-5.5"}],
    )
    assert pdf[:5] == b"%PDF-" and len(pdf) > 800


def test_mermaid_flowchart_renders_png():
    from app.connectors.mermaid_render import render_mermaid_png

    png = render_mermaid_png(
        "graph TD\n A[Start] --> B{OK?}\n B -->|yes| C[Apply]\n B -->|no| D[Stop]"
    )
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_mermaid_unsupported_diagram_returns_none():
    from app.connectors.mermaid_render import render_mermaid_png

    # Sequence/class/etc. are not flowcharts → caller falls back to a code block.
    assert render_mermaid_png("sequenceDiagram\n A->>B: hi") is None
    assert render_mermaid_png("") is None


def test_chat_pdf_embeds_mermaid_diagram():
    from app.connectors import chat_pdf

    mer = "```mermaid\ngraph LR\n A[One] --> B[Two]\n```"
    pdf = chat_pdf.build_chat_pdf(
        "Flow", [{"role": "assistant", "content": f"See:\n\n{mer}\n", "model": "gpt"}]
    )
    assert pdf[:5] == b"%PDF-" and len(pdf) > 800


def test_chat_pdf_renders_markdown_table():
    from app.connectors import chat_pdf

    md = (
        "Summary:\n\n"
        "| Resource | Owner | Status |\n"
        "|----------|-------|:------:|\n"
        "| vm-01 | jane@x.com | apply |\n"
        "| vm-02 | bob@x.com | conflict |\n"
    )
    html = chat_pdf._md_to_html(md)
    assert 'class="mdtable"' in html
    assert html.count("<th>") == 3 and html.count("<tr>") == 3  # 1 header + 2 body rows
    pdf = chat_pdf.build_chat_pdf("Tbl", [{"role": "assistant", "content": md, "model": "gpt"}])
    assert pdf[:5] == b"%PDF-" and len(pdf) > 800

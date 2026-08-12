"""Regression tests for CodeQL alerts 570-574 (release 19 follow-up)."""
from __future__ import annotations

import asyncio
import inspect
import logging

import pytest
from fastapi import HTTPException

from app.agent import orchestrator
from app.api import admin, firewall
from app.core import netaccess_io
from app.core.security import Principal
from app.perfprofile import service


def _principal() -> Principal:
    return Principal(
        subject="security-test",
        email="security@example.test",
        tenant_id="tenant",
        role="admin",
    )


def test_firewall_parser_exception_is_not_reflected(monkeypatch):
    secret = "Traceback (most recent call last):\nAuthorization: Bearer SECRET"

    def fail(*_args, **_kwargs):
        raise netaccess_io.NetAccessImportError(secret)

    monkeypatch.setattr(netaccess_io, "preview_import", fail)
    payload = firewall.ImportPreviewIn(text="not important", mode="monitor")
    request = type("Request", (), {"client": type("Client", (), {"host": "203.0.113.7"})(), "headers": {}})()

    with pytest.raises(HTTPException) as caught:
        asyncio.run(firewall.preview_import(payload, request, _principal()))

    assert caught.value.status_code == 400
    assert caught.value.detail == "The import could not be parsed. Check the UTF-8 TXT/CSV format and limits."
    assert "Traceback" not in str(caught.value.detail)
    assert "SECRET" not in str(caught.value.detail)


def test_tool_catalog_diagnostics_never_returns_mcp_exception_text(monkeypatch):
    secret = "Traceback: password=SECRET\n/app/private/path.py"

    class BrokenClient:
        async def list_tools(self):
            raise RuntimeError(secret)

        def close(self):
            return None

    monkeypatch.setattr(admin, "build_mcp_client", lambda *_args, **_kwargs: BrokenClient())
    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: {"entra_mcp_enabled": False})
    monkeypatch.setattr(
        "app.core.app_settings.agent_runtime_params",
        lambda: {
            "agent_skills_enabled": False,
            "tool_initial_budget": 24,
            "tool_routing_enabled": True,
            "tool_max_per_turn": 32,
            "tool_search_page_size": 8,
        },
    )
    monkeypatch.setattr("app.core.azure_connections.get_default_connection", lambda: None)
    monkeypatch.setattr("app.automations.agents.get_agent", lambda _id: None)

    result = asyncio.run(admin.tool_routing_diagnostics(principal=_principal()))

    assert result["errors"]["azure_mcp"] == "Azure MCP catalog is temporarily unavailable."
    rendered = repr(result)
    assert "Traceback" not in rendered
    assert "SECRET" not in rendered
    assert "/app/private" not in rendered


def test_performance_logs_exclude_request_identifiers(monkeypatch, caplog):
    from app.perfprofile import collector

    async def fail(*_args, **_kwargs):
        raise RuntimeError("collector failed")

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: {"perfprofile_workload_timeout_s": 60})
    monkeypatch.setattr(collector, "profile_workload", fail)
    monkeypatch.setattr(service.runs, "save_run", lambda *_args, **_kwargs: _args[3])

    caplog.set_level(logging.INFO, logger="app.perfprofile.service")
    asyncio.run(
        service.execute_profile(
            tenant_id="tenant\nFORGED_TENANT",
            actor="tester",
            scope_kind="workload\rFORGED_KIND",
            scope_id="scope\nFORGED_SCOPE",
            connection={},
            workload={"id": "scope", "name": "Workload"},
            window="P1D",
            interval="PT15M",
            scan_cap=1,
            trigger="manual\nFORGED_TRIGGER",
        )
    )

    messages = [record.getMessage() for record in caplog.records if record.name == "app.perfprofile.service"]
    assert messages
    rendered = "\n".join(messages)
    assert "FORGED_SCOPE" not in rendered
    assert "FORGED_TENANT" not in rendered
    assert "FORGED_KIND" not in rendered
    assert "FORGED_TRIGGER" not in rendered
    assert any("Performance profile terminal resources=" in message for message in messages)


def test_tool_route_info_log_does_not_include_selected_tool_names():
    source = inspect.getsource(orchestrator.Orchestrator.run)
    log_block = source[source.index('logger.info(\n            "Tool route'):source.index('yield AgentEvent(type="routing"')]
    assert "active_names" not in log_block
    assert "tools=%s" not in log_block

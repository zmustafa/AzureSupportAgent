"""Tests for the run_performance_profile agent tool."""
import asyncio

import pytest

from app.agent import perfprofile_tool as pt
from app.connectors.base import ConnectorToolset


@pytest.fixture(autouse=True)
def _no_persist(monkeypatch):
    """Don't write to the real run-history file during tests."""
    from app.perfprofile import runs

    monkeypatch.setattr(runs, "save_run", lambda *a, **k: {})


def test_register_adds_tool():
    ts = ConnectorToolset()
    pt.register_profiler_tool(ts, workload_id="demo-amba-coverage", connection=None, tenant_id="t1")
    assert ts.has("run_performance_profile")
    assert ts.kind("run_performance_profile") == "read"


def test_demo_workload_profiles_and_summarizes():
    # The demo workload short-circuits to the deterministic demo snapshot (no Azure).
    config = {"workload_id": "demo-amba-coverage", "connection": None, "tenant_id": "t1", "actor": "test"}
    res = asyncio.run(pt._run_performance_profile(config, {}))
    assert res["isError"] is False
    text = res["content"][0]
    assert "Performance profile" in text
    assert "Workload score" in text
    assert "Resources profiled" in text


def test_missing_workload_is_error():
    res = asyncio.run(pt._run_performance_profile({"connection": None}, {}))
    assert res["isError"] is True
    assert "No workload in scope" in res["content"][0]


def test_unknown_workload_is_error():
    # A non-demo, non-existent workload id → not found.
    res = asyncio.run(pt._run_performance_profile({"connection": None}, {"workload_id": "does-not-exist"}))
    assert res["isError"] is True
    assert "not found" in res["content"][0]


def test_window_arg_passed_through_for_demo():
    config = {"workload_id": "demo-amba-coverage", "connection": None}
    res = asyncio.run(pt._run_performance_profile(config, {"window": "PT6H"}))
    assert res["isError"] is False
    assert "PT6H" in res["content"][0]


def test_tool_schema_is_read_only_no_required():
    tools = pt._tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "run_performance_profile"
    assert t.kind == "read"
    assert t.parameters["required"] == []

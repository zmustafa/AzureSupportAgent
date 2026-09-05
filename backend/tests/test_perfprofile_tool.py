"""Tests for the run_performance_profile agent tool."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.agent import perfprofile_tool as pt
from app.connectors.base import ConnectorToolset


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """This module uses JSON stores, not the application database bootstrap."""
    yield


@pytest.fixture(autouse=True)
def _isolated_profile_service(monkeypatch, tmp_path):
    """Seed the required workload and run the real service against temporary stores.

    Only the optional AI narrative is stubbed; workload lookup, demo metric evaluation,
    cache/history persistence and window handling retain their production behavior.
    """
    from cryptography.fernet import Fernet

    from app.core import config

    settings = config.Settings.model_construct(database_url="sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    # Some imported modules initialize encryption eagerly. Use an ephemeral test key,
    # never the developer's key file, if this is their first import in the test process.
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())

    from app.amba import demo, reference
    from app.core import app_settings, coverage_trends, jsonstore
    from app.perfprofile import cache, collector, narrative, runs
    from app.workloads import registry

    for module, filename in (
        (registry, "workloads.json"), (reference, "amba_reference.json"),
        (cache, "perfprofile_cache.json"), (runs, "perfprofile_runs.json"),
        (coverage_trends, "coverage_trends.json"),
    ):
        monkeypatch.setattr(module, "_PATH", tmp_path / filename)
    monkeypatch.setattr(reference, "_REV_PATH", tmp_path / "amba_revisions.json")
    # Exercise the actual local JSON locking/writes, never a configured PostgreSQL host.
    monkeypatch.setattr(jsonstore, "_postgres_connect_kwargs", lambda: None)
    monkeypatch.setattr(app_settings, "load_settings", lambda: dict(app_settings.DEFAULTS))

    async def offline_narrative(snapshot, *, sli_context=""):
        return narrative._fallback(snapshot)

    monkeypatch.setattr(narrative, "narrate", offline_narrative)
    monkeypatch.setattr(collector, "profile_workload", AsyncMock(
        side_effect=AssertionError("A demo profile must not collect live Azure metrics."),
    ))
    demo.ensure_demo_workload()


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
    from app.perfprofile import cache, runs

    saved = runs.list_runs("t1", "workload", "demo-amba-coverage")
    assert len(saved) == 1
    snapshot = cache.read_snapshot("t1", "workload", "demo-amba-coverage")
    assert snapshot is not None and snapshot["status"] == "succeeded"
    assert snapshot["scorecard"]["resources_profiled"] > 0


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
    from app.perfprofile import runs

    snapshot = runs.latest_run("default", "workload", "demo-amba-coverage")
    assert snapshot is not None and snapshot["requested_window"] == "PT6H"


def test_tool_schema_is_read_only_no_required():
    tools = pt._tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "run_performance_profile"
    assert t.kind == "read"
    assert t.parameters["required"] == []

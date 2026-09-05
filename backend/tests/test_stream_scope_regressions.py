"""Offline regressions for telemetry streams, credentials, and resiliency scope.

Use real handlers, JSON stores, the demo analysis pipeline and a temporary durable-job
database. Azure/AI I/O is blocked or supplied at the collector boundary, not by replacing
the API under test. No application lifespan, real registry, credential or database is used.
The repository-wide collection/bootstrap must also be run in an isolated environment.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio

TENANT = "stream-scope-test"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"
DEMO = "demo-amba-coverage"
READ_PATHS = (
    "analyze/job", "snapshot", "summary", "resources", "breaches",
    "workloads", "analysis", "trend", "export",
)


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """Override the global app-database bootstrap; durable tests have their own DB."""
    yield


@pytest.fixture
def _offline_state(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from app.core import config, jsonstore

    # model_construct deliberately bypasses BaseSettings' environment/.env sources.
    settings = config.Settings.model_construct(database_url="sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(jsonstore, "_postgres_connect_kwargs", lambda: None)
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())

    from app.amba import reference as amba_reference
    from app.azure import credentials
    from app.core import app_settings, azure_connections, coverage_runs, coverage_trends
    from app.resiliency import history, reference, snapshot
    from app.telemetry import cache, reference as telemetry_reference
    from app.workloads import registry

    for module, filename in (
        (azure_connections, "connections.json"), (registry, "workloads.json"),
        (app_settings, "settings.json"), (coverage_runs, "coverage_runs.json"),
        (coverage_trends, "coverage_trends.json"), (cache, "telemetry_cache.json"),
        (telemetry_reference, "telemetry_reference.json"),
        (amba_reference, "amba_reference.json"), (reference, "resiliency_reference.json"),
        (snapshot, "resiliency_snapshot.json"), (history, "resiliency_history.json"),
    ):
        monkeypatch.setattr(module, "_PATH", tmp_path / filename)
    for module, filename in (
        (amba_reference, "amba_revisions.json"),
        (telemetry_reference, "telemetry_revisions.json"),
        (reference, "resiliency_revisions.json"),
    ):
        monkeypatch.setattr(module, "_REV_PATH", tmp_path / filename)
    monkeypatch.setattr(cache, "_locks", {})
    monkeypatch.setattr(snapshot, "_locks", {})
    for name in ("_cli_token", "_managed_identity_token", "_sp_secret_token", "_sp_cert_token"):
        monkeypatch.setattr(credentials, name, AsyncMock(
            side_effect=AssertionError("Credential acquisition is forbidden in this offline test."),
        ))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", AsyncMock(
        side_effect=AssertionError("External HTTP is forbidden in this offline test."),
    ))
    return tmp_path


@pytest.fixture(autouse=True)
async def _owned_tasks(_offline_state, monkeypatch):
    from app.api import telemetry

    monkeypatch.setattr(telemetry, "_refresh_tasks", set())
    yield
    await telemetry.shutdown_refresh_tasks()


@pytest.fixture
def api_app(_offline_state):
    from app.api import resiliency, telemetry
    from app.core.db import get_db
    from app.core.security import Principal, get_principal

    principal = Principal(subject="offline", email="offline@example.invalid",
                          tenant_id=TENANT, role="admin")
    audit = SimpleNamespace(add=Mock(), commit=AsyncMock())

    async def audit_session():
        yield audit

    app = FastAPI()
    app.include_router(telemetry.router, prefix="/api")
    app.include_router(resiliency.router, prefix="/api")
    app.dependency_overrides[get_principal] = lambda: principal
    app.dependency_overrides[get_db] = audit_session
    return SimpleNamespace(app=app, principal=principal, audit=audit)


@pytest.fixture
async def client(api_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app.app), base_url="http://offline.invalid",
    ) as session:
        yield session


def _events(response):
    events = []
    for frame in response.text.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in fields and "data" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def _snapshot():
    from app.telemetry.collector import _empty_snapshot

    snap = _empty_snapshot("subscription", SUBSCRIPTION, error="")
    snap.update(coverage_pct=80, connection_configured=True)
    snap["kpis"]["total_resources_in_reference"] = 1
    snap["all_resources"] = [{"id": "offline-resource", "name": "offline-resource"}]
    return snap


def _assert_recorded(scope_kind="subscription", scope_id=SUBSCRIPTION):
    from app.core import coverage_runs, coverage_trends
    from app.telemetry import cache

    snap = cache.read_snapshot(TENANT, scope_kind, scope_id)
    assert snap is not None and not snap.get("error")
    runs = coverage_runs.list_runs("telemetry", TENANT, scope_kind, scope_id)
    points = coverage_trends.series("telemetry", TENANT, scope_kind, scope_id)
    assert len(runs) == len(points) == 1
    assert points[0]["pct"] == snap["coverage_pct"]
    assert coverage_runs.list_runs("telemetry", "other-tenant", scope_kind, scope_id) == []


def _assert_not_recorded():
    from app.core import coverage_runs, coverage_trends

    assert coverage_runs.list_runs("telemetry", TENANT, "subscription", SUBSCRIPTION) == []
    assert coverage_trends.series("telemetry", TENANT, "subscription", SUBSCRIPTION) == []


def _controlled_collector(monkeypatch, *, fail=False):
    from app.api import telemetry

    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def collect(_connection, *, progress=None, **_kwargs):
        try:
            started.set()
            if progress is not None:
                await progress(1, 2, "first-resource")
            await release.wait()
            if fail:
                raise RuntimeError("offline collector failed")
            if progress is not None:
                await progress(2, 2, "second-resource")
            return _snapshot()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    collector = AsyncMock(side_effect=collect)
    monkeypatch.setattr(telemetry, "collect_coverage", collector)
    return SimpleNamespace(started=started, release=release, cancelled=cancelled, call=collector)


async def test_telemetry_demo_stream_uses_real_pipeline_and_records_once(client):
    response = await client.post("/api/telemetry/refresh/stream", params={"workload_id": DEMO})
    events = _events(response)
    assert response.status_code == 200
    assert [event for event, _ in events] == ["start", "done"]
    assert events[-1][1]["demo"] is True
    assert events[-1][1]["all_resources"]
    _assert_recorded("workload", DEMO)


async def test_telemetry_progress_precedes_done_and_scan_is_not_repeated(client, monkeypatch):
    collector = _controlled_collector(monkeypatch)
    collector.release.set()
    response = await client.post("/api/telemetry/refresh/stream", params={"subscription_id": SUBSCRIPTION})
    events = _events(response)
    assert [event for event, _ in events] == ["start", "progress", "progress", "done"]
    assert [data["done"] for event, data in events if event == "progress"] == [1, 2]
    collector.call.assert_awaited_once()
    _assert_recorded()


@pytest.mark.parametrize("fail", [False, True])
async def test_telemetry_real_asgi_disconnect_detaches_only_the_stream(api_app, monkeypatch, caplog, fail):
    """SSE's actual AnyIO cancellation must not cancel collection or its persistence."""
    from app.api import telemetry

    collector = _controlled_collector(monkeypatch, fail=fail)
    disconnected = asyncio.Event()
    sent = []
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if b"event: progress" in message.get("body", b""):
            disconnected.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http", "root_path": "",
        "path": "/api/telemetry/refresh/stream", "headers": [],
        "query_string": f"subscription_id={SUBSCRIPTION}".encode(),
        "server": ("offline.invalid", 80), "client": ("127.0.0.1", 12345),
    }
    try:
        await asyncio.wait_for(api_app.app(scope, receive, send), timeout=5)
        assert disconnected.is_set()
        assert len(telemetry._refresh_tasks) == 1
        task = next(iter(telemetry._refresh_tasks))
        assert not task.done() and not collector.cancelled.is_set()
        collector.release.set()
        if fail:
            with pytest.raises(RuntimeError, match="offline collector failed"):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            _assert_not_recorded()
            assert "Telemetry refresh failed" in caplog.text
        else:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
            _assert_recorded()
        assert not telemetry._refresh_tasks
        assert not any(b"event: done" in message.get("body", b"") for message in sent)
        collector.call.assert_awaited_once()
    finally:
        collector.release.set()


async def test_telemetry_cancelled_post_still_records_without_using_request_db(api_app, monkeypatch):
    from app.api import telemetry

    collector = _controlled_collector(monkeypatch)
    request = asyncio.create_task(telemetry.refresh(
        workload_id=None, subscription_id=SUBSCRIPTION, connection_id=None,
        principal=api_app.principal, db=api_app.audit,
    ))
    try:
        await asyncio.wait_for(collector.started.wait(), timeout=5)
        task = next(iter(telemetry._refresh_tasks))
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not task.done() and not collector.cancelled.is_set()
        collector.release.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        _assert_recorded()
        api_app.audit.add.assert_not_called()
        api_app.audit.commit.assert_not_awaited()
        assert not telemetry._refresh_tasks
    finally:
        collector.release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("endpoint", ["refresh", "refresh/stream"])
async def test_failed_telemetry_attempt_never_records_old_or_empty_healthy_posture(
    client, monkeypatch, previous, endpoint,
):
    from app.api import telemetry
    from app.telemetry import cache
    from app.telemetry.collector import _empty_snapshot

    old = _snapshot()
    if previous:
        cache.write_snapshot(TENANT, "subscription", SUBSCRIPTION, old)
    failed = _empty_snapshot("subscription", SUBSCRIPTION, error="offline permission denied")
    collector = AsyncMock(return_value=failed)
    monkeypatch.setattr(telemetry, "collect_coverage", collector)
    response = await client.post(f"/api/telemetry/{endpoint}", params={"subscription_id": SUBSCRIPTION})
    assert response.status_code == 200
    snap = _events(response)[-1][1] if endpoint.endswith("stream") else response.json()
    assert snap["scan_error" if previous else "error"] == "offline permission denied"
    assert cache.read_snapshot(TENANT, "subscription", SUBSCRIPTION) == (old if previous else None)
    collector.assert_awaited_once()
    _assert_not_recorded()


async def test_telemetry_exception_has_one_error_terminal_and_no_retry(client, monkeypatch):
    from app.api import telemetry

    collector = AsyncMock(side_effect=RuntimeError("offline collector failed"))
    monkeypatch.setattr(telemetry, "collect_coverage", collector)
    response = await client.post("/api/telemetry/refresh/stream", params={"subscription_id": SUBSCRIPTION})
    assert [event for event, _ in _events(response)] == ["start", "error"]
    collector.assert_awaited_once()
    _assert_not_recorded()
    assert not telemetry._refresh_tasks


async def test_telemetry_shutdown_cancels_runner_and_releases_scope_lock(api_app, monkeypatch):
    from app.api import telemetry
    from app.telemetry import cache

    collector = _controlled_collector(monkeypatch)
    task = telemetry._start_refresh(telemetry._refresh_snapshot(
        api_app.principal, "subscription", SUBSCRIPTION,
    ))
    await asyncio.wait_for(collector.started.wait(), timeout=5)
    lock = cache.get_lock(TENANT, "subscription", SUBSCRIPTION)
    assert lock.locked()
    await telemetry.shutdown_refresh_tasks()
    assert task.cancelled() and collector.cancelled.is_set()
    assert not lock.locked() and not telemetry._refresh_tasks
    assert cache.read_snapshot(TENANT, "subscription", SUBSCRIPTION) is None
    _assert_not_recorded()


async def test_arm_none_fails_before_ambient_credential_detection(monkeypatch):
    from app.azure import credentials

    detect = Mock(side_effect=AssertionError("None must not probe the host identity."))
    monkeypatch.setattr(credentials, "_has_managed_identity", detect)
    token, error = await credentials.get_arm_token(None)
    assert token is None and error and "connection" in error.lower()
    detect.assert_not_called()
    credentials._cli_token.assert_not_awaited()
    credentials._managed_identity_token.assert_not_awaited()


async def test_resource_graph_none_connection_must_not_fall_back_to_ambient_cli(monkeypatch):
    """A missing connection must fail before ambient credential or CLI fallback.

    Keep get_arm_token real so this test covers the credential guard as well.
    """
    from app.exec import command_runner

    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("MSI_ENDPOINT", raising=False)
    probe = Mock(side_effect=AssertionError("A missing connection must not probe ambient Azure CLI."))
    monkeypatch.setattr(command_runner.shutil, "which", probe)
    result = await command_runner.run_kql_capture("Resources | take 1", None)
    assert result.ok is False and "connection" in result.error.lower()
    probe.assert_not_called()


@pytest.mark.parametrize("managed", [False, True])
async def test_arm_explicit_default_chain_still_uses_selected_host_identity(monkeypatch, managed):
    from app.azure import credentials

    cli = AsyncMock(return_value=("offline-cli-token", None))
    identity = AsyncMock(return_value=("offline-managed-token", None))
    monkeypatch.setattr(credentials, "_has_managed_identity", lambda: managed)
    monkeypatch.setattr(credentials, "_cli_token", cli)
    monkeypatch.setattr(credentials, "_managed_identity_token", identity)
    token, error = await credentials.get_arm_token({
        "auth_method": "default_chain", "tenant_id": "offline-tenant", "client_id": "offline-client",
    })
    assert error is None
    if managed:
        assert token == "offline-managed-token"
        identity.assert_awaited_once_with("offline-client")
        cli.assert_not_awaited()
    else:
        assert token == "offline-cli-token"
        cli.assert_awaited_once_with("offline-tenant")
        identity.assert_not_awaited()


@pytest.mark.parametrize("path", READ_PATHS)
@pytest.mark.parametrize("params", [{}, {"workload_id": "   "}, {
    "workload_id": DEMO, "subscription_id": SUBSCRIPTION,
}])
async def test_resiliency_required_scope_errors_are_400(client, path, params):
    response = await client.get(f"/api/resiliency/{path}", params=params)
    assert response.status_code == 400
    assert response.json()["detail"]


async def test_resiliency_analyze_start_requires_scope_before_job_creation(client, monkeypatch):
    from app.api import resiliency

    start = AsyncMock(side_effect=AssertionError("Invalid scope must not create a job."))
    monkeypatch.setattr(resiliency._job_executor, "start", start)
    response = await client.post("/api/resiliency/analyze/start")
    assert response.status_code == 400
    start.assert_not_awaited()


@pytest.mark.parametrize("demo_scope", [False, True])
@pytest.mark.parametrize("connection_id, expected", [("missing", 404), ("disabled", 400), ("other", 400)])
async def test_resiliency_explicit_connection_is_never_bypassed_for_demo_ids(
    client, demo_scope, connection_id, expected,
):
    from app.amba import demo
    from app.core import azure_connections
    from app.workloads import registry

    workload_id = DEMO if demo_scope else "offline-live-workload"
    if demo_scope:
        demo.ensure_demo_workload()
    # A matching but disabled canonical link tests the disabled check itself, rather
    # than accidentally getting the same 400 from the earlier mismatched-link check.
    canonical_id = "disabled" if connection_id == "disabled" else "canonical"
    registry.upsert_workload({"id": workload_id, "connection_id": canonical_id})
    azure_connections.upsert_connection({"id": "canonical", "auth_method": "default_chain", "is_default": True})
    azure_connections.upsert_connection({"id": "disabled", "auth_method": "default_chain", "disabled": True})
    azure_connections.upsert_connection({"id": "other", "auth_method": "default_chain"})
    params = {"workload_id": workload_id, "connection_id": connection_id}
    detail = {"missing": "not found", "disabled": "disabled", "other": "different Azure connection"}[connection_id]
    for path in READ_PATHS:
        response = await client.get(f"/api/resiliency/{path}", params=params)
        assert response.status_code == expected, (path, response.text)
        assert detail in response.json()["detail"]
    response = await client.post("/api/resiliency/analyze/start", params=params)
    assert response.status_code == expected


@pytest.mark.parametrize("scope", [{"subscription_id": SUBSCRIPTION}, {"management_group_id": "offline-mg"}])
async def test_resiliency_real_scope_still_requires_a_connection(client, scope):
    response = await client.get("/api/resiliency/snapshot", params=scope)
    assert response.status_code == 400
    assert "connection" in response.json()["detail"].lower()


@pytest.mark.parametrize("disabled", [False, True])
async def test_resiliency_canonical_live_connection_cannot_fall_back_to_default(client, disabled):
    from app.core import azure_connections
    from app.workloads import registry

    registry.upsert_workload({"id": "offline-live-workload", "connection_id": "canonical"})
    azure_connections.upsert_connection({"id": "other", "auth_method": "default_chain", "is_default": True})
    if disabled:
        azure_connections.upsert_connection({"id": "canonical", "auth_method": "default_chain", "disabled": True})
    response = await client.get("/api/resiliency/snapshot", params={"workload_id": "offline-live-workload"})
    assert response.status_code == 400
    assert "connection" in response.json()["detail"].lower()


async def test_resiliency_unknown_live_workload_is_404_even_with_a_default_connection(client):
    from app.core import azure_connections

    azure_connections.upsert_connection({"id": "other", "auth_method": "default_chain", "is_default": True})
    for path in READ_PATHS:
        response = await client.get(f"/api/resiliency/{path}", params={"workload_id": "missing-workload"})
        assert response.status_code == 404
        assert "workload was not found" in response.json()["detail"]


async def test_resiliency_demo_read_is_cache_only_without_connection(client, monkeypatch):
    from app.amba import demo
    from app.api import resiliency
    from app.backup_manager import service

    demo.ensure_demo_workload()
    resolver = Mock(side_effect=AssertionError("An implicit demo selection needs no live resolver."))
    analyze = AsyncMock(side_effect=AssertionError("GET must not start analysis."))
    monkeypatch.setattr(service, "resolve_selected_connection", resolver)
    monkeypatch.setattr(resiliency.analyze_mod, "analyze", analyze)
    response = await client.get("/api/resiliency/snapshot", params={"workload_id": DEMO})
    assert response.status_code == 200
    assert response.json()["report_exists"] is False
    resolver.assert_not_called()
    analyze.assert_not_awaited()


async def test_resiliency_demo_start_runs_real_durable_analysis_without_credentials(
    client, monkeypatch, durable_job_sessions,
):
    from app.amba import demo
    from app.api import resiliency
    from app.backup_manager import service
    from app.core.durable_jobs import DurableJobExecutor
    from app.resiliency import model

    demo.ensure_demo_workload()
    executor = DurableJobExecutor("resiliency.analyze", session_factory=durable_job_sessions, poll_seconds=0.01)
    monkeypatch.setattr(resiliency, "_job_executor", executor)
    resolver = Mock(side_effect=AssertionError("An implicit demo selection needs no live resolver."))
    subscriptions = AsyncMock(side_effect=AssertionError("Demo subscriptions must not query Azure."))
    monkeypatch.setattr(service, "resolve_selected_connection", resolver)
    monkeypatch.setattr(service, "scope_subscriptions", subscriptions)
    try:
        response = await client.post("/api/resiliency/analyze/start", params={"workload_id": DEMO})
        assert response.status_code == 200
        key = f"{TENANT}||workload|{DEMO}"
        durable = await executor.store.load_current(tenant_id=TENANT, feature=executor.feature, key=key)
        assert durable is not None
        terminal = await asyncio.wait_for(executor.store.wait_for_terminal(durable["id"]), timeout=5)
        assert terminal is not None and terminal["status"] == "done"
        assert terminal["result"]["snapshot_written"] is True

        snapshot_response = await client.get("/api/resiliency/snapshot", params={"workload_id": DEMO})
        snap = snapshot_response.json()
        assert snapshot_response.status_code == 200
        assert snap["report_exists"] is True and snap["demo"] is True
        assert snap["resources"] and snap["portal_host"] == ""
        cosmos = next(row for row in snap["resources"] if row["name"] == "contoso-guests-cosmos")
        assert cosmos["verdicts"][model.SCENARIO_DATA_CORRUPTION]["rpo_minutes"] == 1440
        assert cosmos["verdicts"][model.SCENARIO_ZONE_LOSS]["rto_class"] == model.RTO_AUTOMATIC
        job = await client.get("/api/resiliency/analyze/job", params={"workload_id": DEMO})
        assert job.json()["job"]["status"] == "done"
        resolver.assert_not_called()
        subscriptions.assert_not_awaited()
    finally:
        await executor.stop()
"""Change Explorer admission, throttling, paging, and source-fidelity tests (offline)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_analysis_admission_serializes_one_principal_lane():
    from app.changeexplorer import admission

    admission.reset_for_tests()
    active = 0
    peak = 0

    async def one() -> None:
        nonlocal active, peak
        async with admission.analysis_slot("tenant", {"tenant_id": "tenant", "client_id": "same"}):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(one() for _ in range(4)))
    assert peak == 1


@pytest.mark.asyncio
async def test_analysis_admission_has_a_global_ceiling_of_two():
    from app.changeexplorer import admission

    admission.reset_for_tests()
    active = 0
    peak = 0

    async def one(index: int) -> None:
        nonlocal active, peak
        async with admission.analysis_slot("tenant", {"tenant_id": "tenant", "client_id": str(index)}):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1

    await asyncio.gather(*(one(i) for i in range(5)))
    assert peak == 2


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class _DistributedState:
    """Shared PostgreSQL-row model representing several application replicas."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.row = None


class _DistributedSession:
    def __init__(self, state: _DistributedState):
        self.state = state
        self.pending = None
        self.acquired = False

    async def execute(self, statement, _params=None):
        if "pg_advisory_xact_lock" in str(statement):
            await self.state.lock.acquire()
            self.acquired = True
            return _Scalar(None)
        return _Scalar(self.state.row)

    def add(self, row):
        self.pending = row


class _DistributedTransaction:
    def __init__(self, state: _DistributedState):
        self.state = state
        self.session = _DistributedSession(state)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        if self.session.pending is not None:
            self.state.row = self.session.pending
        if self.session.acquired:
            self.state.lock.release()


class _DistributedSessions:
    def __init__(self, state: _DistributedState):
        self.state = state

    def begin(self):
        return _DistributedTransaction(self.state)


@pytest.mark.asyncio
async def test_distributed_arg_pacing_coordinates_concurrent_replicas(monkeypatch):
    from app.azure import arg_throttle
    from app.core import db

    state = _DistributedState()
    monkeypatch.setattr(db, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    monkeypatch.setattr(db, "SessionLocal", _DistributedSessions(state))
    finished: list[float] = []
    started = time.monotonic()

    async def one():
        await arg_throttle._distributed_acquire("same-principal", 4, 0.16)
        finished.append(time.monotonic() - started)

    await asyncio.gather(*(one() for _ in range(6)))
    finished.sort()
    assert len(finished) == 6
    assert finished[-1] >= 0.14, finished
    for start in finished:
        assert sum(start <= value < start + 0.15 for value in finished) <= 4, finished


@pytest.mark.asyncio
async def test_distributed_arg_block_is_seen_by_another_replica(monkeypatch):
    from app.azure import arg_throttle
    from app.core import db

    state = _DistributedState()
    monkeypatch.setattr(db, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    monkeypatch.setattr(db, "SessionLocal", _DistributedSessions(state))

    await arg_throttle._distributed_block("shared-principal", 0.06)
    started = time.monotonic()
    await arg_throttle._distributed_acquire("shared-principal", 12, 0.2)
    assert time.monotonic() - started >= 0.045


class _ActivityResponse:
    def __init__(self, status: int, payload: dict, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


class _ActivityClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_activity_log_rest_retries_429_and_reports_retry(monkeypatch):
    from app.azure import arm

    client = _ActivityClient([
        _ActivityResponse(429, {"error": {"message": "TooManyRequests"}}, {"Retry-After": "0.001"}),
        _ActivityResponse(200, {"value": [{"id": "event"}]}),
    ])
    monkeypatch.setattr(arm.httpx, "AsyncClient", lambda **_kwargs: client)
    retries: list[tuple[int, int]] = []

    async def on_retry(status: int, attempt: int, _delay: float):
        retries.append((status, attempt))

    rows, error = await arm.list_activity_log_events(
        "token", "subscription", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
        on_retry=on_retry,
    )
    assert error is None
    assert rows == [{"id": "event"}]
    assert client.calls == 2
    assert retries == [(429, 1)]


@pytest.mark.asyncio
async def test_resource_changes_uses_paged_collection(monkeypatch):
    from app.changeexplorer import collectors
    from app.exec.command_runner import KqlResult

    seen = {}

    async def fake_collect(kql, _connection, **kwargs):
        seen["kql"] = kql
        seen.update(kwargs)
        return KqlResult(ok=True, rows=[], complete=True, pages=2)

    monkeypatch.setattr("app.exec.command_runner.run_kql_collect", fake_collect)
    rows, note = await collectors.collect_resource_graph_changes(
        "subscriptionId =~ 's'", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", {},
    )
    assert rows == [] and note == ""
    assert seen["max_rows"] == collectors.change_limit()
    assert "take 5000" not in seen["kql"]
    assert "order by ts desc, targetId asc" in seen["kql"]


def test_source_provenance_distinguishes_retryable_partial_results():
    from app.changeexplorer.service import _source_provenance

    throttled = _source_provenance(
        "ActivityLog", [{"id": 1}], "ARM 429: TooManyRequests. Retry-After: 3s.", required=True,
    )
    assert throttled["status"] == "partial"
    assert throttled["throttled"] is True
    assert throttled["retryable"] is True
    assert throttled["required"] is True

    optional = _source_provenance(
        "EntraAudit", [], "Entra ID audit events not included — no Graph token.", required=False,
    )
    assert optional["status"] == "unavailable"
    assert optional["retryable"] is False


@pytest.mark.asyncio
async def test_analysis_pipeline_marks_throttled_required_source_partial(monkeypatch):
    from app.changeexplorer import admission, service

    admission.reset_for_tests()

    async def fake_scope(*_args, **_kwargs):
        return {"predicate": "subscriptionId =~ 's'", "subscriptions": ["s"],
                "resource_ids": [], "mode": "workload", "error": ""}

    async def throttled_rg(*_args, **_kwargs):
        return [], "Resource Graph change history unavailable: Resource Graph 429: RateLimiting"

    async def complete_activity(*_args, **_kwargs):
        return [], ""

    async def optional_entra(*_args, **_kwargs):
        return [], "Entra ID audit events not included — no Graph token."

    monkeypatch.setattr("app.changeexplorer.scope.build_scope", fake_scope)
    monkeypatch.setattr("app.changeexplorer.collectors.collect_resource_graph_changes", throttled_rg)
    monkeypatch.setattr("app.changeexplorer.collectors.collect_activity_log", complete_activity)
    monkeypatch.setattr("app.changeexplorer.entra.collect_entra_audits", optional_entra)

    run = await service.analyze(
        tenant_id="tenant", workload={"id": "workload", "name": "Workload", "nodes": []},
        connection={"tenant_id": "tenant", "client_id": "principal"},
        start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-02T00:00:00Z",
        scope_mode="workload", requested_by="tester", run_ai=False,
    )

    assert run["status"] == "partial"
    assert run["analysisOutcome"] == "partial"
    assert run["retryable"] is True
    assert run["sourceProvenance"]["resourceGraph"]["throttled"] is True
    assert run["sourceProvenance"]["activityLog"]["complete"] is True
    assert run["sourceProvenance"]["entraAudit"]["required"] is False


@pytest.mark.asyncio
async def test_recent_exact_window_is_coalesced_before_collecting(monkeypatch):
    from datetime import datetime, timezone

    from app.changeexplorer import admission, service

    admission.reset_for_tests()
    existing = {
        "runId": "existing", "status": "succeeded", "analysisOutcome": "complete",
        "startTime": "2026-01-01T00:00:00Z", "endTime": "2026-01-02T00:00:00Z",
        "scopeMode": "workload", "completedAt": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setattr("app.changeexplorer.runs.list_runs", lambda *_args, **_kwargs: [existing])
    monkeypatch.setattr("app.changeexplorer.runs.get_run", lambda *_args, **_kwargs: existing)

    async def must_not_collect(*_args, **_kwargs):
        raise AssertionError("an exact concurrent duplicate must reuse the completed run")

    monkeypatch.setattr(service, "_collect_raw", must_not_collect)
    run = await service.analyze(
        tenant_id="tenant", workload={"id": "workload", "name": "Workload"},
        connection={"tenant_id": "tenant", "client_id": "principal"},
        start_iso=existing["startTime"], end_iso=existing["endTime"],
        scope_mode="workload", requested_by="tester", run_ai=False,
    )
    assert run is existing
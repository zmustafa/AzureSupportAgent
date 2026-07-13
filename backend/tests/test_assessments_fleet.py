from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api import assessments
from app.core.security import Principal

_PRINCIPAL = Principal(
    subject="assessment-fleet-test",
    email="fleet@example.com",
    tenant_id="assessment-fleet-tenant",
    role="admin",
)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return _Result(self.rows)


def _run(*, run_id: str, workload_id: str, status: str, score=None, age_hours=0, tenant_id=None, deleted_at=None):
    started_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return SimpleNamespace(
        id=run_id,
        workload_id=workload_id,
        tenant_id=tenant_id or _PRINCIPAL.tenant_id,
        connection_id=f"connection-{workload_id}",
        status=status,
        overall_score=score,
        scores_json={"security": {"score": score}} if score is not None else {},
        totals_json={"passed": 8, "failed": 2, "na": 1, "by_severity": {"critical": 1, "error": 2}},
        severity="critical",
        resource_count=12,
        completeness_pct=90,
        confidence="high",
        is_baseline=True,
        error="latest run failed" if status == "failed" else None,
        started_at=started_at,
        deleted_at=deleted_at,
    )


async def test_assessment_fleet_maps_latest_success_and_current_status(monkeypatch):
    monkeypatch.setattr("app.workloads.registry.list_workloads", lambda: [
        {"id": "w-failed", "name": "Failed now", "connection_id": "c-failed", "criticality": "critical", "environment": "production"},
        {"id": "w-low", "name": "Low score", "connection_id": "c-low", "criticality": "high", "environment": "staging"},
        {"id": "w-never", "name": "Never", "connection_id": "c-never", "criticality": "low", "environment": "development"},
    ])
    db = _Db([
        _run(run_id="failed-new", workload_id="w-failed", status="failed", age_hours=1),
        _run(run_id="failed-success", workload_id="w-failed", status="succeeded", score=72, age_hours=2),
        _run(run_id="low-success", workload_id="w-low", status="succeeded", score=25, age_hours=3),
    ])

    result = await assessments.fleet_endpoint(_PRINCIPAL, db)

    assert db.calls == 1
    assert result["total"] == 3
    assert result["scanned"] == 2
    assert [row["workload_id"] for row in result["workloads"]] == ["w-failed", "w-low", "w-never"]
    failed = result["workloads"][0]
    assert failed["current_run_id"] == "failed-new"
    assert failed["current_status"] == "failed"
    assert failed["run_id"] == "failed-success"
    assert failed["overall_score"] == 72
    assert failed["pillar_scores"] == {"security": 72}
    assert failed["failed"] == 2
    assert failed["findings_by_severity"] == {"critical": 1, "error": 2, "warning": 0, "info": 0}
    assert failed["resources"] == 12
    assert failed["completeness_pct"] == 90
    assert failed["confidence"] == "high"
    assert failed["is_baseline"] is True
    assert failed["error"] == "latest run failed"

    never = result["workloads"][-1]
    assert never["has_scan"] is False
    assert never["current_status"] == "never"
    assert never["overall_score"] is None
    assert never["resources"] is None
    assert never["failed"] is None
    assert never["age_seconds"] is None
    assert never["stale"] is True


async def test_assessment_fleet_is_database_only_and_ignores_non_active_runs(monkeypatch):
    monkeypatch.setattr("app.workloads.registry.list_workloads", lambda: [
        {"id": "active", "name": "Active", "connection_id": "registry-connection", "criticality": "medium", "environment": "test"},
    ])
    # The endpoint receives only tenant-filtered, non-deleted rows from SQL. This extra
    # inactive-workload row proves it is not surfaced even if a test double returns it.
    db = _Db([
        _run(run_id="active-run", workload_id="active", status="succeeded", score=88, age_hours=24 * 31),
        _run(run_id="inactive-run", workload_id="inactive", status="succeeded", score=1),
    ])
    monkeypatch.setattr(assessments, "run_assessment", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("Fleet must never invoke the assessment runner")
    ))

    result = await assessments.fleet_endpoint(_PRINCIPAL, db)

    assert [row["workload_id"] for row in result["workloads"]] == ["active"]
    row = result["workloads"][0]
    assert row["connection_id"] == "registry-connection"
    assert row["overall_score"] == 88
    assert row["stale"] is True

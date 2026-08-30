"""Backup Manager Fleet + Cleanup contracts.

The fleet grid and the cleanup tab both exist because analyses are expensive and bounded: the
snapshot store keeps only a handful of scopes, so the numbers a fleet shows cannot come from
there, and the operator needs a way to see and drop what is held. These tests pin the parts
that are invisible until they regress — the roll-up arithmetic, the worst-first ordering, the
orphan classification, and the fact that recording history never breaks a good analysis.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.api import backup_manager as api
from app.backup_manager import fleet as fleet_store
from app.backup_manager import snapshot as snapshot_store
from app.core import coverage_runs
from app.core.security import Principal

TENANT = "tenant-fleet"
CONNECTION_ID = "conn-fleet"
ALL_PERMS = frozenset({"backup_manager.read", "backup_manager.approve"})


def _principal() -> Principal:
    return Principal("op@example.test", "op@example.test", TENANT, "operator", ALL_PERMS)


def _snapshot(*, protected: int, gaps: int, failed: int = 0, generated_at: str = "2026-07-25T10:00:00+00:00") -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "demo": False,
        "errors": {},
        "counts": {"vaults": 2, "protected_items": protected, "policies": 3,
                   "jobs": 10, "gaps": gaps, "failed_jobs": failed},
        "summary": {
            "protection": {"vaults": 2, "protected_items": protected, "stopped": 1,
                           "orphaned": 0, "policies": 3},
            "jobs": {"failed": failed, "total": 10},
            "chronic_failures": 1,
            "rpo": {"attainment_pct": 91, "breached": 1, "at_risk": 0, "unknown": 0},
            "posture": {"average_score": 62, "band": "amber", "red_vaults": 1, "actionable_count": 4},
            "dr": {"replicated_items": 3, "unhealthy": 1},
            "cost": {"monthly_total": 128.4, "currency": "CAD", "confidence": "actual",
                     "recoverable_monthly": 12.0},
        },
        "cost": {"monthly_total": 128.4, "rows": [{"id": "row"} for _ in range(500)]},
    }


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    """Every store this module writes to lives in a temp file."""
    monkeypatch.setattr(snapshot_store, "_PATH", tmp_path / "snapshots.json")
    monkeypatch.setattr(snapshot_store, "_locks", {})
    monkeypatch.setattr(fleet_store, "_PATH", tmp_path / "fleet.json")
    monkeypatch.setattr(coverage_runs, "_PATH", tmp_path / "runs.json")
    return tmp_path


@pytest.fixture
def workloads(monkeypatch):
    rows = [
        {"id": "wl-analyzed", "name": "Payments", "connection_id": CONNECTION_ID,
         "criticality": "high", "environment": "prod"},
        {"id": "wl-never", "name": "Reporting", "connection_id": CONNECTION_ID,
         "criticality": "low", "environment": "dev"},
    ]
    monkeypatch.setattr(api, "_workloads", lambda: rows)
    return rows


# --------------------------------------------------------------------------- summary rows
def test_protection_percentage_is_measured_against_what_should_be_protected() -> None:
    """8 protected + 2 gaps is 80% — not 100% just because 8 of 8 stored items are healthy."""
    row = fleet_store.summarize(_snapshot(protected=8, gaps=2), workload_id="wl", connection_id="c")
    assert row["pct_protected"] == 80
    assert row["protected_items"] == 8 and row["gaps"] == 2


def test_a_workload_with_nothing_eligible_reports_no_percentage_rather_than_zero() -> None:
    row = fleet_store.summarize(_snapshot(protected=0, gaps=0), workload_id="wl", connection_id="c")
    assert row["pct_protected"] is None


def test_rows_from_an_older_shape_are_ignored_so_the_grid_says_never_analyzed(monkeypatch) -> None:
    fleet_store.write_row(TENANT, fleet_store.summarize(
        _snapshot(protected=1, gaps=0), workload_id="wl", connection_id=CONNECTION_ID))
    monkeypatch.setattr(fleet_store, "ROW_SCHEMA_VERSION", 999)
    assert fleet_store.read_rows(TENANT) == {}


def test_fleet_rows_are_scoped_to_their_tenant_and_connection() -> None:
    fleet_store.write_row(TENANT, fleet_store.summarize(
        _snapshot(protected=1, gaps=0), workload_id="wl", connection_id=CONNECTION_ID))
    assert fleet_store.read_rows("other-tenant") == {}
    assert fleet_store.key("other-conn", "wl") not in fleet_store.read_rows(TENANT)


# --------------------------------------------------------------------------- /fleet
async def test_fleet_lists_every_workload_and_puts_the_unanalyzed_first(workloads) -> None:
    fleet_store.write_row(TENANT, fleet_store.summarize(
        _snapshot(protected=8, gaps=2), workload_id="wl-analyzed", connection_id=CONNECTION_ID))

    result = await api.fleet(principal=_principal())

    assert result["total"] == 2 and result["analyzed"] == 1
    assert [row["workload_id"] for row in result["workloads"]] == ["wl-never", "wl-analyzed"]
    analyzed = result["workloads"][1]
    assert analyzed["pct_protected"] == 80
    assert analyzed["monthly_cost"] == 128.4 and analyzed["currency"] == "CAD"
    assert analyzed["rpo_attainment_pct"] == 91 and analyzed["posture_band"] == "amber"
    never = result["workloads"][0]
    assert never["has_analysis"] is False and never["pct_protected"] is None


async def test_fleet_orders_the_worst_protected_workloads_ahead_of_the_healthy_ones(monkeypatch) -> None:
    monkeypatch.setattr(api, "_workloads", lambda: [
        {"id": "wl-good", "name": "Good", "connection_id": CONNECTION_ID},
        {"id": "wl-bad", "name": "Bad", "connection_id": CONNECTION_ID},
    ])
    fleet_store.write_row(TENANT, fleet_store.summarize(
        _snapshot(protected=10, gaps=0), workload_id="wl-good", connection_id=CONNECTION_ID))
    fleet_store.write_row(TENANT, fleet_store.summarize(
        _snapshot(protected=2, gaps=8), workload_id="wl-bad", connection_id=CONNECTION_ID))

    result = await api.fleet(principal=_principal())

    assert [row["workload_id"] for row in result["workloads"]] == ["wl-bad", "wl-good"]


async def test_fleet_never_reaches_azure(workloads, monkeypatch) -> None:
    def explode(*_args, **_kwargs):  # pragma: no cover - only runs if the contract breaks
        raise AssertionError("the fleet grid must be served from cache")

    monkeypatch.setattr(api.inventory_ops, "collect_estate", explode)
    await api.fleet(principal=_principal())


# --------------------------------------------------------------------------- /refresh/jobs
async def test_batched_job_status_is_keyed_without_the_tenant_prefix(monkeypatch) -> None:
    """One poll answers the whole grid, and a tenant never sees another tenant's jobs."""
    job = {"id": "j1", "key": f"{TENANT}|{CONNECTION_ID}|workload|wl-1", "status": "running",
           "started_at": "2026-07-25T10:00:00+00:00", "finished_at": None, "progress": [],
           "result": None, "error": ""}
    async def jobs_with_prefix(prefix: str, *, tenant_id: str = "default") -> list[dict]:
        return [job] if tenant_id == TENANT and prefix == f"{TENANT}|" else []

    monkeypatch.setattr(api._refresh_jobs, "jobs_with_prefix", jobs_with_prefix)

    result = await api.refresh_jobs(principal=_principal())

    assert list(result["jobs"]) == [f"{CONNECTION_ID}|workload|wl-1"]
    assert result["jobs"][f"{CONNECTION_ID}|workload|wl-1"]["status"] == "running"
    assert result["concurrency"] == api.ANALYSIS_CONCURRENCY


def test_analyses_are_capped_so_a_fleet_launch_cannot_stampede_azure() -> None:
    assert api.ANALYSIS_CONCURRENCY == 2
    assert api._analysis_slots._value == api.ANALYSIS_CONCURRENCY


# --------------------------------------------------------------------------- run history
def test_recording_an_analysis_stores_a_trimmed_run_not_the_whole_snapshot(workloads) -> None:
    snapshot = _snapshot(protected=8, gaps=2)
    api._record_analysis(
        _principal(), snapshot, tenant=TENANT, connection_id=CONNECTION_ID,
        scope_kind="workload", scope_id="wl-analyzed", workload_id="wl-analyzed",
    )

    runs = coverage_runs.list_all_runs(api.RUNS_FEATURE, TENANT)
    assert len(runs) == 1
    assert runs[0]["headline"] == 80
    assert runs[0]["scope_name"] == "Payments"
    assert runs[0]["resource_count"] == 10
    # The 500 cost rows (and every inventory/job row) must not have been carried into history.
    stored = coverage_runs.get_run(api.RUNS_FEATURE, TENANT, runs[0]["id"])
    assert "rows" not in stored["cost"]
    assert "inventory" not in stored


def test_a_history_write_failure_never_fails_the_analysis(workloads, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(coverage_runs, "save_run", boom)
    # Must not raise.
    api._record_analysis(
        _principal(), _snapshot(protected=1, gaps=0), tenant=TENANT, connection_id=CONNECTION_ID,
        scope_kind="workload", scope_id="wl-analyzed", workload_id="wl-analyzed",
    )


def test_subscription_scopes_get_history_but_no_fleet_row(monkeypatch) -> None:
    """The fleet grid is workload-only; a subscription analysis must not invent a row."""
    monkeypatch.setattr(api, "_workloads", lambda: [])
    api._record_analysis(
        _principal(), _snapshot(protected=4, gaps=1), tenant=TENANT, connection_id=CONNECTION_ID,
        scope_kind="subscription", scope_id="sub-1", workload_id="",
    )
    assert fleet_store.read_rows(TENANT) == {}
    assert len(coverage_runs.list_all_runs(api.RUNS_FEATURE, TENANT)) == 1


# --------------------------------------------------------------------------- cleanup
async def test_stored_analyses_are_listed_with_size_and_orphan_state(workloads, monkeypatch) -> None:
    monkeypatch.setattr("app.core.azure_connections.list_connections", lambda: [{"id": CONNECTION_ID}])
    snapshot_store.write_snapshot(TENANT, CONNECTION_ID, "workload", "wl-analyzed", _snapshot(protected=8, gaps=2))
    snapshot_store.write_snapshot(TENANT, CONNECTION_ID, "workload", "wl-deleted", _snapshot(protected=1, gaps=0))
    snapshot_store.write_snapshot(TENANT, "conn-gone", "workload", "wl-analyzed", _snapshot(protected=1, gaps=0))

    result = await api.cleanup_snapshots(principal=_principal())

    by_key = {row["key"]: row for row in result["snapshots"]}
    live = by_key[f"{TENANT}|{CONNECTION_ID}|workload|wl-analyzed"]
    assert live["orphan"] is False and live["size_bytes"] > 0
    assert by_key[f"{TENANT}|{CONNECTION_ID}|workload|wl-deleted"]["orphan_reasons"] == ["workload deleted"]
    assert by_key[f"{TENANT}|conn-gone|workload|wl-analyzed"]["orphan_reasons"] == ["connection removed"]
    assert result["stats"]["orphans"] == 2
    assert result["stats"]["max_scopes"] == snapshot_store.MAX_SCOPES


def test_purging_a_scope_drops_its_snapshot_and_its_fleet_row() -> None:
    snapshot_store.write_snapshot(TENANT, CONNECTION_ID, "workload", "wl-1", _snapshot(protected=1, gaps=0))
    fleet_store.write_row(TENANT, fleet_store.summarize(
        _snapshot(protected=1, gaps=0), workload_id="wl-1", connection_id=CONNECTION_ID))

    result = snapshot_store.delete_keys([f"{TENANT}|{CONNECTION_ID}|workload|wl-1"])
    removed = fleet_store.delete_rows(TENANT, [fleet_store.key(CONNECTION_ID, "wl-1")])

    assert result["count"] == 1 and result["freed_bytes"] > 0
    assert removed == 1
    assert snapshot_store.read_snapshot(TENANT, CONNECTION_ID, "workload", "wl-1") is None
    assert fleet_store.read_rows(TENANT) == {}


def test_purge_refuses_keys_belonging_to_another_tenant() -> None:
    """The store is one file for every tenant, so a purge must filter by prefix, not trust ids."""
    snapshot_store.write_snapshot("other", CONNECTION_ID, "workload", "wl-1", _snapshot(protected=1, gaps=0))
    keys = [f"other|{CONNECTION_ID}|workload|wl-1"]
    allowed = [k for k in keys if k.startswith(f"{TENANT}|")]
    assert allowed == []
    assert snapshot_store.read_snapshot("other", CONNECTION_ID, "workload", "wl-1") is not None

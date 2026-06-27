"""Tests for the cross-scope run Cleanup helpers (perf / change / coverage stores)."""
from __future__ import annotations

import pytest


# ----------------------------------------------------------------- perfprofile
def test_perf_cleanup(tmp_path, monkeypatch):
    from app.perfprofile import runs

    monkeypatch.setattr(runs, "_PATH", tmp_path / "perf.json")
    runs.save_run("t1", "workload", "w1", {"scope_kind": "workload", "scope_id": "w1", "scope_name": "W1", "scorecard": {"workload_score": 80}})
    runs.save_run("t1", "workload", "w2", {"scope_kind": "workload", "scope_id": "w2", "scope_name": "W2", "scorecard": {"workload_score": 50}})
    runs.save_run("t1", "subscription", "s1", {"scope_kind": "subscription", "scope_id": "s1", "scope_name": "Sub", "scorecard": {"workload_score": 90}})

    all_runs = runs.list_all_runs("t1")
    assert len(all_runs) == 3
    assert all(r["size_bytes"] > 0 for r in all_runs)
    assert {r["scope_id"] for r in all_runs} == {"w1", "w2", "s1"}

    stats = runs.cleanup_stats("t1")
    assert stats["total_runs"] == 3 and stats["active_runs"] == 3 and stats["trashed_runs"] == 0
    assert stats["scopes"] == 3 and stats["total_bytes"] > 0

    ids = [r["id"] for r in all_runs[:2]]
    res = runs.trash_runs("t1", ids)
    assert res["count"] == 2 and res["freed_bytes"] > 0
    assert runs.cleanup_stats("t1")["trashed_runs"] == 2

    assert runs.restore_runs("t1", [ids[0]])["count"] == 1
    assert runs.cleanup_stats("t1")["trashed_runs"] == 1

    purged = runs.purge_runs("t1", ids)
    assert purged["count"] == 2
    assert runs.cleanup_stats("t1")["total_runs"] == 1   # only s1 left


# ----------------------------------------------------------------- changeexplorer
def test_change_cleanup(tmp_path, monkeypatch):
    from app.changeexplorer import runs

    monkeypatch.setattr(runs, "_PATH", tmp_path / "change.json")
    runs.save_run("t1", "w1", {"runId": "r1", "workloadId": "w1", "workloadName": "W1", "completedAt": "2026-06-26T00:00:00", "totalChanges": 5})
    runs.save_run("t1", "w2", {"runId": "r2", "workloadId": "w2", "workloadName": "W2", "completedAt": "2026-06-25T00:00:00", "totalChanges": 0})

    all_runs = runs.list_all_runs("t1")
    assert len(all_runs) == 2 and all(r["size_bytes"] > 0 for r in all_runs)
    assert runs.cleanup_stats("t1")["scopes"] == 2

    assert runs.trash_runs("t1", ["r1"])["count"] == 1
    assert runs.cleanup_stats("t1")["trashed_runs"] == 1
    assert runs.restore_runs("t1", ["r1"])["count"] == 1
    assert runs.purge_runs("t1", ["r1", "r2"])["count"] == 2
    assert runs.cleanup_stats("t1")["total_runs"] == 0


# ----------------------------------------------------------------- coverage_runs
@pytest.mark.parametrize("feature", ["amba", "telemetry", "backupdr"])
def test_coverage_cleanup(tmp_path, monkeypatch, feature):
    from app.core import coverage_runs

    monkeypatch.setattr(coverage_runs, "_PATH", tmp_path / "cov.json")
    coverage_runs.save_run(feature, "t1", "workload", "w1", {"scope_kind": "workload", "scope_id": "w1", "scope_name": "W1"}, headline=70, counts={"covered": 7}, resource_count=10)
    coverage_runs.save_run(feature, "t1", "subscription", "s1", {"scope_kind": "subscription", "scope_id": "s1", "scope_name": "Sub"}, headline=90, counts={"covered": 9}, resource_count=20)

    all_runs = coverage_runs.list_all_runs(feature, "t1")
    assert len(all_runs) == 2 and all(r["size_bytes"] > 0 for r in all_runs)
    stats = coverage_runs.cleanup_stats(feature, "t1")
    assert stats["total_runs"] == 2 and stats["scopes"] == 2

    ids = [r["id"] for r in all_runs]
    assert coverage_runs.trash_runs(feature, "t1", ids[:1])["count"] == 1
    assert coverage_runs.cleanup_stats(feature, "t1")["trashed_runs"] == 1
    # A different feature's store is independent.
    other = "telemetry" if feature != "telemetry" else "amba"
    assert coverage_runs.cleanup_stats(other, "t1")["total_runs"] == 0
    assert coverage_runs.purge_runs(feature, "t1", ids)["count"] == 2
    assert coverage_runs.cleanup_stats(feature, "t1")["total_runs"] == 0

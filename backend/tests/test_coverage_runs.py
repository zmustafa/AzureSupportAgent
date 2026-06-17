"""Tests for the shared coverage scan-history store (app/core/coverage_runs.py).

Backs the "Scan history" panel on the Monitoring / Telemetry / Backup-DR dashboards —
mirrors the Performance Profiler run history (soft-delete/trash, per-scope cap, feature +
tenant + scope isolation)."""
from __future__ import annotations

import importlib

import app.core.coverage_runs as cr


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "_PATH", tmp_path / "coverage_runs.json")


def _snap(pct=78, n=3):
    return {
        "coverage_pct": pct, "scope_kind": "workload", "scope_id": "w1",
        "scope_name": "Demo WL", "demo": True, "groups": [{"x": 1}],
        "all_resources": list(range(n)),
    }


def test_save_list_get(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r = cr.save_run("amba", "T", "workload", "w1", _snap(), headline=78, counts={"alerts_present": 5}, resource_count=3, actor="me")
    assert r["id"] and r["run_at"] and r["_headline"] == 78
    runs = cr.list_runs("amba", "T", "workload", "w1")
    assert len(runs) == 1
    assert runs[0]["headline"] == 78 and runs[0]["resource_count"] == 3 and runs[0]["counts"]["alerts_present"] == 5
    full = cr.get_run("amba", "T", r["id"])
    assert full is not None and "groups" in full  # full snapshot retained for re-open


def test_soft_delete_restore_purge(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r = cr.save_run("amba", "T", "workload", "w1", _snap(), headline=70)
    assert cr.delete_run("amba", "T", r["id"]) is True
    assert cr.list_runs("amba", "T", "workload", "w1") == []          # hidden from active
    assert len(cr.list_trashed_runs("amba", "T", "workload", "w1")) == 1
    assert cr.get_run("amba", "T", r["id"]) is None                    # hidden by default
    assert cr.get_run("amba", "T", r["id"], include_deleted=True) is not None
    assert cr.delete_run("amba", "T", r["id"]) is False                # already trashed
    assert cr.restore_run("amba", "T", r["id"]) is True
    assert len(cr.list_runs("amba", "T", "workload", "w1")) == 1
    assert cr.purge_run("amba", "T", r["id"]) is True
    assert cr.list_runs("amba", "T", "workload", "w1") == []
    assert cr.list_trashed_runs("amba", "T", "workload", "w1") == []


def test_cap_evicts_active_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(cr, "_MAX_PER_SCOPE", 3)
    ids = [cr.save_run("amba", "T", "workload", "w1", _snap(), headline=i)["id"] for i in range(5)]
    # Cap keeps the newest 3 active.
    active = cr.list_runs("amba", "T", "workload", "w1")
    assert len(active) == 3
    # A trashed run is preserved beyond the cap (not evicted by new saves).
    cr.delete_run("amba", "T", active[-1]["id"])
    for i in range(5):
        cr.save_run("amba", "T", "workload", "w1", _snap(), headline=100 + i)
    assert len(cr.list_trashed_runs("amba", "T", "workload", "w1")) == 1
    _ = ids


def test_feature_tenant_scope_isolation(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cr.save_run("amba", "T", "workload", "w1", _snap(), headline=1)
    cr.save_run("telemetry", "T", "workload", "w1", _snap(), headline=2)
    cr.save_run("amba", "OTHER", "workload", "w1", _snap(), headline=3)
    cr.save_run("amba", "T", "subscription", "s1", _snap(), headline=4)
    assert len(cr.list_runs("amba", "T", "workload", "w1")) == 1
    assert len(cr.list_runs("telemetry", "T", "workload", "w1")) == 1
    assert len(cr.list_runs("amba", "OTHER", "workload", "w1")) == 1
    assert len(cr.list_runs("amba", "T", "subscription", "s1")) == 1
    # Cross-feature get must not leak.
    aid = cr.list_runs("amba", "T", "workload", "w1")[0]["id"]
    assert cr.get_run("telemetry", "T", aid) is None


def test_empty_trash_and_delete_scope(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r1 = cr.save_run("backupdr", "T", "workload", "w1", _snap(), headline=80)
    cr.save_run("backupdr", "T", "workload", "w1", _snap(), headline=90)
    cr.delete_run("backupdr", "T", r1["id"])
    assert cr.empty_trash("backupdr", "T", "workload", "w1") == 1
    assert cr.list_trashed_runs("backupdr", "T", "workload", "w1") == []
    assert len(cr.list_runs("backupdr", "T", "workload", "w1")) == 1
    assert cr.delete_scope("backupdr", "T", "workload", "w1") is True
    assert cr.list_runs("backupdr", "T", "workload", "w1") == []

"""Tests for the shared coverage/posture trend store + the perf save_run integration."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _fresh(tmp_path, monkeypatch):
    from app.core import coverage_trends as ct

    importlib.reload(ct)
    monkeypatch.setattr(ct, "_PATH", Path(tmp_path) / "coverage_trends.json")
    return ct


def test_record_appends_and_trend_summary(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    ct.record("amba", "t1", "workload", "w1", pct=60)
    ct.record("amba", "t1", "workload", "w1", pct=72, at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    tr = ct.trend("amba", "t1", "workload", "w1")
    assert tr["count"] == 2
    assert tr["current"] == 72 and tr["previous"] == 60 and tr["delta"] == 12
    assert tr["unit"] == "%"


def test_pct_clamped_and_none_allowed(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    ct.record("telemetry", "t1", "workload", "w1", pct=130)
    ct.record("telemetry", "t1", "workload", "w1", pct=None, at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    pts = ct.series("telemetry", "t1", "workload", "w1")
    assert pts[0]["pct"] == 100  # clamped
    assert pts[1]["pct"] is None


def test_dedup_collapses_rapid_same_value(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    ct.record("backupdr", "t1", "workload", "w1", pct=80)
    ct.record("backupdr", "t1", "workload", "w1", pct=80)  # same value, moments later
    assert ct.trend("backupdr", "t1", "workload", "w1")["count"] == 1
    # A different value always appends.
    ct.record("backupdr", "t1", "workload", "w1", pct=85)
    assert ct.trend("backupdr", "t1", "workload", "w1")["count"] == 2


def test_same_value_days_apart_appends(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    ct.record("amba", "t1", "workload", "w1", pct=78, at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    ct.record("amba", "t1", "workload", "w1", pct=78)  # same value but 2 days later
    assert ct.trend("amba", "t1", "workload", "w1")["count"] == 2


def test_seed_demo_series_rises_to_current_and_is_idempotent(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    pts = ct.seed_demo_series("performance", "t1", "workload", "w1", current_pct=78, points=6, climb=16)
    assert len(pts) == 6
    assert pts[-1]["pct"] == 78
    assert pts[0]["pct"] <= 78
    assert all(p["demo"] for p in pts)
    # Non-decreasing ramp (ease-out).
    vals = [p["pct"] for p in pts]
    assert vals == sorted(vals)
    # No-op when a series already exists.
    again = ct.seed_demo_series("performance", "t1", "workload", "w1", current_pct=10)
    assert [p["pct"] for p in again] == vals


def test_seed_demo_series_handles_missing_current(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    assert ct.seed_demo_series("amba", "t1", "workload", "w1", current_pct=None) == []


def test_scopes_and_features_isolated(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    ct.record("amba", "t1", "workload", "w1", pct=50)
    ct.record("telemetry", "t1", "workload", "w1", pct=60)  # same scope, different feature
    ct.record("amba", "t1", "workload", "w2", pct=70)       # same feature, different scope
    ct.record("amba", "t2", "workload", "w1", pct=80)       # different tenant
    assert ct.trend("amba", "t1", "workload", "w1")["current"] == 50
    assert ct.trend("telemetry", "t1", "workload", "w1")["current"] == 60
    assert ct.trend("amba", "t1", "workload", "w2")["current"] == 70
    assert ct.trend("amba", "t2", "workload", "w1")["current"] == 80


def test_delete_scope(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    ct.record("amba", "t1", "workload", "w1", pct=50)
    assert ct.delete_scope("amba", "t1", "workload", "w1") is True
    assert ct.trend("amba", "t1", "workload", "w1")["count"] == 0
    assert ct.delete_scope("amba", "t1", "workload", "w1") is False


def test_max_points_cap(tmp_path, monkeypatch):
    ct = _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(ct, "_MAX_POINTS", 5)
    base = datetime.now(timezone.utc) - timedelta(days=20)
    for i in range(8):
        ct.record("amba", "t1", "workload", "w1", pct=i * 10, at=(base + timedelta(days=i)).isoformat())
    pts = ct.series("amba", "t1", "workload", "w1")
    assert len(pts) == 5
    # Oldest three evicted; newest five kept (values 30..70).
    assert [p["pct"] for p in pts] == [30, 40, 50, 60, 70]


def test_perf_save_run_records_trend_point(tmp_path, monkeypatch):
    """Saving a profile run also records a compact performance-score trend point."""
    ct = _fresh(tmp_path, monkeypatch)
    from app.perfprofile import runs as runs_mod

    importlib.reload(runs_mod)
    monkeypatch.setattr(runs_mod, "_PATH", Path(tmp_path) / "perfprofile_runs.json")
    # runs imports coverage_trends lazily inside save_run, so patch the module it resolves.
    monkeypatch.setattr("app.core.coverage_trends._PATH", ct._PATH)

    snap = {
        "scope_kind": "workload", "scope_id": "w1", "demo": True,
        "scorecard": {"workload_score": 73, "breaching": 1, "approaching": 2, "healthy": 5, "resources_profiled": 8},
    }
    runs_mod.save_run("t1", "workload", "w1", snap, actor="dev")
    tr = ct.trend("performance", "t1", "workload", "w1")
    assert tr["current"] == 73 and tr["count"] == 1

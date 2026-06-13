"""Unit tests for the Performance Profiler run-history store."""
from __future__ import annotations

import importlib
from pathlib import Path


def _fresh_runs(tmp_path, monkeypatch):
    from app.perfprofile import runs as runs_mod

    importlib.reload(runs_mod)
    monkeypatch.setattr(runs_mod, "_PATH", Path(tmp_path) / "perfprofile_runs.json")
    return runs_mod


def _snap(score=78, op="POST /order"):
    return {
        "scope_kind": "workload", "scope_id": "w1", "scope_name": "W1", "window": "P1D", "demo": True,
        "scorecard": {"workload_score": score, "resources_profiled": 9, "breaching": 1, "approaching": 4, "healthy": 3, "bottleneck_count": 5},
        "top_bottleneck": {"resource_name": "shop-redis-prod", "metric_name": "Server load high", "pct_of_threshold": 104.4, "state": "breaching"},
        "resources": [{"resource_id": "/r/redis", "cells": []}],
        "bottlenecks": [],
    }


def test_save_list_get_delete(tmp_path, monkeypatch):
    runs = _fresh_runs(tmp_path, monkeypatch)
    a = runs.save_run("t1", "workload", "w1", _snap(70), actor="dev")
    b = runs.save_run("t1", "workload", "w1", _snap(80), actor="dev")
    assert a["id"] != b["id"]
    lst = runs.list_runs("t1", "workload", "w1")
    assert len(lst) == 2
    # newest first
    assert lst[0]["id"] == b["id"]
    assert lst[0]["workload_score"] == 80
    assert lst[0]["top_bottleneck"]["resource_name"] == "shop-redis-prod"
    # get full
    full = runs.get_run("t1", a["id"])
    assert full and full["scorecard"]["workload_score"] == 70
    # latest
    assert runs.latest_run("t1", "workload", "w1")["id"] == b["id"]
    # delete
    assert runs.delete_run("t1", a["id"]) is True
    assert len(runs.list_runs("t1", "workload", "w1")) == 1
    assert runs.get_run("t1", a["id"]) is None


def test_tenant_and_scope_isolation(tmp_path, monkeypatch):
    runs = _fresh_runs(tmp_path, monkeypatch)
    runs.save_run("t1", "workload", "w1", _snap())
    runs.save_run("t2", "workload", "w1", _snap())
    runs.save_run("t1", "workload", "w2", _snap())
    assert len(runs.list_runs("t1", "workload", "w1")) == 1
    assert len(runs.list_runs("t2", "workload", "w1")) == 1
    assert len(runs.list_runs("t1", "workload", "w2")) == 1
    # t2 can't see t1's run by id
    rid = runs.list_runs("t1", "workload", "w1")[0]["id"]
    assert runs.get_run("t2", rid) is None


def test_cap_per_scope(tmp_path, monkeypatch):
    runs = _fresh_runs(tmp_path, monkeypatch)
    monkeypatch.setattr(runs, "_MAX_PER_SCOPE", 3)
    ids = [runs.save_run("t1", "workload", "w1", _snap(i))["id"] for i in range(5)]
    lst = runs.list_runs("t1", "workload", "w1")
    assert len(lst) == 3
    # only the 3 newest survive
    assert [r["id"] for r in lst] == ids[-1:-4:-1]


def test_window_to_start_parsing():
    from app.perfprofile.collector import _window_to_start

    assert _window_to_start("P1D")  # non-empty ISO
    assert _window_to_start("PT6H")
    assert _window_to_start("P30D")
    assert _window_to_start("garbage") == ""

"""Tests for the admin Demo Data orchestrator (seed / purge / status)."""
import asyncio

from app.api import admin_demo


def test_seed_features_then_status(monkeypatch, tmp_path):
    # Isolate every cache/store/registry file to a temp dir so we never touch real data.
    import app.amba.cache as ac
    import app.telemetry.cache as tc
    import app.backupdr.cache as bc
    import app.perfprofile.cache as pc
    import app.perfprofile.runs as pr
    import app.radar.cache as rc
    import app.teleintel.cache as ti
    import app.workloads.registry as wr
    from pathlib import Path

    for mod, attr, fn in [
        (ac, "_PATH", "amba.json"), (tc, "_PATH", "tel.json"), (bc, "_PATH", "bdr.json"),
        (pc, "_PATH", "perf.json"), (pr, "_PATH", "perfruns.json"), (rc, "_PATH", "radar.json"),
        (ti, "_PATH", "ti.json"), (wr, "_PATH", "workloads.json"),
    ]:
        monkeypatch.setattr(mod, attr, tmp_path / fn)

    tenant = "t-demo"
    res = admin_demo._seed_all(tenant)
    assert "monitoring_coverage" in res["seeded"]
    assert res["errors"] == {} or set(res["errors"]).issubset({"evidence_locker", "dns_debug", "network_reachability"})

    st = admin_demo._status(tenant)
    assert st["loaded"] is True
    assert st["present"]["monitoring_coverage"] is True
    assert st["present"]["workload"] is True


def test_purge_features_is_demo_only(monkeypatch, tmp_path):
    import app.amba.cache as ac
    monkeypatch.setattr(ac, "_PATH", tmp_path / "amba.json")

    tenant = "t-demo"
    # A real (non-demo) workload snapshot in the SAME cache must survive a demo purge.
    ac.write_snapshot(tenant, "workload", "real-workload-123", {"generated_at": "x", "coverage_pct": 50})
    ac.write_snapshot(tenant, "workload", admin_demo.DEMO_WORKLOAD_ID, {"generated_at": "y", "coverage_pct": 99})

    removed = admin_demo._purge_features(tenant)
    assert removed["removed"]["monitoring_coverage"] is True
    # Demo gone, real intact.
    assert ac.read_snapshot(tenant, "workload", admin_demo.DEMO_WORKLOAD_ID) is None
    assert ac.read_snapshot(tenant, "workload", "real-workload-123") is not None


def test_purge_features_idempotent(monkeypatch, tmp_path):
    import app.amba.cache as ac
    monkeypatch.setattr(ac, "_PATH", tmp_path / "amba.json")
    # Purging with nothing present must not raise and reports False.
    removed = admin_demo._purge_features("t-empty")
    assert removed["errors"] == {}
    assert removed["removed"]["monitoring_coverage"] is False

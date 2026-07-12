"""The coverage GET endpoints are cached-only: navigating to a scope with no saved scan must
NOT trigger a live Azure compute (which can hang and strand the view on "Loading…"). Instead
the GET returns a lightweight ``report_exists=False`` sentinel the UI renders as an empty
"run first scan" state. Computing happens only on an explicit Refresh (``force=True``).
"""
from __future__ import annotations

import pytest

from app.api import amba, backupdr, telemetry
from app.amba import cache as amba_cache
from app.core.security import Principal

_FEATURES = [amba, telemetry, backupdr]
_P = Principal(subject="t", email="t@t", tenant_id="probe-tenant-cov", role="admin")


@pytest.mark.parametrize("mod", _FEATURES, ids=lambda m: m.router.prefix.strip("/"))
async def test_get_is_cached_only_no_report(mod):
    """A non-demo scope with no cache returns report_exists=False and empty data, fast."""
    _kind, _id, snap = await mod.latest_snapshot(_P, "no-such-workload-xyz", None)
    assert snap.get("report_exists") is False
    assert (snap.get("all_resources") or []) == []
    assert (snap.get("gaps") or []) == []


@pytest.mark.parametrize("mod", _FEATURES, ids=lambda m: m.router.prefix.strip("/"))
async def test_demo_scope_reports_exists(mod):
    """Demo scopes seed synthetic data, so a report always exists for them."""
    snap = await mod._get_snapshot(_P, "workload", mod.demo.DEMO_WORKLOAD_ID, force=False, compute=False)
    assert snap.get("report_exists") is True
    assert len(snap.get("all_resources") or []) > 0


async def test_amba_fleet_summarizes_cached_and_unscanned_workloads(monkeypatch, tmp_path):
    monkeypatch.setattr(amba_cache, "_PATH", tmp_path / "amba-cache.json")
    monkeypatch.setattr("app.workloads.registry.list_workloads", lambda: [
        {"id": "w-scanned", "name": "Scanned", "connection_id": "c1", "environment": "prod"},
        {"id": "w-never", "name": "Never", "connection_id": "c1", "environment": "dev"},
    ])
    amba_cache.write_snapshot(_P.tenant_id, "workload", "w-scanned", {
        "generated_at": "2099-01-01T00:00:00+00:00", "coverage_pct": 75,
        "kpis": {"total_resources_in_baseline": 4, "recommended_total": 8, "alerts_present": 6, "alerts_missing": 1, "alerts_misconfigured": 1},
        "gaps": [{}, {}],
    })
    result = await amba.fleet(_P)
    assert result["total"] == 2
    assert result["scanned"] == 1
    scanned = next(row for row in result["workloads"] if row["workload_id"] == "w-scanned")
    never = next(row for row in result["workloads"] if row["workload_id"] == "w-never")
    assert scanned["coverage_pct"] == 75
    assert scanned["missing"] == 1
    assert scanned["gaps"] == 2
    assert never["has_scan"] is False

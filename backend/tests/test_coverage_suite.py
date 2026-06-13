"""Comprehensive coverage/profiler suite — demo internal consistency + compute edge cases.

Covers Monitoring (AMBA), Telemetry, and Backup & DR coverage collectors plus the
Performance Profiler. Pure-function level (no Azure), so it runs offline and fast.
"""
from __future__ import annotations

from app.amba import collector as amba_col, demo as amba_demo
from app.telemetry import collector as tel_col, demo as tel_demo
from app.backupdr import collector as bdr_col, demo as bdr_demo
from app.perfprofile import collector as perf_col, demo as perf_demo


# --------------------------------------------------------------------------- AMBA
def test_amba_demo_internal_consistency():
    snap = amba_demo.build_demo_snapshot(misconfig_counts_as_gap=True, tolerance_pct=0.0)
    assert snap["demo"] is True
    k = snap["kpis"]
    # present + missing + misconfigured reconcile with the recommended total.
    assert k["alerts_present"] + k["alerts_missing"] + k["alerts_misconfigured"] == k["recommended_total"]
    # Group rows reconcile with the baseline resource count.
    total_rows = sum(len(g["rows"]) for g in snap["groups"])
    assert total_rows == k["total_resources_in_baseline"]
    assert 0 <= snap["coverage_pct"] <= 100
    # Every gap is an actionable status.
    for gp in snap["gaps"]:
        assert gp["status"] in ("missing", "misconfigured")
    # Each group's present/missing/misconfigured tally matches its rows' cells.
    for g in snap["groups"]:
        cell_statuses = [c["status"] for r in g["rows"] for c in r["cells"]]
        assert g["present"] == cell_statuses.count("present")
        assert g["missing"] == cell_statuses.count("missing")
        assert g["misconfigured"] == cell_statuses.count("misconfigured")


def test_amba_all_resources_flags():
    snap = amba_demo.build_demo_snapshot(misconfig_counts_as_gap=True, tolerance_pct=0.0)
    ar = snap["all_resources"]
    assert len(ar) == snap["kpis"]["total_resources_in_baseline"]
    # Demo resources are all baseline-covered types.
    assert all(r["in_reference"] for r in ar)


def test_amba_compute_empty_resources():
    snap = amba_col.compute_coverage([], [], misconfig_counts_as_gap=True, tolerance_pct=0.0)
    assert snap["groups"] == []
    assert snap["gaps"] == []
    assert snap["all_resources"] == []
    assert snap["coverage_pct"] == 100  # nothing to cover → trivially 100%


def test_amba_compute_resource_not_in_reference():
    res = [{
        "id": "/sub/s/rg/r/providers/microsoft.unknown/foos/foo1",
        "name": "foo1", "type": "microsoft.unknown/foos",
        "resourceGroup": "rg", "subscriptionId": "s", "location": "eastus", "tags": {},
    }]
    snap = amba_col.compute_coverage(res, [], misconfig_counts_as_gap=True, tolerance_pct=0.0)
    # Not in reference → no coverage group, but still surfaced in All Resources.
    assert snap["groups"] == []
    assert len(snap["all_resources"]) == 1
    assert snap["all_resources"][0]["in_reference"] is False


def test_amba_misconfig_toggle_changes_gaps():
    res = amba_demo.demo_resources()
    alerts = amba_demo.demo_alerts()
    on = amba_col.compute_coverage(res, alerts, misconfig_counts_as_gap=True, tolerance_pct=0.0)
    off = amba_col.compute_coverage(res, alerts, misconfig_counts_as_gap=False, tolerance_pct=0.0)
    # Excluding misconfigured from gaps should never produce MORE gaps.
    assert len(off["gaps"]) <= len(on["gaps"])


# --------------------------------------------------------------------------- Telemetry
def test_telemetry_demo_internal_consistency():
    snap = tel_demo.build_demo_snapshot()
    assert snap["demo"] is True
    k = snap["kpis"]
    for key in ("pct_with_any_diag", "pct_with_all_categories", "pct_to_approved"):
        assert 0 <= k[key] <= 100
    # with_all_categories ⊆ with_any_diag ⊆ total.
    assert k["with_all_categories"] <= k["with_any_diag"] <= k["total_resources_in_reference"]
    # Each group's none/partial/compliant tally matches its row statuses.
    for g in snap["groups"]:
        statuses = [r["status"] for r in g["rows"]]
        assert g["none"] == statuses.count("none")
        assert g["partial"] == statuses.count("partial")
        assert g["compliant"] == statuses.count("compliant")
        assert all(s in ("none", "partial", "compliant") for s in statuses)


def test_telemetry_compute_empty():
    snap = tel_col.compute_coverage([], {}, approved_workspaces=[])
    assert snap["groups"] == []
    assert snap["all_resources"] == []


def test_telemetry_no_settings_is_none_status():
    # A reference-covered resource with NO diagnostic settings → status "none" (red).
    res = [{
        "id": "/sub/s/rg/r/providers/microsoft.keyvault/vaults/kv1",
        "name": "kv1", "type": "microsoft.keyvault/vaults",
        "resourceGroup": "rg", "subscriptionId": "s", "location": "eastus", "tags": {},
    }]
    snap = tel_col.compute_coverage(res, {}, approved_workspaces=[])
    rows = [r for g in snap["groups"] for r in g["rows"]]
    assert len(rows) == 1
    assert rows[0]["status"] == "none"


# --------------------------------------------------------------------------- Backup & DR
def test_backupdr_demo_internal_consistency():
    snap = bdr_demo.build_demo_snapshot()
    sc = snap["scorecard"]
    assert 0 <= sc["pct_protected"] <= 100
    assert sc["protected"] <= sc["total"]
    for g in snap["groups"]:
        statuses = [r["status"] for r in g["rows"]]
        assert g["red"] == statuses.count("red")
        assert g["amber"] == statuses.count("amber")
        assert g["green"] == statuses.count("green")


def test_backupdr_dr_pair_flags():
    snap = bdr_demo.build_demo_snapshot()
    sc = snap["scorecard"]
    assert sc["dr_pairs"] == len(snap["dr_pairs"])
    assert sc["dr_pairs_stale"] == sum(1 for p in snap["dr_pairs"] if p["stale"])
    assert sc["dr_pairs_unhealthy"] == sum(1 for p in snap["dr_pairs"] if not p["healthy"])


def test_backupdr_evaluate_cell_states():
    # backup_enabled
    assert bdr_col._evaluate_cell("backup_enabled", {"backup_enabled": True}, "eastus", 24)["status"] == "green"
    assert bdr_col._evaluate_cell("backup_enabled", {"backup_enabled": False}, "eastus", 24)["status"] == "red"
    # last_job age beyond SLA → amber
    fresh = bdr_col._evaluate_cell("last_job", {"backup_enabled": True, "last_job_status": "succeeded", "last_job_age_hours": 2}, "eastus", 24)
    stale = bdr_col._evaluate_cell("last_job", {"backup_enabled": True, "last_job_status": "succeeded", "last_job_age_hours": 99}, "eastus", 24)
    assert fresh["status"] == "green"
    assert stale["status"] == "amber"
    # offsite same region → amber, different → green
    same = bdr_col._evaluate_cell("offsite_region", {"backup_region": "eastus"}, "eastus", 24)
    diff = bdr_col._evaluate_cell("offsite_region", {"backup_region": "westus"}, "eastus", 24)
    assert same["status"] == "amber"
    assert diff["status"] == "green"
    # every cell status is a known value
    for chk in ("backup_enabled", "policy", "retention", "geo_redundancy", "encryption", "soft_delete"):
        assert bdr_col._evaluate_cell(chk, {}, "eastus", 24)["status"] in ("green", "amber", "red", "na")


# --------------------------------------------------------------------------- Performance Profiler
def test_perf_demo_internal_consistency():
    snap = perf_demo.build_demo_snapshot()
    sc = snap["scorecard"]
    assert 0 <= sc["workload_score"] <= 100
    assert sc["resources_profiled"] == len(snap["resources"])
    # breaching + approaching + healthy never exceeds the profiled count (rest = no_data).
    assert sc["breaching"] + sc["approaching"] + sc["healthy"] <= sc["resources_profiled"]
    # bottlenecks sorted by pct_of_threshold descending.
    pcts = [b["pct_of_threshold"] for b in snap["bottlenecks"]]
    assert pcts == sorted(pcts, reverse=True)
    if snap["bottlenecks"]:
        assert snap["top_bottleneck"]["resource_id"] == snap["bottlenecks"][0]["resource_id"]


def test_perf_evaluate_metric_states():
    rec = {"key": "cpu", "metric": "Percentage CPU", "name": "CPU", "amba_category": "performance",
           "severity": "warning", "unit": "%", "operator": "GreaterThan", "threshold": 80}

    def series(v):
        return [{"timestamp": f"2026-06-12T0{i}:00:00Z", "value": v} for i in range(5)]

    healthy = perf_col._evaluate_metric(rec, "microsoft.compute/virtualmachines", series(10))
    breaching = perf_col._evaluate_metric(rec, "microsoft.compute/virtualmachines", series(95))
    nodata = perf_col._evaluate_metric(rec, "microsoft.compute/virtualmachines", [])
    assert healthy["state"] == "healthy"
    assert breaching["state"] == "breaching"
    assert nodata["state"] == "no_data"


def test_perf_threshold_none_is_informational():
    # An informational metric (threshold None) must never be flagged as breaching.
    rec = {"key": "info", "metric": "Some Metric", "name": "Info", "amba_category": "performance",
           "severity": "info", "unit": "count", "operator": "GreaterThan", "threshold": None}
    series = [{"timestamp": f"2026-06-12T0{i}:00:00Z", "value": 9999} for i in range(5)]
    cell = perf_col._evaluate_metric(rec, "microsoft.storage/storageaccounts", series)
    assert cell["state"] != "breaching"

"""Unit tests for the Performance Profiler pure logic + demo."""
from __future__ import annotations

from app.perfprofile.collector import (
    STATE_APPROACHING,
    STATE_BREACHING,
    STATE_HEALTHY,
    _evaluate_metric,
    compute_profile,
)
from app.perfprofile.demo import build_demo_snapshot, demo_metrics_by_resource
from app.perfprofile.metrics_map import metric_semantics


def _flat(base, end, n=12):
    return [{"timestamp": f"t{i:02d}", "value": base + (end - base) * (i / (n - 1))} for i in range(n)]


def test_metric_semantics_defaults():
    assert metric_semantics("microsoft.cache/redis", "serverLoad", "%")["aggregation"] == "Maximum"
    # Unknown %-metric defaults to ceiling 100, Average.
    s = metric_semantics("microsoft.foo/bar", "SomePct", "%")
    assert s["ceiling"] == 100 and s["aggregation"] == "Average"


def test_evaluate_higher_is_worse_states():
    rec = {"key": "k", "metric": "serverLoad", "name": "Load", "amba_category": "performance",
           "severity": "warning", "unit": "%", "operator": "GreaterThan", "threshold": 90}
    # Breaching: peak above threshold.
    cell = _evaluate_metric(rec, "microsoft.cache/redis", _flat(70, 94))
    assert cell["state"] == STATE_BREACHING
    assert cell["pct_of_threshold"] > 100
    # Approaching: peak in 70-90.
    cell2 = _evaluate_metric(rec, "microsoft.cache/redis", _flat(60, 82))
    assert cell2["state"] == STATE_APPROACHING
    # Healthy: well below.
    cell3 = _evaluate_metric(rec, "microsoft.cache/redis", _flat(20, 40))
    assert cell3["state"] == STATE_HEALTHY


def test_evaluate_lower_is_worse_availability():
    rec = {"key": "a", "metric": "Availability", "name": "Avail", "amba_category": "availability",
           "severity": "error", "unit": "%", "operator": "LessThan", "threshold": 99}
    # Healthy: 100 stays above 99.
    cell = _evaluate_metric(rec, "microsoft.storage/storageaccounts", _flat(100, 100))
    assert cell["state"] == STATE_HEALTHY
    # Breaching: dips to/below 99.
    cell2 = _evaluate_metric(rec, "microsoft.storage/storageaccounts", _flat(99, 97))
    assert cell2["state"] == STATE_BREACHING


def test_count_metric_no_threshold_breaches_on_nonzero():
    rec = {"key": "e", "metric": "ServerErrors", "name": "Errors", "amba_category": "availability",
           "severity": "error", "unit": "count", "operator": "GreaterThan", "threshold": 0}
    cell = _evaluate_metric(rec, "microsoft.servicebus/namespaces", _flat(0, 5))
    assert cell["state"] == STATE_BREACHING


def test_dimension_filtered_error_count_empty_is_healthy():
    """A status-code-split error count (e.g. Storage Transactions → 403) returns an EMPTY
    series when zero such errors occurred — that's the healthy case, not no-data."""
    rec = {"key": "stor_auth_failures", "metric": "Transactions", "name": "Authorization failures (403)",
           "amba_category": "security", "severity": "error", "unit": "count", "operator": "GreaterThan",
           "threshold": 0, "dimension_filter": "ResponseType eq 'AuthorizationError'"}
    # No data at all (the dimension value never appeared) → healthy 0, not no-data.
    cell = _evaluate_metric(rec, "microsoft.storage/storageaccounts", [])
    assert cell["state"] == STATE_HEALTHY
    assert cell["observed"] == 0.0
    # A nonzero 403 count still breaches.
    cell2 = _evaluate_metric(rec, "microsoft.storage/storageaccounts", _flat(0, 4))
    assert cell2["state"] == STATE_BREACHING


def test_managed_disk_in_reference():
    from app.amba.reference import load_reference

    ref = load_reference()
    spec = ref["types"].get("microsoft.compute/disks")
    assert spec is not None
    metrics = {a["metric"] for a in spec["alerts"]}
    assert {"Disk IOPS saturation", "Disk throughput saturation"} <= metrics


def test_parse_combined_series_sums_metrics():
    import json

    from app.perfprofile.collector import _parse_combined_series

    blob = json.dumps({"value": [
        {"timeseries": [{"data": [{"timeStamp": "t1", "average": 100.0}, {"timeStamp": "t2", "average": 120.0}]}]},
        {"timeseries": [{"data": [{"timeStamp": "t1", "average": 80.0}, {"timeStamp": "t2", "average": 100.0}]}]},
    ]})
    out = _parse_combined_series(blob)
    assert out == [{"timestamp": "t1", "value": 180.0}, {"timestamp": "t2", "value": 220.0}]


def test_disk_saturation_series_percentage_of_provisioned():
    import asyncio
    import json

    from app.perfprofile.collector import DISK_BW_SAT, DISK_IOPS_SAT, _disk_saturation_series

    class _Cap:
        def __init__(self, ok, out):
            self.ok, self.stdout = ok, out

    async def fake_capture(rid, metrics, conn, **kw):
        # read 180 + write 40 = 220 (for both ops and bytes calls)
        return _Cap(True, json.dumps({"value": [
            {"timeseries": [{"data": [{"timeStamp": "t1", "average": 180.0}]}]},
            {"timeseries": [{"data": [{"timeStamp": "t1", "average": 40.0}]}]},
        ]}))

    res = {"id": "/disk", "type": "microsoft.compute/disks", "provisioned_iops": 240, "provisioned_mbps": 50}

    async def run():
        return await _disk_saturation_series(
            res, None, interval="PT5M", start="", end="",
            sem_lock=asyncio.Semaphore(1), run_metrics_capture=fake_capture,
        )

    out = asyncio.new_event_loop().run_until_complete(run())
    # 220 IOPS / 240 provisioned = 91.67%
    assert out[DISK_IOPS_SAT][0]["value"] == 91.67
    # 220 bytes/sec is negligible vs 50 MB/sec → ~0%
    assert out[DISK_BW_SAT][0]["value"] == 0.0


def test_demo_disk_breaches_iops():
    snap = build_demo_snapshot()
    disks = [r for r in snap["resources"] if str(r.get("resource_type", "")).lower() == "microsoft.compute/disks"]
    assert disks, "demo should include a managed disk"
    cells = {c["name"]: c["state"] for c in disks[0]["cells"]}
    assert cells.get("Disk IOPS saturation high") == STATE_BREACHING


def test_trend_detected():
    rec = {"key": "k", "metric": "serverLoad", "name": "Load", "amba_category": "performance",
           "severity": "warning", "unit": "%", "operator": "GreaterThan", "threshold": 90}
    cell = _evaluate_metric(rec, "microsoft.cache/redis", _flat(50, 90))
    assert cell["trend_pct"] > 0  # rising


def test_demo_scenario_redis_is_top_bottleneck():
    snap = build_demo_snapshot()
    assert snap["demo"] is True
    assert snap["scorecard"]["resources_profiled"] >= 8
    top = snap["top_bottleneck"]
    assert top is not None
    assert top["resource_name"] == "contoso-redis"
    assert top["metric"] == "serverLoad"
    assert top["state"] == STATE_BREACHING
    # SQL DTU should be present as approaching.
    sql = [b for b in snap["bottlenecks"] if b["metric"] == "dtu_consumption_percent"]
    assert sql and sql[0]["state"] == STATE_APPROACHING


def test_demo_plan_is_healthy_overprovisioned():
    snap = build_demo_snapshot()
    plan = next(r for r in snap["resources"] if r["resource_name"] == "contoso-plan")
    assert plan["state"] == STATE_HEALTHY
    assert plan["score"] == 100


def test_workload_score_between_0_and_100():
    snap = compute_profile(
        [{"id": "/r/redis", "name": "r", "type": "microsoft.cache/redis"}],
        {"/r/redis": {"serverLoad": _flat(70, 94), "usedmemorypercentage": _flat(40, 60)}},
    )
    assert 0 <= snap["scorecard"]["workload_score"] <= 100
    assert snap["scorecard"]["breaching"] >= 1


def test_demo_metrics_cover_all_resources():
    m = demo_metrics_by_resource()
    assert len(m) >= 8


def test_demo_snapshot_has_all_resources_tab_data():
    """The demo snapshot carries an all_resources list (for the 'All Resources' tab),
    one entry per in-scope resource, each annotated with the in_reference flag."""
    snap = build_demo_snapshot()
    ar = snap.get("all_resources")
    assert isinstance(ar, list) and ar, "all_resources must be a non-empty list"
    # At least every profiled resource is represented in the full list.
    assert len(ar) >= snap["scorecard"]["resources_profiled"]
    for row in ar:
        assert set(row) >= {"id", "name", "type", "resource_group", "subscription_id", "location", "in_reference"}
        assert isinstance(row["in_reference"], bool)


def test_profiler_uses_workloads_own_connection(monkeypatch):
    """Regression: the profiler must scan a workload with ITS OWN connection, not the default.
    A workload whose subscription is only reachable via a non-default connection used to
    return zero resources because the API hard-coded get_default_connection()."""
    from app.api import perfprofile

    wl = {"id": "wl-x", "connection_id": "conn-workload", "nodes": []}
    import app.core.azure_connections as conns
    import app.workloads.registry as reg

    monkeypatch.setattr(reg, "get_workload", lambda scope_id, **kw: wl if scope_id == "wl-x" else None)
    seen = {}

    def _fake_resolve(cid):
        seen["cid"] = cid
        return {"id": cid or "default-conn"}

    monkeypatch.setattr(conns, "resolve_connection", _fake_resolve)

    connection, workload = perfprofile._conn_and_workload("workload", "wl-x")
    assert seen["cid"] == "conn-workload"  # resolved the workload's own connection id
    assert connection == {"id": "conn-workload"}
    assert workload is wl



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


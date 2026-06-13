"""Unit tests for Telemetry Intelligence pure logic + demo."""
from __future__ import annotations

from app.teleintel.demo import (
    build_overview,
    demo_smart_detection,
    demo_timeline,
    demo_transaction,
    demo_triage,
)
from app.teleintel.nlkql import validate_kql
from app.teleintel.smartdetect import dedupe_rank
from app.teleintel.transaction import _build_spans
from app.teleintel.triage import _pick_worst_operation


def test_validate_kql_allows_read_only_and_caps():
    clean, err = validate_kql("requests | where success == false | summarize count() by operation_Name", max_rows=500)
    assert err == ""
    assert "take 500" in clean  # cap appended


def test_validate_kql_rejects_mutation():
    _clean, err = validate_kql("requests | take 10\n.drop table requests")
    assert err != ""


def test_validate_kql_rejects_unknown_table():
    _clean, err = validate_kql("SecretTable | take 10")
    assert err != ""


def test_validate_kql_allows_union_join_with_let():
    kql = (
        "let f = requests | where success == false | project operation_Id; "
        "dependencies | join kind=inner (f) on operation_Id | summarize count() by target | take 10"
    )
    clean, err = validate_kql(kql)
    assert err == "", err
    assert "dependencies" in clean


def test_pick_worst_operation():
    rows = [
        {"operation_Name": "GET /a", "failed": 2, "failure_rate_pct": 1.0},
        {"operation_Name": "POST /order", "failed": 197, "failure_rate_pct": 41.0},
        {"operation_Name": "GET /b", "failed": 0, "failure_rate_pct": 0.0},
    ]
    worst = _pick_worst_operation(rows)
    assert worst and worst["operation_Name"] == "POST /order"


def test_smartdetect_dedupe_rank():
    detections = [
        {"display_name": "Failure rate", "severity": "error", "component_name": "a"},
        {"display_name": "Failure rate", "severity": "error", "component_name": "b"},
        {"display_name": "Slow page", "severity": "info", "component_name": "a"},
    ]
    ranked = dedupe_rank(detections)
    assert len(ranked) == 2
    # error severity ranks first; it aggregated 2 components.
    assert ranked[0]["severity"] == "error"
    assert set(ranked[0]["components"]) == {"a", "b"}


def test_transaction_spans_flag_failure():
    rows = [
        {"timestamp": "t1", "itemType": "request", "name": "POST /order", "success": False, "duration": 30000},
        {"timestamp": "t1", "itemType": "dependency", "name": "INSERT", "target": "sql", "success": False, "duration": 30000, "resultCode": "Timeout"},
        {"timestamp": "t1", "itemType": "trace", "name": "log", "success": True},
    ]
    spans = _build_spans(rows)
    assert spans[1]["failed"] is True
    assert spans[2]["failed"] is False


def test_demo_timeline_has_4_signals_and_changes():
    tl = demo_timeline()
    assert tl["signal_count"] == 4
    assert len(tl["series_keys"]) == 4
    assert tl["change_events"]  # deploy overlay present
    # The spike is present in later points.
    assert any(p["failure_rate_pct"] >= 40 for p in tl["points"])


def test_demo_triage_scenario():
    t = demo_triage()
    s = t["summary"]
    assert s["operation"] == "POST /order"
    assert s["failure_rate_pct"] == 41.0
    assert s["top_dependency"] == "sql-prod-eu"
    assert s["dependency_correlation_pct"] == 92.0
    assert "app-v412" in s["probable_trigger"]
    assert t["has_spike"] is True


def test_demo_transaction_and_overview():
    tx = demo_transaction()
    assert tx["failing_step"] == "INSERT Orders"
    assert any(sp["failed"] for sp in tx["spans"])
    ov = build_overview()
    assert ov["components"] and ov["components"][0]["name"] == "shop-appinsights"
    sd = demo_smart_detection()
    assert sd["items"] and sd["items"][0]["severity"] == "error"

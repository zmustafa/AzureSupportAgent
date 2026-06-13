"""Unit tests for the Retirement & Breaking-Change Radar pure logic + demo."""
from __future__ import annotations

from datetime import date

from app.radar.builtin_seed import BREAKING_CHANGE, RETIREMENT, classify_text
from app.radar.collector import (
    build_model_items,
    compute_radar,
    days_until,
    merge_events,
    resolve_owners,
    severity_for_days,
)
from app.radar.demo import build_demo_snapshot, demo_aoai_deployments, demo_raw_events
from app.radar.digest import select_digest_items


def test_days_until_and_severity():
    today = date(2026, 1, 1)
    assert days_until("2026-02-01", today=today) == 31
    assert days_until("2025-12-01", today=today) == -31
    assert days_until("", today=today) is None
    assert severity_for_days(10) == "red"
    assert severity_for_days(45) == "amber"
    assert severity_for_days(200) == "grey"
    assert severity_for_days(None) == "grey"


def test_classify_text():
    assert classify_text("Default outbound access retires")["change_type"] == RETIREMENT
    assert classify_text("Minimum TLS version enforced")["change_type"] == BREAKING_CHANGE
    # Unknown text with no breaking hint defaults to retirement.
    assert classify_text("some random event")["change_type"] == RETIREMENT


def test_merge_dedup_by_tracking_id():
    today = date(2026, 1, 1)
    raw = [
        {"source": "advisor", "tracking_id": "T1", "title": "TLS 1.0", "retirement_date": "2026-03-01",
         "impacted_resources": [{"id": "/r/a", "name": "a"}]},
        {"source": "service_health", "tracking_id": "T1", "title": "TLS 1.0",
         "impacted_resources": [{"id": "/r/b", "name": "b"}]},
    ]
    events = merge_events(raw, today=today)
    assert len(events) == 1
    ev = events[0]
    assert ev["change_type"] == BREAKING_CHANGE
    assert ev["impacted_count"] == 2
    assert set(ev["sources"]) == {"advisor", "service_health"}


def test_owner_resolution_flags_unowned():
    wl_index = {"/r/owned": {"workload_id": "w1", "workload_name": "W", "owner": "team-a"}}
    impacted = [
        {"id": "/r/owned", "name": "owned"},
        {"id": "/r/tagged", "name": "tagged", "tags": {"owner": "team-b"}},
        {"id": "/r/orphan", "name": "orphan"},
    ]
    out = resolve_owners(impacted, wl_index)
    by_name = {r["name"]: r for r in out}
    assert by_name["owned"]["owner"] == "team-a" and by_name["owned"]["owner_source"] == "workload"
    assert by_name["tagged"]["owner"] == "team-b" and by_name["tagged"]["owner_source"] == "tag"
    assert by_name["orphan"]["unowned"] is True


def test_model_lane_matches_lifecycle():
    items = build_model_items(demo_aoai_deployments())
    assert items
    gpt35 = next(m for m in items if m["model"] == "gpt-35-turbo")
    assert gpt35["matched"] is True
    assert gpt35["retirement_date"]
    assert gpt35["stage"] in ("deprecated", "retired", "ga")


def test_compute_radar_counts():
    events = merge_events(demo_raw_events())
    snap = compute_radar(events, build_model_items(demo_aoai_deployments()))
    c = snap["counts"]
    assert c["total"] == len(events)
    assert c["retirement"] + c["breaking_change"] == c["total"]
    assert c["unowned"] >= 1  # demo has at least one unowned item
    assert c["models"] == 3
    assert snap["rail"]  # nearest deadlines present


def test_digest_selects_new_and_approaching():
    snap = build_demo_snapshot()
    # No prior run → everything new.
    sel = select_digest_items(snap, known_ids=set(), lead_days=[90, 60, 30])
    assert sel["new_count"] >= 1
    # All known → only deadline-approaching remain.
    known = {e["tracking_id"] for e in snap["events"]}
    sel2 = select_digest_items(snap, known_ids=known, lead_days=[90, 60, 30])
    assert sel2["new_count"] == 0
    assert sel2["approaching_count"] >= 1  # the 47-day outbound item is within 90d


def test_demo_snapshot_shape():
    snap = build_demo_snapshot()
    assert snap["demo"] is True
    assert snap["scope_id"] == "demo-amba-coverage"
    outbound = next(e for e in snap["events"] if e["tracking_id"] == "DOA-2026-0331")
    assert outbound["impacted_count"] == 12
    assert outbound["unowned"] is True  # one orphan VM
    assert outbound["days_until"] == 47

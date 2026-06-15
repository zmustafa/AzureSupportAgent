"""Unit tests for the Reservations Monitor pure logic + digest + demo."""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.reservations.collector import (
    bucket_for_days,
    compute_reservations,
    days_until,
    normalize_order,
    severity_for_days,
)
from app.reservations.demo import seed_demo
from app.reservations.digest import compute_due, render_html, select_digest_items


def test_days_until_and_severity_and_bucket():
    today = date(2026, 1, 1)
    assert days_until("2026-01-31", today=today) == 30
    assert days_until("2025-12-22", today=today) == -10
    assert days_until("", today=today) is None

    # Severity bands (±60 window).
    assert severity_for_days(10) == "red"          # expiring <30d
    assert severity_for_days(-10) == "red"         # recently expired (within window)
    assert severity_for_days(45) == "amber"        # expiring 31..60d
    assert severity_for_days(120) == "grey"        # healthy
    assert severity_for_days(-120) == "grey"       # long expired
    assert severity_for_days(None) == "grey"

    assert bucket_for_days(10) == "expiring_soon"
    assert bucket_for_days(-10) == "recently_expired"
    assert bucket_for_days(120) == "active"
    assert bucket_for_days(-120) == "expired"
    assert bucket_for_days(None) == "unknown"


def test_normalize_order_pulls_renew_and_utilization():
    order = {
        "id": "/providers/Microsoft.Capacity/reservationOrders/abc",
        "name": "abc",
        "properties": {
            "displayName": "prod-vm",
            "term": "P1Y",
            "billingPlan": "Upfront",
            "createdDateTime": "2025-01-01T00:00:00Z",
            "expiryDate": "2026-01-01",
            "provisioningState": "Succeeded",
        },
    }
    reservations = [
        {
            "name": "res1",
            "sku": {"name": "Standard_D4s_v5"},
            "properties": {
                "renew": False,
                "quantity": 3,
                "reservedResourceType": "VirtualMachines",
                "appliedScopeType": "Shared",
                "utilization": {"aggregates": [{"grain": 1, "value": 87.5, "valueUnit": "percent"}]},
            },
        }
    ]
    rec = normalize_order(order, reservations)
    assert rec["id"] == "abc"
    assert rec["display_name"] == "prod-vm"
    assert rec["renew"] is False
    assert rec["utilization_pct"] == 87.5
    assert rec["sku"] == "Standard_D4s_v5"
    assert rec["quantity"] == 3
    assert rec["expiry_date"] == "2026-01-01"
    assert rec["reservation_count"] == 1


def test_normalize_order_without_children_has_no_renew_or_util():
    order = {"name": "x", "properties": {"displayName": "y", "expiryDate": "2026-01-01", "reservationsCount": 0}}
    rec = normalize_order(order, [])
    assert rec["renew"] is None
    assert rec["utilization_pct"] is None
    assert rec["reservation_count"] == 0


def _rec(name: str, expiry: str, **kw) -> dict:
    base = {
        "id": name,
        "display_name": name,
        "term": "P1Y",
        "created_date": "2025-01-01",
        "expiry_date": expiry,
        "provisioning_state": "Succeeded",
        "renew": True,
        "utilization_pct": 80.0,
        "sku": "Standard_D4s_v5",
        "reserved_resource_type": "VirtualMachines",
        "applied_scope_type": "Shared",
        "quantity": 1,
        "reservation_count": 1,
    }
    base.update(kw)
    return base


def test_compute_counts_and_sort():
    today = date(2026, 1, 1)
    records = [
        _rec("soon30", "2026-01-11"),                       # +10 → red, expiring_soon
        _rec("soon45", "2026-02-15"),                       # +45 → amber, expiring_soon
        _rec("recent", "2025-12-22", renew=False),          # -10 → red, recently_expired
        _rec("active", "2026-05-01"),                       # +120 → grey, active
        _rec("old", "2025-09-03", utilization_pct=10.0),    # -124 → grey, expired, low util
    ]
    snap = compute_reservations(records, window_days=60, today=today)
    c = snap["counts"]
    assert c["total"] == 5
    assert c["expiring_soon"] == 2
    assert c["recently_expired"] == 1
    assert c["active"] == 1
    assert c["expired"] == 1
    assert c["in_window"] == 3
    assert c["red"] == 2
    assert c["amber"] == 1
    assert c["non_renew"] == 1
    assert c["low_utilization"] == 1
    # Soonest first; the long-expired one is most negative so it leads.
    assert snap["items"][0]["id"] == "old"


def test_select_digest_items_window():
    today = date(2026, 1, 1)
    records = [
        _rec("soon10", "2026-01-11"),   # +10 in
        _rec("soon45", "2026-02-15"),   # +45 in
        _rec("recent", "2025-12-22"),   # -10 in
        _rec("active", "2026-05-01"),   # +120 out
        _rec("old", "2025-09-03"),      # -120 out
    ]
    snap = compute_reservations(records, window_days=60, today=today)
    sel = select_digest_items(snap, window_days=60)
    assert sel["count"] == 3
    assert len(sel["expiring_soon"]) == 2
    assert len(sel["recently_expired"]) == 1
    ids = {i["id"] for i in sel["items"]}
    assert ids == {"soon10", "soon45", "recent"}


def test_render_html_table_and_empty():
    today = date(2026, 1, 1)
    records = [_rec("R&D vm <prod>", "2026-01-11", renew=False, utilization_pct=12.0)]
    snap = compute_reservations(records, window_days=60, today=today)
    sel = select_digest_items(snap, window_days=60)
    html = render_html(sel["items"], window_days=60)
    assert "<table" in html
    assert "R&amp;D vm &lt;prod&gt;" in html  # HTML-escaped
    assert "No" in html  # auto-renew No
    # Empty render is a friendly message, not a table.
    empty = render_html([], window_days=60)
    assert "<table" not in empty
    assert "Nothing to action" in empty


def test_compute_due_weekly():
    # 2026-06-15 is a Monday; 13:00 UTC == 09:00 America/New_York (EDT), past 08:00.
    now = datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc)
    settings = {
        "reservations_digest_schedule_kind": "weekly",
        "reservations_digest_weekday": 0,
        "reservations_digest_time": "08:00",
        "reservations_digest_timezone": "America/New_York",
    }
    due, period_key = compute_due(settings, {}, now=now)
    assert due is True
    assert period_key == "2026-06-15"
    # Already sent for this period → not due again.
    due2, _ = compute_due(settings, {"period_key": period_key}, now=now)
    assert due2 is False


def test_demo_snapshot_has_items_and_window_buckets():
    snap = seed_demo(window_days=60)
    assert snap["demo"] is True
    assert snap["counts"]["total"] == len(snap["items"]) >= 4
    # The demo set is designed to include in-window items (expiring soon + recently expired).
    assert snap["counts"]["in_window"] >= 2

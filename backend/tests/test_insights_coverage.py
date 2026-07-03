"""AI Insight Packs — watcher-coverage helper tests.

Covers the pure logic behind ``GET /insights/coverage`` (the per-workload watcher view):
cadence-to-interval mapping, run-age computation, subscription-GUID extraction and the
scope→workload matching that decides whether a scheduled pack actually watches a workload.
"""
from datetime import datetime, timezone

from app.api import insights as ins
from app.models import ScheduledTask


# ------------------------------------------------------------------- _interval_days
def test_interval_days_weekly_is_seven():
    assert ins._interval_days("weekly") == 7.0


def test_interval_days_daily_cron_and_unknown_default_to_one():
    assert ins._interval_days("daily") == 1.0
    assert ins._interval_days("cron") == 1.0
    assert ins._interval_days("monthly") == 1.0
    assert ins._interval_days(None) == 1.0
    assert ins._interval_days("") == 1.0


# ------------------------------------------------------------------- _age_seconds
def test_age_seconds_handles_z_suffix_and_past():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert ins._age_seconds("2026-01-01T11:00:00Z", now) == 3600.0


def test_age_seconds_naive_is_treated_as_utc():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert ins._age_seconds("2026-01-01T11:00:00", now) == 3600.0


def test_age_seconds_future_is_negative():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert ins._age_seconds("2026-01-01T13:00:00Z", now) == -3600.0


def test_age_seconds_none_or_invalid_returns_none():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert ins._age_seconds(None, now) is None
    assert ins._age_seconds("", now) is None
    assert ins._age_seconds("not-a-date", now) is None


# ------------------------------------------------------------------- _sub_guid
def test_sub_guid_from_arm_id_and_plain_guid():
    assert ins._sub_guid("/subscriptions/ABCD-1234/resourceGroups/rg") == "abcd-1234"
    assert ins._sub_guid("ABCD-1234") == "abcd-1234"
    assert ins._sub_guid("/subscriptions/XYZ") == "xyz"
    assert ins._sub_guid("") == ""


# ------------------------------------------------------------------- _workload_sub_guids
def test_workload_sub_guids_collects_from_nodes():
    wl = {"nodes": [
        {"subscription_id": "/subscriptions/S1/resourceGroups/rg"},
        {"kind": "subscription", "id": "S2"},
        {"kind": "vm", "id": "vm-1"},
    ]}
    assert ins._workload_sub_guids(wl) == {"s1", "s2"}


def test_workload_sub_guids_empty_when_no_nodes():
    assert ins._workload_sub_guids(None) == set()
    assert ins._workload_sub_guids({}) == set()
    assert ins._workload_sub_guids({"nodes": [{"kind": "vm"}]}) == set()


# ------------------------------------------------------------------- _scope_covers_workload
def test_scope_covers_tenant_matches_everything():
    assert ins._scope_covers_workload({"mode": "tenant"}, "wl-1", set()) is True


def test_scope_covers_subscription_matches_on_guid():
    scope = {"mode": "subscription", "subscription_id": "/subscriptions/S1/x"}
    assert ins._scope_covers_workload(scope, "wl-1", {"s1"}) is True
    assert ins._scope_covers_workload(scope, "wl-1", {"s2"}) is False


def test_scope_covers_workload_by_id_list_or_singular():
    assert ins._scope_covers_workload({"mode": "workload", "workload_ids": ["wl-1", "wl-2"]}, "wl-1", set()) is True
    assert ins._scope_covers_workload({"mode": "workload", "workload_id": "wl-9"}, "wl-9", set()) is True
    assert ins._scope_covers_workload({"mode": "workload", "workload_ids": ["wl-2"]}, "wl-1", set()) is False


def test_scope_covers_defaults_to_workload_mode():
    # Missing mode is treated as workload scope, not tenant-wide.
    assert ins._scope_covers_workload({"workload_ids": ["wl-1"]}, "wl-1", set()) is True
    assert ins._scope_covers_workload({}, "wl-1", set()) is False


# ------------------------------------------------------------------- _task_schedule_dict
def test_task_schedule_dict_maps_schedule_fields():
    t = ScheduledTask(
        id="t1", tenant_id="dev", name="Daily Change", target_type="insight_pack",
        schedule_kind="daily", time_of_day="08:00", weekday=None, timezone="UTC",
    )
    d = ins._task_schedule_dict(t)
    assert d["schedule_kind"] == "daily"
    assert d["time_of_day"] == "08:00"
    assert d["timezone"] == "UTC"
    assert set(d) == {"schedule_kind", "cron_expr", "time_of_day", "weekday", "timezone", "start_date", "end_date"}

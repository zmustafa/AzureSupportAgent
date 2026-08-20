"""Derived RPO — the schedule parser and the configured/observed reconciliation.

The interesting cases are all ones where a plausible implementation is wrong by a large
factor and looks right: the hourly window, two runs an hour apart, and an unrecognized
policy quietly defaulting to a day.
"""
from __future__ import annotations

import json

from app.resiliency import model, rpo


def _rsv(**policy) -> str:
    return json.dumps(policy)


# --------------------------------------------------------------------------- daily
def test_a_daily_backup_is_a_twenty_four_hour_rpo_not_twelve():
    """Worst case, never average. At 01:59 you are 23h59 from the 02:00 recovery point."""
    minutes, summary = rpo.parse_schedule_interval(
        _rsv(scheduleRunFrequency="Daily", scheduleRunTimes=["2026-01-01T02:00:00Z"]))
    assert minutes == 1440
    assert "02:00" in summary


def test_two_runs_a_day_only_halve_the_rpo_if_they_are_twelve_hours_apart():
    """`period // len(points)` reports 12h for both of these. Only one is right."""
    even, _ = rpo.parse_schedule_interval(_rsv(
        scheduleRunFrequency="Daily",
        scheduleRunTimes=["2026-01-01T02:00:00Z", "2026-01-01T14:00:00Z"]))
    assert even == 720

    clustered, _ = rpo.parse_schedule_interval(_rsv(
        scheduleRunFrequency="Daily",
        scheduleRunTimes=["2026-01-01T02:00:00Z", "2026-01-01T03:00:00Z"]))
    assert clustered == 1380, "23 hours pass between 03:00 and the next 02:00"


# --------------------------------------------------------------------------- the window trap
def test_an_hourly_window_is_measured_by_the_overnight_gap_not_the_interval():
    """THE trap. 'Every 4 hours, 08:00-18:00' is not a 4-hour RPO — nothing runs for the
    14 hours after the last job. Reading only `interval` understates it threefold, and
    Backup Manager's own schedule summary renders exactly that way."""
    minutes, summary = rpo.parse_schedule_interval(_rsv(
        scheduleRunFrequency="Hourly",
        hourlySchedule={"interval": 4,
                        "scheduleWindowStartTime": "2026-01-01T08:00:00Z",
                        "scheduleWindowDuration": "PT10H"}))
    # Runs at 08:00, 12:00 and 16:00 — then nothing until 08:00 tomorrow. 16 hours.
    assert minutes == 960
    assert "worst gap" in summary


def test_a_window_narrower_than_the_interval_leaves_one_run_a_day():
    minutes, _ = rpo.parse_schedule_interval(_rsv(
        scheduleRunFrequency="Hourly",
        hourlySchedule={"interval": 4, "scheduleWindowDuration": "PT2H"}))
    assert minutes == 1440


def test_an_hourly_schedule_covering_the_whole_day_is_the_interval():
    minutes, _ = rpo.parse_schedule_interval(_rsv(
        scheduleRunFrequency="Hourly",
        hourlySchedule={"interval": 6, "scheduleWindowDuration": "PT24H"}))
    assert minutes == 360


def test_an_hourly_schedule_with_no_window_is_the_interval():
    minutes, summary = rpo.parse_schedule_interval(
        _rsv(hourlySchedule={"interval": 4}))
    assert minutes == 240
    assert summary == "Every 4h"


# --------------------------------------------------------------------------- weekly
def test_weekly_is_the_worst_gap_between_the_configured_days():
    minutes, summary = rpo.parse_schedule_interval(_rsv(
        scheduleRunFrequency="Weekly",
        scheduleRunDays=["Monday", "Thursday"],
        scheduleRunTimes=["2026-01-01T02:00:00Z"]))
    # Mon -> Thu is 3 days; Thu -> Mon is 4. The worst case is 4.
    assert minutes == 4 * 1440
    assert "Mon" in summary and "Thu" in summary


def test_a_single_weekly_day_is_a_full_week():
    minutes, _ = rpo.parse_schedule_interval(_rsv(
        scheduleRunFrequency="Weekly", scheduleRunDays=["Sunday"],
        scheduleRunTimes=["2026-01-01T02:00:00Z"]))
    assert minutes == 10_080


# --------------------------------------------------------------------------- data protection
def test_a_data_protection_recurrence_is_parsed():
    minutes, summary = rpo.parse_schedule_interval(
        {"repeatingTimeIntervals": ["R/2026-01-01T02:00:00+00:00/PT4H"]})
    assert minutes == 240
    assert "4h" in summary


def test_several_recurrences_take_the_worst():
    minutes, _ = rpo.parse_schedule_interval({"repeatingTimeIntervals": [
        "R/2026-01-01T02:00:00+00:00/PT4H", "R/2026-01-01T02:00:00+00:00/P1D"]})
    assert minutes == 1440


def test_iso_durations():
    assert rpo.parse_iso_duration("PT4H") == 240
    assert rpo.parse_iso_duration("P1D") == 1440
    assert rpo.parse_iso_duration("P1W") == 10_080
    assert rpo.parse_iso_duration("PT30M") == 30
    assert rpo.parse_iso_duration("nonsense") is None


# --------------------------------------------------------------------------- unknown
def test_an_unrecognised_schedule_is_unknown_never_a_daily_default():
    """A wrong default here is invisible and understates exposure."""
    for raw in ("", None, "{}", "not json", json.dumps({"somethingElse": 1})):
        minutes, summary = rpo.parse_schedule_interval(raw)
        assert minutes is None, raw
        assert summary == ""


def test_a_malformed_hourly_interval_is_unknown():
    minutes, _ = rpo.parse_schedule_interval(_rsv(hourlySchedule={"interval": "banana"}))
    assert minutes is None
    minutes, _ = rpo.parse_schedule_interval(_rsv(hourlySchedule={"interval": 0}))
    assert minutes is None


def test_properties_are_accepted_as_dict_or_string():
    """Resource Graph returns properties as dynamic OR string depending on the query."""
    as_dict = {"scheduleRunFrequency": "Daily", "scheduleRunTimes": ["2026-01-01T02:00:00Z"]}
    assert rpo.parse_schedule_interval(as_dict)[0] == 1440
    assert rpo.parse_schedule_interval(json.dumps(as_dict))[0] == 1440


# --------------------------------------------------------------------------- native
def test_native_mechanisms_carry_a_confidence_that_reflects_how_they_are_known():
    minutes, confidence, detail = rpo.native_rpo("cosmos_multi_write")
    assert minutes == 0 and confidence == model.CONFIDENCE_HIGH and detail

    # Published, not SLA'd — the product must not present this as firm.
    minutes, confidence, _ = rpo.native_rpo("storage_grs")
    assert minutes == 15 and confidence == model.CONFIDENCE_LOW

    assert rpo.native_rpo("no-such-mechanism") == (None, model.CONFIDENCE_LOW, "")


# --------------------------------------------------------------- configured vs observed
def test_reality_wins_when_the_backups_are_not_keeping_up():
    """A daily policy whose job failed for six days is 144h, not 24h. Reporting the
    configured value is how a dashboard stays green through a week of failures."""
    minutes, confidence, drift = rpo.observed_vs_configured(1440, 144.0)
    assert minutes == 144 * 60
    assert drift is not None
    assert "not being met" in drift.detail
    assert confidence == model.CONFIDENCE_HIGH


def test_a_healthy_schedule_reports_the_configured_interval_and_no_drift():
    minutes, _, drift = rpo.observed_vs_configured(1440, 20.0)
    assert minutes == 1440
    assert drift is None


def test_a_little_lateness_is_not_drift():
    """A job that starts on time still takes time to finish."""
    minutes, _, drift = rpo.observed_vs_configured(240, 5.0)
    assert minutes == 240 and drift is None


def test_an_unknown_recovery_point_falls_back_to_the_configuration():
    minutes, _, drift = rpo.observed_vs_configured(720, None)
    assert minutes == 720 and drift is None


def test_nothing_known_at_all_stays_none():
    minutes, confidence, drift = rpo.observed_vs_configured(None, None)
    assert minutes is None and drift is None and confidence == model.CONFIDENCE_LOW


def test_an_observed_point_with_no_policy_is_still_usable_but_less_certain():
    minutes, confidence, _ = rpo.observed_vs_configured(None, 6.0)
    assert minutes == 360
    assert confidence == model.CONFIDENCE_MEDIUM

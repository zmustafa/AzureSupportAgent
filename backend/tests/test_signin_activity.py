"""`core.signin_activity.failed_signin` — the inference that turns two stamps into a failure.

Graph has no failure flag. Getting this wrong in either direction is costly: a false
positive brands a healthy application as broken, a false negative hides the expired
credential that a cleanup review exists to find.
"""
from __future__ import annotations

from app.core.signin_activity import failed_signin


def test_an_attempt_after_the_last_success_is_a_failure():
    assert failed_signin("2026-08-21T06:03:08Z", "2025-09-17T11:00:00Z") == "2026-08-21T06:03:08Z"


def test_an_attempt_equal_to_the_last_success_is_that_success_not_a_failure():
    stamp = "2026-08-21T06:03:08Z"
    assert failed_signin(stamp, stamp) == ""


def test_an_attempt_older_than_the_last_success_is_not_a_failure():
    assert failed_signin("2025-01-01T00:00:00Z", "2026-08-21T06:03:08Z") == ""


def test_an_attempt_with_no_recorded_success_is_treated_as_failing():
    assert failed_signin("2026-08-21T06:03:08Z", "") == "2026-08-21T06:03:08Z"
    assert failed_signin("2026-08-21T06:03:08Z", None) == "2026-08-21T06:03:08Z"


def test_no_attempt_means_no_failure_regardless_of_success():
    assert failed_signin("", "2026-08-21T06:03:08Z") == ""
    assert failed_signin(None, None) == ""


def test_fractional_seconds_do_not_invert_the_comparison():
    """The reason this parses instead of comparing strings.

    Lexically ``"...:00.5Z" < "...:00Z"`` because '.' (0x2E) sorts below 'Z' (0x5A), so a
    string compare would call the LATER attempt older and report no failure.
    """
    later_attempt = "2026-08-21T06:03:00.500000Z"
    earlier_success = "2026-08-21T06:03:00Z"
    assert later_attempt < earlier_success, "premise: these sort the wrong way as strings"
    assert failed_signin(later_attempt, earlier_success) == later_attempt


def test_mixed_offsets_are_normalised_before_comparing():
    # 11:00+02:00 == 09:00Z, so an attempt at 09:30Z is genuinely later.
    assert failed_signin("2026-08-21T09:30:00Z", "2026-08-21T11:00:00+02:00") == "2026-08-21T09:30:00Z"
    assert failed_signin("2026-08-21T08:30:00Z", "2026-08-21T11:00:00+02:00") == ""


def test_a_naive_stamp_is_assumed_utc_rather_than_crashing():
    assert failed_signin("2026-08-21T10:00:00", "2026-08-21T09:00:00Z") == "2026-08-21T10:00:00"


def test_unparseable_input_never_raises_and_never_invents_a_failure():
    assert failed_signin("not a date", "2026-08-21T06:03:08Z") == ""
    # An unreadable SUCCESS must not silently promote the attempt... except that "no usable
    # success" is exactly the attempt-with-no-success case, which IS reported as failing.
    assert failed_signin("2026-08-21T06:03:08Z", "junk") == "2026-08-21T06:03:08Z"

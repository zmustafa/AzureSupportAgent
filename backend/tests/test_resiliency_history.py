"""Recovery Readiness history and trend."""
from __future__ import annotations

import pytest

from app.resiliency import history


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    history.set_path_for_tests(tmp_path / "history.json")
    yield
    history.clear()


def _snap(at, *, no_path=0, undetermined=0, breaches=0, resources=10, demo=False,
          exists=True):
    return {
        "report_exists": exists,
        "generated_at": at,
        "demo": demo,
        "summary": {
            "resources": resources,
            "by_scenario": {"region_loss": {"no_recovery_path": no_path,
                                            "undetermined": undetermined}},
            "protection": {"protected": 5, "not_protected": 3, "unknown": 2},
        },
        "breaches": [{"resource_id": f"r{i}"} for i in range(breaches)],
    }


def _record(at, **kw):
    history.record("t", "c", "workload", "w", _snap(at, **kw))


def test_a_point_stores_counts_not_rows():
    """A history that stored snapshots would grow without bound and duplicate the thing it
    is a history of."""
    point = history.point_from(_snap("2026-01-01T00:00:00Z", no_path=4))
    assert "resources" in point and isinstance(point["resources"], int)
    assert "verdicts" not in repr(point)
    assert point["no_recovery_path"] == 4


def test_one_analysis_is_not_a_trend():
    """A sparkline through a single point invites a reader to see a direction that was
    never measured."""
    _record("2026-01-01T00:00:00Z", no_path=4)
    result = history.trend("t", "c", "workload", "w")
    assert result["available"] is False
    assert len(result["points"]) == 1
    assert "two analyses" in result["reason"]


def test_two_analyses_give_a_direction():
    _record("2026-01-01T00:00:00Z", no_path=10, breaches=6)
    _record("2026-02-01T00:00:00Z", no_path=4, breaches=2)
    result = history.trend("t", "c", "workload", "w")
    assert result["available"] is True
    assert result["deltas"]["no_recovery_path"] == -6
    assert result["deltas"]["breaches"] == -4
    assert result["reading_degraded"] is False


def test_an_improvement_caused_by_blindness_is_flagged_not_celebrated():
    """Fewer resources without a recovery path, because more became unreadable, is not an
    improvement — and a line that cannot show the difference will be read as one."""
    _record("2026-01-01T00:00:00Z", no_path=10, undetermined=0)
    _record("2026-02-01T00:00:00Z", no_path=2, undetermined=9)
    result = history.trend("t", "c", "workload", "w")
    assert result["deltas"]["no_recovery_path"] == -8
    assert result["reading_degraded"] is True
    assert "not necessarily an improvement" in result["caveat"]


def test_undetermined_travels_with_every_point():
    _record("2026-01-01T00:00:00Z", undetermined=7)
    assert history.read("t", "c", "workload", "w")[0]["undetermined"] == 7


def test_demo_analyses_are_never_recorded():
    """A synthetic trend line printed beside real numbers is the kind of thing that gets
    quoted."""
    _record("2026-01-01T00:00:00Z", no_path=4, demo=True)
    assert history.read("t", "c", "workload", "w") == []


def test_an_unfinished_analysis_is_not_recorded():
    _record("2026-01-01T00:00:00Z", exists=False)
    assert history.read("t", "c", "workload", "w") == []


def test_re_analysing_at_the_same_instant_replaces_rather_than_duplicates():
    _record("2026-01-01T00:00:00Z", no_path=4)
    _record("2026-01-01T00:00:00Z", no_path=9)
    points = history.read("t", "c", "workload", "w")
    assert len(points) == 1
    assert points[0]["no_recovery_path"] == 9


def test_points_are_kept_in_time_order_even_if_recorded_out_of_order():
    _record("2026-03-01T00:00:00Z", no_path=1)
    _record("2026-01-01T00:00:00Z", no_path=3)
    _record("2026-02-01T00:00:00Z", no_path=2)
    order = [p["generated_at"] for p in history.read("t", "c", "workload", "w")]
    assert order == sorted(order)
    # The delta must be computed oldest -> newest, not first-written -> last-written.
    assert history.trend("t", "c", "workload", "w")["deltas"]["no_recovery_path"] == -2


def test_the_series_is_bounded_and_keeps_the_most_recent():
    for i in range(history.MAX_POINTS + 15):
        _record(f"2026-01-01T00:{i:02d}:00Z", no_path=i)
    points = history.read("t", "c", "workload", "w")
    assert len(points) == history.MAX_POINTS
    assert points[-1]["no_recovery_path"] == history.MAX_POINTS + 14


def test_scopes_are_isolated():
    history.record("t", "c", "workload", "a", _snap("2026-01-01T00:00:00Z", no_path=1))
    history.record("t", "c", "workload", "b", _snap("2026-01-01T00:00:00Z", no_path=9))
    assert history.read("t", "c", "workload", "a")[0]["no_recovery_path"] == 1
    assert history.read("t", "c", "workload", "b")[0]["no_recovery_path"] == 9


def test_the_number_of_tracked_scopes_is_bounded():
    for i in range(history.MAX_SCOPES + 5):
        history.record("t", "c", "workload", f"w{i}",
                       _snap(f"2026-01-{i + 1:02d}T00:00:00Z"))
    # The oldest scopes are dropped; the newest survive.
    assert history.read("t", "c", "workload", "w0") == []
    assert history.read("t", "c", "workload", f"w{history.MAX_SCOPES + 4}")


def test_an_unreadable_store_starts_empty_rather_than_exploding(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not json", encoding="utf-8")
    history.set_path_for_tests(path)
    assert history.read("t", "c", "workload", "w") == []


def test_a_missing_point_leaves_a_gap_rather_than_being_interpolated():
    """Drawing through a gap invents measurements that were never taken."""
    _record("2026-01-01T00:00:00Z", no_path=10)
    _record("2026-04-01T00:00:00Z", no_path=2)
    points = history.read("t", "c", "workload", "w")
    assert len(points) == 2, "no synthetic points for February or March"

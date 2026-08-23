"""The incremental sign-in rollup in the risk collector.

The collector used to re-download the tenant's entire sign-in window on every refresh — up
to 200,000 events across ~200 serial requests — to produce a few hundred aggregate numbers,
then throw the events away and do it again next time.

It now folds events into per-day buckets, persists them, and re-reads only the newest partial
day onwards. These tests exist because that refactor can break the numbers silently: the two
that matter most prove a day-split pass renders *identically* to a flat one, and that a
resumed read does not double-count.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.entra import cache
from app.entra.collectors import CollectContext
from app.entra.collectors import risk as R


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path):
    cache.set_root_for_tests(tmp_path / "entra")
    yield
    cache.clear_memo()


def _ctx(lookback: int = 30) -> CollectContext:
    return CollectContext(tenant_id="t", signin_lookback_days=lookback)


def _signin(day: str, *, hour: int = 9, user: str = "u1", app: str = "a1", code: int = 0,
            client_app: str = "Browser", ip: str = "1.2.3.4", compliant=None,
            interactive: bool = True, country: str = "GB", ca=()):
    return {
        "id": f"{day}-{hour}-{user}-{app}-{code}",
        "createdDateTime": f"{day}T{hour:02d}:00:00Z",
        "userId": user, "userPrincipalName": f"{user}@example.test",
        "userDisplayName": user.upper(), "appId": app, "appDisplayName": f"App {app}",
        "clientAppUsed": client_app, "ipAddress": ip,
        "status": {"errorCode": code, "failureReason": "" if not code else "nope"},
        "location": {"countryOrRegion": country},
        "deviceDetail": {"isCompliant": compliant, "displayName": "dev"},
        "isInteractive": interactive,
        "appliedConditionalAccessPolicies": list(ca),
    }


def _corpus() -> list[dict]:
    """A spread that touches every dimension the aggregator folds."""
    rows = []
    for i, day in enumerate(["2026-08-01", "2026-08-02", "2026-08-03"]):
        rows += [
            _signin(day, user="u1", app="a1"),
            _signin(day, user="u2", app="a1", code=50126),
            _signin(day, user="u2", app="a2", client_app="IMAP4"),
            _signin(day, user=f"u{i}", app="a2", compliant=False),
            _signin(day, user="u3", app="a1", code=500121),
            _signin(day, user="u4", app="a3", country="FR",
                    ca=[{"id": "p1", "displayName": "Report", "result": "reportOnlyFailure",
                         "enforcedGrantControls": ["Mfa"]}]),
        ]
    return rows


def _render(rows):
    agg = R._Aggregator()
    for r in rows:
        agg.add(r)
    return agg.payload(sampled=False, lookback_days=30), agg.patterns()


# --------------------------------------------------------------------------- equivalence
def test_a_day_split_pass_renders_identically_to_a_single_pass():
    """The refactor's core claim. If this drifts, every chart silently changes."""
    rows = _corpus()
    whole, whole_patterns = _render(rows)

    # Same rows, but folded as three separate days then merged — which is what a resumed
    # read produces.
    split = R._Aggregator()
    for r in rows:
        split.add(r)
    rebuilt = R._Aggregator()
    rebuilt.load_days(split.to_json())

    assert rebuilt.payload(sampled=False, lookback_days=30) == whole
    assert rebuilt.patterns() == whole_patterns


def test_a_round_trip_through_json_preserves_every_dimension():
    rows = _corpus()
    before, _ = _render(rows)

    agg = R._Aggregator()
    for r in rows:
        agg.add(r)
    revived = R._Aggregator()
    revived.load_days(agg.to_json())

    assert revived.payload(sampled=False, lookback_days=30) == before


def test_distinct_user_counts_survive_the_merge():
    """Counts can be summed; distinct sets cannot. Merging must union, not add."""
    agg = R._Aggregator()
    agg.add(_signin("2026-08-01", user="u1", app="a1"))
    agg.add(_signin("2026-08-02", user="u1", app="a1"))
    payload = agg.payload(sampled=False, lookback_days=30)
    app_row = next(r for r in payload["by_app"] if r["app_id"] == "a1")
    assert app_row["total"] == 2
    assert app_row["users"] == 1, "the same user on two days is one distinct user"


def test_merging_keeps_the_newest_last_seen():
    agg = R._Aggregator()
    agg.add(_signin("2026-08-01", user="u1", app="a1"))
    agg.add(_signin("2026-08-03", user="u1", app="a1"))
    row = next(r for r in agg.payload(sampled=False, lookback_days=30)["by_app"]
               if r["app_id"] == "a1")
    assert row["last_seen"].startswith("2026-08-03")


def test_by_day_is_derived_from_the_buckets():
    agg = R._Aggregator()
    agg.add(_signin("2026-08-01"))
    agg.add(_signin("2026-08-01", user="u9", code=50126))
    agg.add(_signin("2026-08-02"))
    rows = agg.payload(sampled=False, lookback_days=30)["by_day"]
    assert [r["day"] for r in rows] == ["2026-08-01", "2026-08-02"]
    assert rows[0] == {"day": "2026-08-01", "total": 2, "success": 1, "failure": 1, "mfa": 0}


# --------------------------------------------------------------------------- resume
def _stash(agg, *, lookback=30, sampled=False, version=R.ROLLUP_VERSION):
    cache.write_state("t", R.ROLLUP_STATE, {
        "version": version, "lookback_days": lookback, "sampled": sampled,
        "days": agg.to_json(),
    })


def _today(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _since(lookback: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_cold_cache_reads_the_whole_window():
    agg = R._Aggregator()
    resume, reused = R._resume_point(_ctx(), agg, _since())
    assert resume == ""
    assert reused == 0


def test_whole_days_are_reused_and_the_newest_is_re_read():
    """The newest stored day was partial when written, so it must not be trusted."""
    seed = R._Aggregator()
    for d in (3, 2, 1):
        seed.add(_signin(_today(d)))
    _stash(seed)

    agg = R._Aggregator()
    resume, reused = R._resume_point(_ctx(), agg, _since())
    assert reused == 2, "the newest of three days is dropped and re-read"
    assert resume == f"{_today(1)}T00:00:00Z"
    assert _today(1) not in agg.days


def test_a_resumed_read_does_not_double_count_the_partial_day():
    """End to end: stash two days, then re-read the newest and confirm the totals."""
    seed = R._Aggregator()
    seed.add(_signin(_today(2), user="u1"))
    seed.add(_signin(_today(1), user="u1"))
    _stash(seed)

    agg = R._Aggregator()
    R._resume_point(_ctx(), agg, _since())
    # The read replays the dropped day from Graph.
    agg.add(_signin(_today(1), user="u1"))
    assert agg.payload(sampled=False, lookback_days=30)["total"] == 2


def test_a_changed_lookback_window_forces_a_full_read():
    """A wider window cannot be served from a narrower rollup."""
    seed = R._Aggregator()
    seed.add(_signin(_today(2)))
    seed.add(_signin(_today(1)))
    _stash(seed, lookback=7)
    agg = R._Aggregator()
    assert R._resume_point(_ctx(lookback=30), agg, _since()) == ("", 0)


def test_a_stale_rollup_version_forces_a_full_read():
    seed = R._Aggregator()
    seed.add(_signin(_today(2)))
    seed.add(_signin(_today(1)))
    _stash(seed, version=R.ROLLUP_VERSION - 1)
    agg = R._Aggregator()
    assert R._resume_point(_ctx(), agg, _since()) == ("", 0)


def test_a_capped_rollup_is_still_reused():
    """Measured on a large tenant: a 30-day read hits the cap EVERY time.

    Refusing to reuse it meant the tenants slow enough to cap were the only ones that never
    benefited from the rollup — a 26-minute read repeated on every refresh, forever.
    """
    seed = R._Aggregator()
    seed.add(_signin(_today(3)))
    seed.add(_signin(_today(2)))
    seed.add(_signin(_today(1)))
    _stash(seed, sampled=True)
    agg = R._Aggregator()
    resume, reused = R._resume_point(_ctx(), agg, _since())
    assert reused, "a capped rollup still holds complete days"
    assert resume


def test_days_outside_the_window_are_dropped():
    seed = R._Aggregator()
    seed.add(_signin("2020-01-01"))
    seed.add(_signin(_today(2)))
    seed.add(_signin(_today(1)))
    _stash(seed)
    agg = R._Aggregator()
    R._resume_point(_ctx(), agg, _since())
    assert "2020-01-01" not in agg.days


def test_a_capped_run_drops_only_the_undercounted_oldest_day():
    """The cap stops part-way through the OLDEST day reached; everything newer was read in
    full and is worth keeping."""
    agg = R._Aggregator()
    agg.add(_signin(_today(3)))
    agg.add(_signin(_today(2)))
    agg.add(_signin(_today(1)))
    R._save_rollup(_ctx(), agg, sampled=True)
    blob = cache.read_state("t", R.ROLLUP_STATE, {})
    assert _today(3) not in blob["days"], "the boundary day is an undercount"
    assert sorted(blob["days"]) == sorted([_today(2), _today(1)])
    assert blob["sampled"] is True


def test_a_capped_run_with_a_single_day_persists_nothing():
    """That one day IS the boundary, so there is nothing complete to keep."""
    agg = R._Aggregator()
    agg.add(_signin(_today(1)))
    R._save_rollup(_ctx(), agg, sampled=True)
    assert cache.read_state("t", R.ROLLUP_STATE, {})["days"] == {}


def test_a_clean_run_persists_its_days():
    agg = R._Aggregator()
    agg.add(_signin(_today(1)))
    R._save_rollup(_ctx(), agg, sampled=False)
    blob = cache.read_state("t", R.ROLLUP_STATE, {})
    assert list(blob["days"]) == [_today(1)]


def test_rows_with_no_timestamp_never_become_a_reusable_day():
    """An 'unknown' bucket must not be persisted as if it were a real day."""
    agg = R._Aggregator()
    agg.add({"id": "x", "status": {"errorCode": 0}})
    assert "unknown" in agg.days
    _stash(agg)
    revived = R._Aggregator()
    R._resume_point(_ctx(), revived, _since())
    assert "unknown" not in revived.days

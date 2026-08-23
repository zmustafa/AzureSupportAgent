"""The Entra applications inventory sign-in columns.

Two sources disagree by design: the aggregate report is one cheap call but cannot separate a
success from a rejected attempt, and the per-event log can but costs one slow call per
application. These tests pin the merge rules, the cache that keeps those calls off the refresh
path, and the honesty rules — because getting them wrong renders "we have not read this yet"
as "this application has no failures", which are opposite facts that look identical.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.entra import cache, signin_outcomes as so
from app.entra.collectors import CollectContext
from app.entra.collectors.apps import _signin_activity
from app.entra.graphclient import GraphError


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path):
    cache.set_root_for_tests(tmp_path / "entra")
    yield
    cache.clear_memo()


@pytest.fixture(autouse=True)
def _default_scope(monkeypatch):
    """Pin the scope so an operator's saved settings cannot change what these prove."""
    monkeypatch.setattr(so, "settings", lambda: (so.SCOPE_VISIBLE, 86400, 100, 300))


def _iso(delta_s: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- merge rules
def _block(**seen):
    return {"last_seen": dict(seen), "last_failed": {}, "measured": True}


def test_the_event_log_supplies_a_success_the_aggregate_never_reports():
    """The reason the per-app read exists: the aggregate has no success stamp for SPs."""
    block = _block(a="2026-08-20T09:00:00Z")
    so.merge_outcomes(block, {"a": {"success": "2026-08-22T08:00:00Z", "failed": ""}})
    assert block["last_seen"]["a"] == "2026-08-22T08:00:00Z"


def test_an_app_whose_every_event_failed_loses_the_aggregate_stamp():
    """The regression this column exists to prevent.

    The aggregate's stamp is an ATTEMPT. When the log proves every attempt in the same window
    was rejected, reporting it as a sign-in makes an app nobody can authenticate to look live.
    """
    block = _block(a="2026-08-21T09:00:00Z")
    so.merge_outcomes(block, {"a": {"success": "", "failed": "2026-08-21T09:00:00Z"}})
    assert "a" not in block["last_seen"]
    assert block["last_failed"]["a"] == "2026-08-21T09:00:00Z"


def test_an_app_the_log_had_no_opinion_on_keeps_the_aggregate_stamp():
    """An empty outcome is not a contradiction — the aggregate reaches back further."""
    block = _block(a="2026-08-21T09:00:00Z")
    so.merge_outcomes(block, {"a": {"success": "", "failed": ""}})
    assert block["last_seen"]["a"] == "2026-08-21T09:00:00Z"


def test_a_success_wins_over_a_failure_for_the_same_app():
    block = _block()
    so.merge_outcomes(block, {"a": {"success": "2026-08-22T10:00:00Z",
                                    "failed": "2026-08-21T09:00:00Z"}})
    assert block["last_seen"]["a"] == "2026-08-22T10:00:00Z"
    assert block["last_failed"]["a"] == "2026-08-21T09:00:00Z"


def test_active_app_ids_tracks_the_merged_result():
    block = _block(a="2026-08-20T09:00:00Z", b="2026-08-20T09:00:00Z")
    so.merge_outcomes(block, {"a": {"success": "", "failed": "2026-08-21T09:00:00Z"}})
    assert block["active_app_ids"] == ["b"], "a rejected app must drop out of the active set"


def test_app_ids_are_matched_case_insensitively():
    block = _block(**{"aaaa-bbbb": "2026-08-20T09:00:00Z"})
    so.merge_outcomes(block, {"AAAA-BBBB": {"success": "2026-08-22T08:00:00Z", "failed": ""}})
    assert block["last_seen"]["aaaa-bbbb"] == "2026-08-22T08:00:00Z"


# --------------------------------------------------------------------------- cache
def test_a_quiet_application_is_recorded_as_checked():
    """Otherwise the ~97% of applications with no events are re-read on every single run."""
    entries = so.record({}, ["a", "b"], {"a": {"success": "2026-08-22T08:00:00Z"}})
    assert entries["b"]["success"] == ""
    assert entries["b"]["checked_at"], "a quiet app still needs a stamp or it never ages out"


def test_only_apps_with_an_outcome_reach_the_merge():
    entries = so.record({}, ["a", "b"], {"a": {"success": "2026-08-22T08:00:00Z"}})
    assert sorted(so.cached_by_app(entries)) == ["a"]


def test_the_cache_survives_a_round_trip():
    so.write_cache("t", so.record({}, ["a"], {"a": {"success": "2026-08-22T08:00:00Z"}}))
    assert so.read_cache("t")["a"]["success"] == "2026-08-22T08:00:00Z"


def test_a_never_checked_app_is_always_stale():
    assert so.select_stale(["a"], {}, ttl_s=86400, cap=10) == ["a"]


def test_an_app_inside_the_ttl_is_skipped():
    entries = so.record({}, ["a"], {}, at=_iso(-60))
    assert so.select_stale(["a"], entries, ttl_s=86400, cap=10) == []


def test_an_app_past_the_ttl_is_re_read():
    entries = so.record({}, ["a"], {}, at=_iso(-90_000))
    assert so.select_stale(["a"], entries, ttl_s=86400, cap=10) == ["a"]


def test_never_checked_apps_are_read_before_merely_stale_ones():
    entries = so.record({}, ["old"], {}, at=_iso(-90_000))
    assert so.select_stale(["old", "new"], entries, ttl_s=86400, cap=10)[0] == "new"


def test_the_stalest_is_read_first_among_checked_apps():
    entries = so.record({}, ["older"], {}, at=_iso(-200_000))
    entries = so.record(entries, ["newer"], {}, at=_iso(-90_000))
    assert so.select_stale(["newer", "older"], entries, ttl_s=86400, cap=10) == ["older", "newer"]


def test_the_cap_bounds_one_run():
    ids = [f"a{i}" for i in range(50)]
    assert len(so.select_stale(ids, {}, ttl_s=86400, cap=10)) == 10


def test_a_zero_cap_reads_nothing():
    assert so.select_stale(["a"], {}, ttl_s=86400, cap=0) == []


def test_an_unparseable_timestamp_is_treated_as_stale():
    """A corrupt stamp must re-read, not silently pin the app as fresh forever."""
    assert so.select_stale(["a"], {"a": {"checked_at": "not-a-date"}}, ttl_s=86400, cap=5) == ["a"]


# --------------------------------------------------------------------------- scope
_PAYLOAD = {
    "applications": [{"app_id": "LOCAL-1"}],
    "service_principals": [
        {"app_id": "third-party", "is_first_party": False},
        {"app_id": "ms-graph", "is_first_party": True},
    ],
}


def test_visible_scope_excludes_first_party_microsoft_apps():
    """They are filtered out of the grid, so reading their log populates nothing."""
    assert so.in_scope_app_ids(_PAYLOAD, so.SCOPE_VISIBLE) == ["local-1", "third-party"]


def test_all_scope_includes_first_party():
    assert "ms-graph" in so.in_scope_app_ids(_PAYLOAD, so.SCOPE_ALL)


def test_off_scope_reads_nothing():
    assert so.in_scope_app_ids(_PAYLOAD, so.SCOPE_OFF) == []


def test_apps_without_an_id_are_dropped_rather_than_queried_as_blank():
    payload = {"applications": [{"app_id": ""}], "service_principals": [{"is_first_party": False}]}
    assert so.in_scope_app_ids(payload, so.SCOPE_VISIBLE) == []


# --------------------------------------------------------------------------- collector
class FakeClient:
    concurrency = 4

    def __init__(self, *, aggregate=None, aggregate_error=None):
        self.aggregate = aggregate if aggregate is not None else []
        self.aggregate_error = aggregate_error
        self.event_calls: list[str] = []

    async def get_all(self, path, **kw):
        if self.aggregate_error:
            raise self.aggregate_error
        return list(self.aggregate), False

    async def get(self, path, **kw):
        self.event_calls.append(path)
        raise AssertionError("the refresh path must not read the per-event log")


def _row(app_id: str, *, last: str = ""):
    return {"appId": app_id, "lastSignInActivity": {"lastSignInDateTime": last} if last else {}}


def _run(client, tenant="t"):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    return asyncio.run(_signin_activity(client, CollectContext(tenant_id=tenant), now))


def test_the_refresh_path_makes_no_per_application_calls():
    """The point of detaching: a refresh must not pay for dozens of slow reads."""
    client = FakeClient(aggregate=[_row("a", last="2026-08-21T09:00:00Z")])
    block = _run(client)
    assert client.event_calls == []
    assert block["last_seen"]["a"] == "2026-08-21T09:00:00Z"


def test_an_unread_cache_reports_pending_not_no_failures():
    block = _run(FakeClient(aggregate=[]))
    assert block["outcomes"]["measured"] is False
    assert block["outcomes"]["pending"] is True


def test_a_populated_cache_is_applied_on_the_refresh_path():
    so.write_cache("t", so.record({}, ["a"], {"a": {"success": "", "failed": "2026-08-22T08:00:00Z"}}))
    block = _run(FakeClient(aggregate=[_row("a", last="2026-08-22T08:00:00Z")]))
    assert block["last_failed"]["a"] == "2026-08-22T08:00:00Z"
    assert "a" not in block["last_seen"]
    assert block["outcomes"]["measured"] is True


def test_turning_outcomes_off_says_so_rather_than_reporting_no_failures(monkeypatch):
    monkeypatch.setattr(so, "settings", lambda: (so.SCOPE_OFF, 86400, 100, 300))
    block = _run(FakeClient(aggregate=[]))
    assert block["outcomes"]["measured"] is False
    assert "turned off" in block["outcomes"]["reason"]


def test_a_dead_aggregate_is_reported_as_unmeasured():
    block = _run(FakeClient(aggregate_error=GraphError(403, "no")))
    assert block["measured"] is False
    assert block["last_seen"] == {}
    assert block["last_failed"] == {}


# --------------------------------------------------------------------------- backfill
class OutcomeClient:
    """Serves the per-event log for the backfill."""

    concurrency = 4

    def __init__(self, events=None, error=None, delay=0.0):
        self.events = events or {}
        self.error = error
        self.delay = delay
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, *, params=None, beta=False, advanced=False):
        filt = str((params or {}).get("$filter") or "")
        app_id = filt.split("appId eq '")[-1].rstrip("'") if "appId eq '" in filt else ""
        self.calls.append(app_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return {"value": list(self.events.get(app_id) or [])}


def _event(at: str, code: int = 0):
    return {"createdDateTime": at, "status": {"errorCode": code}}


def _seed_snapshot(sps=None, tenant="t"):
    cache.write_domain(tenant, "apps", {
        "domain": "apps",
        "data": {
            "applications": [],
            "service_principals": sps if sps is not None else [
                {"app_id": "a", "is_first_party": False},
                {"app_id": "ms", "is_first_party": True},
            ],
            "signin_activity": {"measured": True, "last_seen": {}, "last_failed": {},
                                "window_days": 30},
        },
    })


def test_the_backfill_reads_only_in_scope_apps_and_patches_the_snapshot(monkeypatch):
    _seed_snapshot()
    client = OutcomeClient(events={"a": [_event("2026-08-22T08:00:00Z", code=50126)]})
    monkeypatch.setattr(so, "GraphClient", lambda *a, **k: client)

    out = asyncio.run(so.run_backfill("t", {}))
    assert out["ran"] is True
    assert client.calls == ["a"], "a first-party app must not be read under the visible scope"

    block = cache.read_domain("t", "apps")["data"]["signin_activity"]
    assert block["last_failed"]["a"] == "2026-08-22T08:00:00Z"
    assert block["outcomes"]["measured"] is True


def test_a_second_backfill_inside_the_ttl_reads_nothing(monkeypatch):
    _seed_snapshot()
    client = OutcomeClient(events={"a": []})
    monkeypatch.setattr(so, "GraphClient", lambda *a, **k: client)

    asyncio.run(so.run_backfill("t", {}))
    first = len(client.calls)
    out = asyncio.run(so.run_backfill("t", {}))
    assert len(client.calls) == first, "a cached quiet app must not be re-read inside the TTL"
    assert out["ran"] is False


def test_the_backfill_is_capped_and_reports_what_is_left(monkeypatch):
    _seed_snapshot(sps=[{"app_id": f"a{i}", "is_first_party": False} for i in range(5)])
    client = OutcomeClient()
    monkeypatch.setattr(so, "GraphClient", lambda *a, **k: client)
    monkeypatch.setattr(so, "settings", lambda: (so.SCOPE_VISIBLE, 86400, 2, 300))

    out = asyncio.run(so.run_backfill("t", {}))
    assert out["checked"] == 2
    assert out["remaining"] == 3, "the rest must be reported, not silently dropped"


def test_a_spent_time_budget_stops_the_pass():
    """A count alone is a weak bound: the same 100 apps cost 12 s on one tenant and ~700 s on
    another, so the pass is bounded by wall clock too.

    The invariant that matters is not *how many* got through, but that an app abandoned at the
    deadline is never recorded as checked — doing so would freeze "no events" into the cache
    for a whole TTL without ever having asked.
    """
    ids = [f"a{i}" for i in range(40)]
    client = OutcomeClient(events={i: [] for i in ids}, delay=0.05)

    async def run():
        return await so.read_signin_outcomes(client, ids, max_seconds=0.02, concurrency=2)

    out = asyncio.run(run())
    assert len(client.calls) < len(ids), "the budget must cut the pass short"
    assert sorted(out["checked"]) == sorted(client.calls), "only queried apps count as checked"
    assert out["capped"] is True


def test_an_unspent_time_budget_reads_everything():
    client = OutcomeClient(events={f"a{i}": [] for i in range(4)})

    async def run():
        return await so.read_signin_outcomes(
            client, [f"a{i}" for i in range(4)], max_seconds=600)

    out = asyncio.run(run())
    assert sorted(client.calls) == ["a0", "a1", "a2", "a3"]
    assert sorted(out["checked"]) == ["a0", "a1", "a2", "a3"]
    assert out["capped"] is False


def test_no_time_budget_means_unlimited():
    client = OutcomeClient(events={f"a{i}": [] for i in range(3)})

    async def run():
        return await so.read_signin_outcomes(client, [f"a{i}" for i in range(3)], max_seconds=0)

    out = asyncio.run(run())
    assert len(client.calls) == 3
    assert out["capped"] is False


def test_a_failed_backfill_leaves_the_snapshot_untouched(monkeypatch):
    _seed_snapshot()
    monkeypatch.setattr(so, "GraphClient",
                        lambda *a, **k: OutcomeClient(error=GraphError(403, "no premium license")))

    out = asyncio.run(so.run_backfill("t", {}))
    assert out["ran"] is False
    block = cache.read_domain("t", "apps")["data"]["signin_activity"]
    assert block["last_failed"] == {}
    assert so.job_state("t")["status"] == "unmeasured"


def test_the_backfill_is_a_no_op_when_turned_off(monkeypatch):
    _seed_snapshot()
    client = OutcomeClient()
    monkeypatch.setattr(so, "GraphClient", lambda *a, **k: client)
    monkeypatch.setattr(so, "settings", lambda: (so.SCOPE_OFF, 86400, 100, 300))

    out = asyncio.run(so.run_backfill("t", {}))
    assert out["ran"] is False
    assert client.calls == []


def test_the_backfill_without_a_snapshot_does_not_invent_one():
    out = asyncio.run(so.run_backfill("t", {}))
    assert out["ran"] is False


@pytest.mark.parametrize("code", [0, "0", None])
def test_a_zero_or_absent_error_code_counts_as_success(monkeypatch, code):
    _seed_snapshot()
    status = {} if code is None else {"errorCode": code}
    client = OutcomeClient(events={"a": [{"createdDateTime": "2026-08-22T08:00:00Z",
                                          "status": status}]})
    monkeypatch.setattr(so, "GraphClient", lambda *a, **k: client)

    asyncio.run(so.run_backfill("t", {}))
    block = cache.read_domain("t", "apps")["data"]["signin_activity"]
    assert block["last_seen"]["a"] == "2026-08-22T08:00:00Z"
    assert "a" not in block["last_failed"]

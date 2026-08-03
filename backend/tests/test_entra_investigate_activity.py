"""Investigate activity — window clamping and per-principal log filters.

The behaviours worth locking down are the ones that would otherwise produce a confident
lie: a silently shortened window reads as "nothing happened", and a workload identity
filtered by the wrong id reads as "never signed in".
"""
from __future__ import annotations

from app.entra import investigate as inv
from app.entra import investigate_activity as act


# --------------------------------------------------------------------------- clamping
def test_a_window_within_retention_is_left_alone():
    days, note = act.clamp_days(7)
    assert days == 7
    assert note == ""


def test_an_over_long_window_is_clamped_AND_says_so():
    days, note = act.clamp_days(90)
    assert days == act.GRAPH_RETENTION_DAYS
    # Silence here would read as "nothing happened before 30 days ago" — the opposite fact.
    assert note
    assert "no longer exists at the source" in note


def test_the_azure_log_reaches_further_back_than_graph():
    assert act.clamp_days(90, azure=True) == (90, "")
    assert act.clamp_days(90)[0] == 30


def test_a_nonsense_window_is_floored_rather_than_inverted():
    assert act.clamp_days(0)[0] == 1
    assert act.clamp_days(-5)[0] == 1


def test_the_window_is_ordered_and_utc():
    start, end = act.window(3)
    assert start < end
    assert start.endswith("Z") and end.endswith("Z")


# --------------------------------------------------------------------------- eager vs on demand
def test_the_azure_activity_log_is_never_eager():
    # It is per-subscription and slow, and this screen is linked from dozens of places.
    assert act.TYPE_AZURE in act.ALL_TYPES
    assert act.TYPE_AZURE not in act.EAGER_TYPES


def test_the_cheap_graph_sources_are_eager():
    assert set(act.EAGER_TYPES) == {act.TYPE_SIGNINS, act.TYPE_AUDIT, act.TYPE_RISK}


# --------------------------------------------------------------------------- scoping
def _row(pid: str, sub: str) -> dict:
    return {"principalId": pid, "effectivePrincipalId": pid, "subscriptionId": sub}


def test_activity_is_scoped_to_the_subscriptions_the_principal_can_reach():
    rows = [_row("p-1", "s-a"), _row("p-1", "s-b"), _row("p-2", "s-c"), _row("p-1", "s-a")]
    assert act.subscriptions_for(rows, "p-1") == ["s-a", "s-b"]


def test_scoping_matches_case_insensitively_and_dedupes():
    rows = [_row("P-1", "s-a"), _row("p-1", "s-a")]
    assert act.subscriptions_for(rows, "p-1") == ["s-a"]


def test_a_principal_with_no_azure_access_scopes_to_nothing():
    assert act.subscriptions_for([_row("p-2", "s-c")], "p-1") == []


# --------------------------------------------------------------------------- signin filter
class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def test_a_workload_identity_is_filtered_by_appid_not_object_id(monkeypatch):
    """A service principal signs in as itself; userId would match nothing at all."""
    seen: dict = {}

    async def fake_pages(connection, path, params, cap):
        seen["path"] = path
        seen["filter"] = params["$filter"]
        return [], ""

    monkeypatch.setattr(act, "_graph_pages", fake_pages)
    sp = {"id": "obj-1", "app_id": "app-9", "kind": inv.KIND_SP}
    import asyncio
    asyncio.run(act.signins({}, sp, "S", "E"))
    assert "appId eq 'app-9'" in seen["filter"]
    assert "obj-1" not in seen["filter"]


def test_a_user_is_filtered_by_object_id(monkeypatch):
    seen: dict = {}

    async def fake_pages(connection, path, params, cap):
        seen["filter"] = params["$filter"]
        return [], ""

    monkeypatch.setattr(act, "_graph_pages", fake_pages)
    import asyncio
    asyncio.run(act.signins({}, {"id": "u-1", "kind": inv.KIND_USER}, "S", "E"))
    assert "userId eq 'u-1'" in seen["filter"]


def test_a_workload_identity_without_an_appid_says_so_rather_than_returning_empty(monkeypatch):
    async def fake_pages(connection, path, params, cap):  # pragma: no cover - must not run
        raise AssertionError("should not query Graph without an appId")

    monkeypatch.setattr(act, "_graph_pages", fake_pages)
    import asyncio
    rows, err = asyncio.run(act.signins({}, {"id": "o-1", "app_id": "", "kind": inv.KIND_MI},
                                        "S", "E"))
    assert rows == []
    # "no appId recorded" and "never signed in" are opposite facts.
    assert "appId" in err


def test_signin_rows_are_normalised_with_an_explicit_success_flag(monkeypatch):
    async def fake_pages(connection, path, params, cap):
        return [
            {"createdDateTime": "2026-01-01T00:00:00Z", "appDisplayName": "Portal",
             "status": {"errorCode": 0}},
            {"createdDateTime": "2026-01-02T00:00:00Z", "appDisplayName": "Portal",
             "status": {"errorCode": 50126, "failureReason": "Invalid credentials"}},
        ], ""

    monkeypatch.setattr(act, "_graph_pages", fake_pages)
    import asyncio
    rows, err = asyncio.run(act.signins({}, {"id": "u-1", "kind": inv.KIND_USER}, "S", "E"))
    assert err == ""
    assert [r["success"] for r in rows] == [True, False]
    assert rows[1]["failure_reason"] == "Invalid credentials"

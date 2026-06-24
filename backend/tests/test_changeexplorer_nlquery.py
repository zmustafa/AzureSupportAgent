"""NL change search (app.changeexplorer.nlquery) — deterministic time + grounded filters.

Pins that: (1) relative time phrases resolve to exact UTC windows, (2) the AI 'what' is grounded
against the run's facets (hallucinated types dropped), (3) apply_spec filters events correctly,
(4) window_in_run detects out-of-window queries, (5) a provider failure degrades to keyword search.
"""
from datetime import datetime, timezone

import asyncio

from app.changeexplorer import nlquery


NOW = datetime(2026, 6, 23, 10, 30, tzinfo=timezone.utc)


def test_parse_time_yesterday():
    w = nlquery.parse_time_window("show me all VMs modified yesterday", NOW)
    assert w is not None
    assert w["start_iso"].startswith("2026-06-22T00:00:00")
    assert w["end_iso"].startswith("2026-06-23T00:00:00")
    assert w["label"] == "yesterday"


def test_parse_time_last_n_days():
    w = nlquery.parse_time_window("changes in the last 7 days", NOW)
    assert w is not None and w["label"] == "last 7 days"
    assert w["start_iso"].startswith("2026-06-16T10:30")
    assert w["end_iso"].startswith("2026-06-23T10:30")


def test_parse_time_none():
    assert nlquery.parse_time_window("risky RBAC changes", NOW) is None


def test_parse_query_grounds_types_and_keeps_window(monkeypatch):
    facets = {"resource_types": ["microsoft.compute/virtualmachines", "microsoft.storage/storageaccounts"],
              "actors": ["alice@contoso.com"], "categories": []}

    async def _fake_complete(system, user):
        return {"explanation": "VM changes",
                "resource_types": ["microsoft.compute/virtualmachines", "microsoft.fake/widgets"],
                "categories": ["Compute", "Nonsense"], "risk_min": "high"}

    monkeypatch.setattr(nlquery, "_complete_json", _fake_complete)
    spec = asyncio.run(nlquery.parse_query("all VMs modified yesterday", now=NOW, facets=facets))
    # Hallucinated type/category dropped; risk normalized; window preserved.
    assert spec["resource_types"] == ["microsoft.compute/virtualmachines"]
    assert spec["categories"] == ["Compute"]
    assert spec["risk_min"] == "High"
    assert spec["time_window"]["label"] == "yesterday"


def test_parse_query_keyword_fallback_when_ai_down(monkeypatch):
    async def _boom(system, user):
        raise RuntimeError("no provider")

    monkeypatch.setattr(nlquery, "_complete_json", _boom)
    spec = asyncio.run(nlquery.parse_query("VMs changed yesterday", now=NOW, facets={}))
    assert "keyword" in spec and "vms" in spec["keyword"]
    assert spec["time_window"]["label"] == "yesterday"


def _ev(cid, rtype, op, risk, actor, name, when):
    return {"changeId": cid, "resourceType": rtype, "operation": op, "riskLabel": risk,
            "actor": actor, "actorType": "User", "resourceName": name, "plainEnglishSummary": "",
            "category": "Compute", "eventTime": when}


def test_apply_spec_filters_type_risk_and_window():
    events = [
        _ev("c1", "microsoft.compute/virtualmachines", "Update", "High", "a", "vm1", "2026-06-22T08:00:00+00:00"),
        _ev("c2", "microsoft.storage/storageaccounts", "Update", "High", "a", "st1", "2026-06-22T09:00:00+00:00"),
        _ev("c3", "microsoft.compute/virtualmachines", "Update", "Low", "a", "vm2", "2026-06-22T10:00:00+00:00"),
        _ev("c4", "microsoft.compute/virtualmachines", "Update", "High", "a", "vm3", "2026-06-20T10:00:00+00:00"),
    ]
    spec = {
        "resource_types": ["microsoft.compute/virtualmachines"],
        "risk_min": "High",
        "time_window": {"start_iso": "2026-06-22T00:00:00+00:00", "end_iso": "2026-06-23T00:00:00+00:00"},
    }
    out = nlquery.apply_spec(events, spec)
    # Only the high-risk VM inside yesterday's window: c1 (c2 wrong type, c3 low risk, c4 out of window).
    assert [e["changeId"] for e in out] == ["c1"]


def test_window_in_run():
    win = {"start_iso": "2026-06-22T00:00:00+00:00", "end_iso": "2026-06-23T00:00:00+00:00"}
    assert nlquery.window_in_run(win, "2026-06-21T00:00:00+00:00", "2026-06-23T12:00:00+00:00") is True
    assert nlquery.window_in_run(win, "2026-06-22T06:00:00+00:00", "2026-06-23T00:00:00+00:00") is False
    assert nlquery.window_in_run(None, "", "") is True  # no time constraint


def test_window_in_run_tolerates_z_suffix():
    """Run times are stored as '...Z' while windows use '+00:00'; comparison must be by datetime,
    not raw string (where 'Z' > '+' would wrongly report out-of-window)."""
    win = {"start_iso": "2026-06-01T00:00:00+00:00", "end_iso": "2026-06-02T00:00:00+00:00"}
    assert nlquery.window_in_run(win, "2026-06-01T00:00:00Z", "2026-06-21T00:00:00Z") is True


def test_apply_spec_window_tolerates_z_suffix():
    events = [{"changeId": "c1", "resourceType": "x", "operation": "Update", "riskLabel": "High",
               "actor": "a", "actorType": "User", "resourceName": "r1", "plainEnglishSummary": "",
               "category": "Network", "eventTime": "2026-06-22T08:00:00Z"}]
    spec = {"time_window": {"start_iso": "2026-06-22T00:00:00+00:00", "end_iso": "2026-06-23T00:00:00+00:00"}}
    assert [e["changeId"] for e in nlquery.apply_spec(events, spec)] == ["c1"]


def test_parse_time_between_dates():
    w = nlquery.parse_time_window("changes between 2026-06-01 and 2026-06-05", NOW)
    assert w is not None
    assert w["start_iso"].startswith("2026-06-01T00:00:00")
    assert w["end_iso"].startswith("2026-06-06T00:00:00")  # inclusive end day

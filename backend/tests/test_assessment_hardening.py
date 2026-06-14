"""Trust-critical tests for the assessment hardening (P0-P4).

All offline: Azure (Resource Graph REST/CLI) and the DB are monkeypatched, so nothing here
touches a live subscription. The focus is the *contracts* that make a mission-critical
assessment trustworthy: fail-closed evaluation, accurate paged counts, throttle retry,
completeness/confidence scoring, the manual/signal control kinds, and the WARA/WASA/WAF packs.
"""
from __future__ import annotations

import asyncio

import pytest

from app.assessments import catalog, runner
from app.azure import arm
from app.exec import command_runner
from app.exec.command_runner import KqlResult


# --------------------------------------------------------------------------- helpers
class _FakeResp:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = "err"

    def json(self):
        return self._payload


def _client_factory(pages):
    class _C:
        def __init__(self, *a, **k):
            self._i = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            r = pages[min(self._i, len(pages) - 1)]
            self._i += 1
            return r

    return _C


# --------------------------------------------------------------------------- P0: paging + total
def test_paged_query_accumulates_all_pages_and_total(monkeypatch):
    pages = [
        _FakeResp(200, {"data": [{"id": f"a{i}"} for i in range(10)], "$skipToken": "tok", "totalRecords": 25}),
        _FakeResp(200, {"data": [{"id": f"b{i}"} for i in range(15)], "totalRecords": 25}),
    ]
    monkeypatch.setattr(arm.httpx, "AsyncClient", _client_factory(pages))
    rows, err, complete, total = asyncio.run(arm.query_resource_graph_paged("tok", "resources", max_rows=5000))
    assert err is None
    assert len(rows) == 25
    assert complete is True
    assert total == 25


def test_paged_query_caps_rows_but_reports_true_total(monkeypatch):
    pages = [_FakeResp(200, {"data": [{"id": f"a{i}"} for i in range(15)], "$skipToken": "t", "totalRecords": 200})]
    monkeypatch.setattr(arm.httpx, "AsyncClient", _client_factory(pages))
    rows, err, complete, total = asyncio.run(arm.query_resource_graph_paged("tok", "resources", max_rows=10))
    assert err is None
    assert len(rows) == 10  # capped
    assert complete is False  # more exist
    assert total == 200  # accurate count despite capping


def test_paged_query_retries_on_throttle_then_succeeds(monkeypatch):
    pages = [
        _FakeResp(429, {}, headers={"Retry-After": "0"}),
        _FakeResp(200, {"data": [{"id": "x"}], "totalRecords": 1}),
    ]
    monkeypatch.setattr(arm.httpx, "AsyncClient", _client_factory(pages))
    rows, err, complete, total = asyncio.run(arm.query_resource_graph_paged("tok", "resources"))
    assert err is None
    assert len(rows) == 1
    assert total == 1


def test_paged_query_hard_error_is_fail_closed(monkeypatch):
    pages = [_FakeResp(403, {"error": {"message": "Forbidden"}})]
    monkeypatch.setattr(arm.httpx, "AsyncClient", _client_factory(pages))
    rows, err, complete, total = asyncio.run(arm.query_resource_graph_paged("tok", "resources"))
    assert err is not None and "403" in err
    assert complete is False


def test_retry_after_seconds_parses_header():
    assert arm._retry_after_seconds(_FakeResp(429, {}, {"Retry-After": "12"})) == 12.0
    assert arm._retry_after_seconds(_FakeResp(429, {}, {})) is None
    assert arm._retry_after_seconds(_FakeResp(429, {}, {"retry-after": "bad"})) is None


def test_run_kql_collect_rest_passes_through_total(monkeypatch):
    async def _fake_token(conn):
        return "tok", None

    async def _fake_paged(token, query, subscriptions=None, *, page_size=1000, max_rows=5000, max_retries=4):
        return [{"id": "r1"}, {"id": "r2"}], None, False, 99

    monkeypatch.setattr("app.azure.credentials.get_arm_token", _fake_token)
    monkeypatch.setattr("app.azure.arm.query_resource_graph_paged", _fake_paged)
    res = asyncio.run(command_runner.run_kql_collect("Resources | take 1", None))
    assert res.ok is True
    assert res.total == 99
    assert res.complete is False
    assert len(res.rows) == 2


def test_run_kql_collect_rest_error_is_fail_closed(monkeypatch):
    async def _fake_token(conn):
        return "tok", None

    async def _fake_paged(token, query, subscriptions=None, *, page_size=1000, max_rows=5000, max_retries=4):
        return [], "Resource Graph 429: throttled", False, None

    monkeypatch.setattr("app.azure.credentials.get_arm_token", _fake_token)
    monkeypatch.setattr("app.azure.arm.query_resource_graph_paged", _fake_paged)
    res = asyncio.run(command_runner.run_kql_collect("Resources | take 1", None))
    assert res.ok is False  # never a silent empty pass
    assert "429" in res.error


# --------------------------------------------------------------------------- P1/P3: _execute_check
def _graph_check():
    return next(c for c in catalog.ALL_CHECKS if c.get("kind", "graph") == "graph")


def test_execute_check_graph_uses_arg_total_for_count(monkeypatch):
    chk = _graph_check()
    present = set(chk["resource_types"])

    async def _fake_collect(kql, connection, *, session_config_dir=None, max_rows=5000, page_size=1000):
        return KqlResult(ok=True, rows=[{"id": "a"}, {"id": "b"}], complete=False, total=137)

    monkeypatch.setattr(runner, "run_kql_collect", _fake_collect)
    base = asyncio.run(runner._execute_check(chk, "subscriptionId =~ 's'", present, None, None))
    assert base["status"] == "fail"
    assert base["flagged_count"] == 137  # accurate count, not the 2-row sample
    assert base["partial"] is True
    assert len(base["flagged_resources"]) == 2


def test_execute_check_graph_error_is_fail_closed(monkeypatch):
    chk = _graph_check()
    present = set(chk["resource_types"])

    async def _fake_collect(kql, connection, *, session_config_dir=None, max_rows=5000, page_size=1000):
        return KqlResult(ok=False, error="ARG throttled out")

    monkeypatch.setattr(runner, "run_kql_collect", _fake_collect)
    base = asyncio.run(runner._execute_check(chk, "subscriptionId =~ 's'", present, None, None))
    assert base["status"] == "error"  # NOT pass
    assert "throttled" in base["error"]


def test_execute_check_not_applicable_when_type_absent(monkeypatch):
    chk = _graph_check()
    base = asyncio.run(runner._execute_check(chk, "subscriptionId =~ 's'", {"microsoft.nonexistent/type"}, None, None))
    assert base["status"] == "not_applicable"


def test_manual_check_pending_then_attested():
    chk = next(c for c in catalog.ALL_CHECKS if c.get("kind") == "manual")
    # No attestation -> pending (manual), excluded from auto-score.
    base = asyncio.run(runner._execute_check(chk, "p", set(), None, None, attestation=None))
    assert base["status"] == "manual"
    # Attested pass -> scores like a normal pass.
    base = asyncio.run(runner._execute_check(chk, "p", set(), None, None, attestation={"status": "pass", "by": "qa"}))
    assert base["status"] == "pass"
    assert base["attestation"]["by"] == "qa"


def test_signal_check_builds_advisor_join(monkeypatch):
    chk = next(c for c in catalog.ALL_CHECKS if c.get("kind") == "signal")
    captured = {}

    async def _fake_collect(kql, connection, *, session_config_dir=None, max_rows=5000, page_size=1000):
        captured["kql"] = kql
        return KqlResult(ok=True, rows=[], complete=True, total=0)

    monkeypatch.setattr(runner, "run_kql_collect", _fake_collect)
    # Signal checks have empty resource_types => always applicable.
    base = asyncio.run(runner._execute_check(chk, "subscriptionId =~ 's'", set(), None, None))
    assert base["status"] == "pass"
    q = captured["kql"]
    assert "advisorresources" in q
    assert "join kind=inner" in q
    assert "highavailability" in q.lower()


def test_signal_kql_empty_for_misconfigured():
    assert runner._signal_kql({"signal": {"provider": "advisor", "category": ""}}, "p") == ""
    assert runner._signal_kql({"signal": {"provider": "other", "category": "x"}}, "p") == ""


# --------------------------------------------------------------------------- P2: completeness/confidence
def test_scored_full_coverage_is_high_confidence():
    findings = [
        {"pillar": "security", "status": "pass", "severity": "warning"},
        {"pillar": "security", "status": "fail", "severity": "critical"},
    ]
    sc = runner._scored([], findings)
    assert sc["completeness_pct"] == 100
    assert sc["confidence"] == "high"
    assert sc["worst_case_score"] == sc["overall_score"]  # nothing errored


def test_scored_errors_drop_completeness_and_worst_case():
    findings = [
        {"pillar": "reliability", "status": "pass", "severity": "warning"},
        {"pillar": "reliability", "status": "error", "severity": "critical"},
        {"pillar": "reliability", "status": "error", "severity": "critical"},
    ]
    sc = runner._scored([], findings)
    # 1 of 3 evaluatable evaluated -> 33% -> low confidence.
    assert sc["completeness_pct"] == 33
    assert sc["confidence"] == "low"
    # Optimistic score ignores errors (100); worst-case treats them as fails (< 100).
    assert sc["overall_score"] == 100
    assert sc["worst_case_score"] is not None and sc["worst_case_score"] < 100


def test_scored_manual_excluded_from_score():
    findings = [
        {"pillar": "reliability", "status": "pass", "severity": "warning"},
        {"pillar": "reliability", "status": "manual", "severity": "error"},
    ]
    sc = runner._scored([], findings)
    assert sc["totals"]["manual"] == 1
    assert sc["overall_score"] == 100  # manual pending doesn't count
    assert sc["completeness_pct"] == 100  # manual isn't 'evaluatable'


# --------------------------------------------------------------------------- P4: packs + priority
def test_pack_pillars_resolution():
    assert catalog.pack_pillars("wara") == ["reliability"]
    assert catalog.pack_pillars("wasa") == ["security"]
    assert set(catalog.pack_pillars("waf")) == set(catalog.PILLARS)
    assert catalog.pack_pillars("nope") is None


def test_public_catalog_exposes_packs_and_subcategories():
    pub = catalog.public_catalog()
    pack_ids = {p["id"] for p in pub["packs"]}
    assert {"waf", "wara", "wasa"}.issubset(pack_ids)
    assert "High availability" in pub["sub_categories"]


def test_priority_score_orders_by_value():
    from app.api.assessments import _priority_score

    high = {"severity": "critical", "impact": "high", "effort": "low", "flagged_count": 20}
    low = {"severity": "info", "impact": "low", "effort": "high", "flagged_count": 1}
    assert _priority_score(high) > _priority_score(low)


# --------------------------------------------------------------------------- P3: attestation store
def test_attestation_store_set_get_clear(monkeypatch, tmp_path):
    from app.assessments import attestations

    monkeypatch.setattr(attestations, "_PATH", tmp_path / "att.json")
    assert attestations.get_attestations("t1", "w1") == {}
    attestations.set_attestation("t1", "w1", "rel_manual_dr_drill", status="pass", note="tested", by="qa")
    got = attestations.get_attestations("t1", "w1")
    assert got["rel_manual_dr_drill"]["status"] == "pass"
    assert got["rel_manual_dr_drill"]["by"] == "qa"
    # Tenant isolation.
    assert attestations.get_attestations("t2", "w1") == {}
    # Clear reverts to pending.
    attestations.set_attestation("t1", "w1", "rel_manual_dr_drill", status="")
    assert attestations.get_attestations("t1", "w1") == {}

"""Tests for the enhanced Workload Autopilot — classification, signal-evidence, map-reduce
grouping, grouping memory, and estate coverage. All offline (no Azure)."""
from __future__ import annotations

import asyncio

from app.workloads import autopilot as ap
from app.workloads import grouping_memory as gm


def _r(rid, name, rtype, rg="rg1", sub="s1", tags=None):
    return {
        "id": rid, "name": name, "resource_type": rtype, "resource_group": rg,
        "subscription_id": sub, "location": "eastus", "tags": tags or {},
    }


# --------------------------------------------------------------------- classification
def test_classify_type_by_resource_mix():
    assert ap._classify_type([_r("/a", "ws", "microsoft.web/sites")]) == "web_app"
    assert ap._classify_type([_r("/a", "df", "microsoft.datafactory/factories")]) == "data_pipeline"
    assert ap._classify_type([_r("/a", "oai", "microsoft.cognitiveservices/accounts")]) == "ai_ml"
    assert ap._classify_type([_r("/a", "vn", "microsoft.network/virtualnetworks")]) == "networking"
    assert ap._classify_type([_r("/a", "x", "microsoft.unknown/foo")]) == "other"


def test_classify_env_from_naming_and_tags():
    assert ap._classify_env("rg-prod", [_r("/a", "web-prod-01", "microsoft.web/sites")]) == "production"
    assert ap._classify_env("rg-dev", [_r("/a", "api-dev", "microsoft.web/sites")]) == "development"
    assert ap._classify_env("rg", [_r("/a", "x", "microsoft.web/sites", tags={"environment": "staging"})]) == "staging"
    assert ap._classify_env("misc", [_r("/a", "thing", "microsoft.web/sites")]) == "unknown"


def test_norm_class_validates_against_allowed():
    assert ap._norm_class("Frontend", ap.VALID_TYPES, "other") == "other"  # invalid -> default
    assert ap._norm_class("Web App", ap.VALID_TYPES, "other") == "web_app"  # normalized -> valid
    assert ap._norm_class("web_app", ap.VALID_TYPES, "other") == "web_app"
    assert ap._norm_class("CRITICAL", ap.VALID_CRIT, "") == "critical"
    assert ap._norm_class("bogus", ap.VALID_CRIT, "low") == "low"


# --------------------------------------------------------------------- evidence
def test_evidence_cites_provenance_and_scope():
    members = [_r("/s/rg/a", "a", "microsoft.web/sites"), _r("/s/rg/b", "b", "microsoft.sql/servers")]
    signals = {"provenance": {"/s/rg/a": "myapp", "/s/rg/b": "myapp"}, "private_endpoints": [{"pe": "/s/rg/pe", "target": "/s/rg/b"}], "network": {}}
    ev = ap._evidence_for_group(members, signals)
    kinds = {e["kind"] for e in ev}
    assert "provenance" in kinds  # 2 share marker myapp
    assert "network" in kinds     # 1 PE link to /s/rg/b
    assert "scope" in kinds       # single RG


def test_evidence_empty_signals_still_reports_scope():
    members = [_r("/s/rg1/a", "a", "microsoft.web/sites", rg="rg1"), _r("/s/rg2/b", "b", "microsoft.sql/servers", rg="rg2")]
    ev = ap._evidence_for_group(members, {})
    assert any(e["kind"] == "scope" and "2 resource groups" in e["detail"] for e in ev)


# --------------------------------------------------------------------- grouping
def test_rg_grouping_carries_classification():
    res = [_r("/s/rgp/web", "web-prod", "microsoft.web/sites", rg="rg-prod")]
    groups = ap._rg_grouping(res)
    assert groups[0]["workload_type"] == "web_app"
    assert groups[0]["environment"] == "production"
    assert groups[0]["confidence"] == 0.5


def test_candidate_shape_has_classification_and_evidence():
    g = ap._rg_grouping([_r("/s/rg/a", "a", "microsoft.web/sites")])[0]
    cand = ap._candidate(g, {"provenance": {}, "private_endpoints": [], "network": {}})
    for k in ("workload_type", "environment", "criticality", "data_classification", "evidence", "nodes", "confidence"):
        assert k in cand
    assert cand["nodes"][0]["kind"] == "resource"


def test_merge_cross_batch_combines_shared_marker():
    a = _r("/s/rg/a", "a", "microsoft.web/sites")
    b = _r("/s/rg/b", "b", "microsoft.web/sites")
    c = _r("/s/rg/c", "c", "microsoft.sql/servers")
    d = _r("/s/rg/d", "d", "microsoft.sql/servers")
    # Two batches each proposing a 2-member group with the SAME dominant marker -> merge to 1.
    signals = {"provenance": {"/s/rg/a": "app1", "/s/rg/b": "app1", "/s/rg/c": "app1", "/s/rg/d": "app1"}, "private_endpoints": [], "network": {}}
    groups = [
        {"name": "G1", "description": "", "reasoning": "", "confidence": 0.6, "members": [a, b], "criticality": ""},
        {"name": "G2", "description": "", "reasoning": "", "confidence": 0.8, "members": [c, d], "criticality": "high"},
    ]
    merged = ap._merge_cross_batch(groups, signals)
    assert len(merged) == 1
    assert len(merged[0]["members"]) == 4
    assert merged[0]["confidence"] == 0.8  # max
    assert merged[0]["criticality"] == "high"  # inherited


def test_merge_cross_batch_keeps_distinct_names_apart():
    a = _r("/s/rg/a", "a", "microsoft.web/sites")
    b = _r("/s/rg/b", "b", "microsoft.sql/servers")
    groups = [
        {"name": "Alpha", "description": "", "reasoning": "", "confidence": 0.6, "members": [a], "criticality": ""},
        {"name": "Beta", "description": "", "reasoning": "", "confidence": 0.6, "members": [b], "criticality": ""},
    ]
    merged = ap._merge_cross_batch(groups, {"provenance": {}, "private_endpoints": [], "network": {}})
    assert len(merged) == 2  # no shared marker, different names -> stay apart


def test_ai_group_falls_back_to_rg_when_ai_unavailable(monkeypatch):
    async def _none(*a, **k):
        return None

    monkeypatch.setattr(ap, "_ai_group_batch", _none)
    res = [_r("/s/rgx/a", "a", "microsoft.web/sites", rg="rgx")]
    groups, used_ai = asyncio.run(ap._ai_group(res, {"provenance": {}, "private_endpoints": [], "network": {}}))
    assert used_ai is False
    assert groups and groups[0]["name"] == "rgx"


# --------------------------------------------------------------------- grouping memory
def test_grouping_memory_records_and_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "_PATH", tmp_path / "gm.json")
    n = gm.record_decisions("t1", "c1", [
        {"action": "rename", "from": "rg-prod", "to": "Billing"},
        {"action": "reject", "name": "Noise"},
        {"action": "accept", "name": "Billing"},
    ])
    assert n == 3
    hint = gm.prompt_hint("t1", "c1")
    assert "renamed 'rg-prod' to 'Billing'" in hint
    assert "rejected" in hint and "Noise" in hint
    # Isolation: different tenant has no memory.
    assert gm.prompt_hint("t2", "c1") == ""


def test_grouping_memory_dedupes_consecutive(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "_PATH", tmp_path / "gm.json")
    gm.record_decisions("t", "c", [{"action": "accept", "name": "X"}])
    gm.record_decisions("t", "c", [{"action": "accept", "name": "X"}])  # dupe
    assert len(gm.get_decisions("t", "c")) == 1

"""AI Tag Generator (app.tagintel.generate) — grounding + target resolution guarantees.

The generator turns a plain-English instruction into a concrete, grounded change-set. These
tests pin that it: (1) grounds the proposal against the REAL estate (hallucinated ARM types are
dropped), (2) resolves each op's target to actual resource_ids, (3) drops ops that match nothing,
and (4) degrades to None when the AI provider is unavailable.
"""
import asyncio

import pytest

from app.tagintel import analysis, generate as gen_mod


def _r(rid, name, rtype, rg, sub, tags):
    return {
        "id": rid, "name": name, "type": rtype, "location": "eastus",
        "resource_group": rg, "subscription_id": sub, "tags": tags, "workloads": [],
    }


@pytest.fixture()
def estate():
    return [
        _r("/s/sub1/vm1", "app-prod-vm1", "microsoft.compute/virtualmachines", "rg-app-prod", "sub1",
           {"Environment": "Prod", "Owner": "team-a"}),
        _r("/s/sub1/vm2", "app-prod-vm2", "microsoft.compute/virtualmachines", "rg-app-prod", "sub1",
           {"Environment": "PRD"}),
        _r("/s/sub2/st1", "sharedstore", "microsoft.storage/storageaccounts", "rg-shared", "sub2",
           {"Environment": "Production", "Owner": "team-b"}),
    ]


def test_propose_grounds_and_resolves_targets(estate, monkeypatch):
    """add_tag on resources MISSING Owner resolves to exactly the untagged VM; a hallucinated
    resource type in the target is dropped (so the op still grounds to real resources)."""
    async def _fake_complete(system, user):
        return {
            "summary": "Fill missing Owner",
            "operations": [
                {"type": "add_tag", "key": "Owner", "value": "platform-team",
                 "rationale": "ownership", "target": {"missing_all_tags": ["Owner"],
                                                       "types": ["microsoft.fake/widgets"]}},
            ],
        }

    monkeypatch.setattr(gen_mod, "_complete_json", _fake_complete)
    cen = analysis.census(estate)
    out = asyncio.run(gen_mod.propose("add owner to anything missing it", cen, estate))
    assert out is not None and out["available"] is True
    assert len(out["operations"]) == 1
    op = out["operations"][0]
    assert op["type"] == "add_tag" and op["key"] == "Owner" and op["value"] == "platform-team"
    # Hallucinated type dropped -> matched purely on "missing Owner" -> only vm2.
    assert op["resource_ids"] == ["/s/sub1/vm2"]
    assert op["match_count"] == 1


def test_propose_drops_zero_match_ops_with_note(estate, monkeypatch):
    """An op whose target matches no resources is dropped and explained in notes."""
    async def _fake_complete(system, user):
        return {
            "summary": "x",
            "operations": [
                {"type": "set_tag", "key": "Environment", "value": "Production",
                 "target": {"name_contains": "no-such-resource"}},
            ],
        }

    monkeypatch.setattr(gen_mod, "_complete_json", _fake_complete)
    cen = analysis.census(estate)
    out = asyncio.run(gen_mod.propose("set env on nonexistent", cen, estate))
    assert out is not None
    assert out["operations"] == []
    assert any("0 resources" in n for n in out["notes"])


def test_propose_estate_wide_when_no_target(estate, monkeypatch):
    """An empty target applies estate-wide (every resource id is carried)."""
    async def _fake_complete(system, user):
        return {"summary": "tag all", "operations": [
            {"type": "set_tag", "key": "managed-by", "value": "platform", "target": {}}]}

    monkeypatch.setattr(gen_mod, "_complete_json", _fake_complete)
    cen = analysis.census(estate)
    out = asyncio.run(gen_mod.propose("tag everything managed-by=platform", cen, estate))
    assert out is not None and len(out["operations"]) == 1
    assert out["operations"][0]["match_count"] == 3


def test_propose_skips_incomplete_and_disallowed_ops(estate, monkeypatch):
    """Ops missing required fields, or of an unknown type, are filtered out."""
    async def _fake_complete(system, user):
        return {"summary": "mixed", "operations": [
            {"type": "add_tag", "key": "Owner"},                    # no value -> incomplete
            {"type": "bogus_op", "key": "X", "value": "Y"},         # unknown type
            {"type": "set_tag", "key": "Environment", "value": "Production", "target": {}},  # ok
        ]}

    monkeypatch.setattr(gen_mod, "_complete_json", _fake_complete)
    cen = analysis.census(estate)
    out = asyncio.run(gen_mod.propose("do stuff", cen, estate))
    assert out is not None
    assert [o["type"] for o in out["operations"]] == ["set_tag"]


def test_propose_returns_none_when_provider_unavailable(estate, monkeypatch):
    """A provider failure degrades to None so the endpoint shows a friendly message."""
    async def _boom(system, user):
        raise RuntimeError("no provider")

    monkeypatch.setattr(gen_mod, "_complete_json", _boom)
    cen = analysis.census(estate)
    assert asyncio.run(gen_mod.propose("anything", cen, estate)) is None

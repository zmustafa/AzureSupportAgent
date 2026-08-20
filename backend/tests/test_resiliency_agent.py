"""Agent tools, tool registration, and the Mission Control headline.

Both surfaces restate derived numbers to a human or a model, so both have to carry the
caveats with them. A tool that returns a bare RTO gets it quoted as fact.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import demo_catalog
from app.missions import systems
from app.resiliency import agent_tool, analyze as analyze_mod, model, snapshot as store

CONTOSO = demo_catalog.CONTOSO_ID


class _Principal:
    is_admin = False
    tenant_id = "t-demo"
    subject = "dev"

    def __init__(self, allowed=("resiliency.read",)):
        self._allowed = set(allowed)

    def has(self, perm: str) -> bool:
        return perm in self._allowed


class _Toolset:
    def __init__(self) -> None:
        self.registered: list[object] = []

    def add_connector(self, _config, tools) -> None:
        self.registered.extend(tools)


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    store.set_path_for_tests(tmp_path / "resiliency_snapshot.json")
    yield
    store.clear()


def _run(coro):
    return asyncio.run(coro)


def _seed() -> dict:
    snap = _run(analyze_mod.analyze(None, tenant_id="t-demo", scope_kind="workload",
                                    scope_id=CONTOSO, subscriptions=[], workload_id=CONTOSO))
    store.write("t-demo", "", "workload", CONTOSO, snap)
    return snap


def _payload(result: dict) -> dict:
    """Tool results travel in the shared MCP shape: ``{isError, content: [text]}``."""
    assert result["isError"] is False, result["content"]
    return json.loads(result["content"][0])


def _message(result: dict) -> str:
    return str(result["content"][0])


# ============================================================== permissions
def test_posture_requires_the_permission():
    handler = agent_tool.make_recovery_posture("t-demo", _Principal(allowed=()), "")
    out = _run(handler({}, {"workload": CONTOSO}))
    assert out["isError"] is True
    assert "resiliency.read" in _message(out)


def test_posture_reports_nothing_rather_than_zeros_before_analysis():
    handler = agent_tool.make_recovery_posture("t-demo", _Principal(), "")
    out = _run(handler({}, {"workload": CONTOSO}))
    assert out["isError"] is True
    assert "has not analyzed" in _message(out)


# ============================================================== content
def test_posture_carries_the_basis_and_the_reading_instructions():
    """Given a bare number a model states it as fact. The basis is what stops that."""
    _seed()
    handler = agent_tool.make_recovery_posture("t-demo", _Principal(), "")
    data = _payload(_run(handler({}, {"workload": CONTOSO, "resource": "contoso-guests-cosmos"})))
    corruption = data["resources"][0]["scenarios"][model.SCENARIO_DATA_CORRUPTION]
    assert corruption["rpo_minutes"] == 1440
    assert corruption["basis"], "a number without its basis is unquotable"
    assert "not proven by a recovery drill" in data["how_to_read"]
    assert "replicates the damage" in data["how_to_read"]


def test_gaps_finds_the_resource_with_no_recovery_path():
    _seed()
    handler = agent_tool.make_recovery_gaps("t-demo", _Principal(), "")
    data = _payload(_run(handler({}, {"workload": CONTOSO})))
    names = {g["name"] for g in data["no_recovery_path"]}
    assert "contoso-pms-vm" in names
    assert all(g["why"] for g in data["no_recovery_path"])


def test_gaps_says_that_unknown_resources_are_excluded():
    """Otherwise the agent reports the list as exhaustive."""
    _seed()
    handler = agent_tool.make_recovery_gaps("t-demo", _Principal(), "")
    data = _payload(_run(handler({}, {"workload": CONTOSO})))
    assert "unknown" in data["undetermined_note"]


def test_gaps_rejects_an_unknown_scenario_by_listing_the_real_ones():
    _seed()
    handler = agent_tool.make_recovery_gaps("t-demo", _Principal(), "")
    out = _run(handler({}, {"workload": CONTOSO, "scenario": "meteor_strike"}))
    assert out["isError"] is True
    assert model.SCENARIO_ZONE_LOSS in _message(out)


def test_breaches_are_returned_worst_first():
    _seed()
    handler = agent_tool.make_recovery_breaches("t-demo", _Principal(), "")
    rows = _payload(_run(handler({}, {"workload": CONTOSO})))["breaches"]
    assert rows
    assert rows[0]["no_recovery_path"] is True


# ============================================================== registration
def test_the_tool_specs_are_well_formed():
    specs = agent_tool.tool_specs("t-demo", _Principal(), "")
    assert [s[0] for s in specs] == ["recovery_posture", "recovery_gaps", "recovery_breaches"]
    for _name, description, schema, handler in specs:
        assert description and schema["type"] == "object" and callable(handler)


def test_only_the_default_on_tools_are_built():
    """Every extra tool costs every turn, so the opt-in one stays out until asked for."""
    tools = agent_tool.build_recovery_tools("t-demo", _Principal(), "")
    assert {t.name for t in tools} == {"recovery_posture", "recovery_gaps"}


def test_registration_adds_the_tools_to_a_toolset():
    """Without this the tools exist and the agent can never call them."""
    toolset = _Toolset()
    agent_tool.register_recovery_tools(toolset, tenant_id="t-demo", principal=_Principal(),
                                       connection={"id": "c1"})
    assert {t.name for t in toolset.registered} == {"recovery_posture", "recovery_gaps"}


def test_the_master_switch_withholds_every_tool(monkeypatch):
    from app.core import app_settings

    monkeypatch.setattr(app_settings, "load_settings",
                        lambda: {"resiliency_tools_enabled": False})
    toolset = _Toolset()
    agent_tool.register_recovery_tools(toolset, tenant_id="t-demo", principal=_Principal())
    assert toolset.registered == []


def test_a_per_tool_override_is_honoured(monkeypatch):
    from app.core import app_settings

    monkeypatch.setattr(app_settings, "load_settings", lambda: {
        "resiliency_tools_enabled": True,
        "resiliency_tools": {"recovery_posture": False, "recovery_breaches": True},
    })
    names = {t.name for t in agent_tool.build_recovery_tools("t-demo", _Principal(), "")}
    assert names == {"recovery_gaps", "recovery_breaches"}


def test_registration_never_breaks_a_turn(monkeypatch):
    """Tool registration failing must not take the chat turn down with it."""
    def _boom(*_a, **_kw):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(agent_tool, "build_recovery_tools", _boom)
    agent_tool.register_recovery_tools(_Toolset(), tenant_id="t", principal=_Principal())


def test_the_catalog_classifies_the_tools():
    from app.agent.tool_catalog import _RECOVERY_TOOLS

    assert _RECOVERY_TOOLS == {"recovery_posture", "recovery_gaps", "recovery_breaches"}


# ============================================================== mission headline
def test_the_mission_headline_leads_with_the_count_not_a_percentage():
    """A score invites comparison; a count of unrecoverable resources invites action."""
    snap = _seed()
    head, score, attention = systems._recovery_headline(snap)
    assert "no recovery path" in head
    assert attention is True
    assert isinstance(score, int)


def test_a_clean_estate_reads_as_clean_rather_than_alarming():
    head, score, attention = systems._recovery_headline({
        "summary": {"resources": 10, "protection": {"unknown": 0},
                    "worst": {"scenario": "zone_loss", "no_recovery_path": 0}}})
    assert attention is False
    assert score == 100
    assert "recoverable" in head


def test_an_all_unknown_estate_does_not_report_a_perfect_score():
    head, score, _attention = systems._recovery_headline({
        "summary": {"resources": 4, "protection": {"unknown": 4},
                    "worst": {"scenario": "zone_loss", "no_recovery_path": 0}}})
    assert score is None, "nothing was determined, so there is no score to give"
    assert "unknown" in head


def test_the_system_is_registered_in_mission_control():
    assert systems.get_system("recovery") is not None
    assert any(s.key == "recovery" for s in systems.SYSTEMS)

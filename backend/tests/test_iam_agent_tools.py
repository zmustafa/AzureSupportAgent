"""The IAM agent tools — what an LLM is allowed to say about somebody's access.

These matter more than an ordinary read endpoint because a model **summarizes**. A UI can show
"unmeasured" in amber next to an empty list and a human reads the amber; a model handed the same
empty list writes "no unused permissions were found" and the amber is gone. So every tool here
has to carry its uncertainty *in the words*, not in a field the caller may drop.

Each tool is also read-only over the cache by construction: no Azure call, no write, and no
disagreement with what the screens show.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.iam import agent_tool, cache, schema

SUB = "11111111-1111-1111-1111-111111111111"
SCOPE = f"/subscriptions/{SUB}"
RES = f"{SCOPE}/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/sa1"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    return tmp_path


def _row(**kw):
    base = dict(
        surface=schema.SURFACE_AZURE_RBAC, effect=schema.EFFECT_ALLOW,
        assignmentState=schema.STATE_ACTIVE, accessPath=schema.PATH_DIRECT,
        principalId="alice", effectivePrincipalId="alice", effectivePrincipalName="Alice",
        effectivePrincipalType="User", roleDefinitionId="/rd/reader", roleName="Reader",
        scope=SCOPE, scopeType=schema.SCOPE_SUBSCRIPTION, subscriptionId=SUB,
        assignmentId="/subscriptions/x/providers/Microsoft.Authorization/roleAssignments/a1",
        principalExists=schema.EXISTS_TRUE,
    )
    base.update(kw)
    return schema.make_row(**base)


def _tool(name: str, tenant: str = "t1"):
    return next(t for t in agent_tool.build_iam_tools(tenant) if t.name == name)


def _text(result: Any) -> str:
    """`ok()` wraps its body in a list of content blocks; join them so assertions read the words
    the model will actually receive."""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            return "\n".join(str(c) for c in content)
        return str(content or result)
    return str(result)


# =========================================================================== registry
def test_every_planned_tool_is_registered():
    names = {t.name for t in agent_tool.build_iam_tools("t1")}
    assert {
        "who_can_access", "privileged_access_review", "effective_access_for_principal",
        "can_principal_do", "why_does_principal_have_access", "escalation_paths_to",
        "unused_permissions_for", "simulate_revoke", "access_changed_since",
        "who_can_reach_resource",
    } <= names


def test_every_tool_is_read_only():
    """No tool may mutate Azure or the cache. `simulate_revoke` in particular is a what-if, and
    a model that reads it as an action would delete somebody's access."""
    for t in agent_tool.build_iam_tools("t1"):
        assert t.kind == "read", f"{t.name} is not declared read-only"


def test_simulate_revoke_says_it_changes_nothing_in_its_description():
    t = _tool("simulate_revoke")
    assert "NOTHING in Azure" in t.description or "Changes NOTHING" in t.description


def test_tool_names_are_stable():
    """Tool names are LLM-visible and appear in `automations/builtin_agents.json` prompts.
    Renaming one silently breaks every prompt that mentions it."""
    names = [t.name for t in agent_tool.build_iam_tools("t1")]
    assert len(names) == len(set(names))
    assert all(n.islower() and " " not in n for n in names)


def test_the_builtin_identity_agent_prompt_names_only_tools_that_exist():
    """The prompt tells the model to prefer these tools by name. A tool named there but not
    registered is a silent dead end: the model asks for it, gets nothing back, and falls through
    to guessing from raw Resource Graph output."""
    import json
    import pathlib

    path = (pathlib.Path(__file__).resolve().parents[1]
            / "app" / "automations" / "builtin_agents.json")
    agents = json.loads(path.read_text(encoding="utf-8"))["agents"]
    prompt = agents["builtin-identity-access-agent"]["instructions"]
    registered = {t.name for t in agent_tool.build_iam_tools("t1")}

    named = {n for n in registered if f"`{n}`" in prompt}
    assert named, "the identity agent prompt no longer references the IAM tools at all"
    # And it must warn the model not to read UNKNOWN as a negative result.
    assert "UNMEASURED" in prompt
    assert "negative result" in prompt
    assert "no escalation paths" in prompt, "the prompt must name the wrong summaries explicitly"


# =========================================================================== honesty
@pytest.mark.anyio
async def test_unused_permissions_reports_unmeasured_rather_than_none(isolated_cache):
    """The single most dangerous confusion for a summarising model: an empty recommendation
    list means "we never measured usage", and reads as "nothing is over-privileged"."""
    out = _text(await _tool("unused_permissions_for").handler({}, {}))
    assert "UNMEASURED" in out
    assert "nothing here is a claim about what is unused" in out


@pytest.mark.anyio
async def test_unused_permissions_distinguishes_a_measured_empty_result(isolated_cache):
    cache.write_rightsizing("t1", {
        "measured": True, "recommendations": [], "assessed": 12, "window_days": 30,
        "action_universe_size": 900, "limitations": [], "excluded": [], "notes": [],
    })
    out = _text(await _tool("unused_permissions_for").handler({}, {}))
    assert "UNMEASURED" not in out
    assert "30-day window" in out and "900" in out


@pytest.mark.anyio
async def test_escalation_says_an_empty_result_is_not_an_all_clear(isolated_cache, monkeypatch):
    from app.iam import escalation

    monkeypatch.setattr(
        escalation, "graph_for_tenant",
        lambda *a, **k: {"paths": [], "limitations": ["Managed identities were not collected."]},
    )
    out = _text(await _tool("escalation_paths_to").handler({}, {"target_role": "Owner"}))
    assert "NOT an all-clear" in out
    assert "Managed identities were not collected." in out


@pytest.mark.anyio
async def test_escalation_preserves_the_role_name_as_written(isolated_cache, monkeypatch):
    from app.iam import escalation

    monkeypatch.setattr(escalation, "graph_for_tenant", lambda *a, **k: {"paths": [], "limitations": []})
    out = _text(await _tool("escalation_paths_to").handler({}, {"target_role": "Owner"}))
    assert "to Owner" in out, "lower-casing the target makes the answer read as a different role"


@pytest.mark.anyio
async def test_access_changed_since_distinguishes_no_baseline_from_no_changes(isolated_cache):
    cache.write_drift("t1", {"available": False, "note": "Only one run is retained.",
                             "changes": [], "total": 0})
    out = _text(await _tool("access_changed_since").handler({}, {}))
    assert "unknown" in out.lower()
    assert "Only one run is retained." in out

    cache.write_drift("t1", {"available": True, "changes": [], "total": 0, "worsening": 0})
    out2 = _text(await _tool("access_changed_since").handler({}, {}))
    assert "measured result" in out2


@pytest.mark.anyio
async def test_who_can_reach_resource_reports_unknown_on_an_unscanned_tenant(isolated_cache):
    out = _text(await _tool("who_can_reach_resource").handler({}, {"resource_id": RES}))
    assert out.startswith("UNKNOWN")
    assert "not nobody" in out


@pytest.mark.anyio
async def test_who_can_reach_resource_flags_deleted_principals_and_bypass(isolated_cache, monkeypatch):
    from app.iam import resource_access

    monkeypatch.setattr(resource_access, "for_resource", lambda t, r: {
        "measured": True, "total": 2, "privilegedTotal": 1, "reason": "",
        "principals": [
            {"principalId": "p1", "principalName": "Alice", "principalType": "User",
             "principalExists": schema.EXISTS_TRUE, "privileged": True,
             "grants": [{"roleName": "Owner", "grantedAt": "subscription"}]},
            {"principalId": "p2", "principalName": "", "principalType": "ServicePrincipal",
             "principalExists": schema.EXISTS_FALSE, "privileged": False,
             "grants": [{"roleName": "Reader", "grantedAt": "this resource"}]},
        ],
        "bypass": {"measured": True, "checked": 1, "reason": "",
                   "openDoors": [{"title": "Shared key authentication is enabled"}]},
        "limitations": ["2 of these grants are inherited from a broader scope."],
    })
    out = _text(await _tool("who_can_reach_resource").handler({}, {"resource_id": RES}))
    assert "DELETED PRINCIPAL" in out
    assert "privileged" in out
    assert "RBAC is not the only door" in out
    assert "inherited from a broader scope" in out


@pytest.mark.anyio
async def test_who_can_reach_resource_says_when_the_bypass_sweep_never_ran(isolated_cache, monkeypatch):
    from app.iam import resource_access

    monkeypatch.setattr(resource_access, "for_resource", lambda t, r: {
        "measured": True, "total": 0, "privilegedTotal": 0, "reason": "", "principals": [],
        "bypass": {"measured": False, "checked": 0, "openDoors": [],
                   "reason": "No RBAC-bypass sweep has been run for this tenant."},
        "limitations": [],
    })
    out = _text(await _tool("who_can_reach_resource").handler({}, {"resource_id": RES}))
    assert "No RBAC-bypass sweep has been run" in out


# =========================================================================== behavior
@pytest.mark.anyio
async def test_why_access_names_the_assignment_and_where_it_was_made(isolated_cache, monkeypatch):
    """The question is asked by somebody about to REMOVE access, so the answer has to be the
    assignment id and the scope it lives at — not merely "yes they have access"."""
    from app.iam import compose

    monkeypatch.setattr(compose, "build_master_rows", lambda t: [
        _row(roleName="Owner", scope=SCOPE,
             assignmentId="/subscriptions/x/providers/Microsoft.Authorization/roleAssignments/keep-me"),
    ])
    out = _text(await _tool("why_does_principal_have_access").handler(
        {}, {"principal": "alice", "scope": RES}))
    assert "keep-me" in out
    assert "assigned directly" in out
    assert "affects every other resource under that scope" in out


@pytest.mark.anyio
async def test_why_access_explains_a_group_derived_grant(isolated_cache, monkeypatch):
    from app.iam import compose

    monkeypatch.setattr(compose, "build_master_rows", lambda t: [
        _row(accessPath=schema.PATH_GROUP, sourceGroupName="Platform Admins", roleName="Contributor"),
    ])
    out = _text(await _tool("why_does_principal_have_access").handler(
        {}, {"principal": "alice", "scope": RES}))
    assert "via group Platform Admins" in out


@pytest.mark.anyio
async def test_why_access_does_not_claim_absence_of_access(isolated_cache, monkeypatch):
    from app.iam import compose

    monkeypatch.setattr(compose, "build_master_rows", lambda t: [_row()])
    out = _text(await _tool("why_does_principal_have_access").handler(
        {}, {"principal": "alice", "scope": "/subscriptions/99999999-9999-9999-9999-999999999999"}))
    assert "not proof of no access" in out


@pytest.mark.anyio
async def test_simulate_revoke_refuses_an_assignment_that_is_not_in_the_snapshot(isolated_cache, monkeypatch):
    from app.iam import compose

    monkeypatch.setattr(compose, "build_master_rows", lambda t: [_row()])
    out = _text(await _tool("simulate_revoke").handler({}, {"assignment_id": "nope"}))
    assert "no assignment" in out and "already be gone" in out


@pytest.mark.anyio
async def test_simulate_revoke_reports_a_revocation_that_changes_nothing(isolated_cache, monkeypatch):
    """A revoke that removes nothing gets signed off as "access removed" while the access is
    still there. It is the one result this tool must never bury."""
    from app.iam import compose, simulator

    monkeypatch.setattr(compose, "build_master_rows", lambda t: [_row()])
    monkeypatch.setattr(simulator, "simulate", lambda *a, **k: {
        "principals_affected": 0, "access_lost": [], "access_retained_via_other_path": [{"x": 1}],
        "orphaned_resources": [], "unchanged": True,
        "standing_privilege_before": 5, "standing_privilege_after": 5, "limitations": [],
    })
    out = _text(await _tool("simulate_revoke").handler({}, {"assignment_id": "a1"}))
    assert "changes NOTHING" in out


@pytest.mark.anyio
async def test_a_tool_never_issues_an_azure_call(isolated_cache, monkeypatch):
    """Every tool answers from the cache. One that reached for a token would be slow, would
    disagree with the screens, and would make a read-only assistant capable of rate-limiting the
    customer's tenant."""
    import app.azure.credentials as creds

    async def _boom(*_a, **_k):
        raise AssertionError("an agent tool tried to acquire an Azure token")

    monkeypatch.setattr(creds, "get_arm_token", _boom, raising=False)
    monkeypatch.setattr(creds, "get_graph_token", _boom, raising=False)

    for name, args in (
        ("unused_permissions_for", {}),
        ("access_changed_since", {}),
        ("who_can_reach_resource", {"resource_id": RES}),
        ("privileged_access_review", {}),
    ):
        await _tool(name).handler({}, args)

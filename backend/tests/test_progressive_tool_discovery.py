"""Regression tests for bounded catalogs, deferred search, skills, and provider guards."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agent.openai_provider import OpenAIProvider
from app.agent.orchestrator import Orchestrator
from app.agent.provider import LLMProvider, StreamEvent, ToolSpec
from app.agent.provider_capabilities import (
    ToolDefinitionLimitError,
    capabilities_for,
    validate_tool_count,
)
from app.agent.skills import get_skill, list_skills
from app.agent.tool_catalog import ToolCatalog, ToolNameCollisionError, make_entry
from app.agent.tool_results import ToolArtifactStore, prepare_tool_result
from app.agent.tool_router import internal_tool_specs, route_initial
from app.connectors.base import ConnectorTool, ConnectorToolset
from app.core.config import get_settings
from app.mcp.client import DiscoveredTool


def _spec(name: str, description: str, source: str, *, kind: str = "read") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        kind=kind,
        source=source,
    )


def _production_sized_catalog() -> ToolCatalog:
    entries = []
    for index in range(68):
        name = "monitor" if index == 0 else f"azure_service_{index}"
        desc = "Azure Monitor metrics health logs" if index == 0 else f"Azure service {index}"
        entries.append(make_entry(_spec(name, desc, "azure_mcp"), source_hint="azure_mcp"))
    entra = [
        ("search_users", "Search Entra users by UPN"),
        ("get_user_mfa_status", "Get a user's MFA authentication registration"),
        ("get_conditional_access_policies", "List Conditional Access policies"),
    ]
    entra.extend((f"entra_operation_{i}", f"Other Entra operation {i}") for i in range(41))
    for name, desc in entra:
        entries.append(make_entry(_spec(name, desc, "entra_mcp"), source_hint="entra_mcp"))
    for index in range(21):
        entries.append(
            make_entry(
                _spec(f"first_party_{index}", f"First-party helper {index}", "connector"),
                source_hint="connector",
            )
        )
    entries.extend(make_entry(spec, source_hint="internal") for spec in internal_tool_specs())
    return ToolCatalog(entries)


def test_137_available_tools_route_to_bounded_surface():
    catalog = _production_sized_catalog()
    assert len(catalog.names()) == 137  # original 133 executable tools + four routing tools
    surface = route_initial(
        "Investigate a user's MFA and Conditional Access",
        catalog,
        initial_budget=24,
        max_per_turn=32,
    )
    assert len(surface.active_names) <= 24
    assert "search_users" in surface.active_names
    assert "get_user_mfa_status" in surface.active_names
    assert "get_conditional_access_policies" in surface.active_names
    assert set(spec.name for spec in surface.specs()) == set(surface.active_names)


def test_greeting_exposes_only_internal_progressive_tools():
    surface = route_initial("hi", _production_sized_catalog())
    assert set(surface.active_names) == {spec.name for spec in internal_tool_specs()}


def test_conditional_access_does_not_activate_unrelated_azure_access_bundle():
    catalog = _production_sized_catalog()
    surface = route_initial(
        "List the names and enabled or report-only states of Conditional Access policies",
        catalog,
    )
    assert "get_conditional_access_policies" in surface.active_names
    assert "who_can_access" not in surface.active_names
    assert "why_does_principal_have_access" not in surface.active_names


def test_read_route_excludes_entra_writes_but_mutation_route_includes_them():
    entries = [
        make_entry(
            _spec("list_applications", "List app registrations", "entra_mcp"),
            source_hint="entra_mcp",
        ),
        make_entry(
            _spec(
                "create_application",
                "Create an app registration",
                "entra_mcp",
                kind="write",
            ),
            source_hint="entra_mcp",
            kind="write",
        ),
    ]
    entries.extend(make_entry(spec, source_hint="internal") for spec in internal_tool_specs())
    catalog = ToolCatalog(entries)
    read_surface = route_initial("List application registrations", catalog)
    write_surface = route_initial("Create an application registration", catalog)
    assert "list_applications" in read_surface.active_names
    assert "create_application" not in read_surface.active_names
    assert "create_application" in write_surface.active_names


def test_connector_write_intent_does_not_activate_unrelated_entra_write():
    entries = [
        make_entry(
            _spec(
                "create_application",
                "Create an Entra app registration",
                "entra_mcp",
                kind="write",
            ),
            source_hint="entra_mcp",
            kind="write",
        ),
        make_entry(
            _spec(
                "jira_create_issue",
                "Create a Jira issue or ticket",
                "connector:jira",
                kind="write",
            ),
            source_hint="connector:jira",
            kind="write",
        ),
    ]
    entries.extend(make_entry(spec, source_hint="internal") for spec in internal_tool_specs())
    surface = route_initial("Create a Jira ticket summarizing this incident", ToolCatalog(entries))
    assert "jira_create_issue" in surface.active_names
    assert "create_application" not in surface.active_names


def test_selection_is_stable_and_deferred_expansion_never_exceeds_ceiling():
    catalog = _production_sized_catalog()
    first = route_initial("diagnose Azure monitor health", catalog, initial_budget=12, max_per_turn=16)
    second = route_initial("diagnose Azure monitor health", catalog, initial_budget=12, max_per_turn=16)
    assert first.active_names == second.active_names
    found = first.search("Entra user MFA Conditional Access", limit=12)
    first.load_search_results(found)
    first.load_bundle(["entra.users", "entra.conditional_access"])
    assert len(first.active_names) <= 16
    assert len(first.active_names) == len(set(first.active_names))


def test_catalog_rejects_cross_source_name_collision():
    one = make_entry(_spec("same_name", "Azure", "azure_mcp"), source_hint="azure_mcp")
    two = make_entry(_spec("same_name", "Connector", "connector"), source_hint="connector")
    with pytest.raises(ToolNameCollisionError):
        ToolCatalog([one, two])


def test_provider_limit_guard_stops_129_tools():
    tools = [_spec(f"tool_{index}", "test", "unknown") for index in range(129)]
    with pytest.raises(ToolDefinitionLimitError) as exc:
        validate_tool_count("openai", "gpt-4.1", tools)
    assert exc.value.offered == 129
    assert exc.value.limit == 128


def test_openai_transport_and_native_search_are_independent_capabilities():
    sol = capabilities_for("openai", "gpt-5.6-sol")
    assert sol.api == "responses"
    assert sol.supports_responses is True
    assert sol.responses_required_for_tools is True
    assert sol.native_tool_search is False

    prior = capabilities_for("openai", "gpt-5.5")
    assert prior.api == "chat_completions"
    assert prior.supports_responses is True
    assert prior.responses_required_for_tools is False
    assert prior.native_tool_search is True


def test_large_result_is_valid_json_and_resumable():
    store = ToolArtifactStore()
    original = {"isError": False, "content": ["x" * 10000], "items": list(range(1000))}
    compact, meta = prepare_tool_result(original, cap=2000, artifacts=store)
    assert meta is not None
    json.loads(json.dumps(compact))
    first = store.read(str(meta["artifact_id"]), offset=0, limit=1200)
    assert first["isError"] is False
    assert first["next_offset"] == 1200
    final = store.read(
        str(meta["artifact_id"]),
        offset=int(first["next_offset"]),
        limit=20000,
    )
    assert final["total_chars"] > 10000


def test_skill_catalog_loads_workflow_and_bundle_metadata():
    skills = list_skills()
    assert len(skills) >= 6
    skill = get_skill("entra-user-investigation")
    assert skill is not None
    assert "entra.authentication" in skill.bundles
    assert "Eligibility is not standing access" in skill.instructions


class _CaptureProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls: list[list[ToolSpec]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ):
        self.calls.append(list(tools or []))
        yield StreamEvent(type="done")


class _FakeMcp:
    def __init__(self, tools: list[DiscoveredTool]) -> None:
        self.tools = tools

    async def list_tools(self) -> list[DiscoveredTool]:
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"isError": False, "content": [json.dumps({"name": name})]}

    def close(self) -> None:
        pass


def _discovered(prefix: str, count: int, description: str) -> list[DiscoveredTool]:
    return [
        DiscoveredTool(
            name=f"{prefix}_{index}",
            description=description,
            parameters={"type": "object", "properties": {}},
            kind="read",
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_orchestrator_never_sends_full_oversized_catalog(monkeypatch):
    provider = _CaptureProvider()
    monkeypatch.setattr("app.agent.orchestrator.build_provider_for", lambda *_: provider)
    monkeypatch.setattr(
        "app.agent.orchestrator.getattr" if False else "app.agent.orchestrator.build_mcp_client",
        lambda *_args, **_kwargs: _FakeMcp([]),
    )
    runner = Orchestrator(get_settings(), provider="openai", model="gpt-4.1", entra_enabled=True)
    runner._mcp = cast(Any, _FakeMcp(_discovered("azure", 68, "Azure monitoring network storage")))
    runner._entra = cast(Any, _FakeMcp(_discovered("entra", 44, "Entra users groups MFA Conditional Access")))
    runner._entra_blocked = frozenset()
    toolset = ConnectorToolset()

    async def _handler(_config: dict[str, Any], _args: dict[str, Any]) -> dict[str, Any]:
        return {"isError": False, "content": ["ok"]}

    toolset.add_connector(
        {},
        [
            ConnectorTool(
                name=f"helper_{index}",
                description="First party helper",
                parameters={"type": "object", "properties": {}},
                kind="read",
                handler=_handler,
            )
            for index in range(21)
        ],
    )
    runner._connectors = toolset
    events = [event async for event in runner.run([{"role": "user", "content": "hi"}])]
    assert provider.calls
    assert len(provider.calls[0]) == 4
    routing = next(event.data for event in events if event.type == "routing")
    assert routing["available"] == 137
    assert routing["selected"] == 4


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any):
        self.calls.append(kwargs)
        call_number = len(self.calls)

        async def _events():
            response = SimpleNamespace(id=f"resp-{call_number}", usage=SimpleNamespace(input_tokens=2, output_tokens=3))
            yield SimpleNamespace(type="response.created", response=response)
            if call_number == 1:
                item = SimpleNamespace(
                    type="tool_search_call",
                    id="search-1",
                    call_id="search-call-1",
                    arguments={"query": "user MFA", "limit": 2},
                )
                yield SimpleNamespace(type="response.output_item.done", item=item)
            else:
                item = SimpleNamespace(
                    type="function_call",
                    id="fc-1",
                    call_id="call-1",
                    name="get_user_mfa_status",
                    arguments='{"user":"alex@example.com"}',
                )
                yield SimpleNamespace(type="response.output_item.done", item=item)
            yield SimpleNamespace(type="response.completed", response=response)

        return _events()


class _FakeResponsesClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


@pytest.mark.asyncio
async def test_native_openai_client_search_loads_matching_deferred_tools():
    provider = OpenAIProvider(provider="openai", api_key="test", model="gpt-5.5")
    fake = _FakeResponsesClient()
    provider._client = fake  # type: ignore[assignment]
    tools = [
        ToolSpec(
            "search_users",
            "Search directory users",
            {"type": "object", "properties": {}},
            defer_loading=True,
        ),
        ToolSpec(
            "get_user_mfa_status",
            "Get user MFA registration",
            {"type": "object", "properties": {}},
            defer_loading=True,
        ),
        ToolSpec(
            "monitor",
            "Azure Monitor metrics",
            {"type": "object", "properties": {}},
            defer_loading=True,
        ),
    ]
    emitted = [
        event
        async for event in provider.stream([{"role": "user", "content": "Check Alex MFA"}], tools)
    ]
    calls = [event for event in emitted if event.type == "tool_calls"]
    assert calls and calls[0].tool_calls[0].name == "get_user_mfa_status"
    assert len(fake.responses.calls) == 2
    output = fake.responses.calls[1]["input"][0]
    loaded_names = [tool["name"] for tool in output["tools"]]
    assert "get_user_mfa_status" in loaded_names
    assert "monitor" not in loaded_names

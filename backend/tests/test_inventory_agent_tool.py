"""Validated natural-language Resource Graph inventory tool."""
from __future__ import annotations

import json

import pytest

from app.agent import inventory_tool


class _Mcp:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        command = arguments["command"]
        if command == "generate_query":
            return {"isError": False, "content": [json.dumps({"query": "Resources | project name"})]}
        if command == "validate_query":
            return {"isError": False, "content": [json.dumps({
                "isValid": self.valid,
                "syntaxErrors": "bad syntax" if not self.valid else "No Syntax Errors",
            })]}
        options = arguments["parameters"]["options"]
        page = 2 if options.get("$skipToken") else 1
        return {"isError": False, "content": [json.dumps({
            "results": {"data": [{"name": f"row-{page}"}]},
            "totalRecords": 2,
            "skipToken": "next" if page == 1 else "",
        })]}


class _TransientMcp(_Mcp):
    def __init__(self) -> None:
        super().__init__()
        self.generate_attempts = 0

    async def call_tool(self, name: str, arguments: dict):
        if arguments.get("command") == "generate_query":
            self.generate_attempts += 1
            if self.generate_attempts < 3:
                return {"isError": True, "content": ["GatewayTimeout"]}
        return await super().call_tool(name, arguments)


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0])


@pytest.mark.asyncio
async def test_generate_validate_execute_and_page_in_order() -> None:
    mcp = _Mcp()
    handler = inventory_tool.make_inventory_query(
        mcp, scope_hint="Only subscription selected-sub is in scope.",
    )
    payload = _payload(await handler({}, {"request": "List public endpoints", "top": 1}))
    assert [call[1]["command"] for call in mcp.calls] == [
        "generate_query", "validate_query", "execute_query", "execute_query",
    ]
    assert "selected-sub" in mcp.calls[0][1]["parameters"]["prompt"]
    assert payload["query_validated"] is True
    assert payload["pagination_complete"] is True
    assert [row["name"] for row in payload["rows"]] == ["row-1", "row-2"]


@pytest.mark.asyncio
async def test_invalid_generated_query_is_never_executed() -> None:
    mcp = _Mcp(valid=False)
    handler = inventory_tool.make_inventory_query(mcp)
    result = await handler({}, {"request": "List resources"})
    assert result["isError"] is True
    assert "bad syntax" in result["content"][0]
    assert [call[1]["command"] for call in mcp.calls] == ["generate_query", "validate_query"]


@pytest.mark.asyncio
async def test_transient_gateway_timeout_is_retried(monkeypatch) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.agent.inventory_tool.asyncio.sleep", no_sleep)
    mcp = _TransientMcp()
    handler = inventory_tool.make_inventory_query(mcp)
    result = await handler({}, {"request": "List resources"})
    assert result["isError"] is False
    assert mcp.generate_attempts == 3


def test_registered_tool_is_read_only_and_direct() -> None:
    class Toolset:
        tools = []

        def add_connector(self, _config, tools):
            self.tools.extend(tools)

    toolset = Toolset()
    inventory_tool.register_inventory_tool(toolset, mcp_client=_Mcp())
    assert [tool.name for tool in toolset.tools] == ["azure_resource_inventory"]
    assert toolset.tools[0].kind == "read"
    assert "never executed unless Azure validates it" in toolset.tools[0].description


def test_generated_query_is_hard_scoped_before_validation() -> None:
    query = inventory_tool._scope_query(
        "Resources | project subscriptionId, name", ["sub-one", "sub-two"],
    )
    assert query.startswith("Resources\n| where subscriptionId in~ (\"sub-one\", \"sub-two\")")
    assert "| project subscriptionId, name" in query


def test_unknown_query_table_is_refused_when_scope_is_required() -> None:
    with pytest.raises(ValueError, match="scope-safe"):
        inventory_tool._scope_query("SomeUnknownTable | take 5", ["sub-one"])

"""Deterministic public-exposure inventory tool."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent import public_exposure_tool


def _result(rows, ok=True, error=""):
    return SimpleNamespace(ok=ok, rows=rows, error=error, complete=True, total=len(rows))


@pytest.mark.asyncio
async def test_collects_three_sources_and_classifies(monkeypatch) -> None:
    async def query(kql, _connection, **_kwargs):
        if "publicipaddresses" in kql:
            return _result([{"name": "pip", "type": "microsoft.network/publicipaddresses"}])
        if "storageaccounts" in kql:
            return _result([{
                "name": "storage", "type": "microsoft.storage/storageaccounts",
                "publicNetworkAccess": "Enabled", "defaultAction": "Allow",
            }])
        return _result([{
            "name": "flow", "type": "microsoft.logic/workflows",
            "triggers": {"manual": {"type": "Request", "kind": "Http"}},
        }])

    monkeypatch.setattr(public_exposure_tool, "run_kql_collect", query)
    handler = public_exposure_tool.make_public_exposure_query(
        {"id": "connection"}, allowed_subscription_ids=["s1"],
    )
    result = await handler({}, {})
    assert result["isError"] is False
    payload = json.loads(result["content"][0])
    assert payload["status"] == "complete"
    assert payload["inventory_complete"] is True
    assert payload["row_count"] == 3
    assert payload["counts_by_exposure_status"] == {
        "public_endpoint_resource": 1,
        "public_or_firewall_controlled": 1,
        "public_http_trigger": 1,
    }
    flow = next(row for row in payload["rows"] if row["name"] == "flow")
    assert flow["http_trigger_count"] == 1
    assert all("where subscriptionId in~ (\"s1\")" in query for _, query in public_exposure_tool._queries(["s1"]))


@pytest.mark.asyncio
async def test_partial_query_failure_is_explicit(monkeypatch) -> None:
    calls = 0

    async def query(_kql, _connection, **_kwargs):
        nonlocal calls
        calls += 1
        return _result([], ok=calls != 2, error="ARM 503 timeout" if calls == 2 else "")

    monkeypatch.setattr(public_exposure_tool, "run_kql_collect", query)
    handler = public_exposure_tool.make_public_exposure_query(
        {}, allowed_subscription_ids=["s1"],
    )
    payload = json.loads((await handler({}, {}))["content"][0])
    assert payload["status"] == "partial"
    assert payload["errors"] == [{"source": "paas", "error": "ARM 503 timeout"}]


@pytest.mark.asyncio
async def test_row_cap_marks_inventory_partial(monkeypatch) -> None:
    async def query(_kql, _connection, **_kwargs):
        result = _result([])
        result.complete = False
        result.total = 6000
        return result

    monkeypatch.setattr(public_exposure_tool, "run_kql_collect", query)
    handler = public_exposure_tool.make_public_exposure_query(
        {}, allowed_subscription_ids=["s1"],
    )
    payload = json.loads((await handler({}, {}))["content"][0])
    assert payload["status"] == "partial"
    assert payload["inventory_complete"] is False
    assert payload["incomplete_sources"][0] == {
        "source": "network_edge",
        "rows_collected": 0,
        "total_rows": 6000,
        "reason": "Collection reached the 5000-row safety limit.",
    }


def test_registration_is_read_only_and_has_no_arguments() -> None:
    class Toolset:
        tools = []

        def add_connector(self, _config, tools):
            self.tools.extend(tools)

    toolset = Toolset()
    public_exposure_tool.register_public_exposure_tool(
        toolset, connection={}, allowed_subscription_ids=["s1"],
    )
    assert [tool.name for tool in toolset.tools] == ["azure_public_exposure_inventory"]
    assert toolset.tools[0].kind == "read"
    assert toolset.tools[0].parameters == {"type": "object", "properties": {}}

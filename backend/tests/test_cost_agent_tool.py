"""Actual-cost chat tool: dates, scope, aggregation, caching, and registration."""
from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pytest

from app.cost import agent_tool
from app.inventory import cost


class _Principal:
    is_admin = False

    def __init__(self, permissions: tuple[str, ...] = ("cost.read",)) -> None:
        self.permissions = set(permissions)

    def has(self, permission: str) -> bool:
        return permission in self.permissions


class _Toolset:
    def __init__(self) -> None:
        self.tools: list[Any] = []

    def add_connector(self, _config: dict[str, Any], tools: list[Any]) -> None:
        self.tools.extend(tools)


@pytest.fixture(autouse=True)
def _clear_query_cache() -> None:
    cost._query_cache.clear()


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False, result
    return json.loads(result["content"][0])


def test_last_seven_days_uses_seven_inclusive_server_dates() -> None:
    period = cost.resolve_query_period("last_7_days", today=date(2026, 8, 29))
    assert period["start_date"] == "2026-08-23"
    assert period["end_date"] == "2026-08-29"
    assert period["from"] == "2026-08-23T00:00:00+00:00"
    assert period["to"] == "2026-08-29T23:59:59+00:00"


def test_previous_month_handles_year_boundary() -> None:
    period = cost.resolve_query_period("previous_month", today=date(2026, 1, 5))
    assert period["start_date"] == "2025-12-01"
    assert period["end_date"] == "2025-12-31"


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("", "", "ISO dates"),
        ("2026-08-29", "2026-08-28", "must not be after"),
        ("2026-08-01", "2026-08-30", "must not be in the future"),
        ("2025-01-01", "2026-08-29", "366 inclusive days"),
    ],
)
def test_invalid_custom_periods_fail_closed(start: str, end: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        cost.resolve_query_period("custom", start, end, today=date(2026, 8, 29))


def test_query_body_distinguishes_actual_amortized_daily_and_filters() -> None:
    period = cost.resolve_query_period("last_7_days", today=date(2026, 8, 29))
    actual = cost.build_cost_query_body(
        period,
        cost_type="ActualCost",
        group_by="resource",
        filters={"resource_group": "rg-app", "service_name": "Virtual Machines"},
    )
    assert actual["type"] == "ActualCost"
    assert actual["dataset"]["grouping"][0]["name"] == "ResourceId"
    assert len(actual["dataset"]["filter"]["and"]) == 2

    amortized = cost.build_cost_query_body(period, cost_type="AmortizedCost", group_by="day")
    assert amortized["type"] == "AmortizedCost"
    assert amortized["dataset"]["granularity"] == "Daily"
    assert "grouping" not in amortized["dataset"]


@pytest.mark.asyncio
async def test_breakdown_preserves_multiple_currencies_instead_of_combining(monkeypatch) -> None:
    async def token(_connection):
        return "token", None

    async def query(_token, subscription_id, _body):
        currency = "USD" if subscription_id == "s1" else "EUR"
        return [{"Cost": 10, "Currency": currency, "ServiceName": "Compute"}], ""

    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr(cost, "_subscription_breakdown", query)
    result = await cost.query_cost_breakdown(
        {"id": "c1"}, ["s1", "s2"], "tenant", "c1", group_by="service",
    )
    assert result["available"] is True
    assert result["total"] is None
    assert result["currency"] == ""
    assert result["totals_by_currency"] == {"EUR": 10.0, "USD": 10.0}
    assert "not combined" in result["currency_note"]


@pytest.mark.asyncio
async def test_breakdown_cache_key_includes_period_group_and_filter(monkeypatch) -> None:
    calls = 0

    async def token(_connection):
        return "token", None

    async def query(_token, _subscription_id, _body):
        nonlocal calls
        calls += 1
        return [{"Cost": 5, "Currency": "USD", "ResourceId": "/r"}], ""

    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr(cost, "_subscription_breakdown", query)
    common = ({"id": "c1"}, ["s1"], "tenant", "c1")
    first = await cost.query_cost_breakdown(*common, timeframe="last_7_days")
    second = await cost.query_cost_breakdown(*common, timeframe="last_7_days")
    await cost.query_cost_breakdown(*common, timeframe="last_30_days")
    await cost.query_cost_breakdown(*common, timeframe="last_7_days", group_by="service")
    await cost.query_cost_breakdown(
        *common, timeframe="last_7_days", filters={"resource_group": "rg-one"},
    )
    assert first["cached"] is False
    assert second["cached"] is True
    assert calls == 4


@pytest.mark.asyncio
async def test_partial_subscription_failure_is_explicit_and_not_cached(monkeypatch) -> None:
    async def token(_connection):
        return "token", None

    async def query(_token, subscription_id, _body):
        if subscription_id == "s2":
            return [], "ARM 403: Cost Management Reader is required"
        return [{"Cost": 3, "Currency": "USD", "ResourceId": f"/{subscription_id}"}], ""

    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr(cost, "_subscription_breakdown", query)
    result = await cost.query_cost_breakdown(
        {"id": "c1"}, ["s1", "s2"], "tenant", "c1",
    )
    assert result["subscriptions_succeeded"] == 1
    assert result["subscriptions_failed"] == 1
    assert result["errors"] == [{
        "subscription_id": "s2", "error": "ARM 403: Cost Management Reader is required",
    }]
    assert not cost._query_cache


@pytest.mark.asyncio
async def test_transport_retries_429_then_returns_rows(monkeypatch) -> None:
    attempts = 0

    async def arm_rest(_token, _method, _url, _body):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return "", "ARM 429: throttled"
        return json.dumps({
            "properties": {
                "columns": [{"name": "Cost"}, {"name": "Currency"}],
                "rows": [[12.5, "USD"]],
            },
        }), None

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.azure.arm.arm_rest", arm_rest)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    rows, error = await cost._subscription_breakdown("token", "s1", {})
    assert error == ""
    assert rows == [{"Cost": 12.5, "Currency": "USD"}]
    assert attempts == 3


@pytest.mark.asyncio
async def test_tool_requires_cost_permission() -> None:
    handler = agent_tool.make_cost_query("tenant", _Principal(()), {"id": "c1"})
    result = await handler({}, {})
    assert result["isError"] is True
    assert "cost.read" in result["content"][0]


@pytest.mark.asyncio
async def test_tool_enforces_selected_scope_and_returns_subscription_names(monkeypatch) -> None:
    async def visible(_connection):
        return [
            {"id": "s1", "name": "One", "state": "Enabled"},
            {"id": "s2", "name": "Two", "state": "Enabled"},
        ], ""

    captured: dict[str, Any] = {}

    async def query(_connection, subscriptions, _tenant, _connection_id, **kwargs):
        captured["subscriptions"] = subscriptions
        captured.update(kwargs)
        return {
            "available": True,
            "period": {"label": "Aug 23, 2026 – Aug 29, 2026"},
            "cost_type": "ActualCost",
            "subscriptions_succeeded": 1,
            "rows": [],
        }

    monkeypatch.setattr(agent_tool, "_visible_subscriptions", visible)
    monkeypatch.setattr(cost, "query_cost_breakdown", query)
    handler = agent_tool.make_cost_query(
        "tenant", _Principal(), {"id": "c1"}, scope="sub:s2",
    )
    payload = _payload(await handler({}, {"timeframe": "last_7_days"}))
    assert captured["subscriptions"] == ["s2"]
    assert payload["subscription_names"] == {"s2": "Two"}

    denied = await handler({}, {"subscription_ids": ["s1"]})
    assert denied["isError"] is True
    assert "outside the selected chat scope" in denied["content"][0]


def test_workload_subscription_extraction_is_deduplicated() -> None:
    workload = {"nodes": [
        {"kind": "subscription", "id": "S1"},
        {"id": "/subscriptions/s1/resourceGroups/rg/providers/x/y"},
        {"id": "/subscriptions/s2/resourceGroups/rg/providers/x/z"},
    ]}
    assert agent_tool.subscription_ids_from_workload(workload) == ["S1", "s2"]


def test_registration_withholds_tool_without_permission() -> None:
    toolset = _Toolset()
    agent_tool.register_cost_tools(
        toolset,
        tenant_id="tenant",
        principal=_Principal(()),
        connection={"id": "c1"},
    )
    assert toolset.tools == []


def test_registration_adds_read_only_actual_cost_tool() -> None:
    toolset = _Toolset()
    agent_tool.register_cost_tools(
        toolset,
        tenant_id="tenant",
        principal=_Principal(),
        connection={"id": "c1"},
    )
    assert [tool.name for tool in toolset.tools] == ["azure_cost_query"]
    assert toolset.tools[0].kind == "read"
    assert "NOT retail SKU pricing" in toolset.tools[0].description


def test_master_switch_withholds_cost_tool(monkeypatch) -> None:
    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: {"cost_tools_enabled": False})
    tools = agent_tool.build_cost_tools("tenant", _Principal(), {"id": "c1"})
    assert tools == []


def test_admin_update_model_accepts_cost_tool_setting() -> None:
    from app.api.admin import AppSettingsUpdate

    update = AppSettingsUpdate(cost_tools_enabled=False)
    assert update.model_dump(exclude_none=True) == {"cost_tools_enabled": False}

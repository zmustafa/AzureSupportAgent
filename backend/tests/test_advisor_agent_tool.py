"""Cross-subscription Advisor tool aggregation and scope safety."""
from __future__ import annotations

import json

import pytest

from app.advisor import agent_tool


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0])


@pytest.mark.asyncio
async def test_queries_every_visible_subscription_and_aggregates(monkeypatch) -> None:
    async def visible(_connection):
        return [
            {"id": "s1", "name": "One", "state": "Enabled"},
            {"id": "s2", "name": "Two", "state": "Enabled"},
        ], ""

    async def token(_connection):
        return "token", None

    async def recommendations(_token, subscription_id):
        return [{
            "id": f"/{subscription_id}/r1",
            "recommendation_type_id": "type-1",
            "category": "Security" if subscription_id == "s1" else "Cost",
            "impact": "High" if subscription_id == "s1" else "Medium",
            "problem": f"Problem {subscription_id}",
            "solution": "Fix it",
            "resource_id": f"/{subscription_id}/resource",
            "impacted_field": "x",
            "impacted_value": "y",
            "last_updated": "2026-08-29",
            "subscription_id": subscription_id,
        }], ""

    monkeypatch.setattr(agent_tool, "_visible_subscriptions", visible)
    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr(agent_tool, "_subscription_recommendations", recommendations)
    handler = agent_tool.make_advisor_query("tenant", {"id": "connection"})
    payload = _payload(await handler({}, {"top": 10, "category": "All", "impact": "All"}))
    assert payload["subscriptions_queried"] == 2
    assert payload["subscriptions_succeeded"] == 2
    assert payload["total_recommendations"] == 2
    assert payload["counts_by_category"] == {"Security": 1, "Cost": 1}
    assert payload["counts_by_subscription"] == {"One": 1, "Two": 1}
    assert [row["impact"] for row in payload["recommendations"]] == ["High", "Medium"]


@pytest.mark.asyncio
async def test_selected_scope_cannot_be_escaped(monkeypatch) -> None:
    async def visible(_connection):
        return [
            {"id": "s1", "name": "One", "state": "Enabled"},
            {"id": "s2", "name": "Two", "state": "Enabled"},
        ], ""

    monkeypatch.setattr(agent_tool, "_visible_subscriptions", visible)
    handler = agent_tool.make_advisor_query(
        "tenant", {"id": "connection"}, scope="sub:s2",
    )
    result = await handler({}, {"subscription_ids": ["s1"]})
    assert result["isError"] is True
    assert "outside the selected chat scope" in result["content"][0]


@pytest.mark.asyncio
async def test_partial_subscription_failure_is_explicit(monkeypatch) -> None:
    async def visible(_connection):
        return [
            {"id": "s1", "name": "One", "state": "Enabled"},
            {"id": "s2", "name": "Two", "state": "Enabled"},
        ], ""

    async def token(_connection):
        return "token", None

    async def recommendations(_token, subscription_id):
        if subscription_id == "s2":
            return [], "ARM 403: Reader access is required"
        return [], ""

    monkeypatch.setattr(agent_tool, "_visible_subscriptions", visible)
    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr(agent_tool, "_subscription_recommendations", recommendations)
    handler = agent_tool.make_advisor_query("tenant", {"id": "connection"})
    payload = _payload(await handler({}, {}))
    assert payload["status"] == "partial"
    assert payload["subscriptions_succeeded"] == 1
    assert payload["subscriptions_failed"] == 1
    assert payload["errors"] == [{
        "subscription_id": "s2", "error": "ARM 403: Reader access is required",
    }]


@pytest.mark.asyncio
async def test_filters_and_deduplicates(monkeypatch) -> None:
    async def visible(_connection):
        return [{"id": "s1", "name": "One", "state": "Enabled"}], ""

    async def token(_connection):
        return "token", None

    row = {
        "id": "/s1/r1",
        "recommendation_type_id": "type-1",
        "category": "Security",
        "impact": "High",
        "problem": "Restrict public access",
        "solution": "Use a private endpoint",
        "resource_id": "/s1/storage",
        "impacted_field": "network",
        "impacted_value": "public",
        "last_updated": "2026-08-29",
        "subscription_id": "s1",
    }

    async def recommendations(_token, _subscription_id):
        return [dict(row), dict(row), {**row, "category": "Cost", "problem": "Resize"}], ""

    monkeypatch.setattr(agent_tool, "_visible_subscriptions", visible)
    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr(agent_tool, "_subscription_recommendations", recommendations)
    handler = agent_tool.make_advisor_query("tenant", {"id": "connection"})
    payload = _payload(await handler({}, {"category": "Security", "search": "private endpoint"}))
    assert payload["total_recommendations"] == 1
    assert payload["recommendations"][0]["problem"] == "Restrict public access"


@pytest.mark.asyncio
async def test_zero_recommendation_subscription_remains_in_coverage(monkeypatch) -> None:
    async def visible(_connection):
        return [
            {"id": "s1", "name": "One", "state": "Enabled"},
            {"id": "s2", "name": "Two", "state": "Enabled"},
        ], ""

    async def token(_connection):
        return "token", None

    async def recommendations(_token, _subscription_id):
        return [], ""

    monkeypatch.setattr(agent_tool, "_visible_subscriptions", visible)
    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr(agent_tool, "_subscription_recommendations", recommendations)
    handler = agent_tool.make_advisor_query("tenant", {"id": "connection"})
    payload = _payload(await handler({}, {}))
    assert payload["subscriptions_queried"] == 2
    assert payload["counts_by_subscription"] == {"One": 0, "Two": 0}


def test_tool_is_explicitly_cross_subscription_and_read_only() -> None:
    [tool] = agent_tool.build_advisor_tools("tenant", {"id": "connection"})
    assert tool.name == "azure_advisor_recommendations"
    assert tool.kind == "read"
    assert "EVERY subscription" in tool.description


@pytest.mark.asyncio
async def test_transport_retries_transient_failure(monkeypatch) -> None:
    attempts = 0

    async def arm_rest(_token, _method, _url):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return "", "ARM 503: timeout"
        return json.dumps({"value": []}), None

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.azure.arm.arm_rest", arm_rest)
    monkeypatch.setattr("app.advisor.agent_tool.asyncio.sleep", no_sleep)
    rows, error = await agent_tool._subscription_recommendations("token", "s1")
    assert rows == []
    assert error == ""
    assert attempts == 3

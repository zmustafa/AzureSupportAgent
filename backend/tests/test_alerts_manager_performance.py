"""Alerts Manager phase 2/3 caching, summary, and pagination contracts."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alerts_manager import cache, planner, service
from app.api import alerts_manager as api
from app.core.db import Base
from app.core.security import Principal
from app.models import AlertManagerChange


@pytest.mark.asyncio
async def test_cache_hit_single_flight_defensive_copy_key_isolation_and_invalidation() -> None:
    calls = 0
    gate = asyncio.Event()

    async def produce():
        nonlocal calls
        calls += 1
        await gate.wait()
        return ([{"id": "one", "nested": {"enabled": True}}], {"partial": False})

    connection = {"id": "conn-1", "tenant_id": "azure-tenant"}
    key_a = cache.inventory_key("rules", connection, tenant_id="app-a", subscription_id="sub-1")
    key_b = cache.inventory_key("rules", connection, tenant_id="app-b", subscription_id="sub-1")
    first = asyncio.create_task(cache.get_or_create(key_a, produce))
    second = asyncio.create_task(cache.get_or_create(key_a, produce))
    await asyncio.sleep(0)
    gate.set()
    one, two = await asyncio.gather(first, second)
    assert calls == 1
    one[0][0]["nested"]["enabled"] = False
    hit = await cache.get_or_create(key_a, produce)
    assert calls == 1
    assert hit[0][0]["nested"]["enabled"] is True

    await cache.get_or_create(key_b, produce)
    assert calls == 2
    await cache.invalidate(kinds={"rules"}, tenant_id="app-a", connection_id="conn-1")
    await cache.get_or_create(key_a, produce)
    assert calls == 3
    await cache.get_or_create(key_b, produce)
    assert calls == 3


@pytest.mark.asyncio
async def test_inventory_endpoints_paginate_and_surface_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = Principal("reader", "reader@example.test", "tenant-a", "admin", frozenset())
    monkeypatch.setattr(api, "_connection", lambda *_args, **_kwargs: {"id": "conn-1", "tenant_id": "azure-a"})
    rows = [{"id": str(index), "name": f"rule-{index}"} for index in range(7)]

    async def list_rules(*_args, **_kwargs):
        return rows, {"partial": True, "truncated": True, "source_total": 10001, "source_count": 10000, "source_limit": 10000, "normalized_count": 7}

    monkeypatch.setattr("app.alerts_manager.rules.list_rules", list_rules)
    result = await api.alert_rules(
        connection_id="conn-1", workload_id=None, subscription_id="sub-1",
        management_group_id=None, family="", page=2, page_size=3, principal=principal,
    )
    assert [item["id"] for item in result["rules"]] == ["3", "4", "5"]
    assert result["total"] == 7
    assert result["partial"] is True
    assert result["truncated"] is True
    assert result["source_total"] == 10001

    legacy = await api.alert_rules(
        connection_id="conn-1", workload_id=None, subscription_id="sub-1",
        management_group_id=None, family="", page=None, page_size=None, principal=principal,
    )
    assert legacy["rules"] == rows
    assert legacy["paginated"] is False


@pytest.mark.asyncio
async def test_summary_is_connection_and_tenant_scoped_without_azure_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(planner, "_PATH", tmp_path / "planner.json")
    monkeypatch.setattr(api, "_connection", lambda connection_id, *_args: {
        "id": connection_id or "conn-default", "display_name": "Local connection", "read_only": True,
    })
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'summary.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    principal = Principal("reader", "reader@example.test", "tenant-a", "admin", frozenset())
    try:
        async with Session() as db:
            for tenant, connection_id, status in [
                ("tenant-a", "conn-1", "pending"),
                ("tenant-a", "conn-1", "approved"),
                ("tenant-a", "conn-2", "pending"),
                ("tenant-b", "conn-1", "pending"),
                ("tenant-a", "conn-1", "applied"),
            ]:
                db.add(AlertManagerChange(
                    tenant_id=tenant, connection_id=connection_id, target_id=f"/{tenant}/{status}",
                    target_type="metric_rule", operation="update", status=status, requested_by="tester",
                    applied_at=service.now() if status == "applied" else None,
                ))
            await db.commit()
            result = await api.summary("conn-1", principal, db)
        assert result["pending_count"] == 1
        assert result["approved_count"] == 1
        assert result["actionable_count"] == 2
        assert result["latest_applied_at"]
        assert result["capabilities"]["connection_id"] == "conn-1"
        assert result["capabilities"]["read_only"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_successful_rule_apply_invalidates_affected_connection_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    connection = {"id": "conn-1", "tenant_id": "azure-a", "read_only": False}
    principal = Principal("approver", "approver@example.test", "tenant-a", "admin", frozenset())
    key = cache.inventory_key("rules", connection, tenant_id="tenant-a", subscription_id="sub-1")
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return ([{"id": f"cached-{calls}"}], {"partial": False})

    await cache.get_or_create(key, produce)
    assert calls == 1
    monkeypatch.setattr(api, "_connection", lambda *_args, **_kwargs: connection)

    async def apply_rule_change(_connection, change):
        return {
            "id": change.target_id, "name": "cpu", "type": "microsoft.insights/metricalerts",
            "location": "global", "properties": {"enabled": True, "severity": 2, "scopes": []},
        }, 200, ""

    monkeypatch.setattr("app.alerts_manager.rules.apply_rule_change", apply_rule_change)
    monkeypatch.setattr("app.evidence.registry.create_snapshot", lambda **_kwargs: {"id": "evidence-1"})
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'apply.db'}")
    async with engine.begin() as db_connection:
        await db_connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            change = AlertManagerChange(
                tenant_id="tenant-a", connection_id="conn-1", target_type="metric_rule",
                target_id="/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/cpu",
                operation="update", status="approved", requested_by="requester",
                desired_encrypted=service.encrypted_json({"payload": {}, "body": {}}),
            )
            db.add(change)
            await db.commit()
            result = await api.apply_change(change.id, principal, db)
        assert result["change"]["status"] == "applied"
        await cache.get_or_create(key, produce)
        assert calls == 2
    finally:
        await engine.dispose()


def test_plan_list_summaries_omit_items_while_detail_remains_full(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(planner, "_PATH", tmp_path / "planner.json")
    stored = planner._put("tenant-a", "plans", {
        "id": "plan-1", "status": "draft", "created_at": "2026-07-11T00:00:00Z",
        "counts": {"missing": 2}, "items": [{"id": "one"}, {"id": "two"}],
    })
    summaries = planner.list_plans("tenant-a")
    assert len(summaries) == 1
    assert summaries[0]["item_count"] == 2
    assert "items" not in summaries[0]
    assert planner.get_plan("tenant-a", stored["id"])["items"] == [{"id": "one"}, {"id": "two"}]


@pytest.mark.asyncio
async def test_plan_list_api_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = Principal("reader", "reader@example.test", "tenant-a", "admin", frozenset())
    monkeypatch.setattr(planner, "list_plans", lambda *_args, **_kwargs: [{"id": str(index), "item_count": index} for index in range(5)])
    result = await api.list_deployment_plans(status="", page=2, page_size=2, principal=principal)
    assert [item["id"] for item in result["plans"]] == ["2", "3"]
    assert result["total"] == 5
    assert result["page"] == 2

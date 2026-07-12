"""Subscription Activity Log diagnostic-settings workflow tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alerts_manager import activity_diagnostics, cache, destination_options, service
from app.api import alerts_manager as api
from app.core.db import Base
from app.core.security import Principal
from app.models import AlertManagerChange, AuditLog

TENANT = "tenant-diag"
CONNECTION = {"id": "connection-diag", "read_only": False}
SUB = "sub-1"
WORKSPACE = "/subscriptions/sub-1/resourceGroups/rg-monitor/providers/Microsoft.OperationalInsights/workspaces/logs"
DESTINATION = {"kind": "workspace", "resource_id": WORKSPACE, "event_hub_name": ""}


def principal() -> Principal:
    return Principal("requester", "requester@example.test", TENANT, "operator", frozenset({"alerts_manager.rule_write", "alerts_manager.approve"}))


def request(**updates) -> dict:
    value = {
        "connection_id": CONNECTION["id"], "subscription_id": SUB, "subscription_ids": [],
        "categories": list(activity_diagnostics.REQUIRED_CATEGORIES), "destination": DESTINATION,
        "setting_name": "send-activity-to-siem",
    }
    value.update(updates)
    return value


def setting(*, categories: list[str] | None = None, workspace: str = WORKSPACE) -> dict:
    return {
        "id": activity_diagnostics.setting_path(SUB, "existing"), "name": "existing",
        "properties": {
            "workspaceId": workspace,
            "logs": [{"category": category, "enabled": True} for category in (categories or ["Administrative"])],
        },
    }


@pytest.fixture()
async def database(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activity-diag.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield Session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inventory_reads_each_subscription_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    await cache.clear()
    calls: list[tuple[str, str, str]] = []

    async def token(_connection): return "token"
    async def arm_write(_token, method, path, **kwargs):
        calls.append((method, path, kwargs["api_version"]))
        if "sub-b" in path:
            return {"unexpected": []}, None, 200
        return {"value": [setting()]}, None, 200

    monkeypatch.setattr(service, "_token", token)
    monkeypatch.setattr("app.azure.arm.arm_write", arm_write)
    result = await activity_diagnostics.inventory(CONNECTION, {"sub-a", "sub-b"}, tenant_id=TENANT)
    assert calls == [
        ("GET", activity_diagnostics.collection_path("sub-a"), activity_diagnostics.API_VERSION),
        ("GET", activity_diagnostics.collection_path("sub-b"), activity_diagnostics.API_VERSION),
    ]
    assert result["partial"] is True
    rows = {row["subscription_id"]: row for row in result["subscriptions"]}
    assert rows["sub-a"]["status"] == "partial"
    assert rows["sub-b"]["status"] == "partial"
    assert rows["sub-b"]["complete"] is False


def test_classification_and_planning_create_update_equivalent_and_unknown() -> None:
    covered = setting(categories=list(activity_diagnostics.REQUIRED_CATEGORIES))
    inventory = {
        "subscriptions": [activity_diagnostics.classify_subscription(SUB, [covered])],
    }
    equivalent = activity_diagnostics.preview_plan(request(), inventory, allowed_subscriptions={SUB})
    assert equivalent["items"][0]["classification"] == "equivalent"

    partial = {"subscriptions": [activity_diagnostics.classify_subscription(SUB, [setting()])]}
    update = activity_diagnostics.preview_plan(request(), partial, allowed_subscriptions={SUB})
    item = update["items"][0]
    assert item["classification"] == "update"
    assert {log["category"] for log in item["desired"]["properties"]["logs"]} == set(activity_diagnostics.REQUIRED_CATEGORIES)

    missing = {"subscriptions": [activity_diagnostics.classify_subscription(SUB, [])]}
    create = activity_diagnostics.preview_plan(request(), missing, allowed_subscriptions={SUB})
    assert create["items"][0]["classification"] == "create"

    unknown = {"subscriptions": [activity_diagnostics.classify_subscription(SUB, [], error="denied")]}
    blocked = activity_diagnostics.preview_plan(request(), unknown, allowed_subscriptions={SUB})
    assert blocked["items"][0]["classification"] == "blocked"
    assert blocked["valid"] is False


def test_strict_destination_category_scope_and_pending_change_validation() -> None:
    missing = {"subscriptions": [activity_diagnostics.classify_subscription(SUB, [])]}
    with pytest.raises(ValueError):
        activity_diagnostics.preview_plan(request(categories=["ServiceHealth"]), missing, allowed_subscriptions={SUB})
    with pytest.raises(ValueError):
        activity_diagnostics.preview_plan(request(subscription_ids=["sub-outside"]), missing, allowed_subscriptions={SUB})
    invalid_destination = {"kind": "workspace", "resource_id": "/not/a/workspace", "event_hub_name": ""}
    plan = activity_diagnostics.preview_plan(request(destination=invalid_destination), missing, allowed_subscriptions={SUB})
    assert plan["items"][0]["classification"] == "blocked"
    target = plan["items"][0]["target_id"]
    blocked = activity_diagnostics.preview_plan(
        request(), missing, allowed_subscriptions={SUB},
        blockers={target.lower(): {"status": "approved", "change_id": "change-1"}},
    )
    assert blocked["items"][0]["blocker"]["status"] == "approved"


@pytest.mark.asyncio
async def test_destination_options_filter_management_group_subscription_rg_and_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    async def token(_connection): return "token"
    async def management_groups(_token):
        return [{"id": "root", "name": "Root", "depth": 0}, {"id": "child", "name": "Child", "depth": 1}], None
    async def subscriptions(_token):
        return [{"id": "sub-out", "name": "Outside"}, {"id": SUB, "name": "Production"}], None
    async def children(_token, group_id):
        return ([{"kind": "mg", "id": "child", "name": "Child"}], None) if group_id == "root" else ([{"kind": "subscription", "id": SUB, "name": "Production"}], None)
    queries: list[tuple[str, set[str] | None]] = []
    async def arg(_connection, query, selected=None, **_kwargs):
        queries.append((query, selected))
        if "resourcecontainers" in query:
            return [{"id": f"/subscriptions/{SUB}/resourceGroups/rg-monitor", "name": "rg-monitor", "subscriptionId": SUB, "location": "eastus"}]
        return [{"id": WORKSPACE, "name": "logs", "subscriptionId": SUB, "resourceGroup": "rg-monitor", "location": "eastus"}]
    monkeypatch.setattr(service, "_token", token)
    monkeypatch.setattr("app.azure.arm.list_all_management_groups", management_groups)
    monkeypatch.setattr("app.azure.arm.list_subscriptions", subscriptions)
    monkeypatch.setattr("app.azure.arm.get_management_group_children", children)
    monkeypatch.setattr(service, "_arg", arg)

    result = await destination_options.options(
        CONNECTION, management_group_id="root", subscription_id=SUB,
        resource_group="rg-monitor", kind="workspace",
    )
    assert [item["id"] for item in result["subscriptions"]] == [SUB]
    assert next(item for item in result["management_groups"] if item["id"] == "child")["depth"] == 1
    assert result["resource_groups"][0]["name"] == "rg-monitor"
    assert result["resources"] == [{
        "id": WORKSPACE, "name": "logs", "subscription_id": SUB,
        "resource_group": "rg-monitor", "location": "eastus",
    }]
    assert all(selected == {SUB} for _query, selected in queries)
    assert "microsoft.operationalinsights/workspaces" in queries[1][0]


@pytest.mark.asyncio
async def test_destination_options_event_hub_returns_hubs_and_namespace_authorization_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    namespace = f"/subscriptions/{SUB}/resourceGroups/rg-stream/providers/Microsoft.EventHub/namespaces/events"
    hub = f"{namespace}/eventhubs/activity"
    rule = f"{namespace}/authorizationRules/diagnostics-send"
    async def token(_connection): return "token"
    async def management_groups(_token): return [], None
    async def subscriptions(_token): return [{"id": SUB, "name": "Production"}], None
    async def arg(_connection, query, _selected=None, **_kwargs):
        if "resourcecontainers" in query:
            return [{"id": f"/subscriptions/{SUB}/resourceGroups/rg-stream", "name": "rg-stream", "subscriptionId": SUB}]
        if "namespaces/eventhubs" in query:
            return [{"id": hub, "name": "activity", "subscriptionId": SUB, "resourceGroup": "rg-stream"}]
        return [{"id": namespace, "name": "events", "subscriptionId": SUB, "resourceGroup": "rg-stream"}]
    async def arm_write(_token, method, path, **kwargs):
        assert (method, path, kwargs["api_version"]) == ("GET", f"{namespace}/authorizationRules", "2024-01-01")
        return {"value": [{"id": rule, "name": "diagnostics-send"}]}, None, 200
    monkeypatch.setattr(service, "_token", token)
    monkeypatch.setattr("app.azure.arm.list_all_management_groups", management_groups)
    monkeypatch.setattr("app.azure.arm.list_subscriptions", subscriptions)
    monkeypatch.setattr(service, "_arg", arg)
    monkeypatch.setattr("app.azure.arm.arm_write", arm_write)

    result = await destination_options.options(
        CONNECTION, subscription_id=SUB, resource_group="rg-stream",
        kind="event_hub", namespace_id=namespace,
    )
    assert result["event_hubs"][0]["id"] == hub
    assert result["authorization_rules"] == [{"id": rule, "name": "diagnostics-send"}]
    assert result["authorization_rules_complete"] is True


@pytest.mark.asyncio
async def test_preview_validate_submit_never_write_and_submit_encrypted_ledger(database, monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = {"api_version": activity_diagnostics.API_VERSION, "subscriptions": [activity_diagnostics.classify_subscription(SUB, [])], "counts": {}, "partial": False}

    async def scope(_payload, _principal): return CONNECTION, {SUB}, inventory
    monkeypatch.setattr(api, "_activity_diagnostic_scope", scope)
    writes = 0
    async def forbidden(*_args, **_kwargs):
        nonlocal writes
        writes += 1
        raise AssertionError("planning must not write to Azure")
    monkeypatch.setattr("app.azure.arm.arm_write", forbidden)

    async with database() as db:
        preview = await api.preview_activity_log_diagnostic_plan(api.ActivityLogDiagnosticPlanRequest(**request()), principal(), db)
        valid = await api.validate_activity_log_diagnostic_plan(
            api.ActivityLogDiagnosticValidationRequest(**request(), plan_token=preview["plan"]["plan_token"]), principal(), db,
        )
        assert valid["valid"] is True
        result = await api.submit_activity_log_diagnostic_plan(
            api.ActivityLogDiagnosticSubmitRequest(**request(), plan_token=preview["plan"]["plan_token"], reason="Send required Activity Logs to SIEM"), principal(), db,
        )
        assert result["azure_writes_performed"] is False
        assert writes == 0
        change = (await db.execute(select(AlertManagerChange))).scalar_one()
        assert change.target_type == "activity_log_diagnostic_setting"
        assert change.status == "pending" and change.auto_apply is False
        assert change.desired_encrypted.startswith("enc:v1:")
        assert service.decrypted_json(change.desired_encrypted)["body"]["properties"]["workspaceId"] == WORKSPACE
        assert change.summary_json["desired_hash"]
        audits = (await db.execute(select(AuditLog))).scalars().all()
        assert len(audits) == 2


@pytest.mark.asyncio
async def test_apply_requires_approval_then_puts_and_invalidates(database, monkeypatch: pytest.MonkeyPatch) -> None:
    target = activity_diagnostics.setting_path(SUB, "send-activity-to-siem")
    body = {"properties": {"workspaceId": WORKSPACE, "logs": [{"category": "Security", "enabled": True}]}}
    change = AlertManagerChange(
        tenant_id=TENANT, connection_id=CONNECTION["id"], target_type="activity_log_diagnostic_setting",
        target_id=target, operation="create", status="pending", risk="medium", summary_json={},
        desired_encrypted=service.encrypted_json({"body": body}), before_encrypted=service.encrypted_json({}),
        after_encrypted="", expected_state_hash="", requested_by="requester", auto_apply=False,
    )
    monkeypatch.setattr(api, "_connection", lambda *_args, **_kwargs: CONNECTION)
    calls: list[tuple[str, str]] = []
    async def token(_connection): return "token"
    async def get_setting(_connection, _target): return None, 404, ""
    async def arm_write(_token, method, path, **kwargs):
        calls.append((method, path))
        assert kwargs["body"] == body
        return {"id": path, "name": "send-activity-to-siem", **body}, None, 200
    invalidated: list[dict] = []
    async def invalidate(**kwargs): invalidated.append(kwargs)
    monkeypatch.setattr(service, "_token", token)
    monkeypatch.setattr(activity_diagnostics, "get_setting", get_setting)
    monkeypatch.setattr("app.azure.arm.arm_write", arm_write)
    monkeypatch.setattr(cache, "invalidate", invalidate)
    monkeypatch.setattr("app.evidence.registry.create_snapshot", lambda **_kwargs: {"id": "evidence-1"})

    async with database() as db:
        db.add(change)
        await db.commit()
        with pytest.raises(HTTPException) as pending:
            await api.apply_change(change.id, principal(), db)
        assert pending.value.status_code == 409 and calls == []
        change.status = "approved"
        change.decided_by = "approver"
        await db.commit()
        result = await api.apply_change(change.id, principal(), db)
        assert calls == [("PUT", target)]
        assert result["change"]["status"] == "applied"
        assert invalidated[0]["kinds"] == {"activity_log_diagnostic_settings"}


@pytest.mark.asyncio
async def test_update_apply_checks_live_hash_before_put(monkeypatch: pytest.MonkeyPatch) -> None:
    target = activity_diagnostics.setting_path(SUB, "existing")
    live = setting()
    body = {"properties": {**live["properties"], "logs": [
        {"category": category, "enabled": True} for category in activity_diagnostics.REQUIRED_CATEGORIES
    ]}}
    change = AlertManagerChange(
        tenant_id=TENANT, connection_id=CONNECTION["id"], target_type="activity_log_diagnostic_setting",
        target_id=target, operation="update", status="approved", risk="medium", summary_json={},
        desired_encrypted=service.encrypted_json({"body": body}), before_encrypted=service.encrypted_json(live),
        after_encrypted="", expected_state_hash=service.canonical_hash(service._resource_body(live)),
        requested_by="requester", auto_apply=False,
    )
    writes = 0
    async def token(_connection): return "token"
    async def get_setting(_connection, _target): return live, 200, ""
    async def arm_write(_token, method, path, **kwargs):
        nonlocal writes
        writes += 1
        assert method == "PUT" and path == target and kwargs["body"] == body
        return {"id": target, **body}, None, 200
    monkeypatch.setattr(service, "_token", token)
    monkeypatch.setattr(activity_diagnostics, "get_setting", get_setting)
    monkeypatch.setattr("app.azure.arm.arm_write", arm_write)
    data, status, error = await activity_diagnostics.apply_change(CONNECTION, change)
    assert status == 200 and error == "" and data["id"] == target and writes == 1

    changed = {**live, "properties": {**live["properties"], "workspaceId": WORKSPACE + "-changed"}}
    async def changed_setting(_connection, _target): return changed, 200, ""
    monkeypatch.setattr(activity_diagnostics, "get_setting", changed_setting)
    data, status, error = await activity_diagnostics.apply_change(CONNECTION, change)
    assert data is None and status == 409 and "state changed" in error and writes == 1


def test_routes_registered() -> None:
    paths = {route.path for route in api.router.routes}
    assert "/alerts-manager/activity-log-diagnostic-settings/inventory" in paths
    assert "/alerts-manager/activity-log-diagnostic-settings/destination-options" in paths
    assert "/alerts-manager/activity-log-diagnostic-settings/plan/preview" in paths
    assert "/alerts-manager/activity-log-diagnostic-settings/plan/validate" in paths
    assert "/alerts-manager/activity-log-diagnostic-settings/plan/submit" in paths

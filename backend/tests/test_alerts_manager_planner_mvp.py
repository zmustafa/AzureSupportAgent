"""Hermetic contracts for the Alerts Manager AMBA planner MVP."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alerts_manager import planner, service
from app.alerts_manager import rules
from app.api import alerts_manager as api
from app.core.db import Base
from app.core.security import Principal
from app.models import AlertManagerChange


TENANT = "tenant-planner"
ACTION_GROUP_ID = "/subscriptions/sub-1/resourceGroups/rg-monitor/providers/Microsoft.Insights/actionGroups/platform"
RESOURCE_ID = "/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-one"


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "planner.json"
    monkeypatch.setattr(planner, "_PATH", path)
    async def live_action_groups(*_args, **_kwargs) -> list[dict]:
        return [{"id": ACTION_GROUP_ID, "name": "Platform on-call", "enabled": True, "receiver_count": 1, "active_receiver_count": 1}]
    monkeypatch.setattr(service, "list_action_groups", live_action_groups)
    return path


def _blueprint_assignment() -> tuple[dict, dict]:
    blueprint = planner.create_blueprint_version(TENANT, {
        "name": "Compute baseline",
        "amba_version": "7",
        "included_resource_types": ["microsoft.compute/virtualmachines"],
        "severity_overrides": {"vm_cpu": 2},
        "default_disabled": False,
    }, actor="alice")
    assignment = planner.save_assignment(TENANT, {
        "blueprint_id": blueprint["blueprint_id"],
        "blueprint_version": blueprint["version"],
        "scope_kind": "subscription",
        "scope_id": "sub-1",
        "connection_id": "connection-1",
        "environment": "production",
        "monitoring_resource_group": "rg-monitor",
    }, actor="alice")
    return blueprint, assignment


def _gap(*, status: str = "missing", threshold: float | None = 90) -> dict:
    return {
        "resource_id": RESOURCE_ID,
        "resource_name": "vm-one",
        "resource_type": "microsoft.compute/virtualmachines",
        "resource_group": "rg-app",
        "subscription_id": "sub-1",
        "location": "eastus",
        "alert_key": "vm_cpu",
        "alert_name": "CPU utilization high",
        "amba_category": "performance",
        "severity": "warning",
        "status": status,
        "recommended": {
            "metric": "Percentage CPU",
            "operator": "GreaterThan",
            "threshold": threshold,
            "window": "PT5M",
        },
        "why": "Sustained high CPU degrades the workload.",
    }


def _analysis_gap(*, decision_key: str = "baseline_missing:vm-one:vm_cpu", gap_type: str = "baseline_missing") -> dict:
    gap = _gap(status="missing" if gap_type == "baseline_missing" else "misconfigured")
    return {
        "decision_key": decision_key,
        "type": gap_type,
        "risk": gap["severity"],
        "resource_id": gap["resource_id"],
        "resource_name": gap["resource_name"],
        "resource_type": gap["resource_type"],
        "resource_group": gap["resource_group"],
        "subscription_id": gap["subscription_id"],
        "location": gap["location"],
        "alert_key": gap["alert_key"],
        "signal": gap["alert_name"],
        "amba_category": gap["amba_category"],
        "recommended": gap["recommended"],
        "explanation": gap["why"],
    }


def test_immutable_blueprints_assignments_and_manual_preview_classifications() -> None:
    blueprint, assignment = _blueprint_assignment()
    second = planner.create_blueprint_version(TENANT, {
        "name": "Compute baseline v2",
        "included_resource_types": ["microsoft.compute/virtualmachines"],
        "severity_overrides": {},
        "default_disabled": False,
    }, actor="bob", blueprint_id=blueprint["blueprint_id"])
    assert second["version"] == 2
    assert second["amba_version"] == "7"
    assert planner.get_blueprint(TENANT, blueprint["blueprint_id"], 1)["created_by"] == "alice"

    plan = planner.preview_plan(TENANT, assignment["id"], actor="alice", common_action_group_id=ACTION_GROUP_ID, coverage_items=[
        _gap(status="present"),
        {**_gap(status="equivalent"), "alert_key": "vm_cpu_equivalent"},
        {**_gap(status="misconfigured"), "alert_key": "vm_cpu_drift"},
        {**_gap(status="missing"), "alert_key": "vm_cpu_missing"},
        {**_gap(status="missing", threshold=None), "alert_key": "vm_cpu_blocked"},
    ])
    assert plan["counts"] == {"covered": 1, "equivalent": 1, "drifted": 1, "missing": 1, "blocked": 1}
    actionable = [item for item in plan["items"] if item["actionable"]]
    assert len(actionable) == 2
    assert all(item["proposal"]["desired"]["enabled"] is True for item in actionable)
    assert all(item["proposal"]["desired"]["action_group_ids"] == [ACTION_GROUP_ID] for item in actionable)
    assert all(item["action_group"]["id"] == ACTION_GROUP_ID for item in actionable)
    assert plan["common_action_group_id"] == ACTION_GROUP_ID
    assert planner.validate_plan(TENANT, plan["id"], actor="alice")["valid"] is True

    planner.update_plan_items(TENANT, plan["id"], [{"item_id": actionable[0]["id"], "included": False}], actor="alice")
    updated = planner.get_plan(TENANT, plan["id"])
    assert sum(1 for item in updated["items"] if item["included"]) == 1


@pytest.mark.asyncio
async def test_submit_and_plan_decision_create_only_approval_ledger_rows(tmp_path: Path) -> None:
    _blueprint, assignment = _blueprint_assignment()
    plan = planner.preview_plan(TENANT, assignment["id"], actor="requester", common_action_group_id=ACTION_GROUP_ID, coverage_items=[_gap()])

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'planner.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    requester = Principal("requester", "requester@example.test", TENANT, "operator", frozenset({"alerts_manager.rule_write"}))
    approver = Principal("approver", "approver@example.test", TENANT, "admin", frozenset({"alerts_manager.approve"}))

    try:
        async with Session() as db:
            submitted = await api.submit_deployment_plan(plan["id"], requester, db)
            assert submitted["plan"]["status"] == "pending"
            assert len(submitted["changes"]) == 1
            change = (await db.execute(select(AlertManagerChange))).scalar_one()
            assert change.status == "pending"
            assert change.connection_id == "connection-1"
            assert change.auto_apply is False
            assert change.target_type == "metric_rule"
            assert change.summary_json["batch_id"] == submitted["batch_id"]
            encrypted = change.desired_encrypted
            assert encrypted.startswith("enc:v1:")
            desired = service.decrypted_json(encrypted)["payload"]
            assert desired["enabled"] is True

        async with Session() as db:
            decided = await api.decide_deployment_plan(
                plan["id"], api.ChangeDecisionRequest(decision="approved", reason="Reviewed as one plan."), approver, db,
            )
            assert decided["plan"]["status"] == "approved"
            change = (await db.execute(select(AlertManagerChange))).scalar_one()
            assert change.status == "approved"
            assert change.applied_at is None
            assert change.after_encrypted == ""

        async with Session() as db:
            cancelled = await api.decide_deployment_plan(
                plan["id"], api.ChangeDecisionRequest(decision="rejected", reason="Superseded before application."), approver, db,
            )
            assert cancelled["plan"]["status"] == "rejected"
            change = (await db.execute(select(AlertManagerChange))).scalar_one()
            assert change.status == "rejected"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_plan_decision_rejects_child_from_different_connection(tmp_path: Path) -> None:
    _blueprint, assignment = _blueprint_assignment()
    plan = planner.preview_plan(TENANT, assignment["id"], actor="requester", common_action_group_id=ACTION_GROUP_ID, coverage_items=[_gap()])
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'planner-connection.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    requester = Principal("requester", "requester@example.test", TENANT, "operator", frozenset({"alerts_manager.rule_write"}))
    approver = Principal("approver", "approver@example.test", TENANT, "admin", frozenset({"alerts_manager.approve"}))
    try:
        async with Session() as db:
            submitted = await api.submit_deployment_plan(plan["id"], requester, db)
            change = await db.get(AlertManagerChange, submitted["changes"][0]["id"])
            assert change is not None
            change.connection_id = "different-connection"
            await db.commit()
        async with Session() as db:
            with pytest.raises(HTTPException) as error:
                await api.decide_deployment_plan(
                    plan["id"], api.ChangeDecisionRequest(decision="approved", reason="reviewed"), approver, db,
                )
            assert error.value.status_code == 409
            assert "Azure connection" in str(error.value.detail)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_selected_gap_plan_is_server_built_approval_gated_and_status_is_ledger_aware(tmp_path: Path) -> None:
    context = {"connection_id": "connection-1", "subscription_id": "sub-1", "environment": "production"}
    selected = _analysis_gap()
    assert planner.gap_identity({key: value for key, value in selected.items() if key != "decision_key"}) == planner.gap_identity(
        {key: value for key, value in reversed(list(selected.items())) if key != "decision_key"}
    )

    plan = planner.preview_gap_plan(
        TENANT, context, [selected, _analysis_gap(decision_key="unsupported:vm-one", gap_type="disabled_rule")],
        actor="requester", common_action_group_id=ACTION_GROUP_ID,
        live_action_groups=[{"id": ACTION_GROUP_ID, "name": "Platform on-call", "enabled": True, "active_receiver_count": 1}],
    )
    assert plan["source_gap_ids"] == [selected["decision_key"], "unsupported:vm-one"]
    assert plan["counts"] == {"covered": 0, "equivalent": 0, "drifted": 0, "missing": 1, "blocked": 1}
    actionable = next(item for item in plan["items"] if item["actionable"])
    blocked = next(item for item in plan["items"] if not item["actionable"])
    assert actionable["source_gap_id"] == selected["decision_key"]
    assert "routing_mode" not in plan
    assert "routing" not in actionable
    assert actionable["action_group"]["id"] == ACTION_GROUP_ID
    assert actionable["proposal"]["desired"]["enabled"] is True
    assert actionable["proposal"]["desired"]["action_group_ids"] == [ACTION_GROUP_ID]
    assert "unsupported" in blocked["reasons"][0]

    unusable = planner.preview_gap_plan(
        "other-tenant", context, [selected], actor="requester",
        common_action_group_id=ACTION_GROUP_ID,
        live_action_groups=[{"id": ACTION_GROUP_ID, "name": "Disabled", "enabled": False, "active_receiver_count": 1}],
    )
    assert unusable["counts"]["blocked"] == 1
    assert "disabled" in unusable["items"][0]["reasons"][0].lower()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'selected-gaps.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    requester = Principal("requester", "requester@example.test", TENANT, "operator", frozenset({"alerts_manager.rule_write"}))
    try:
        async with Session() as db:
            submitted = await api.submit_deployment_plan(plan["id"], requester, db)
            assert submitted["plan"]["status"] == "pending"
            assert len(submitted["changes"]) == 1
            change = (await db.execute(select(AlertManagerChange))).scalar_one()
            assert change.summary_json["source_gap_id"] == selected["decision_key"]
            assert change.auto_apply is False
            change.status = "applied"
            change.applied_at = service.now()
            await db.commit()

        async with Session() as db:
            status = await api.deployment_plans_by_gap([selected["decision_key"], "not-planned"], requester, db)
            assert status["by_gap"][selected["decision_key"]]["status"] == "applied"
            assert status["by_gap"]["not-planned"]["status"] == "none"

            change = (await db.execute(select(AlertManagerChange))).scalar_one()
            change.status = "failed"
            change.error = "ARM rejected the original proposal"
            await db.commit()

        async with Session() as db:
            status = await api.deployment_plans_by_gap([selected["decision_key"]], requester, db)
            assert status["by_gap"][selected["decision_key"]]["status"] == "failed"

        retry = planner.preview_gap_plan(
            TENANT, context, [selected], actor="requester",
            common_action_group_id=ACTION_GROUP_ID,
            live_action_groups=[{"id": ACTION_GROUP_ID, "name": "Platform on-call", "enabled": True, "active_receiver_count": 1}],
            active_gap_ids=set(), pending_target_ids=set(),
        )
        assert retry["counts"]["missing"] == 1
        assert retry["items"][0]["actionable"] is True
    finally:
        await engine.dispose()


def test_planner_routes_are_registered() -> None:
    paths = {route.path for route in api.router.routes}
    assert not any("action-group-catalog" in path for path in paths)
    assert not any("routing-rules" in path for path in paths)
    assert "/alerts-manager/amba-blueprints/{blueprint_id}/versions" in paths
    assert "/alerts-manager/amba-blueprint-assignments" in paths
    assert "/alerts-manager/deployment-plans/preview" in paths
    assert "/alerts-manager/deployment-plans/from-gaps/preview" in paths
    assert "/alerts-manager/deployment-plans/by-gap" in paths
    assert "/alerts-manager/deployment-plans/{plan_id}/items" in paths
    assert "/alerts-manager/deployment-plans/{plan_id}/validate" in paths
    assert "/alerts-manager/deployment-plans/{plan_id}/submit" in paths
    assert "/alerts-manager/deployment-plans/{plan_id}/decision" in paths


def test_gap_preview_contract_only_accepts_a_direct_action_group() -> None:
    fields = api.GapsDeploymentPlanPreviewRequest.model_fields
    assert "common_action_group_id" in fields
    assert fields["common_action_group_id"].is_required()
    assert "routing_mode" not in fields
    with pytest.raises(ValueError):
        api.GapsDeploymentPlanPreviewRequest(
            subscription_id="sub-1", gaps=[_analysis_gap()],
            common_action_group_id=ACTION_GROUP_ID, routing_mode="rules",
        )


@pytest.mark.asyncio
async def test_selected_gap_metric_catalog_corrects_aggregation_and_blocks_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def definitions(_connection: dict, resource_id: str) -> list[dict]:
        if "configurationstores" in resource_id.lower():
            return [{
                "name": "RequestQuotaUsage", "primary_aggregation": "Maximum",
                "supported_aggregations": ["Minimum", "Maximum"], "dimensions": [],
            }]
        return [{
            "name": "TotalRequests", "primary_aggregation": "Count",
            "supported_aggregations": ["Count"], "dimensions": [{"name": "StatusCode"}],
        }]

    monkeypatch.setattr(rules, "metric_definitions", definitions)
    gaps = [
        {
            **_analysis_gap(decision_key="appcfg"),
            "resource_id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.AppConfiguration/configurationStores/appcfg",
            "resource_type": "microsoft.appconfiguration/configurationstores",
            "recommended": {"metric": "RequestQuotaUsage", "threshold": 80, "aggregation": "Average"},
        },
        {
            **_analysis_gap(decision_key="cosmos"),
            "resource_id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.DocumentDB/databaseAccounts/cosmos",
            "resource_type": "microsoft.documentdb/databaseaccounts",
            "recommended": {
                "metric": "TotalRequests", "threshold": 0, "aggregation": "Average",
                "dimensions": [{"name": "StatusCode", "operator": "Include", "values": ["429"]}],
            },
        },
        {
            **_analysis_gap(decision_key="disk"),
            "resource_id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/disks/orphan",
            "resource_type": "microsoft.compute/disks",
            "recommended": {"metric": "Disk IOPS saturation", "threshold": 80, "aggregation": "Average"},
        },
    ]
    validated = await api._validate_selected_gap_metrics({}, gaps)
    assert validated[0]["recommended"]["aggregation"] == "Maximum"
    assert validated[0]["recommended"]["metric_validation_errors"] == []
    assert validated[1]["recommended"]["aggregation"] == "Count"
    assert validated[1]["recommended"]["metric_validation_errors"] == []
    assert "does not expose metric" in validated[2]["recommended"]["metric_validation_errors"][0]

    proposal, errors = planner._proposal(
        {"monitoring_resource_group": "rg"},
        {"blueprint_id": "selected-gaps", "amba_version": "live", "severity_overrides": {}},
        planner._normalized_selected_gap(validated[2]), [ACTION_GROUP_ID],
    )
    assert proposal is None
    assert any("does not expose metric" in error for error in errors)

"""Essential Activity Log coverage and approval-gated setup wizard contracts."""
from __future__ import annotations

from pathlib import Path
import csv
import io
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alerts_manager import activity_coverage, activity_export, activity_planner, rules, service
from app.api import alerts_manager as api
from app.core.db import Base
from app.core.security import Principal
from app.models import AlertManagerChange, AuditLog

TENANT = "tenant-activity"
CONNECTION = {"id": "connection-activity", "read_only": False}
AG_ID = "/subscriptions/sub-1/resourceGroups/rg-monitor/providers/Microsoft.Insights/actionGroups/on-call"


def _group(*, enabled: bool = True, active: int = 1) -> dict:
    return {
        "id": AG_ID, "name": "On call", "subscription_id": "sub-1",
        "enabled": enabled, "receiver_count": 1, "active_receiver_count": active,
    }


def _other_subscription_group() -> dict:
    return {
        "id": AG_ID.replace("sub-1", "sub-2"), "name": "Other subscription",
        "subscription_id": "sub-2", "enabled": True,
        "receiver_count": 3, "active_receiver_count": 3,
    }


def _rule(category: str, *, enabled: bool = True, action_group_ids: list[str] | None = None) -> dict:
    return {
        "id": f"/subscriptions/sub-1/resourceGroups/rg-monitor/providers/Microsoft.Insights/activityLogAlerts/{category}",
        "name": category, "family": "activity", "category": category,
        "subscription_id": "sub-1", "enabled": enabled,
        "scopes": ["/subscriptions/sub-1"],
        "action_group_ids": [AG_ID] if action_group_ids is None else action_group_ids,
    }


def _request(**updates) -> dict:
    value = {
        "connection_id": CONNECTION["id"], "subscription_id": "sub-1",
        "subscription_ids": [], "categories": list(activity_coverage.ESSENTIAL_CATEGORIES),
        "resource_group": "rg-monitor", "routing_mode": "common",
        "common_action_group_id": AG_ID, "action_group_ids_by_category": {},
        "name_prefix": "essential-activity",
    }
    value.update(updates)
    return value


def _principal(tenant: str = TENANT) -> Principal:
    return Principal("requester", "requester@example.test", tenant, "operator", frozenset({"alerts_manager.rule_write"}))


@pytest.fixture()
async def database(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activity.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield Session
    finally:
        await engine.dispose()


def test_coverage_evaluates_all_statuses_and_partial_metadata() -> None:
    rules_inventory = [
        _rule("ServiceHealth"),
        _rule("ResourceHealth", enabled=False),
        _rule("Security", action_group_ids=[]),
    ]
    result = activity_coverage.evaluate_coverage({"sub-1"}, rules_inventory, [_group()])
    statuses = {item["category"]: item["status"] for item in result["scopes"][0]["categories"]}
    assert statuses == {
        "ServiceHealth": "covered", "ResourceHealth": "disabled",
        "Security": "no_routing", "Recommendation": "missing",
    }
    assert result["counts"]["covered"] == 1
    assert "do not send Activity Log records" in result["security_guidance"]

    partial = activity_coverage.evaluate_coverage(
        {"sub-1"}, [], [], metadata={"partial": True, "source_limit": 10},
    )
    assert {item["status"] for item in partial["scopes"][0]["categories"]} == {"unknown"}
    assert partial["partial"] is True
    assert partial["metadata"]["source_limit"] == 10


def test_coverage_reports_partial_duplicates_pending_changes_and_display_name() -> None:
    enabled = _rule("ServiceHealth")
    enabled["activity_conditions"] = [{"field": "category", "equals": "ServiceHealth"}]
    disabled = _rule("ServiceHealth", enabled=False)
    disabled["activity_conditions"] = list(enabled["activity_conditions"])
    result = activity_coverage.evaluate_coverage(
        {"sub-1"}, [enabled, disabled], [_group()],
        blockers=[{"change_id": "pending-1", "status": "pending", "subscription_id": "sub-1", "category": "ServiceHealth"}],
        subscription_names={"sub-1": "Production"},
    )
    scope = result["scopes"][0]
    cell = scope["categories"][0]
    assert scope["subscription_display_name"] == "Production"
    assert cell["status"] == "partial"
    assert cell["blocked"] is True
    assert {issue["type"] for issue in cell["issues"]} == {"duplicate", "pending_change"}
    assert result["pending_change_count"] == 1


def test_coverage_classifies_incomplete_conditions_as_partial() -> None:
    rule = _rule("ServiceHealth")
    rule["activity_conditions"] = [
        {"field": "category", "equals": "ServiceHealth"},
        {"field": "properties.incidentType", "containsAny": ["Incident"]},
    ]
    result = activity_coverage.evaluate_coverage({"sub-1"}, [rule], [_group()])
    cell = result["scopes"][0]["categories"][0]
    assert cell["status"] == "partial"
    assert cell["condition_complete"] is False
    assert cell["partial_healthy_rule_count"] == 1
    assert cell["rules"][0]["condition_completeness"]["missing_values"]["properties.incidentType"] == [
        "actionrequired", "maintenance", "security",
    ]
    assert any(issue["type"] == "condition_partial" for issue in cell["issues"])


def test_coverage_projects_pending_deletion_and_reports_duplicate_pairs() -> None:
    first = _rule("Security")
    first["activity_conditions"] = [{"field": "category", "equals": "Security"}]
    second = {**first, "id": first["id"] + "-copy", "name": "Security copy"}
    result = activity_coverage.evaluate_coverage(
        {"sub-1"}, [first, second], [_group()],
        blockers=[{
            "change_id": "delete-1", "status": "approved", "operation": "delete",
            "subscription_id": "sub-1", "category": "Security",
        }],
    )
    cell = next(item for item in result["scopes"][0]["categories"] if item["category"] == "Security")
    assert cell["pending_effect"] == "deletion"
    assert cell["projected_status"] == "covered"
    assert cell["overlap_details"][0]["type"] == "exact_duplicate"
    duplicate = next(issue for issue in cell["issues"] if issue["type"] == "duplicate")
    assert duplicate["overlaps"][0]["same_conditions"] is True
    pending = next(issue for issue in cell["issues"] if issue["type"] == "pending_change")
    assert pending["pending_effect"] == "deletion"


def test_activity_coverage_exports_are_complete_and_sanitize_secrets() -> None:
    rule = _rule("Security")
    rule["name"] = "=FORMULA"
    rule["activity_conditions"] = [{"field": "category", "equals": "Security"}]
    group = _group()
    group["serviceUri"] = "https://hooks.example.test/path?sig=super-secret"
    coverage = activity_coverage.evaluate_coverage({"sub-1"}, [rule], [group])
    coverage["scopes"][0]["categories"][2]["issues"].append({
        "type": "test", "token": "raw-secret", "url": "https://example.test/callback?code=secret",
    })
    payload = {"connection_id": "c1", "scope": {"kind": "subscription", "id": "sub-1"}, "coverage": coverage}

    document = json.loads(activity_export.to_json(payload))
    security = next(row for row in document["rows"] if row["category"] == "Security")
    assert set(security) == {
        "category", "status", "covered_subscriptions", "missing_subscriptions",
        "partial_subscriptions", "routing", "existing_rules", "issues", "recommended_action",
    }
    serialized = json.dumps(document)
    assert "raw-secret" not in serialized
    assert "code=secret" not in serialized
    assert "<redacted>" in serialized

    csv_rows = list(csv.DictReader(io.StringIO(activity_export.to_csv(payload))))
    csv_security = next(row for row in csv_rows if row["category"] == "Security")
    assert csv_security["existing_rules"].find("'=FORMULA") >= 0
    workbook = activity_export.to_workbook(payload)
    assert workbook.startswith(b"PK")
    assert b"raw-secret" not in workbook


def test_plan_defaults_are_deterministic_valid_and_support_per_category_routing() -> None:
    plan1 = activity_planner.preview_plan(
        _request(), subscription_ids={"sub-1"}, rules_inventory=[], action_groups=[_group()],
    )
    plan2 = activity_planner.preview_plan(
        _request(), subscription_ids={"sub-1"}, rules_inventory=[], action_groups=[_group()],
    )
    assert plan1["plan_token"] == plan2["plan_token"]
    assert [item["target_id"] for item in plan1["items"]] == [item["target_id"] for item in plan2["items"]]
    assert plan1["counts"] == {"create": 4, "update": 0, "enable": 0, "equivalent": 0, "blocked": 0, "invalid": 0, "total": 4, "actionable": 4}
    assert all(rules.validate_activity_payload(item["desired"], create=True) == [] for item in plan1["items"])
    assert all(item["desired"]["enabled"] is True for item in plan1["items"])
    assert next(item for item in plan1["items"] if item["category"] == "ResourceHealth")["desired"]["activity_conditions"][1]["field"] == "properties.currentHealthStatus"

    per_category = activity_planner.preview_plan(
        _request(
            categories=["Security"], routing_mode="per_category", common_action_group_id="",
            action_group_ids_by_category={"Security": [AG_ID]},
        ),
        subscription_ids={"sub-1"}, rules_inventory=[], action_groups=[_group()],
    )
    assert per_category["items"][0]["desired"]["action_group_ids"] == [AG_ID]


def test_plan_exposes_reusable_ownership_noise_and_detailed_overlaps() -> None:
    first = _rule("Security")
    first["activity_conditions"] = [{"field": "category", "equals": "Security"}]
    second = {**first, "id": first["id"] + "-copy", "name": "Security copy"}
    group = _group()
    group["tags"] = {"owner": "SecOps"}
    plan = activity_planner.preview_plan(
        _request(categories=["Security"]), subscription_ids={"sub-1"},
        rules_inventory=[first, second], action_groups=[group],
    )
    item = plan["items"][0]
    assert item["ownership"] == {"source": "action_group_tags", "owners": ["SecOps"], "ai_used": False}
    assert item["noise"]["exact_duplicate_count"] == 1
    assert item["issues"][0]["overlaps"][0]["type"] == "exact_duplicate"


def test_custom_conditions_are_allowlisted_and_category_is_mandatory() -> None:
    custom = [
        {"field": "category", "equals": "ServiceHealth"},
        {"field": "properties.service", "containsAny": ["Compute", "Storage"]},
    ]
    plan = activity_planner.preview_plan(
        _request(categories=["ServiceHealth"], conditions_by_category={"ServiceHealth": custom}),
        subscription_ids={"sub-1"}, rules_inventory=[], action_groups=[_group()],
    )
    assert plan["items"][0]["desired"]["activity_conditions"] == custom
    assert plan["items"][0]["validation_status"] == "valid"
    assert plan["items"][0]["cost"]["classification"] == "free"
    assert plan["items"][0]["receiver_count"] == 1

    for invalid in [
        [{"field": "properties.service", "equals": "Compute"}],
        [{"field": "category", "equals": "Security"}],
        [{"field": "properties.secret", "equals": "nope"}, {"field": "category", "equals": "ServiceHealth"}],
        [{"field": "category", "equals": "ServiceHealth", "containsAny": ["ServiceHealth"]}],
    ]:
        with pytest.raises(ValueError):
            activity_planner.preview_plan(
                _request(categories=["ServiceHealth"], conditions_by_category={"ServiceHealth": invalid}),
                subscription_ids={"sub-1"}, rules_inventory=[], action_groups=[_group()],
            )


def test_cross_subscription_action_group_is_rejected() -> None:
    other = _other_subscription_group()
    plan = activity_planner.preview_plan(
        _request(categories=["Security"], common_action_group_id=other["id"]),
        subscription_ids={"sub-1"}, rules_inventory=[], action_groups=[other],
    )
    assert plan["items"][0]["classification"] == "invalid"
    assert "must be in subscription sub-1" in plan["items"][0]["errors"][0]


def test_preview_detects_equivalent_disabled_routing_and_approval_conflicts() -> None:
    equivalent = activity_planner.preview_plan(
        _request(categories=["ServiceHealth"]), subscription_ids={"sub-1"},
        rules_inventory=[_rule("ServiceHealth")], action_groups=[_group()],
    )
    assert equivalent["items"][0]["classification"] == "equivalent"
    disabled = activity_planner.preview_plan(
        _request(categories=["ResourceHealth"]), subscription_ids={"sub-1"},
        rules_inventory=[_rule("ResourceHealth", enabled=False)], action_groups=[_group()],
    )
    assert disabled["items"][0]["classification"] == "enable"
    assert disabled["items"][0]["operation"] == "update"
    no_receiver = activity_planner.preview_plan(
        _request(categories=["Security"]), subscription_ids={"sub-1"},
        rules_inventory=[], action_groups=[_group(active=0)],
    )
    assert no_receiver["items"][0]["classification"] == "invalid"
    target = activity_planner.target_id(activity_planner.build_desired(
        subscription_id="sub-1", category="Recommendation", resource_group="rg-monitor",
        action_group_ids=[AG_ID],
    ))
    blocked = activity_planner.preview_plan(
        _request(categories=["Recommendation"]), subscription_ids={"sub-1"},
        rules_inventory=[], action_groups=[_group()],
        blockers={target.lower(): {"change_id": "change-1", "status": "approved", "target_id": target}},
    )
    assert blocked["items"][0]["classification"] == "blocked"
    assert blocked["items"][0]["blocker"]["status"] == "approved"


@pytest.mark.asyncio
async def test_validation_rebuilds_plan_and_rejects_changed_token(database, monkeypatch: pytest.MonkeyPatch) -> None:
    async def inventory(*_args, **_kwargs):
        return CONNECTION, {"sub-1"}, [], [_group()], {"partial": False}

    monkeypatch.setattr(api, "_activity_scope_inventory", inventory)
    async def existing_resource_group(*_args, **_kwargs):
        return {"id": "/subscriptions/sub-1/resourceGroups/rg-monitor"}, 200, ""
    monkeypatch.setattr(service, "get_arm_resource", existing_resource_group)
    request = api.ActivityLogPlanRequest(**_request(categories=["Security"]))
    async with database() as db:
        preview = await api.preview_activity_log_plan(request, _principal(), db)
        valid = await api.validate_activity_log_plan(
            api.ActivityLogPlanValidationRequest(**_request(categories=["Security"]), plan_token=preview["plan"]["plan_token"]),
            _principal(), db,
        )
        assert valid["valid"] is True
        stale = await api.validate_activity_log_plan(
            api.ActivityLogPlanValidationRequest(**_request(categories=["Security"]), plan_token="0" * 64),
            _principal(), db,
        )
        assert stale["valid"] is False
        assert "changed after preview" in stale["errors"][0]


@pytest.mark.asyncio
async def test_submit_creates_ordered_pending_rows_and_audits_without_azure_writes(database, monkeypatch: pytest.MonkeyPatch) -> None:
    async def inventory(*_args, **_kwargs):
        return CONNECTION, {"sub-1"}, [], [_group()], {"partial": False}

    monkeypatch.setattr(api, "_activity_scope_inventory", inventory)
    async def existing_resource_group(*_args, **_kwargs):
        return {"id": "/subscriptions/sub-1/resourceGroups/rg-monitor"}, 200, ""
    monkeypatch.setattr(service, "get_arm_resource", existing_resource_group)
    arm_calls = 0

    async def forbidden_arm(*_args, **_kwargs):
        nonlocal arm_calls
        arm_calls += 1
        raise AssertionError("submit must not write to Azure")

    monkeypatch.setattr("app.azure.arm.arm_write", forbidden_arm)
    principal = _principal()
    preview_request = api.ActivityLogPlanRequest(**_request(categories=["ServiceHealth", "Security"]))
    async with database() as db:
        preview = await api.preview_activity_log_plan(preview_request, principal, db)
        submitted = await api.submit_activity_log_plan(
            api.ActivityLogPlanSubmitRequest(
                **_request(categories=["ServiceHealth", "Security"]),
                plan_token=preview["plan"]["plan_token"], reason="Establish essential platform-event routing.",
            ),
            principal, db,
        )
        assert submitted["status"] == "pending"
        assert submitted["azure_writes_performed"] is False
        assert arm_calls == 0
        changes = (await db.execute(select(AlertManagerChange).order_by(AlertManagerChange.target_id))).scalars().all()
        assert len(changes) == 2
        assert {change.status for change in changes} == {"pending"}
        assert {change.auto_apply for change in changes} == {False}
        assert {change.summary_json["batch_id"] for change in changes} == {submitted["batch_id"]}
        assert sorted(change.summary_json["batch_order"] for change in changes) == [1, 2]
        assert all(change.desired_encrypted.startswith("enc:v1:") for change in changes)
        assert all(service.decrypted_json(change.desired_encrypted)["payload"]["enabled"] for change in changes)
        assert all(change.summary_json["evidence_summary"]["approval_required"] is True for change in changes)
        assert all(change.summary_json["cost"]["classification"] == "free" for change in changes)
        audits = (await db.execute(select(AuditLog))).scalars().all()
        assert len(audits) == 3
        assert all(audit.metadata_json.get("evidence_summary") for audit in audits)


@pytest.mark.asyncio
async def test_submit_update_fetches_live_state_and_preserves_before_evidence(database, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _rule("Security")
    existing["action_group_ids"] = []

    async def inventory(*_args, **_kwargs):
        return CONNECTION, {"sub-1"}, [existing], [_group()], {"partial": False}

    live = {
        "id": existing["id"], "name": existing["name"], "location": "Global",
        "properties": {
            "enabled": True, "description": "old", "scopes": ["/subscriptions/sub-1"],
            "condition": {"allOf": [{"field": "category", "equals": "Security"}]},
            "actions": {"actionGroups": []},
        },
    }
    calls = 0

    async def get_rule(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return live, 200, ""

    monkeypatch.setattr(api, "_activity_scope_inventory", inventory)
    monkeypatch.setattr(rules, "get_rule", get_rule)
    request_data = _request(categories=["Security"])
    principal = _principal()
    async with database() as db:
        preview = await api.preview_activity_log_plan(api.ActivityLogPlanRequest(**request_data), principal, db)
        assert preview["plan"]["items"][0]["classification"] == "update"
        result = await api.submit_activity_log_plan(
            api.ActivityLogPlanSubmitRequest(
                **request_data, plan_token=preview["plan"]["plan_token"], reason="Repair routing",
            ),
            principal, db,
        )
        assert result["azure_writes_performed"] is False
        assert calls == 1
        change = (await db.execute(select(AlertManagerChange))).scalar_one()
        assert change.operation == "update"
        assert change.expected_state_hash
        assert service.decrypted_json(change.before_encrypted)["properties"]["description"] == "old"


@pytest.mark.asyncio
async def test_blockers_are_tenant_and_connection_isolated(database) -> None:
    async with database() as db:
        for tenant, connection, status in [
            (TENANT, CONNECTION["id"], "pending"),
            ("other-tenant", CONNECTION["id"], "approved"),
            (TENANT, "other-connection", "pending"),
            (TENANT, CONNECTION["id"], "applied"),
        ]:
            db.add(AlertManagerChange(
                tenant_id=tenant, connection_id=connection, target_type="activity_rule",
                target_id=f"/subscriptions/sub-1/rules/{tenant}-{connection}-{status}", operation="create",
                status=status, risk="medium", summary_json={}, desired_encrypted="",
                before_encrypted="", after_encrypted="", expected_state_hash="",
                requested_by="requester", auto_apply=False,
            ))
        await db.commit()
        blockers = await api._activity_blockers(db, TENANT, CONNECTION["id"])
        assert len(blockers) == 1
        assert next(iter(blockers.values()))["status"] == "pending"


@pytest.mark.asyncio
async def test_pending_delete_blocker_preserves_operation_and_category(database) -> None:
    async with database() as db:
        db.add(AlertManagerChange(
            tenant_id=TENANT, connection_id=CONNECTION["id"], target_type="activity_rule",
            target_id=_rule("Security")["id"], operation="delete", status="approved", risk="critical",
            summary_json={"category": "Security", "subscription_id": "sub-1"}, desired_encrypted="",
            before_encrypted="", after_encrypted="", expected_state_hash="", requested_by="approver",
            auto_apply=False,
        ))
        await db.commit()
        blocker = next(iter((await api._activity_blockers(db, TENANT, CONNECTION["id"])).values()))
        assert blocker["operation"] == "delete"
        assert blocker["category"] == "Security"


def test_endpoint_contracts_are_registered() -> None:
    paths = {route.path for route in api.router.routes}
    assert "/alerts-manager/activity-log-coverage" in paths
    assert "/alerts-manager/activity-log-coverage/export" in paths
    assert "/alerts-manager/activity-log-plan/preview" in paths
    assert "/alerts-manager/activity-log-plan/validate" in paths
    assert "/alerts-manager/activity-log-plan/submit" in paths

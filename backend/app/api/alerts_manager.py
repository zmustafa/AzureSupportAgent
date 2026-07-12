"""Unified fired-alert inbox and approval-gated Action Groups management API."""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts_manager import service
from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AlertManagerChange, AuditLog

router = APIRouter(prefix="/alerts-manager", tags=["alerts-manager"])
_read = require_permission("alerts_manager.read")
_alert_state = require_permission("alerts_manager.alert_state_write")
_ag_write = require_permission("alerts_manager.action_group_write")
_rule_write = require_permission("alerts_manager.rule_write")
_advanced_write = require_permission("alerts_manager.advanced_rule_write")
_bulk_write = require_permission("alerts_manager.bulk_write")
_amba_blueprint_write = require_permission("alerts_manager.amba_blueprint_write")
_query_preview = require_permission("alerts_manager.query_preview")
_approve = require_permission("alerts_manager.approve")
_delete = require_permission("alerts_manager.delete")
_test = require_permission("alerts_manager.test_notifications")


_ALERT_INSTANCE_ID_PATTERN = r"^/subscriptions/[^/]+/providers/Microsoft\.AlertsManagement/alerts/[^/]+$"


class AlertStateRequest(BaseModel):
    connection_id: str = ""
    alert_id: str = Field(min_length=1, max_length=1000, pattern=_ALERT_INSTANCE_ID_PATTERN)
    state: Literal["New", "Acknowledged", "Closed"]


class AlertHistoryRequest(BaseModel):
    connection_id: str = ""
    alert_id: str = Field(min_length=1, max_length=1000, pattern=_ALERT_INSTANCE_ID_PATTERN)


class ActionGroupChangeRequest(BaseModel):
    connection_id: str = ""
    operation: Literal["create", "update", "delete"]
    target_id: str = ""
    clone_source_id: str = ""
    desired: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=1000)


class ChangeDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=1000)


class ActionGroupTestRequest(BaseModel):
    connection_id: str = ""
    action_group_id: str = Field(min_length=1, max_length=1000)
    alert_type: Literal[
        "servicehealth", "metricstaticthreshold", "metricsdynamicthreshold", "logalertv2",
        "smartalert", "webtestalert", "resourcehealth", "activitylog",
    ] = "metricstaticthreshold"
    confirmation: str


class AlertRuleChangeRequest(BaseModel):
    connection_id: str = ""
    family: Literal["metric", "log", "activity", "smart", "prometheus"]
    operation: Literal["create", "update", "delete"]
    target_id: str = ""
    desired: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=1000)


class RuleValidationRequest(BaseModel):
    family: Literal["metric", "log", "activity", "smart", "prometheus"]
    desired: dict[str, Any] = Field(default_factory=dict)
    create: bool = True


class MetricPreviewRequest(BaseModel):
    connection_id: str = ""
    resource_id: str = Field(min_length=1, max_length=1000)
    metric_name: str = Field(min_length=1, max_length=256)
    aggregation: str = "Average"
    interval: str = "PT5M"


class LogPreviewRequest(BaseModel):
    connection_id: str = ""
    workspace_id: str = Field(min_length=1, max_length=1000)
    query: str = Field(min_length=1, max_length=8000)
    timespan: str = "PT1H"


class BulkRuleTarget(BaseModel):
    target_id: str = Field(min_length=1, max_length=1000)
    family: Literal["metric", "log", "activity", "smart", "prometheus"]


class BulkRuleChangeRequest(BaseModel):
    connection_id: str = ""
    action: Literal["enable", "disable", "delete", "add_action_group"]
    targets: list[BulkRuleTarget] = Field(min_length=1, max_length=50)
    action_group_id: str = ""
    reason: str = Field(min_length=1, max_length=1000)


class NotificationSimulationRequest(BaseModel):
    connection_id: str = ""
    rule_id: str = ""
    rule_name: str = ""
    family: Literal["metric", "log", "activity", "smart", "prometheus"] = "metric"
    resource_id: str = ""
    severity: int = Field(default=3, ge=0, le=4)
    timestamp: str = ""
    description: str = ""
    action_group_ids: list[str] = Field(default_factory=list, max_length=20)
    selected_action_group_ids: list[str] = Field(default_factory=list, max_length=20)
    use_selected_only: bool = False
    monitor_condition: Literal["Fired", "Resolved"] = "Fired"


class BulkNotificationSimulationRequest(BaseModel):
    connection_id: str = ""
    workload_id: str | None = None
    subscription_id: str | None = None
    management_group_id: str | None = None
    monitor_condition: Literal["Fired", "Resolved"] = "Fired"
    include_disabled: bool = True
    families: list[Literal["metric", "log", "activity", "smart", "prometheus"]] = Field(default_factory=list)
    severities: list[int] = Field(default_factory=list, max_length=5)


class NoiseGuardRequest(BaseModel):
    connection_id: str = ""
    workload_id: str | None = None
    family: Literal["metric", "log", "activity", "smart", "prometheus"]
    desired: dict[str, Any] = Field(default_factory=dict)
    threshold_tolerance_pct: float | None = Field(default=None, ge=0, le=100)


class AuthoringResolveRequest(BaseModel):
    connection_id: str = ""
    resource_ids: list[str] = Field(min_length=1, max_length=100)


class BlueprintVersionRequest(BaseModel):
    name: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=1000)
    amba_version: str = Field(default="", max_length=64)
    included_resource_types: list[str] = Field(min_length=1, max_length=200)
    severity_overrides: dict[str, int | str] = Field(default_factory=dict)
    default_disabled: bool = False


class BlueprintAssignmentRequest(BaseModel):
    blueprint_id: str = Field(min_length=1, max_length=64)
    blueprint_version: int = Field(ge=1)
    scope_kind: Literal["subscription", "workload", "workload_group"]
    scope_id: str = Field(min_length=1, max_length=1000)
    connection_id: str = Field(default="", max_length=128)
    environment: str = Field(default="", max_length=64)
    monitoring_resource_group: str = Field(default="", max_length=90)
    enabled: bool = True


class DeploymentPlanPreviewRequest(BaseModel):
    assignment_id: str = Field(min_length=1, max_length=64)
    common_action_group_id: str = Field(min_length=1, max_length=1000)
    coverage_items: list[dict[str, Any]] | None = Field(default=None, max_length=5000)


class SelectedGapRecommendation(BaseModel):
    signal: str = Field(default="", max_length=32)
    metric: str = Field(default="", max_length=256)
    operator: str = Field(default="", max_length=64)
    threshold: float | None = None
    aggregation: str = Field(default="", max_length=64)
    window: str = Field(default="", max_length=32)
    dimensions: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class SelectedGapPayload(BaseModel):
    decision_key: str = Field(default="", max_length=1000)
    type: str = Field(default="", max_length=64)
    risk: str = Field(default="warning", max_length=32)
    resource_id: str = Field(default="", max_length=1000)
    resource_name: str = Field(default="", max_length=256)
    resource_type: str = Field(default="", max_length=256)
    subscription_id: str = Field(default="", max_length=128)
    resource_group: str = Field(default="", max_length=256)
    location: str = Field(default="", max_length=128)
    alert_key: str = Field(default="", max_length=128)
    rule_id: str = Field(default="", max_length=1000)
    action_group_id: str = Field(default="", max_length=1000)
    signal: str = Field(default="", max_length=256)
    amba_category: str = Field(default="", max_length=128)
    status: str = Field(default="", max_length=32)
    recommended: SelectedGapRecommendation = Field(default_factory=SelectedGapRecommendation)
    explanation: str = Field(default="", max_length=2048)


class GapsDeploymentPlanPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(default="", max_length=128)
    workload_id: str | None = Field(default=None, max_length=1000)
    subscription_id: str | None = Field(default=None, max_length=1000)
    management_group_id: str | None = Field(default=None, max_length=1000)
    environment: str = Field(default="", max_length=64)
    monitoring_resource_group: str = Field(default="", max_length=90)
    gaps: list[SelectedGapPayload] = Field(min_length=1, max_length=5000)
    common_action_group_id: str = Field(min_length=1, max_length=1000)


class DeploymentPlanItemSelection(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    included: bool


class DeploymentPlanItemsRequest(BaseModel):
    items: list[DeploymentPlanItemSelection] = Field(min_length=1, max_length=5000)


class ActivityLogPlanRequest(BaseModel):
    connection_id: str = Field(default="", max_length=128)
    workload_id: str | None = Field(default=None, max_length=1000)
    subscription_id: str | None = Field(default=None, max_length=128)
    management_group_id: str | None = Field(default=None, max_length=1000)
    subscription_ids: list[str] = Field(default_factory=list, max_length=5000)
    categories: list[str] = Field(default_factory=list, max_length=5)
    resource_group: str = Field(min_length=1, max_length=90)
    routing_mode: Literal["common", "per_category"] = "common"
    common_action_group_id: str = Field(default="", max_length=1000)
    action_group_ids_by_category: dict[str, list[str]] = Field(default_factory=dict)
    conditions_by_category: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    name_prefix: str = Field(default="essential-activity", max_length=120)


class ActivityLogPlanValidationRequest(ActivityLogPlanRequest):
    plan_token: str = Field(min_length=64, max_length=64)


class ActivityLogPlanSubmitRequest(ActivityLogPlanValidationRequest):
    reason: str = Field(min_length=1, max_length=1000)


class ActivityLogDiagnosticDestination(BaseModel):
    kind: Literal["workspace", "event_hub", "storage"]
    resource_id: str = Field(min_length=1, max_length=1000)
    event_hub_name: str = Field(default="", max_length=256)


class ActivityLogDiagnosticPlanRequest(BaseModel):
    connection_id: str = Field(default="", max_length=128)
    workload_id: str | None = Field(default=None, max_length=1000)
    subscription_id: str | None = Field(default=None, max_length=128)
    management_group_id: str | None = Field(default=None, max_length=1000)
    subscription_ids: list[str] = Field(default_factory=list, max_length=5000)
    categories: list[str] = Field(default_factory=list, max_length=4)
    destination: ActivityLogDiagnosticDestination
    setting_name: str = Field(default="aznetagent-activity-log", min_length=1, max_length=260)


class ActivityLogDiagnosticValidationRequest(ActivityLogDiagnosticPlanRequest):
    plan_token: str = Field(min_length=64, max_length=64)


class ActivityLogDiagnosticSubmitRequest(ActivityLogDiagnosticValidationRequest):
    reason: str = Field(min_length=1, max_length=1000)


def _tenant(principal: Principal) -> str:
    return principal.tenant_id or "default"


def _connection(connection_id: str, workload_id: str | None = None) -> dict[str, Any]:
    try:
        return service.resolve_selected_connection(connection_id or None, workload_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _planner_action_groups(assignment: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(assignment.get("scope_kind") or "")
    scope_id = str(assignment.get("scope_id") or "")
    workload_id = str(assignment.get("workload_id") or "") or (scope_id if kind == "workload" else None)
    subscription_id = str(assignment.get("subscription_id") or "") or (scope_id.removeprefix("/subscriptions/") if kind == "subscription" else None)
    management_group_id = str(assignment.get("management_group_id") or "") or (scope_id if kind == "management_group" else None)
    return await service.list_action_groups(
        _connection(str(assignment.get("connection_id") or ""), workload_id),
        workload_id=workload_id, subscription_id=subscription_id, management_group_id=management_group_id,
    )


def _planner_connection_id(context: dict[str, Any]) -> str:
    """Resolve a plan's connection, including legacy workload plans that stored an empty ID."""
    stored_connection_id = str(context.get("connection_id") or "")
    if stored_connection_id:
        return stored_connection_id
    kind = str(context.get("scope_kind") or "")
    scope_id = str(context.get("scope_id") or "")
    workload_id = str(context.get("workload_id") or "") or (scope_id if kind == "workload" else None)
    connection = _connection("", workload_id)
    return str(connection.get("id") or "")


async def _validate_selected_gap_metrics(
    connection: dict[str, Any], gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fail closed and normalize metric proposals against Azure's live metric catalog."""
    from app.alerts_manager import rules

    representatives: dict[str, str] = {}
    for gap in gaps:
        resource_type = str(gap.get("resource_type") or "").lower()
        resource_id = str(gap.get("resource_id") or "")
        if resource_type and resource_id:
            representatives.setdefault(resource_type, resource_id)

    semaphore = asyncio.Semaphore(4)

    async def load(resource_type: str, resource_id: str) -> tuple[str, list[dict[str, Any]], str]:
        try:
            async with semaphore:
                return resource_type, await rules.metric_definitions(connection, resource_id), ""
        except ValueError as exc:
            return resource_type, [], str(exc)

    loaded = await asyncio.gather(*(load(kind, rid) for kind, rid in representatives.items()))
    catalogs = {kind: values for kind, values, _error in loaded}
    catalog_errors = {kind: error for kind, _values, error in loaded if error}
    normalized: list[dict[str, Any]] = []
    for raw in gaps:
        gap = dict(raw)
        recommended = dict(gap.get("recommended") or {})
        gap["recommended"] = recommended
        metric = str(recommended.get("metric") or "").strip()
        if not metric or not isinstance(recommended.get("threshold"), (int, float)):
            normalized.append(gap)
            continue
        resource_type = str(gap.get("resource_type") or "").lower()
        errors: list[str] = []
        if catalog_errors.get(resource_type):
            errors.append(f"Azure metric catalog validation failed: {catalog_errors[resource_type]}")
        else:
            definitions = catalogs.get(resource_type) or []
            definition = next((item for item in definitions if str(item.get("name") or "").lower() == metric.lower()), None)
            if not definition:
                errors.append(f"Azure Monitor does not expose metric '{metric}' for {resource_type or 'this resource type'}.")
            else:
                supported = [str(value) for value in definition.get("supported_aggregations") or [] if str(value)]
                primary = str(definition.get("primary_aggregation") or "")
                requested = str(recommended.get("aggregation") or "")
                supported_by_lower = {value.lower(): value for value in supported}
                if requested.lower() in supported_by_lower:
                    recommended["aggregation"] = supported_by_lower[requested.lower()]
                elif primary and (not supported or primary.lower() in supported_by_lower):
                    recommended["aggregation"] = supported_by_lower.get(primary.lower(), primary)
                elif supported:
                    recommended["aggregation"] = supported[0]
                else:
                    errors.append(f"Azure Monitor returned no supported aggregation for metric '{metric}'.")
                available_dimensions = {
                    str(item.get("name") or "").lower()
                    for item in definition.get("dimensions") or [] if item.get("name")
                }
                requested_dimensions = {
                    str(item.get("name") or "").lower()
                    for item in recommended.get("dimensions") or [] if isinstance(item, dict) and item.get("name")
                }
                missing_dimensions = sorted(requested_dimensions - available_dimensions)
                if missing_dimensions:
                    errors.append(f"Azure Monitor metric '{metric}' does not expose dimension(s): {', '.join(missing_dimensions)}.")
        recommended["metric_validation_errors"] = errors
        normalized.append(gap)
    return normalized


def _change_dict(change: AlertManagerChange) -> dict[str, Any]:
    summary = dict(change.summary_json or {})
    clone_source_id = str(summary.get("clone_source_id") or "")
    if change.target_type == "action_group" and change.operation == "create" and not clone_source_id:
        encrypted = service.decrypted_json(change.desired_encrypted)
        payload = encrypted.get("payload") if isinstance(encrypted.get("payload"), dict) else {}
        clone_source_id = str(payload.get("clone_source_id") or "")
        if clone_source_id:
            summary["clone_source_id"] = clone_source_id
            summary["clone_source_name"] = service._name_from_id(clone_source_id)
    return {
        "id": change.id,
        "connection_id": change.connection_id,
        "target_type": change.target_type,
        "target_id": change.target_id,
        "target_name": change.target_id.rstrip("/").rsplit("/", 1)[-1],
        "operation": change.operation,
        "status": change.status,
        "risk": change.risk,
        "summary": summary,
        "can_retry": change.status == "failed" and bool(clone_source_id),
        "requested_by": change.requested_by,
        "requested_at": change.requested_at.isoformat() if change.requested_at else "",
        "decided_by": change.decided_by or "",
        "decided_at": change.decided_at.isoformat() if change.decided_at else "",
        "decision_reason": change.decision_reason or "",
        "applied_by": change.applied_by or "",
        "applied_at": change.applied_at.isoformat() if change.applied_at else "",
        "error_code": change.error_code or "",
        "error_message": service.safe_error(change.error_message),
        "rollback_of": change.rollback_of or "",
        "evidence_id": change.evidence_id or "",
    }


def _audit(principal: Principal, action: str, target: str, metadata: dict[str, Any]) -> AuditLog:
    return AuditLog(
        tenant_id=principal.tenant_id,
        actor_id=principal.subject,
        action=action,
        target=target,
        metadata_json=metadata,
    )


def _capability_payload(connection: dict[str, Any], connection_id: str, principal: Principal) -> dict[str, Any]:
    return {
        "connection_id": str(connection.get("id") or connection_id),
        "connection_name": str(connection.get("display_name") or "Azure connection"),
        "auth_method": str(connection.get("auth_method") or ""),
        "read_only": bool(connection.get("read_only", True)),
        "auto_execute_writes": bool(connection.get("auto_execute_writes", False)),
        "can_manage_alert_state": principal.is_admin or principal.has("alerts_manager.alert_state_write"),
        "can_manage_action_groups": principal.is_admin or principal.has("alerts_manager.action_group_write"),
        "can_manage_rules": principal.is_admin or principal.has("alerts_manager.rule_write"),
        "can_manage_advanced_rules": principal.is_admin or principal.has("alerts_manager.advanced_rule_write"),
        "can_bulk_manage": principal.is_admin or principal.has("alerts_manager.bulk_write"),
        "can_preview_queries": principal.is_admin or principal.has("alerts_manager.query_preview"),
        "can_test_notifications": principal.is_admin or principal.has("alerts_manager.test_notifications"),
        "can_delete": principal.is_admin or principal.has("alerts_manager.delete"),
        "can_approve": principal.is_admin or principal.has("alerts_manager.approve"),
        "can_manage_amba_blueprints": principal.is_admin or principal.has("alerts_manager.amba_blueprint_write"),
        "can_submit_deployment_plans": principal.is_admin or principal.has("alerts_manager.rule_write"),
    }


def _paged_rows(rows: list[dict[str, Any]], page: int | None, page_size: int | None) -> tuple[list[dict[str, Any]], int, int, bool]:
    """Paginate only when requested, preserving legacy full-array responses by default."""
    requested = page is not None or page_size is not None
    effective_page = page or 1
    effective_size = page_size or (100 if requested else max(1, len(rows)))
    if not requested:
        return rows, effective_page, effective_size, False
    start = (effective_page - 1) * effective_size
    return rows[start:start + effective_size], effective_page, effective_size, True


async def _activity_scope_inventory(
    *, connection_id: str, workload_id: str | None,
    subscription_id: str | None, management_group_id: str | None, tenant_id: str,
) -> tuple[dict[str, Any], set[str], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    from app.alerts_manager import rules

    connection = _connection(connection_id, workload_id)
    subscriptions, _workload_ids = await rules._subscriptions(
        connection, workload_id, subscription_id, management_group_id,
    )
    rules_result, groups_result = await asyncio.gather(
        rules.list_rules(
            connection, workload_id=workload_id, subscription_id=subscription_id,
            management_group_id=management_group_id, family="activity", tenant_id=tenant_id,
            with_metadata=True,
        ),
        service.list_action_groups(
            connection, workload_id=workload_id, subscription_id=subscription_id,
            management_group_id=management_group_id, tenant_id=tenant_id, with_metadata=True,
        ),
    )
    rule_rows, rule_metadata = rules_result
    action_groups, group_metadata = groups_result
    if not subscriptions:
        subscriptions = {
            str(item.get("subscription_id") or "")
            for item in [*rule_rows, *action_groups] if item.get("subscription_id")
        }
    metadata = {
        "partial": bool(rule_metadata.get("partial") or group_metadata.get("partial")),
        "truncated": bool(rule_metadata.get("truncated") or group_metadata.get("truncated")),
        "rules": rule_metadata,
        "action_groups": group_metadata,
    }
    return connection, subscriptions, rule_rows, action_groups, metadata


async def _activity_blockers(
    db: AsyncSession, tenant_id: str, connection_id: str,
) -> dict[str, dict[str, Any]]:
    rows = (await db.execute(
        select(AlertManagerChange).where(
            AlertManagerChange.tenant_id == tenant_id,
            AlertManagerChange.connection_id == connection_id,
            AlertManagerChange.target_type == "activity_rule",
            AlertManagerChange.status.in_(["pending", "approved"]),
        )
    )).scalars().all()
    blockers: dict[str, dict[str, Any]] = {}
    for change in rows:
        summary = change.summary_json or {}
        desired = summary.get("desired") if isinstance(summary.get("desired"), dict) else {}
        before = summary.get("before") if isinstance(summary.get("before"), dict) else {}
        category = str(summary.get("category") or desired.get("activity_category") or before.get("activity_category") or "")
        blockers[change.target_id.lower().rstrip("/")] = {
            "change_id": change.id, "status": change.status, "target_id": change.target_id,
            "operation": change.operation,
            "category": category,
            "subscription_id": str(summary.get("subscription_id") or service._subscription_from_id(change.target_id)),
            "requested_by": change.requested_by,
            "requested_at": change.requested_at.isoformat() if change.requested_at else "",
        }
    return blockers


def _cached_subscription_names(
    tenant_id: str, connection_id: str, subscription_ids: set[str],
) -> dict[str, str]:
    """Best-effort labels from already-collected inventory; never initiates an Azure call."""
    from app.inventory import cache as inventory_cache

    names: dict[str, str] = {}
    candidates = [inventory_cache.get(tenant_id, connection_id)]
    candidates.extend(
        inventory_cache.get(tenant_id, connection_id, scope=f"sub:{subscription_id}")
        for subscription_id in subscription_ids
    )
    for hit in candidates:
        payload = hit.get("payload") if hit else {}
        for row in payload.get("subscriptions") or []:
            if isinstance(row, dict) and row.get("id"):
                names[str(row["id"]).lower()] = str(row.get("name") or row["id"])
        for row in (payload.get("facets") or {}).get("subscriptions") or []:
            if isinstance(row, dict) and row.get("key"):
                names[str(row["key"]).lower()] = str(row.get("name") or row["key"])
    return names


async def _build_activity_plan(
    payload: ActivityLogPlanRequest, principal: Principal, db: AsyncSession,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.alerts_manager import activity_planner

    scopes = [payload.workload_id, payload.subscription_id, payload.management_group_id]
    if sum(bool(value) for value in scopes) != 1:
        raise HTTPException(status_code=422, detail="Select exactly one workload, subscription, or management group scope.")
    try:
        connection, subscriptions, rule_rows, action_groups, _metadata = await _activity_scope_inventory(
            connection_id=payload.connection_id, workload_id=payload.workload_id,
            subscription_id=payload.subscription_id, management_group_id=payload.management_group_id,
            tenant_id=_tenant(principal),
        )
        resolved_connection_id = str(connection.get("id") or payload.connection_id)
        inputs = payload.model_dump(exclude={"plan_token", "reason"})
        inputs["connection_id"] = resolved_connection_id
        plan = activity_planner.preview_plan(
            inputs, subscription_ids=subscriptions, rules_inventory=rule_rows,
            action_groups=action_groups,
            blockers=await _activity_blockers(db, _tenant(principal), resolved_connection_id),
        )
        create_items = [item for item in plan["items"] if item["classification"] == "create"]
        if create_items:
            resource_group = str(inputs.get("resource_group") or "")
            checks: dict[str, tuple[dict[str, Any] | None, int, str]] = {}
            for subscription in sorted({str(item["subscription_id"]) for item in create_items}):
                checks[subscription] = await service.get_arm_resource(
                    connection,
                    f"/subscriptions/{subscription}/resourceGroups/{resource_group}",
                    "2021-04-01",
                )
            for item in create_items:
                live, status, error = checks[str(item["subscription_id"])]
                if live:
                    continue
                detail = (
                    f"Monitoring resource group '{resource_group}' does not exist in subscription {item['subscription_id']}."
                    if status == 404 else
                    f"Could not verify monitoring resource group '{resource_group}': {error or f'ARM status {status}'}."
                )
                item["classification"] = "invalid"
                item["operation"] = "none"
                item["actionable"] = False
                item["validation_status"] = "invalid"
                item["errors"].append(detail)
                item["reason"] = detail
                plan["counts"]["create"] -= 1
                plan["counts"]["invalid"] += 1
            plan["counts"]["actionable"] = sum(1 for item in plan["items"] if item["actionable"])
            plan["valid"] = all(not item["errors"] for item in plan["items"] if item["actionable"]) and bool(plan["counts"]["actionable"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return connection, plan


async def _activity_coverage_payload(
    *, connection_id: str, workload_id: str | None, subscription_id: str | None,
    management_group_id: str | None, principal: Principal, db: AsyncSession,
) -> dict[str, Any]:
    from app.alerts_manager import activity_coverage

    scopes = [workload_id, subscription_id, management_group_id]
    if sum(bool(value) for value in scopes) != 1:
        raise HTTPException(status_code=422, detail="Select exactly one workload, subscription, or management group scope.")
    try:
        connection, subscriptions, rule_rows, action_groups, metadata = await _activity_scope_inventory(
            connection_id=connection_id, workload_id=workload_id,
            subscription_id=subscription_id, management_group_id=management_group_id,
            tenant_id=_tenant(principal),
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    resolved_connection_id = str(connection.get("id") or connection_id)
    coverage = activity_coverage.evaluate_coverage(
        subscriptions, rule_rows, action_groups, metadata=metadata,
        blockers=list((await _activity_blockers(db, _tenant(principal), resolved_connection_id)).values()),
        subscription_names=_cached_subscription_names(_tenant(principal), resolved_connection_id, subscriptions),
    )
    return {
        "connection_id": resolved_connection_id,
        "scope": {
            "kind": "workload" if workload_id else "subscription" if subscription_id else "management_group",
            "id": workload_id or subscription_id or management_group_id or "",
        },
        "coverage": coverage,
    }


@router.get("/activity-log-coverage")
async def activity_log_coverage(
    connection_id: str = Query(default=""),
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _activity_coverage_payload(
        connection_id=connection_id, workload_id=workload_id, subscription_id=subscription_id,
        management_group_id=management_group_id, principal=principal, db=db,
    )


@router.get("/activity-log-coverage/export")
async def export_activity_log_coverage(
    format: str = Query(default="csv", pattern="^(csv|json|xlsx)$"),
    connection_id: str = Query(default=""),
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> Response:
    from app.alerts_manager import activity_export

    payload = await _activity_coverage_payload(
        connection_id=connection_id, workload_id=workload_id, subscription_id=subscription_id,
        management_group_id=management_group_id, principal=principal, db=db,
    )
    if format == "xlsx":
        content, media_type = activity_export.to_workbook(payload), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif format == "json":
        content, media_type = activity_export.to_json(payload), "application/json"
    else:
        content, media_type = activity_export.to_csv(payload), "text/csv; charset=utf-8"
    scope_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(payload["scope"]["id"])).strip("-") or "scope"
    db.add(_audit(principal, "alerts_manager.activity_log_coverage.exported", f"{payload['scope']['kind']}:{payload['scope']['id']}", {
        "format": format, "connection_id": payload["connection_id"], "sanitized": True,
        "azure_writes_performed": False,
    }))
    await db.commit()
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="activity-log-coverage-{scope_name}.{format}"'},
    )


@router.post("/activity-log-plan/preview")
async def preview_activity_log_plan(
    payload: ActivityLogPlanRequest,
    principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _connection_value, plan = await _build_activity_plan(payload, principal, db)
    return {"plan": plan}


@router.post("/activity-log-plan/validate")
async def validate_activity_log_plan(
    payload: ActivityLogPlanValidationRequest,
    principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _connection_value, plan = await _build_activity_plan(payload, principal, db)
    token_matches = payload.plan_token == plan["plan_token"]
    errors = [] if token_matches else ["The plan inputs changed after preview. Preview the plan again."]
    errors.extend(
        f"{item['category']} ({item['subscription_id']}): {error}"
        for item in plan["items"] if item["classification"] in {"invalid", "blocked"} for error in item["errors"]
    )
    if not plan["counts"]["actionable"]:
        errors.append("The plan has no new Activity Log rules to submit.")
    return {"valid": not errors, "errors": errors, "plan": plan}


@router.post("/activity-log-plan/submit")
async def submit_activity_log_plan(
    payload: ActivityLogPlanSubmitRequest,
    principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    connection, plan = await _build_activity_plan(payload, principal, db)
    if payload.plan_token != plan["plan_token"]:
        raise HTTPException(status_code=409, detail="The plan inputs changed after preview. Preview the plan again.")
    actionable = [item for item in plan["items"] if item["actionable"]]
    validation_errors = [error for item in actionable for error in item["errors"]]
    if validation_errors or not actionable:
        raise HTTPException(status_code=422, detail={"message": "Activity Log plan is not submittable.", "errors": validation_errors})
    try:
        service.assert_writable(connection)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    batch_id = str(uuid.uuid4())
    changes: list[AlertManagerChange] = []
    for item in actionable:
        operation = str(item.get("operation") or "create")
        before: dict[str, Any] = {}
        desired = dict(item["desired"])
        body = item["body"]
        expected_hash = ""
        if operation == "update":
            before, live_status, error = await rules.get_rule(connection, item["target_id"], "activity")
            if error or not before:
                raise HTTPException(
                    status_code=404 if live_status == 404 else 502,
                    detail=error or f"Activity Log rule {item['target_id']} was not found.",
                )
            expected_hash = service.canonical_hash(service._resource_body(before))
            merged = rules.editable_rule(before)
            merged.update(desired)
            merged["enabled"] = True
            errors = rules.validate_rule_payload("activity", merged, create=False)
            if errors:
                raise HTTPException(status_code=422, detail=errors)
            desired = merged
            body = rules.build_rule_body("activity", merged, before)
        change = AlertManagerChange(
            id=str(uuid.uuid4()),
            tenant_id=_tenant(principal),
            connection_id=str(connection.get("id") or payload.connection_id),
            target_type="activity_rule", target_id=item["target_id"], operation=operation,
            status="pending", risk="medium",
            summary_json={
                "reason": payload.reason, "batch_id": batch_id, "batch_order": item["order"],
                "source": "essential_activity_log_wizard", "category": item["category"],
                "subscription_id": item["subscription_id"],
                "reason_detail": item["reason"], "validation_status": item["validation_status"],
                "receiver_count": item["receiver_count"], "cost": item["cost"],
                "existing_rule_details": item["existing_rule_details"], "issues": item["issues"],
                "before": rules.summarize_rule_body("activity", service._resource_body(before)) if before else {},
                "desired": rules.summarize_rule_body("activity", body),
                "evidence_summary": {
                    "source": "essential_activity_log_wizard", "plan_token": plan["plan_token"],
                    "classification": item["classification"], "validation_status": item["validation_status"],
                    "receiver_count": item["receiver_count"], "cost_classification": "free",
                    "existing_rule_count": len(item["existing_rule_details"]),
                    "approval_required": True, "azure_writes_performed": False,
                },
            },
            desired_encrypted=service.encrypted_json({"payload": desired, "body": body}),
            before_encrypted=service.encrypted_json(before), after_encrypted="", expected_state_hash=expected_hash,
            requested_by=principal.subject, requested_at=service.now(), auto_apply=False,
        )
        changes.append(change)
        db.add(change)
        db.add(_audit(principal, "alerts_manager.activity_log_change.requested", item["target_id"], {
            "change_id": change.id, "batch_id": batch_id, "batch_order": item["order"],
            "category": item["category"], "operation": operation,
            "evidence_summary": {
                "classification": item["classification"], "validation_status": item["validation_status"],
                "receiver_count": item["receiver_count"], "cost_classification": "free",
                "existing_rule_count": len(item["existing_rule_details"]),
                "approval_required": True, "azure_writes_performed": False,
            },
        }))
    db.add(_audit(principal, "alerts_manager.activity_log_plan.submitted", batch_id, {
        "batch_id": batch_id, "change_count": len(changes), "plan_token": plan["plan_token"],
        "evidence_summary": {
            "actionable_count": plan["counts"]["actionable"], "cost_classification": "free",
            "approval_required": True, "azure_writes_performed": False,
        },
    }))
    await db.commit()
    return {
        "status": "pending", "batch_id": batch_id, "change_count": len(changes),
        "changes": [_change_dict(change) for change in changes],
        "azure_writes_performed": False,
    }


async def _activity_diagnostic_scope(
    payload: ActivityLogDiagnosticPlanRequest, principal: Principal,
) -> tuple[dict[str, Any], set[str], dict[str, Any]]:
    from app.alerts_manager import activity_diagnostics, rules

    scopes = [payload.workload_id, payload.subscription_id, payload.management_group_id]
    if sum(bool(value) for value in scopes) != 1:
        raise HTTPException(status_code=422, detail="Select exactly one workload, subscription, or management group scope.")
    connection = _connection(payload.connection_id, payload.workload_id)
    try:
        subscriptions, _workload_ids = await rules._subscriptions(
            connection, payload.workload_id, payload.subscription_id, payload.management_group_id,
        )
        result = await activity_diagnostics.inventory(connection, subscriptions, tenant_id=_tenant(principal))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return connection, subscriptions, result


async def _activity_diagnostic_blockers(
    db: AsyncSession, tenant_id: str, connection_id: str,
) -> dict[str, dict[str, Any]]:
    rows = (await db.execute(select(AlertManagerChange).where(
        AlertManagerChange.tenant_id == tenant_id,
        AlertManagerChange.connection_id == connection_id,
        AlertManagerChange.target_type == "activity_log_diagnostic_setting",
        AlertManagerChange.status.in_(["pending", "approved"]),
    ))).scalars().all()
    return {
        row.target_id.lower().rstrip("/"): {
            "change_id": row.id, "status": row.status, "operation": row.operation,
        }
        for row in rows
    }


async def _build_activity_diagnostic_plan(
    payload: ActivityLogDiagnosticPlanRequest, principal: Principal, db: AsyncSession,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.alerts_manager import activity_diagnostics

    connection, subscriptions, inventory_result = await _activity_diagnostic_scope(payload, principal)
    inputs = payload.model_dump(exclude={"plan_token", "reason"})
    inputs["connection_id"] = str(connection.get("id") or payload.connection_id)
    try:
        plan = activity_diagnostics.preview_plan(
            inputs, inventory_result, allowed_subscriptions=subscriptions,
            blockers=await _activity_diagnostic_blockers(
                db, _tenant(principal), str(connection.get("id") or payload.connection_id),
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return connection, plan


@router.get("/activity-log-diagnostic-settings/inventory")
async def activity_log_diagnostic_settings_inventory(
    connection_id: str = Query(default=""), workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None), management_group_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    request = ActivityLogDiagnosticPlanRequest(
        connection_id=connection_id, workload_id=workload_id, subscription_id=subscription_id,
        management_group_id=management_group_id,
        destination=ActivityLogDiagnosticDestination(
            kind="workspace", resource_id="/subscriptions/placeholder/resourceGroups/placeholder/providers/Microsoft.OperationalInsights/workspaces/placeholder",
        ),
    )
    connection, subscriptions, result = await _activity_diagnostic_scope(request, principal)
    return {
        "connection_id": str(connection.get("id") or connection_id),
        "scope": {"kind": "workload" if workload_id else "subscription" if subscription_id else "management_group", "id": workload_id or subscription_id or management_group_id or ""},
        "selected_subscription_ids": sorted(subscriptions), **result,
    }


@router.get("/activity-log-diagnostic-settings/destination-options")
async def activity_log_diagnostic_destination_options(
    connection_id: str = Query(default=""),
    management_group_id: str = Query(default="", max_length=1000),
    subscription_id: str = Query(default="", max_length=128),
    resource_group: str = Query(default="", max_length=90),
    kind: Literal["workspace", "event_hub", "storage"] = Query(default="workspace"),
    namespace_id: str = Query(default="", max_length=1000),
    _: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alerts_manager import destination_options

    try:
        return await destination_options.options(
            _connection(connection_id), management_group_id=management_group_id,
            subscription_id=subscription_id, resource_group=resource_group,
            kind=kind, namespace_id=namespace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/activity-log-diagnostic-settings/plan/preview")
async def preview_activity_log_diagnostic_plan(
    payload: ActivityLogDiagnosticPlanRequest, principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _connection_value, plan = await _build_activity_diagnostic_plan(payload, principal, db)
    return {"plan": plan, "azure_writes_performed": False}


@router.post("/activity-log-diagnostic-settings/plan/validate")
async def validate_activity_log_diagnostic_plan(
    payload: ActivityLogDiagnosticValidationRequest, principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _connection_value, plan = await _build_activity_diagnostic_plan(payload, principal, db)
    errors = [] if payload.plan_token == plan["plan_token"] else ["The plan inputs or Azure inventory changed after preview. Preview the plan again."]
    errors.extend(error for item in plan["items"] for error in item["errors"])
    if not plan["counts"]["actionable"]:
        errors.append("The plan has no diagnostic-setting changes to submit.")
    return {"valid": not errors, "errors": errors, "plan": plan, "azure_writes_performed": False}


@router.post("/activity-log-diagnostic-settings/plan/submit")
async def submit_activity_log_diagnostic_plan(
    payload: ActivityLogDiagnosticSubmitRequest, principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    connection, plan = await _build_activity_diagnostic_plan(payload, principal, db)
    if payload.plan_token != plan["plan_token"]:
        raise HTTPException(status_code=409, detail="The plan inputs or Azure inventory changed after preview. Preview the plan again.")
    actionable = [item for item in plan["items"] if item["actionable"]]
    errors = [error for item in plan["items"] for error in item["errors"]]
    if errors or not actionable:
        raise HTTPException(status_code=422, detail={"message": "Diagnostic-settings plan is not submittable.", "errors": errors})
    try:
        service.assert_writable(connection)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    batch_id = str(uuid.uuid4())
    changes: list[AlertManagerChange] = []
    for item in actionable:
        before = dict(item["before"])
        before_resource = {
            "id": str(before.get("id") or item["target_id"]), "name": item["setting_name"],
            "properties": dict(before.get("properties") or {}),
        } if before else {}
        body = dict(item["desired"])
        expected_hash = service.canonical_hash(service._resource_body(before_resource)) if before_resource else ""
        change = AlertManagerChange(
            id=str(uuid.uuid4()), tenant_id=_tenant(principal),
            connection_id=str(connection.get("id") or payload.connection_id),
            target_type="activity_log_diagnostic_setting", target_id=item["target_id"],
            operation=item["operation"], status="pending", risk="medium",
            summary_json={
                "reason": payload.reason, "batch_id": batch_id, "batch_order": item["order"],
                "source": "activity_log_diagnostic_settings_planner", "subscription_id": item["subscription_id"],
                "categories": item["categories"], "destination_kind": item["destination"]["kind"],
                "before_hash": expected_hash, "desired_hash": service.canonical_hash(body),
                "approval_required": True, "azure_writes_performed": False,
            },
            desired_encrypted=service.encrypted_json({"body": body}),
            before_encrypted=service.encrypted_json(before_resource), after_encrypted="",
            expected_state_hash=expected_hash, requested_by=principal.subject,
            requested_at=service.now(), auto_apply=False,
        )
        changes.append(change)
        db.add(change)
        db.add(_audit(principal, "alerts_manager.activity_log_diagnostic_setting.requested", item["target_id"], {
            "change_id": change.id, "batch_id": batch_id, "operation": item["operation"],
            "subscription_id": item["subscription_id"], "before_hash": expected_hash,
            "desired_hash": change.summary_json["desired_hash"], "approval_required": True,
            "azure_writes_performed": False,
        }))
    db.add(_audit(principal, "alerts_manager.activity_log_diagnostic_plan.submitted", batch_id, {
        "batch_id": batch_id, "change_count": len(changes), "plan_token": plan["plan_token"],
        "approval_required": True, "azure_writes_performed": False,
    }))
    await db.commit()
    return {"status": "pending", "batch_id": batch_id, "change_count": len(changes), "changes": [_change_dict(change) for change in changes], "azure_writes_performed": False}


@router.get("/capabilities")
async def capabilities(
    connection_id: str = Query(default=""),
    workload_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    connection = _connection(connection_id, workload_id)
    return _capability_payload(connection, connection_id, principal)


@router.get("/summary")
async def summary(
    connection_id: str = Query(default=""),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Local-only header counts and capabilities; performs no Azure API calls."""
    from app.alerts_manager import planner

    connection = _connection(connection_id)
    resolved_connection_id = str(connection.get("id") or connection_id)
    filters = [AlertManagerChange.tenant_id == _tenant(principal)]
    if resolved_connection_id:
        filters.append(AlertManagerChange.connection_id == resolved_connection_id)
    counts = dict((await db.execute(
        select(AlertManagerChange.status, func.count())
        .where(*filters, AlertManagerChange.status.in_(["pending", "approved"]))
        .group_by(AlertManagerChange.status)
    )).all())
    pending = int(counts.get("pending", 0))
    approved = int(counts.get("approved", 0))
    latest_applied_at = (await db.execute(
        select(func.max(AlertManagerChange.applied_at)).where(
            *filters, AlertManagerChange.status == "applied",
        )
    )).scalar_one_or_none()
    return {
        "connection_id": resolved_connection_id,
        "pending_count": pending,
        "approved_count": approved,
        "actionable_count": pending + approved,
        "latest_applied_at": latest_applied_at.isoformat() if latest_applied_at else "",
        "deployment_plan_count": len(planner.list_plans(_tenant(principal))),
        "capabilities": _capability_payload(connection, connection_id, principal),
    }


@router.get("/authoring/options")
async def authoring_options(
    connection_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    _: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.azure.arm import list_subscriptions

    connection = _connection(connection_id)
    try:
        token = await service._token(connection)
        subscriptions, subscription_error = await list_subscriptions(token)
        selected_subscriptions = {subscription_id} if subscription_id else set()
        group_rows = await service._arg(
            connection,
            "resourcecontainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups' | project id,name,subscriptionId,location",
            selected_subscriptions, max_rows=5000,
        ) if subscription_id else []
        resource_rows: list[dict[str, Any]] = []
        if subscription_id and resource_group:
            escaped_group = resource_group.replace("'", "''")
            resource_rows = await service._arg(
                connection,
                f"resources | where resourceGroup =~ '{escaped_group}' | where type in~ ('microsoft.web/sites','microsoft.web/sites/functions','microsoft.logic/workflows','microsoft.eventhub/namespaces','microsoft.eventhub/namespaces/eventhubs','microsoft.automation/automationaccounts','microsoft.automation/automationaccounts/webhooks','microsoft.operationalinsights/workspaces','microsoft.insights/components','microsoft.monitor/accounts') | project id,name,type,kind,subscriptionId,resourceGroup,location,properties",
                {subscription_id}, max_rows=5000,
            )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    resources = []
    for row in resource_rows:
        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        resources.append({
            "id": str(row.get("id") or ""), "name": str(row.get("name") or ""),
            "type": str(row.get("type") or "").lower(), "kind": str(row.get("kind") or "").lower(),
            "subscription_id": str(row.get("subscriptionId") or ""),
            "resource_group": str(row.get("resourceGroup") or ""), "location": str(row.get("location") or ""),
            "workspace_id": str(props.get("customerId") or "") if str(row.get("type") or "").lower() == "microsoft.operationalinsights/workspaces" else "",
        })
    return {
        "subscriptions": subscriptions,
        "subscription_error": service.safe_error(subscription_error),
        "resource_groups": sorted(
            [{"id": str(row.get("id") or ""), "name": str(row.get("name") or ""), "subscription_id": str(row.get("subscriptionId") or ""), "location": str(row.get("location") or "")} for row in group_rows],
            key=lambda item: item["name"].lower(),
        ),
        "resources": sorted(resources, key=lambda item: (item["type"], item["name"].lower())),
    }


@router.post("/authoring/resolve")
async def resolve_authoring_scopes(
    payload: AuthoringResolveRequest,
    _: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.azure.arm import list_subscriptions

    connection = _connection(payload.connection_id)
    ids = list(dict.fromkeys(str(value).strip() for value in payload.resource_ids if str(value).strip()))
    subscriptions = {service._subscription_from_id(value) for value in ids if service._subscription_from_id(value)}
    quoted = ",".join(f"'{value.replace(chr(39), chr(39) * 2)}'" for value in ids)
    resource_rows: list[dict[str, Any]] = []
    container_rows: list[dict[str, Any]] = []
    try:
        resource_rows = await service._arg(
            connection,
            f"resources | where id in~ ({quoted}) | project id,name,type,kind,subscriptionId,resourceGroup,location",
            subscriptions, max_rows=200,
        )
        container_rows = await service._arg(
            connection,
            f"resourcecontainers | where id in~ ({quoted}) | project id,name,type,subscriptionId,resourceGroup,location",
            subscriptions, max_rows=200,
        )
        token = await service._token(connection)
        subscription_rows, _subscription_error = await list_subscriptions(token)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    by_id = {str(row.get("id") or "").lower(): row for row in [*resource_rows, *container_rows]}
    subscription_names = {str(item.get("id") or "").lower(): str(item.get("name") or item.get("id") or "") for item in subscription_rows}
    resolved = []
    for resource_id in ids:
        normalized = resource_id.lower().rstrip("/")
        row = by_id.get(normalized, {})
        parts = resource_id.strip("/").split("/")
        lower_parts = [part.lower() for part in parts]
        subscription_id = service._subscription_from_id(resource_id)
        resource_group = service._resource_group_from_id(resource_id)
        bare_subscription_name = subscription_names.get(normalized, "") if len(parts) == 1 else ""
        if bare_subscription_name:
            kind = "subscription"
            subscription_id = parts[0]
            name = bare_subscription_name
        elif len(parts) == 2 and lower_parts[0] == "subscriptions":
            kind = "subscription"
            name = subscription_names.get(subscription_id.lower(), subscription_id)
        elif len(parts) == 4 and lower_parts[0] == "subscriptions" and lower_parts[2] == "resourcegroups":
            kind = "resource_group"
            name = resource_group
        else:
            kind = "resource"
            name = str(row.get("name") or service._name_from_id(resource_id))
        resolved.append({
            "kind": kind, "id": resource_id, "name": name,
            "subscription_id": str(row.get("subscriptionId") or subscription_id),
            "subscription_name": subscription_names.get(subscription_id.lower(), subscription_id),
            "resource_group": str(row.get("resourceGroup") or resource_group),
            "resource_type": "" if kind == "subscription" else str(row.get("type") or ""),
            "location": str(row.get("location") or ""),
        })
    return {"resources": resolved}


@router.get("/alert-instances")
async def alert_instances(
    connection_id: str = Query(default=""),
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=90),
    states: str = Query(default=""),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=250),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    connection = _connection(connection_id, workload_id)
    try:
        rows, metadata = await service.list_fired_alerts(
            connection,
            workload_id=workload_id,
            subscription_id=subscription_id,
            management_group_id=management_group_id,
            days=days,
            states={part.strip() for part in states.split(",") if part.strip()},
            tenant_id=_tenant(principal), with_metadata=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    visible, effective_page, effective_size, paginated = _paged_rows(rows, page, page_size)
    return {
        "alerts": visible, "count": len(rows), "total": len(rows), "days": days,
        "page": effective_page, "page_size": effective_size, "paginated": paginated,
        **metadata,
    }


@router.post("/alert-instances/history")
async def alert_instance_history(
    payload: AlertHistoryRequest,
    _: Principal = Depends(_read),
) -> dict[str, Any]:
    connection = _connection(payload.connection_id)
    try:
        return await service.fired_alert_history(connection, payload.alert_id)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/alert-instances/state")
async def alert_instance_state(
    payload: AlertStateRequest,
    principal: Principal = Depends(_alert_state),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    connection = _connection(payload.connection_id)
    try:
        result = await service.change_fired_alert_state(connection, payload.alert_id, payload.state)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.add(_audit(principal, "alerts_manager.alert.state", payload.alert_id, {"state": payload.state}))
    await db.commit()
    from app.alerts_manager import cache as inventory_cache

    await inventory_cache.invalidate(
        kinds={"fired_alerts"},
        connection_id=str(connection.get("id") or payload.connection_id),
    )
    return result


@router.get("/action-groups")
async def action_groups(
    connection_id: str = Query(default=""),
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=250),
    all_visible: bool = Query(default=False),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    connection = _connection(connection_id, workload_id)
    try:
        rows, metadata = await service.list_action_groups(
            connection, workload_id=workload_id, subscription_id=subscription_id,
            management_group_id=management_group_id, tenant_id=_tenant(principal), with_metadata=True,
            all_visible=all_visible,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    visible, effective_page, effective_size, paginated = _paged_rows(rows, page, page_size)
    return {
        "action_groups": visible, "count": len(rows), "total": len(rows),
        "page": effective_page, "page_size": effective_size, "paginated": paginated,
        **metadata,
    }


@router.get("/action-groups/details")
async def action_group_details(
    action_group_id: str = Query(min_length=1),
    connection_id: str = Query(default=""),
    _: Principal = Depends(_ag_write),
) -> dict[str, Any]:
    connection = _connection(connection_id)
    resource, status, error = await service.get_arm_resource(connection, action_group_id)
    if error or not resource:
        raise HTTPException(status_code=404 if status == 404 else 502, detail=error or "Action group not found.")
    return {"action_group": service.editable_action_group(resource)}


@router.get("/alert-rules")
async def alert_rules(
    connection_id: str = Query(default=""),
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    family: str = Query(default=""),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=250),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    connection = _connection(connection_id, workload_id)
    try:
        rows, metadata = await rules.list_rules(
            connection,
            workload_id=workload_id,
            subscription_id=subscription_id,
            management_group_id=management_group_id,
            family=family,
            tenant_id=_tenant(principal), with_metadata=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    visible, effective_page, effective_size, paginated = _paged_rows(rows, page, page_size)
    return {
        "rules": visible, "count": len(rows), "total": len(rows),
        "page": effective_page, "page_size": effective_size, "paginated": paginated,
        **metadata,
    }


@router.get("/alert-rules/details")
async def alert_rule_details(
    rule_id: str = Query(min_length=1),
    family: Literal["metric", "log", "activity", "smart", "prometheus"] = Query(),
    connection_id: str = Query(default=""),
    principal: Principal = Depends(_rule_write),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    connection = _connection(connection_id)
    if family in {"smart", "prometheus"} and not (principal.is_admin or principal.has("alerts_manager.advanced_rule_write")):
        raise HTTPException(status_code=403, detail="Advanced alert-rule permission is required.")
    resource, status, error = await rules.get_rule(connection, rule_id, family)
    if error or not resource:
        raise HTTPException(status_code=404 if status == 404 else 502, detail=error or "Alert rule not found.")
    return {"rule": rules.editable_rule(resource)}


@router.get("/metrics/definitions")
async def metric_definitions(
    resource_id: str = Query(min_length=1),
    connection_id: str = Query(default=""),
    _: Principal = Depends(_query_preview),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    try:
        values = await rules.metric_definitions(_connection(connection_id), resource_id)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"metrics": values, "count": len(values)}


@router.post("/metrics/preview")
async def preview_metric(
    payload: MetricPreviewRequest,
    _: Principal = Depends(_query_preview),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    try:
        return await rules.metric_preview(
            _connection(payload.connection_id), payload.resource_id, payload.metric_name,
            payload.aggregation, payload.interval,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/logs/preview")
async def preview_log_query(
    payload: LogPreviewRequest,
    _: Principal = Depends(_query_preview),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    try:
        return await rules.log_preview(
            _connection(payload.connection_id), payload.workspace_id, payload.query, payload.timespan
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/alert-rules/validate")
async def validate_alert_rule(
    payload: RuleValidationRequest,
    principal: Principal = Depends(_rule_write),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    if payload.family in {"smart", "prometheus"} and not (principal.is_admin or principal.has("alerts_manager.advanced_rule_write")):
        raise HTTPException(status_code=403, detail="Advanced alert-rule permission is required.")
    errors = rules.validate_rule_payload(payload.family, payload.desired, create=payload.create)
    return {
        "valid": not errors,
        "errors": errors,
        "cost": rules.cost_advisory(payload.family, payload.desired),
    }


@router.post("/alert-rules/noise-guard")
async def alert_rule_noise_guard(
    payload: NoiseGuardRequest,
    _: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alerts_manager import advisory

    try:
        tolerance = payload.threshold_tolerance_pct
        if tolerance is None:
            from app.core.app_settings import load_settings

            tolerance = float(load_settings().get("alert_analysis_threshold_tolerance_pct", 20) or 20)
        return await advisory.noise_guard(
            _connection(payload.connection_id, payload.workload_id), payload.family,
            payload.desired, workload_id=payload.workload_id, threshold_tolerance_pct=tolerance,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/notifications/simulate")
async def simulate_notification_path(
    payload: NotificationSimulationRequest,
    _: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alerts_manager import advisory

    if not payload.rule_id and not payload.resource_id:
        raise HTTPException(status_code=422, detail="Select an alert rule or target resource.")
    try:
        event = payload.model_dump()
        event["tenant_id"] = _tenant(_)
        return await advisory.simulate_notification_path(_connection(payload.connection_id), event)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/notifications/bulk-simulate")
async def bulk_simulate_notification_paths(
    payload: BulkNotificationSimulationRequest,
    _: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alerts_manager import advisory

    scopes = [payload.workload_id, payload.subscription_id, payload.management_group_id]
    if sum(bool(value) for value in scopes) != 1:
        raise HTTPException(status_code=422, detail="Select exactly one workload, subscription, or management group scope.")
    try:
        return await advisory.bulk_simulate_notification_paths(
            _connection(payload.connection_id, payload.workload_id),
            workload_id=payload.workload_id, subscription_id=payload.subscription_id,
            management_group_id=payload.management_group_id, monitor_condition=payload.monitor_condition,
            include_disabled=payload.include_disabled, families=set(payload.families), severities=set(payload.severities),
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/action-groups/suggestions")
async def action_group_suggestions(
    subject_kind: Literal["resource", "workload"] = Query(),
    subject_id: str = Query(min_length=1),
    connection_id: str = Query(default=""),
    workload_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alerts_manager import advisory

    try:
        return await advisory.suggest_action_groups(
            _connection(connection_id, workload_id), _tenant(principal), subject_kind=subject_kind,
            subject_id=subject_id, workload_id=workload_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/alert-rules/changes")
async def request_alert_rule_change(
    payload: AlertRuleChangeRequest,
    principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    connection = _connection(payload.connection_id)
    if payload.family in {"smart", "prometheus"} and not (principal.is_admin or principal.has("alerts_manager.advanced_rule_write")):
        raise HTTPException(status_code=403, detail="Advanced alert-rule permission is required.")
    try:
        service.assert_writable(connection)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    family = payload.family
    operation = payload.operation
    desired = payload.desired
    target_id = payload.target_id.strip()
    before: dict[str, Any] = {}
    resource_type, _api_version = rules.api_for_family(family)
    if operation == "create":
        errors = rules.validate_rule_payload(family, desired, create=True)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        subscription = str(desired.get("subscription_id") or "").strip()
        group = str(desired.get("resource_group") or "").strip()
        name = str(desired.get("name") or "").strip()
        target_id = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/{resource_type}/{name}"
        live, live_status, _error = await rules.get_rule(connection, target_id, family)
        if live:
            raise HTTPException(status_code=409, detail="An alert rule with this name already exists.")
        if live_status not in (0, 404):
            raise HTTPException(status_code=502, detail="Could not verify that the alert-rule name is available.")
        body = rules.build_rule_body(family, desired)
        expected_hash = ""
    else:
        if f"/providers/{resource_type}/" not in target_id.lower():
            raise HTTPException(status_code=422, detail="The rule ID doesn't match the selected alert family.")
        before, live_status, error = await rules.get_rule(connection, target_id, family)
        if error or not before:
            raise HTTPException(status_code=404 if live_status == 404 else 502, detail=error or "Alert rule not found.")
        expected_hash = service.canonical_hash(service._resource_body(before))
        if operation == "delete":
            body = {}
        else:
            merged = rules.editable_rule(before)
            merged.update(desired)
            errors = rules.validate_rule_payload(family, merged, create=False)
            if errors:
                raise HTTPException(status_code=422, detail=errors)
            desired = merged
            body = rules.build_rule_body(family, merged, before)
    summary = {
        "reason": payload.reason,
        "before": rules.summarize_rule_body(family, service._resource_body(before)) if before else {},
        "desired": rules.summarize_rule_body(family, body) if body else {},
        "cost": rules.cost_advisory(family, desired) if operation != "delete" else {},
    }
    change = AlertManagerChange(
        tenant_id=_tenant(principal),
        connection_id=str(connection.get("id") or payload.connection_id),
        target_type=f"{family}_rule",
        target_id=target_id,
        operation=operation,
        status="pending",
        risk="critical" if operation == "delete" else "high" if family == "log" else "medium",
        summary_json=summary,
        desired_encrypted=service.encrypted_json({"payload": desired, "body": body}),
        before_encrypted=service.encrypted_json(before),
        after_encrypted="",
        expected_state_hash=expected_hash,
        requested_by=principal.subject,
        requested_at=service.now(),
        auto_apply=False,
    )
    db.add(change)
    db.add(_audit(principal, "alerts_manager.rule_change.requested", target_id, {"change_id": change.id, "family": family, "operation": operation, "risk": change.risk}))
    await db.commit()
    await db.refresh(change)
    return {"change": _change_dict(change)}


@router.post("/alert-rules/bulk-changes")
async def request_bulk_rule_changes(
    payload: BulkRuleChangeRequest,
    principal: Principal = Depends(_bulk_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import rules

    connection = _connection(payload.connection_id)
    try:
        service.assert_writable(connection)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if any(item.family in {"smart", "prometheus"} for item in payload.targets) and not (
        principal.is_admin or principal.has("alerts_manager.advanced_rule_write")
    ):
        raise HTTPException(status_code=403, detail="Advanced alert-rule permission is required.")
    if payload.action == "add_action_group" and "/providers/microsoft.insights/actiongroups/" not in payload.action_group_id.lower():
        raise HTTPException(status_code=422, detail="A full Action Group resource ID is required.")
    errors: list[dict[str, str]] = []
    for target in payload.targets:
        resource_type, _api = rules.api_for_family(target.family)
        if f"/providers/{resource_type}/" not in target.target_id.lower():
            errors.append({"target_id": target.target_id, "error": "Resource ID does not match the selected family."})
    if errors:
        raise HTTPException(status_code=422, detail={"message": "No bulk changes were created; correct every target and retry.", "errors": errors})

    from app.azure.arm import arm_write

    token = await service._token(connection)
    semaphore = asyncio.Semaphore(6)

    async def fetch(index: int, target: BulkRuleTarget) -> tuple[int, BulkRuleTarget, dict[str, Any] | None, int, str]:
        _resource_type, api_version = rules.api_for_family(target.family)
        async with semaphore:
            data, error, status = await arm_write(token, "GET", target.target_id, api_version=api_version)
        return index, target, data if isinstance(data, dict) else None, status, service.safe_error(error)

    fetched = await asyncio.gather(*(fetch(index, target) for index, target in enumerate(payload.targets)))
    fetched.sort(key=lambda item: item[0])
    prepared: list[tuple[BulkRuleTarget, dict[str, Any], dict[str, Any], str, str]] = []
    for _index, target, before, status, error in fetched:
        if error or not before:
            errors.append({"target_id": target.target_id, "error": error or f"Rule lookup failed ({status})."})
            continue
        expected_hash = service.canonical_hash(service._resource_body(before))
        if payload.action == "delete":
            prepared.append((target, before, {}, expected_hash, "delete"))
            continue
        desired = rules.editable_rule(before)
        if payload.action in {"enable", "disable"}:
            desired["enabled"] = payload.action == "enable"
        elif target.family == "prometheus":
            groups = desired.get("prometheus_rules") or []
            for rule in groups:
                actions = list(rule.get("actions") or [])
                known = [str(item.get("actionGroupId") if isinstance(item, dict) else item).lower() for item in actions]
                if payload.action_group_id.lower() not in known:
                    actions.append({"actionGroupId": payload.action_group_id})
                rule["actions"] = actions
        else:
            groups = [str(value) for value in desired.get("action_group_ids") or []]
            if payload.action_group_id.lower() not in {value.lower() for value in groups}:
                groups.append(payload.action_group_id)
            desired["action_group_ids"] = groups
        validation = rules.validate_rule_payload(target.family, desired, create=False)
        if validation:
            errors.append({"target_id": target.target_id, "error": "; ".join(validation)})
            continue
        prepared.append((target, before, rules.build_rule_body(target.family, desired, before), expected_hash, "update"))
    if errors:
        raise HTTPException(status_code=422, detail={"message": "No bulk changes were created; correct every target and retry.", "errors": errors})
    batch_id = str(uuid.uuid4())
    changes: list[AlertManagerChange] = []
    for target, before, body, expected_hash, operation in prepared:
        change = AlertManagerChange(
            tenant_id=_tenant(principal), connection_id=str(connection.get("id") or payload.connection_id),
            target_type=f"{target.family}_rule", target_id=target.target_id, operation=operation,
            status="pending", risk="critical" if operation == "delete" else "high" if payload.action == "enable" else "medium",
            summary_json={
                "reason": payload.reason, "batch_id": batch_id, "bulk_action": payload.action,
                "before": rules.summarize_rule_body(target.family, service._resource_body(before)),
                "desired": rules.summarize_rule_body(target.family, body) if body else {},
            },
            desired_encrypted=service.encrypted_json({"body": body}), before_encrypted=service.encrypted_json(before),
            after_encrypted="", expected_state_hash=expected_hash, requested_by=principal.subject,
            requested_at=service.now(), auto_apply=False,
        )
        changes.append(change)
        db.add(change)
    db.add(_audit(principal, "alerts_manager.bulk_change.requested", batch_id, {"action": payload.action, "count": len(changes)}))
    await db.commit()
    return {"batch_id": batch_id, "atomic": False, "count": len(changes), "changes": [_change_dict(change) for change in changes]}


@router.get("/amba-blueprints")
async def list_amba_blueprints(principal: Principal = Depends(_read)) -> dict[str, Any]:
    from app.alerts_manager import planner

    rows = planner.list_blueprints(_tenant(principal))
    return {"blueprints": rows, "count": len(rows)}


@router.post("/amba-blueprints")
async def create_amba_blueprint(
    payload: BlueprintVersionRequest,
    principal: Principal = Depends(_amba_blueprint_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner
    from app.amba.reference import load_reference

    values = payload.model_dump()
    values["amba_version"] = values["amba_version"] or str(load_reference().get("version") or "builtin")
    try:
        blueprint = planner.create_blueprint_version(_tenant(principal), values, actor=principal.subject)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(_audit(principal, "alerts_manager.amba_blueprint.created", blueprint["blueprint_id"], {"version": blueprint["version"], "amba_version": blueprint["amba_version"]}))
    await db.commit()
    return {"blueprint": blueprint}


@router.get("/amba-blueprints/{blueprint_id}")
async def get_amba_blueprint(
    blueprint_id: str,
    version: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    blueprint = planner.get_blueprint(_tenant(principal), blueprint_id, version)
    if not blueprint:
        raise HTTPException(status_code=404, detail="AMBA blueprint version not found.")
    return {"blueprint": blueprint, "versions": planner.list_blueprint_versions(_tenant(principal), blueprint_id)}


@router.post("/amba-blueprints/{blueprint_id}/versions")
async def create_amba_blueprint_version(
    blueprint_id: str,
    payload: BlueprintVersionRequest,
    principal: Principal = Depends(_amba_blueprint_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    try:
        blueprint = planner.create_blueprint_version(_tenant(principal), payload.model_dump(), actor=principal.subject, blueprint_id=blueprint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(_audit(principal, "alerts_manager.amba_blueprint.version_created", blueprint_id, {"version": blueprint["version"], "amba_version": blueprint["amba_version"]}))
    await db.commit()
    return {"blueprint": blueprint}


@router.delete("/amba-blueprints/{blueprint_id}/versions/{version}")
async def delete_amba_blueprint_version(
    blueprint_id: str,
    version: int,
    principal: Principal = Depends(_amba_blueprint_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    try:
        deleted = planner.delete_blueprint_version(_tenant(principal), blueprint_id, version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="AMBA blueprint version not found.")
    db.add(_audit(principal, "alerts_manager.amba_blueprint.version_deleted", blueprint_id, {"version": version}))
    await db.commit()
    return {"deleted": True}


@router.get("/amba-blueprint-assignments")
async def list_amba_blueprint_assignments(principal: Principal = Depends(_read)) -> dict[str, Any]:
    from app.alerts_manager import planner

    rows = planner.list_assignments(_tenant(principal))
    return {"assignments": rows, "count": len(rows)}


@router.get("/amba-blueprint-assignments/{assignment_id}")
async def get_amba_blueprint_assignment(assignment_id: str, principal: Principal = Depends(_read)) -> dict[str, Any]:
    from app.alerts_manager import planner

    assignment = planner.get_assignment(_tenant(principal), assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="AMBA blueprint assignment not found.")
    return {"assignment": assignment}


@router.post("/amba-blueprint-assignments")
async def create_amba_blueprint_assignment(
    payload: BlueprintAssignmentRequest,
    principal: Principal = Depends(_amba_blueprint_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    try:
        assignment = planner.save_assignment(_tenant(principal), payload.model_dump(), actor=principal.subject)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(_audit(principal, "alerts_manager.amba_blueprint.assigned", assignment["id"], {"blueprint_id": assignment["blueprint_id"], "version": assignment["blueprint_version"], "scope_kind": assignment["scope_kind"]}))
    await db.commit()
    return {"assignment": assignment}


@router.put("/amba-blueprint-assignments/{assignment_id}")
async def update_amba_blueprint_assignment(
    assignment_id: str,
    payload: BlueprintAssignmentRequest,
    principal: Principal = Depends(_amba_blueprint_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    try:
        assignment = planner.save_assignment(_tenant(principal), payload.model_dump(), actor=principal.subject, assignment_id=assignment_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(_audit(principal, "alerts_manager.amba_blueprint.assignment_updated", assignment_id, {"blueprint_id": assignment["blueprint_id"], "version": assignment["blueprint_version"]}))
    await db.commit()
    return {"assignment": assignment}


@router.delete("/amba-blueprint-assignments/{assignment_id}")
async def delete_amba_blueprint_assignment(
    assignment_id: str,
    principal: Principal = Depends(_amba_blueprint_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    try:
        deleted = planner.delete_assignment(_tenant(principal), assignment_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="AMBA blueprint assignment not found.")
    db.add(_audit(principal, "alerts_manager.amba_blueprint.assignment_deleted", assignment_id, {}))
    await db.commit()
    return {"deleted": True}


@router.post("/deployment-plans/preview")
async def preview_deployment_plan(
    payload: DeploymentPlanPreviewRequest,
    principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    try:
        assignment = planner.get_assignment(_tenant(principal), payload.assignment_id)
        if not assignment:
            raise ValueError("Enabled blueprint assignment not found.")
        live = await _planner_action_groups(assignment)
        plan = planner.preview_plan(
            _tenant(principal), payload.assignment_id, actor=principal.subject,
            common_action_group_id=payload.common_action_group_id,
            coverage_items=payload.coverage_items, live_action_groups=live,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(_audit(principal, "alerts_manager.deployment_plan.previewed", plan["id"], {"assignment_id": plan["assignment_id"], "counts": plan["counts"]}))
    await db.commit()
    return {"plan": plan}


@router.post("/deployment-plans/from-gaps/preview")
async def preview_gaps_deployment_plan(
    payload: GapsDeploymentPlanPreviewRequest,
    principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    context = {
        "connection_id": payload.connection_id,
        "workload_id": payload.workload_id or "",
        "subscription_id": payload.subscription_id or "",
        "management_group_id": payload.management_group_id or "",
        "environment": payload.environment,
        "monitoring_resource_group": payload.monitoring_resource_group,
    }
    scope_values = [value for value in (payload.workload_id, payload.subscription_id, payload.management_group_id) if value]
    if len(scope_values) != 1:
        raise HTTPException(status_code=422, detail="Exactly one workload, subscription, or management-group scope is required.")
    try:
        scope_kind = "workload" if payload.workload_id else "subscription" if payload.subscription_id else "management_group"
        live = await _planner_action_groups({**context, "scope_kind": scope_kind, "scope_id": scope_values[0]})
        active_changes = (
            await db.execute(
                select(AlertManagerChange).where(
                    AlertManagerChange.tenant_id == _tenant(principal),
                    AlertManagerChange.target_type == "metric_rule",
                    AlertManagerChange.status.in_(["pending", "approved"]),
                )
            )
        ).scalars().all()
        connection = _connection(payload.connection_id, payload.workload_id)
        validated_gaps = await _validate_selected_gap_metrics(connection, [gap.model_dump() for gap in payload.gaps])
        plan = planner.preview_gap_plan(
            _tenant(principal), context, validated_gaps,
            actor=principal.subject, common_action_group_id=payload.common_action_group_id,
            live_action_groups=live,
            pending_target_ids={str(change.target_id or "") for change in active_changes},
            active_gap_ids={
                str((change.summary_json or {}).get("source_gap_id") or "")
                for change in active_changes
                if (change.summary_json or {}).get("source_gap_id")
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(_audit(principal, "alerts_manager.deployment_plan.gaps_previewed", plan["id"], {
        "source_gap_count": len(plan["source_gap_ids"]), "counts": plan["counts"],
        "common_action_group_id": plan["common_action_group_id"],
    }))
    await db.commit()
    return {"plan": plan}


@router.get("/deployment-plans")
async def list_deployment_plans(
    status: str = Query(default=""),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    rows = planner.list_plans(_tenant(principal), status, compact=True)
    visible, effective_page, effective_size, paginated = _paged_rows(rows, page, page_size)
    return {
        "plans": visible, "count": len(rows), "total": len(rows),
        "page": effective_page, "page_size": effective_size, "paginated": paginated,
    }


@router.get("/deployment-plans/by-gap")
async def deployment_plans_by_gap(
    gap_ids: list[str] = Query(default=[]),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    requested = list(dict.fromkeys(value.strip() for value in gap_ids if value.strip()))
    if not requested or len(requested) > 5000 or any(len(value) > 1000 for value in requested):
        raise HTTPException(status_code=422, detail="Provide between 1 and 5000 valid gap_ids query values.")
    requested_set = set(requested)
    matching_plans = [
        plan for plan in planner.list_plans(_tenant(principal))
        if requested_set.intersection(str(value) for value in (plan.get("source_gap_ids") or []))
    ]
    change_ids = [str(value) for plan in matching_plans for value in (plan.get("change_ids") or [])]
    changes = (
        await db.execute(
            select(AlertManagerChange).where(
                AlertManagerChange.tenant_id == _tenant(principal), AlertManagerChange.id.in_(change_ids),
            )
        )
    ).scalars().all() if change_ids else []
    changes_by_id = {change.id: change for change in changes}
    priority = {"rejected": 1, "failed": 1, "stale": 1, "draft": 2, "pending": 3, "approved": 4, "applied": 5}
    results: dict[str, dict[str, Any]] = {}
    for plan in matching_plans:
        plan_changes = [changes_by_id[value] for value in plan.get("change_ids") or [] if value in changes_by_id]
        status = str(plan.get("status") or "draft")
        if plan_changes and all(change.status == "applied" for change in plan_changes):
            status = "applied"
        elif any(change.status == "pending" for change in plan_changes):
            status = "pending"
        elif any(change.status == "approved" for change in plan_changes):
            status = "approved"
        elif plan_changes and all(change.status == "rejected" for change in plan_changes):
            status = "rejected"
        elif any(change.status == "stale" for change in plan_changes):
            status = "stale"
        elif any(change.status == "failed" for change in plan_changes):
            status = "failed"
        for gap_id in set(str(value) for value in plan.get("source_gap_ids") or []) & requested_set:
            candidate = {
                "gap_id": gap_id, "status": status, "plan_id": plan["id"],
                "change_ids": list(plan.get("change_ids") or []), "updated_at": plan.get("updated_at", ""),
            }
            current = results.get(gap_id)
            active_statuses = {"pending", "approved"}
            candidate_is_active = status in active_statuses
            current_is_active = str((current or {}).get("status")) in active_statuses
            if (
                not current
                or (candidate_is_active and not current_is_active)
                or (candidate_is_active == current_is_active and str(candidate["updated_at"]) > str(current.get("updated_at") or ""))
                or (
                    candidate_is_active == current_is_active
                    and str(candidate["updated_at"]) == str(current.get("updated_at") or "")
                    and priority.get(status, 0) > priority.get(str(current.get("status")), 0)
                )
            ):
                results[gap_id] = candidate
    rows = [results.get(gap_id, {"gap_id": gap_id, "status": "none", "plan_id": "", "change_ids": [], "updated_at": ""}) for gap_id in requested]
    return {"gaps": rows, "by_gap": {row["gap_id"]: row for row in rows}}


@router.get("/deployment-plans/{plan_id}")
async def get_deployment_plan(plan_id: str, principal: Principal = Depends(_read)) -> dict[str, Any]:
    from app.alerts_manager import planner

    plan = planner.get_plan(_tenant(principal), plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Deployment plan not found.")
    return {"plan": plan}


@router.patch("/deployment-plans/{plan_id}/items")
async def update_deployment_plan_items(
    plan_id: str,
    payload: DeploymentPlanItemsRequest,
    principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    try:
        plan = planner.update_plan_items(
            _tenant(principal), plan_id,
            [item.model_dump() for item in payload.items], actor=principal.subject,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.add(_audit(principal, "alerts_manager.deployment_plan.items_updated", plan_id, {"selection_count": len(payload.items)}))
    await db.commit()
    return {"plan": plan}


@router.post("/deployment-plans/{plan_id}/validate")
async def validate_deployment_plan(
    plan_id: str,
    principal: Principal = Depends(_rule_write),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    try:
        plan = planner.get_plan(_tenant(principal), plan_id)
        if not plan:
            raise KeyError("Deployment plan not found.")
        assignment = planner.get_assignment(_tenant(principal), str(plan.get("assignment_id") or ""))
        live = await _planner_action_groups(assignment or plan)
        return planner.validate_plan(_tenant(principal), plan_id, actor=principal.subject, live_action_groups=live)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/deployment-plans/{plan_id}/submit")
async def submit_deployment_plan(
    plan_id: str,
    principal: Principal = Depends(_rule_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner, rules

    plan = planner.get_plan(_tenant(principal), plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Deployment plan not found.")
    if plan.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only a draft deployment plan can be submitted.")
    assignment = planner.get_assignment(_tenant(principal), str(plan.get("assignment_id") or ""))
    try:
        live = await _planner_action_groups(assignment or plan)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    validation = planner.validate_plan(
        _tenant(principal), plan_id, actor=principal.subject, live_action_groups=live,
    )
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail=validation)
    batch_id = str(uuid.uuid4())
    resolved_connection_id = _planner_connection_id(assignment or plan)
    changes: list[AlertManagerChange] = []
    for item in plan.get("items") or []:
        if not item.get("included"):
            continue
        proposal = item.get("proposal") or {}
        desired = proposal.get("desired") or {}
        body = proposal.get("body") or rules.build_rule_body("metric", desired)
        change = AlertManagerChange(
            tenant_id=_tenant(principal), connection_id=resolved_connection_id,
            target_type="metric_rule", target_id=str(proposal.get("target_id") or ""),
            operation="create", status="pending", risk="medium",
            summary_json={
                "reason": "Selected-gap remediation deployment plan" if plan.get("source") == "selected_gaps" else "AMBA blueprint deployment plan",
                "batch_id": batch_id, "deployment_plan_id": plan_id,
                "blueprint_id": plan.get("blueprint_id"), "blueprint_version": plan.get("blueprint_version"),
                "amba_version": plan.get("amba_version"), "classification": item.get("classification"),
                "source_gap_id": item.get("source_gap_id", ""),
                "desired": rules.summarize_rule_body("metric", body),
            },
            desired_encrypted=service.encrypted_json({"payload": desired, "body": body}),
            before_encrypted=service.encrypted_json({}), after_encrypted="", expected_state_hash="",
            requested_by=principal.subject, requested_at=service.now(), auto_apply=False,
        )
        changes.append(change)
        db.add(change)
    db.add(_audit(principal, "alerts_manager.deployment_plan.submitted", plan_id, {"batch_id": batch_id, "change_count": len(changes)}))
    await db.commit()
    for change in changes:
        await db.refresh(change)
    plan = planner.mark_plan_submitted(
        _tenant(principal), plan_id, actor=principal.subject, batch_id=batch_id,
        change_ids=[change.id for change in changes],
    )
    return {"plan": plan, "batch_id": batch_id, "changes": [_change_dict(change) for change in changes]}


@router.post("/deployment-plans/{plan_id}/decision")
async def decide_deployment_plan(
    plan_id: str,
    payload: ChangeDecisionRequest,
    principal: Principal = Depends(_approve),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager import planner

    plan = planner.get_plan(_tenant(principal), plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Deployment plan not found.")
    allowed_plan_states = {"pending", "approved"} if payload.decision == "rejected" else {"pending"}
    if plan.get("status") not in allowed_plan_states:
        raise HTTPException(status_code=409, detail="Only a pending plan can be approved; a pending or approved plan can be cancelled.")
    changes: list[AlertManagerChange] = []
    for change_id in plan.get("change_ids") or []:
        change = await db.get(AlertManagerChange, change_id)
        if not change or change.tenant_id != _tenant(principal):
            raise HTTPException(status_code=409, detail="One or more deployment-plan changes could not be found.")
        changes.append(change)
    assignment = planner.get_assignment(_tenant(principal), str(plan.get("assignment_id") or ""))
    resolved_connection_id = _planner_connection_id(assignment or plan)
    mismatched = [change.id for change in changes if change.connection_id != resolved_connection_id]
    if mismatched:
        raise HTTPException(
            status_code=409,
            detail="One or more deployment-plan changes do not belong to this plan's Azure connection.",
        )
    mutable_states = {"pending", "approved"} if payload.decision == "rejected" else {"pending"}
    changed = [change for change in changes if change.status in mutable_states]
    for change in changed:
        change.status = payload.decision
        change.decided_by = principal.subject
        change.decided_at = service.now()
        change.decision_reason = payload.reason
    state_counts: dict[str, int] = {}
    for change in changes:
        state_counts[change.status] = state_counts.get(change.status, 0) + 1
    db.add(_audit(principal, f"alerts_manager.deployment_plan.{payload.decision}", plan_id, {"batch_id": plan.get("batch_id"), "change_count": len(changes), "changed_count": len(changed), "child_states": state_counts}))
    await db.commit()
    plan = planner.mark_plan_decided(
        _tenant(principal), plan_id, actor=principal.subject,
        decision=payload.decision, reason=payload.reason, decision_summary={"changed_count": len(changed), "child_states": state_counts},
    )
    return {"plan": plan, "changes": [_change_dict(change) for change in changes]}


@router.get("/changes")
async def list_changes(
    connection_id: str = Query(default=""),
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    base_filters = [AlertManagerChange.tenant_id == _tenant(principal)]
    if connection_id:
        base_filters.append(AlertManagerChange.connection_id == connection_id)
    stmt = select(AlertManagerChange).where(*base_filters)
    if status:
        stmt = stmt.where(AlertManagerChange.status == status)
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await db.execute(count_stmt)).scalar_one())
    actionable_counts = dict((await db.execute(
        select(AlertManagerChange.status, func.count())
        .where(*base_filters, AlertManagerChange.status.in_(["pending", "approved"]))
        .group_by(AlertManagerChange.status)
    )).all())
    stmt = stmt.order_by(AlertManagerChange.requested_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    pending = int(actionable_counts.get("pending", 0))
    approved = int(actionable_counts.get("approved", 0))
    return {
        "changes": [_change_dict(row) for row in rows], "total": total, "page": page, "page_size": page_size,
        "pending_count": pending, "approved_count": approved, "actionable_count": pending + approved,
    }


@router.get("/changes/{change_id}/details")
async def managed_change_details(
    change_id: str,
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alerts_manager.activity_export import sanitize

    change = await db.get(AlertManagerChange, change_id)
    if not change or change.tenant_id != _tenant(principal):
        raise HTTPException(status_code=404, detail="Managed change not found.")
    desired = service.decrypted_json(change.desired_encrypted)
    before = service.decrypted_json(change.before_encrypted)
    body = desired.get("body") if isinstance(desired.get("body"), dict) else {}
    payload = desired.get("payload") if isinstance(desired.get("payload"), dict) else {}
    return {
        "change": _change_dict(change),
        "execution": {
            "operation": change.operation,
            "target_type": change.target_type,
            "target_id": change.target_id,
            "approval_required": change.status == "pending",
            "ready_to_apply": change.status == "approved",
            "azure_method": "DELETE" if change.operation == "delete" else "PUT",
            "expected_state_hash": change.expected_state_hash or "",
        },
        "before": sanitize(before),
        "desired_payload": sanitize(payload),
        "azure_body": sanitize(body),
        "redaction_notice": "Signed URL query strings and secret-bearing fields are redacted. Azure receives the encrypted stored values when Apply to Azure runs.",
    }


@router.post("/action-groups/changes")
async def request_action_group_change(
    payload: ActionGroupChangeRequest,
    principal: Principal = Depends(_ag_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    connection = _connection(payload.connection_id)
    try:
        service.assert_writable(connection)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    operation = payload.operation
    before: dict[str, Any] = {}
    target_id = payload.target_id.strip()
    desired = payload.desired
    if operation == "create":
        errors = service.validate_action_group_payload(desired, create=True)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        subscription = str(desired.get("subscription_id") or "").strip()
        group = str(desired.get("resource_group") or "").strip()
        name = str(desired.get("name") or "").strip()
        target_id = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.Insights/actionGroups/{name}"
        live, live_status, _error = await service.get_arm_resource(connection, target_id)
        if live:
            raise HTTPException(status_code=409, detail="An action group with this name already exists.")
        if live_status not in (0, 404):
            raise HTTPException(status_code=502, detail="Could not verify that the action group name is available.")
        clone_source: dict[str, Any] = {}
        source_id = payload.clone_source_id.strip()
        if source_id:
            if "/providers/microsoft.insights/actiongroups/" not in source_id.lower():
                raise HTTPException(status_code=422, detail="A valid source Action Group resource ID is required.")
            clone_source, source_status, source_error = await service.get_arm_resource(connection, source_id)
            if source_error or not clone_source:
                raise HTTPException(
                    status_code=404 if source_status == 404 else 502,
                    detail=source_error or "Source Action Group not found.",
                )
        body = service.build_action_group_body(desired, clone_source or None)
        expected_hash = ""
    else:
        if "/providers/microsoft.insights/actiongroups/" not in target_id.lower():
            raise HTTPException(status_code=422, detail="A valid Action Group resource ID is required.")
        before, live_status, error = await service.get_arm_resource(connection, target_id)
        if error or not before:
            raise HTTPException(status_code=404 if live_status == 404 else 502, detail=error or "Action group not found.")
        expected_hash = service.canonical_hash(service._resource_body(before))
        if operation == "delete":
            groups = await service.list_action_groups(
                connection, subscription_id=service._subscription_from_id(target_id)
            )
            match = next((item for item in groups if item["id"].lower() == target_id.lower()), None)
            if match and match["dependency_count"]:
                raise HTTPException(
                    status_code=409,
                    detail={"message": "Action group is still referenced and cannot be deleted.", "dependencies": match["dependencies"]},
                )
            body = {}
        else:
            merged = dict(service.editable_action_group(before))
            merged.update(desired)
            errors = service.validate_action_group_payload(merged, create=False)
            if errors:
                raise HTTPException(status_code=422, detail=errors)
            body = service.build_action_group_body(merged, before)
            desired = merged

    summary = {
        "reason": payload.reason,
        "before": service.summarize_action_group_body(service._resource_body(before)) if before else {},
        "desired": service.summarize_action_group_body(body) if body else {},
        "clone_source_id": payload.clone_source_id.strip() if operation == "create" else "",
        "clone_source_name": service._name_from_id(payload.clone_source_id) if operation == "create" and payload.clone_source_id.strip() else "",
    }
    risk = "critical" if operation == "delete" else "high" if summary["desired"].get("webhook_receivers") or summary["desired"].get("sms_receivers") else "medium"
    change = AlertManagerChange(
        tenant_id=_tenant(principal),
        connection_id=str(connection.get("id") or payload.connection_id),
        target_type="action_group",
        target_id=target_id,
        operation=operation,
        status="pending",
        risk=risk,
        summary_json=summary,
        desired_encrypted=service.encrypted_json({"payload": desired, "body": body}),
        before_encrypted=service.encrypted_json(before),
        after_encrypted="",
        expected_state_hash=expected_hash,
        requested_by=principal.subject,
        requested_at=service.now(),
        auto_apply=False,
    )
    db.add(change)
    db.add(_audit(principal, "alerts_manager.change.requested", target_id, {"change_id": change.id, "operation": operation, "risk": risk}))
    await db.commit()
    await db.refresh(change)
    return {"change": _change_dict(change)}


@router.post("/changes/{change_id}/decision")
async def decide_change(
    change_id: str,
    payload: ChangeDecisionRequest,
    principal: Principal = Depends(_approve),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    change = await db.get(AlertManagerChange, change_id)
    if not change or change.tenant_id != _tenant(principal):
        raise HTTPException(status_code=404, detail="Change request not found.")
    if change.status != "pending":
        raise HTTPException(status_code=409, detail="Only pending changes can be decided.")
    change.status = payload.decision
    change.decided_by = principal.subject
    change.decided_at = service.now()
    change.decision_reason = payload.reason
    db.add(_audit(principal, f"alerts_manager.change.{payload.decision}", change.target_id, {"change_id": change.id, "operation": change.operation}))
    await db.commit()
    return {"change": _change_dict(change)}


@router.post("/changes/{change_id}/apply")
async def apply_change(
    change_id: str,
    principal: Principal = Depends(_approve),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    change = await db.get(AlertManagerChange, change_id)
    if not change or change.tenant_id != _tenant(principal):
        raise HTTPException(status_code=404, detail="Change request not found.")
    desired = service.decrypted_json(change.desired_encrypted)
    desired_payload = desired.get("payload") if isinstance(desired.get("payload"), dict) else {}
    clone_source_id = str(desired_payload.get("clone_source_id") or (change.summary_json or {}).get("clone_source_id") or "")
    retrying_failed_clone = change.status == "failed" and change.target_type == "action_group" and change.operation == "create" and bool(clone_source_id)
    if change.status != "approved" and not retrying_failed_clone:
        raise HTTPException(status_code=409, detail="The change must be approved before it can be applied.")
    connection = _connection(change.connection_id)
    if change.target_type == "action_group" and change.operation == "delete":
        groups = await service.list_action_groups(
            connection, subscription_id=service._subscription_from_id(change.target_id)
        )
        match = next((item for item in groups if item["id"].lower() == change.target_id.lower()), None)
        if match and match["dependency_count"]:
            raise HTTPException(status_code=409, detail="Action group gained references after approval; delete was blocked.")
    if change.target_type == "action_group":
        data, status, error = await service.apply_action_group_change(connection, change)
    elif change.target_type in {"metric_rule", "log_rule", "activity_rule", "smart_rule", "prometheus_rule"}:
        from app.alerts_manager import rules

        data, status, error = await rules.apply_rule_change(connection, change)
    elif change.target_type == "activity_log_diagnostic_setting":
        from app.alerts_manager import activity_diagnostics

        data, status, error = await activity_diagnostics.apply_change(connection, change)
    else:
        raise HTTPException(status_code=422, detail="Unsupported managed change target.")
    change.applied_by = principal.subject
    change.applied_at = service.now()
    if error:
        change.status = "stale" if status == 409 else "failed"
        change.error_code = f"ARM_{status}" if status else "ARM_ERROR"
        change.error_message = service.safe_error(error)
        db.add(_audit(principal, "alerts_manager.change.failed", change.target_id, {"change_id": change.id, "operation": change.operation, "status": status}))
        await db.commit()
        raise HTTPException(status_code=409 if status == 409 else 502, detail=change.error_message)

    after = data or {}
    change.status = "applied"
    change.after_encrypted = service.encrypted_json(after)
    change.error_code = None
    change.error_message = None
    from app.evidence.registry import create_snapshot

    evidence = create_snapshot(
        tenant_id=_tenant(principal),
        name=f"Alerts Manager change — {change.target_id.rstrip('/').rsplit('/', 1)[-1]}",
        scope={"kind": "azure_resource", "id": change.target_id, "name": change.target_id.rstrip('/').rsplit('/', 1)[-1]},
        included=["changes"],
        retention_class="audit",
        tags=["alerts-manager", change.target_type.replace("_", "-"), change.operation],
        content={"changes": [{"change_id": change.id, "operation": change.operation, "risk": change.risk, "summary": change.summary_json}]},
        created_by=principal.subject,
    )
    change.evidence_id = evidence["id"]
    db.add(_audit(principal, "alerts_manager.change.applied", change.target_id, {"change_id": change.id, "operation": change.operation, "status": status, "evidence_id": evidence["id"]}))
    await db.commit()
    from app.alerts_manager import cache as inventory_cache

    affected = (
        {"action_groups"} if change.target_type == "action_group"
        else {"activity_log_diagnostic_settings"} if change.target_type == "activity_log_diagnostic_setting"
        else {"rules", "action_groups"}
    )
    await inventory_cache.invalidate(
        kinds=affected,
        connection_id=str(connection.get("id") or change.connection_id),
    )
    resource: dict[str, Any] | None = None
    if after:
        if change.target_type == "action_group":
            resource = service.editable_action_group(after)
        elif change.target_type in {"metric_rule", "log_rule", "activity_rule", "smart_rule", "prometheus_rule"}:
            from app.alerts_manager import rules

            resource = rules.editable_rule(after)
        elif change.target_type == "activity_log_diagnostic_setting":
            resource = after
    return {"change": _change_dict(change), "resource": resource}


@router.post("/changes/{change_id}/rollback")
async def rollback_change(
    change_id: str,
    principal: Principal = Depends(_delete),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    original = await db.get(AlertManagerChange, change_id)
    if not original or original.tenant_id != _tenant(principal):
        raise HTTPException(status_code=404, detail="Change request not found.")
    if original.status != "applied":
        raise HTTPException(status_code=409, detail="Only applied changes can be rolled back.")
    before = service.decrypted_json(original.before_encrypted)
    after = service.decrypted_json(original.after_encrypted)
    if original.operation == "create":
        operation, desired = "delete", {}
        expected = service.canonical_hash(service._resource_body(after)) if after else ""
    elif original.operation == "delete":
        operation, desired = "create", {"body": service._resource_body(before)}
        expected = ""
    else:
        operation, desired = "update", {"body": service._resource_body(before)}
        expected = service.canonical_hash(service._resource_body(after)) if after else ""
    rollback = AlertManagerChange(
        tenant_id=original.tenant_id,
        connection_id=original.connection_id,
        target_type=original.target_type,
        target_id=original.target_id,
        operation=operation,
        status="pending",
        risk="critical" if operation == "delete" else "high",
        summary_json={"reason": f"Rollback of {original.id}", "before": original.summary_json.get("desired", {}), "desired": original.summary_json.get("before", {})},
        desired_encrypted=service.encrypted_json(desired),
        before_encrypted=service.encrypted_json(after),
        after_encrypted="",
        expected_state_hash=expected,
        requested_by=principal.subject,
        requested_at=service.now(),
        rollback_of=original.id,
        auto_apply=False,
    )
    db.add(rollback)
    db.add(_audit(principal, "alerts_manager.rollback.requested", original.target_id, {"change_id": rollback.id, "rollback_of": original.id}))
    await db.commit()
    await db.refresh(rollback)
    return {"change": _change_dict(rollback)}


@router.post("/action-groups/test")
async def test_action_group(
    payload: ActionGroupTestRequest,
    principal: Principal = Depends(_test),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if payload.confirmation.strip() != "SEND TEST":
        raise HTTPException(status_code=422, detail="Type SEND TEST to confirm real notifications.")
    connection = _connection(payload.connection_id)
    try:
        result = await service.test_action_group(connection, payload.action_group_id, payload.alert_type)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    details = result.get("actionDetails") or []
    safe_details = [
        {
            "mechanism": str(item.get("MechanismType") or ""),
            "name": str(item.get("Name") or ""),
            "status": str(item.get("Status") or ""),
            "sub_state": str(item.get("SubState") or ""),
            "detail": service.safe_error(item.get("Detail")),
        }
        for item in details if isinstance(item, dict)
    ]
    from app.alerts_manager import delivery_history

    delivery_history.record(
        _tenant(principal), payload.action_group_id,
        {"state": result.get("state") or "Unknown", "actionDetails": safe_details}, actor=principal.subject,
    )
    db.add(_audit(principal, "alerts_manager.action_group.test", payload.action_group_id, {"alert_type": payload.alert_type, "state": result.get("state"), "receiver_count": len(safe_details)}))
    await db.commit()
    return {"state": result.get("state") or "Unknown", "details": safe_details}

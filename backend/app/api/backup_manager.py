"""Backup Manager API — protection inventory, job inbox, policies, vault posture, DR readiness,
cost, reporting, drills, and the approval-gated managed change ledger.

Scope follows the Alerts Manager contract (connection + workload / subscription / management
group).  Reads are cached and fail-soft; every write drafts a managed change and nothing
reaches Azure until an approver applies it.

Two whole classes of operation are absent by design and reported through ``/refusals`` so the
gap is explicit: **restores** and **destructive backup operations**.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backup_manager import (
    analysis as analysis_ops,
    changes as change_ops,
    cost as cost_ops,
    costmgmt,
    demo as demo_data,
    dr as dr_ops,
    drills as drill_ops,
    export as export_ops,
    fleet as fleet_store,
    gaps as gap_ops,
    inventory as inventory_ops,
    jobs as job_ops,
    policies as policy_ops,
    posture as posture_ops,
    pricing,
    reference,
    reports as report_ops,
    service,
    snapshot as snapshot_store,
)
from app.backup_manager.builtin_seed import PORTAL_ONLY_OPERATIONS
from app.core import coverage_runs
from app.core.azure_portal import portal_host
from app.core.db import SessionLocal, get_db
from app.core.genjob import JobRegistry, ProgressFn
from app.core.security import Principal, require_permission
from app.models import AuditLog, BackupDrill, BackupManagerChange

router = APIRouter(prefix="/backup-manager", tags=["backup-manager"])

log = logging.getLogger("app.api.backup_manager")

#: Detached refresh jobs, one per (tenant, connection, scope). Starting a refresh for a scope
#: that is already analyzing simply re-attaches to the running job, so a double click — or two
#: operators on the same scope — cannot launch two sweeps.
_refresh_jobs = JobRegistry("backup-manager-refresh")

#: Feature key for the shared run-history / cleanup store.
RUNS_FEATURE = "backup_manager"

#: A single analysis is nine Resource Graph sources per subscription plus vault, Cost
#: Management and Retail Prices calls. The job registry is idempotent per scope but has no
#: global cap, so a fleet launch (or a scripted client) could otherwise fan out dozens of
#: concurrent sweeps against one tenant. Jobs beyond the cap stay queued inside the runner and
#: report that they are waiting, rather than being rejected.
ANALYSIS_CONCURRENCY = 2
_analysis_slots = asyncio.Semaphore(ANALYSIS_CONCURRENCY)

_read = require_permission("backup_manager.read")
_protect_write = require_permission("backup_manager.protect_write")
_policy_write = require_permission("backup_manager.policy_write")
_vault_write = require_permission("backup_manager.vault_write")
_ondemand = require_permission("backup_manager.ondemand")
_drill_write = require_permission("backup_manager.drill_write")
_reference_write = require_permission("backup_manager.reference_write")
_approve = require_permission("backup_manager.approve")

MAX_PAGE_SIZE = 200
APPLY_CONCURRENCY = 6
WORKBOOK_CHANGE_LIMIT = 10_000


# --------------------------------------------------------------------------- request models
class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(default="", max_length=128)
    workload_id: str = Field(default="", max_length=128)
    subscription_id: str = Field(default="", max_length=64)
    management_group_id: str = Field(default="", max_length=128)


class ProtectionChangeRequest(ScopeBody):
    action: Literal["enable", "change_policy", "stop_retain_data", "resume"]
    target_id: str = Field(default="", max_length=1024)
    resource_id: str = Field(default="", max_length=1024)
    vault_id: str = Field(default="", max_length=1024)
    policy_id: str = Field(default="", max_length=1024)
    reason: str = Field(default="", max_length=1000)


class RemediationPreviewRequest(ScopeBody):
    gap_ids: list[str] = Field(default_factory=list, max_length=500)
    vault_id: str = Field(default="", max_length=1024)
    policy_id: str = Field(default="", max_length=1024)
    validate_datasources: bool = True


class RemediationSubmitRequest(RemediationPreviewRequest):
    reason: str = Field(default="", max_length=1000)


class AdhocBackupRequest(ScopeBody):
    instance_id: str = Field(min_length=1, max_length=1024)
    retain_until_days: int = Field(default=30, ge=1, le=99)
    reason: str = Field(default="", max_length=1000)


class JobCancelRequest(ScopeBody):
    job_id: str = Field(min_length=1, max_length=1024)
    reason: str = Field(default="", max_length=1000)


class VaultHardenRequest(ScopeBody):
    vault_id: str = Field(min_length=1, max_length=1024)
    controls: list[Literal["enable_soft_delete", "extend_soft_delete_retention", "enable_crr",
                           "set_redundancy", "enable_vault_alerts", "enable_diagnostics"]] = Field(min_length=1, max_length=10)
    soft_delete_retention_days: int = Field(default=14, ge=14, le=180)
    redundancy: Literal["GeoRedundant", "ZoneRedundant"] = "GeoRedundant"
    workspace_id: str = Field(default="", max_length=1024)
    reason: str = Field(default="", max_length=1000)


class RetentionImpactRequest(ScopeBody):
    policy_id: str = Field(min_length=1, max_length=1024)
    proposed_retention_days: int = Field(ge=1, le=36500)
    exact: bool = True


class TestFailoverRequest(ScopeBody):
    replicated_item_id: str = Field(default="", max_length=1024)
    recovery_plan_id: str = Field(default="", max_length=1024)
    network_type: Literal["NoNetwork", "ExistingNetwork"] = "NoNetwork"
    network_id: str = Field(default="", max_length=1024)
    recovery_point_id: str = Field(default="", max_length=1024)
    drill_id: str = Field(default="", max_length=64)
    reason: str = Field(default="", max_length=1000)


class CleanupRequest(ScopeBody):
    replicated_item_id: str = Field(default="", max_length=1024)
    recovery_plan_id: str = Field(default="", max_length=1024)
    comments: str = Field(default="", max_length=500)


class DrillCreateRequest(ScopeBody):
    name: str = Field(min_length=1, max_length=256)
    kind: Literal["restore", "test_failover"] = "restore"
    scope_kind: Literal["workload", "subscription", "resource"] = "workload"
    scope_id: str = Field(default="", max_length=256)
    target_id: str = Field(default="", max_length=1024)
    target_name: str = Field(default="", max_length=256)
    cadence_days: int = Field(default=180, ge=0, le=3650)


class DrillOutcomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed", "cancelled"]
    notes: str = Field(default="", max_length=4000)
    rto_minutes: int | None = Field(default=None, ge=0, le=100000)
    capture_evidence: bool = False


class ChangeDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=1000)


class ChangeSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(default="", max_length=128)
    change_ids: list[str] = Field(min_length=1, max_length=1000)


class BulkDecisionRequest(ChangeSelectionRequest):
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=1000)


class ReferenceSaveRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    reason: str = Field(default="", max_length=500)


class EvidenceRequest(ScopeBody):
    name: str = Field(default="", max_length=200)


# --------------------------------------------------------------------------- helpers
def _tenant(principal: Principal) -> str:
    return principal.tenant_id or "default"


def _connection(connection_id: str, workload_id: str | None = None) -> dict[str, Any]:
    try:
        return service.resolve_selected_connection(connection_id or None, workload_id or None)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _resolve_scope_for_request(
    connection: dict[str, Any], *, workload_id: str, subscription_id: str, management_group_id: str,
) -> dict[str, Any]:
    try:
        return await service.resolve_scope(
            connection, workload_id=workload_id or None,
            subscription_id=subscription_id or None,
            management_group_id=management_group_id or None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=service.safe_error(str(exc))) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _is_demo(workload_id: str | None) -> bool:
    from app.demo_catalog import is_demo_workload

    return bool(workload_id) and is_demo_workload(workload_id)


async def _estate(
    principal: Principal,
    *,
    connection_id: str,
    workload_id: str,
    subscription_id: str,
    management_group_id: str,
    force: bool = False,
    enrich: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(estate, connection)`` for a scope, serving demo data where applicable."""
    _scope_identity(workload_id, subscription_id, management_group_id)
    if _is_demo(workload_id):
        return demo_data.build_demo_estate(workload_id), {"id": "demo", "read_only": True, "display_name": "Demo"}
    connection = _connection(connection_id, workload_id)
    try:
        estate = await inventory_ops.collect_estate(
            connection, tenant_id=_tenant(principal), workload_id=workload_id or None,
            subscription_id=subscription_id or None, management_group_id=management_group_id or None,
            force=force,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=service.safe_error(str(exc))) from exc
    if enrich and estate.get("vaults"):
        await inventory_ops.enrich_vaults(connection, estate["vaults"])
    return estate, connection


def _paged(rows: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    size = max(1, min(int(page_size or 100), MAX_PAGE_SIZE))
    index = max(1, int(page or 1))
    start = (index - 1) * size
    window = rows[start:start + size]
    return {
        "rows": window,
        "page": index,
        "page_size": size,
        "total_count": len(rows),
        "has_more": start + size < len(rows),
    }


def _audit(principal: Principal, action: str, target: str, metadata: dict[str, Any]) -> AuditLog:
    return AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action=action, target=str(target or "")[:512], metadata_json=metadata,
    )


def _capabilities(
    connection: dict[str, Any], principal: Principal, *, scope_kind: str = "none",
) -> dict[str, Any]:
    has = lambda perm: principal.is_admin or principal.has(perm)  # noqa: E731 - local shorthand
    return {
        "connection_id": str(connection.get("id") or ""),
        "connection_name": str(connection.get("display_name") or "Azure connection"),
        "auth_method": str(connection.get("auth_method") or ""),
        "read_only": bool(connection.get("read_only", True)),
        "can_read": has("backup_manager.read"),
        "can_protect": has("backup_manager.protect_write"),
        "can_manage_policies": has("backup_manager.policy_write"),
        "can_manage_vaults": has("backup_manager.vault_write"),
        "can_ondemand": has("backup_manager.ondemand"),
        "can_drill": has("backup_manager.drill_write"),
        "can_edit_reference": has("backup_manager.reference_write"),
        "can_approve": has("backup_manager.approve"),
        # Structural refusals — surfaced so the UI explains the absence.
        "can_restore": False,
        "can_delete_backup_data": False,
        "portal_only_operations": PORTAL_ONLY_OPERATIONS,
        "portal_host": portal_host(connection),
        "scope_kind": scope_kind,
        "analysis_only_scope": scope_kind == "management_group",
    }


def _find(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any] | None:
    target = service.canonical_id(value)
    for row in rows:
        if service.canonical_id(row.get(key, "")) == target:
            return row
    return None


def _guard_write(connection: dict[str, Any]) -> None:
    if str(connection.get("id") or "") == "demo":
        raise HTTPException(status_code=400, detail="Demo mode is read-only; select a live Azure connection.")
    try:
        service.assert_writable(connection)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _guard_mutation_scope(body: ScopeBody) -> None:
    kind, _scope_id = _scope_identity(
        body.workload_id, body.subscription_id, body.management_group_id, required=False,
    )
    if kind == "management_group":
        raise HTTPException(
            status_code=400,
            detail="Management-group scope is analysis-only. Narrow to a workload or subscription to draft or apply changes.",
        )


# --------------------------------------------------------------------------- system
@router.get("/capabilities")
async def capabilities(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    subscription_id = subscription_id if isinstance(subscription_id, str) else ""
    management_group_id = management_group_id if isinstance(management_group_id, str) else ""
    scope_kind, _scope_id = _scope_identity(
        workload_id, subscription_id, management_group_id, required=False,
    )
    if _is_demo(workload_id):
        return {**_capabilities(
            {"id": "demo", "display_name": "Demo", "read_only": True}, principal,
            scope_kind=scope_kind,
        ), "demo": True}
    return _capabilities(_connection(connection_id, workload_id), principal, scope_kind=scope_kind)


@router.get("/refusals")
async def refusals(_principal: Principal = Depends(_read)) -> dict[str, Any]:
    """Operations Backup Manager deliberately does not implement, and why."""
    return {"operations": PORTAL_ONLY_OPERATIONS}


@router.get("/summary")
async def summary(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The overview scorecard: protection, jobs, RPO, posture, cost, actionable changes."""
    estate, connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
    )
    enriched_jobs = job_ops.enrich(estate.get("jobs", []))
    posture = posture_ops.build_posture(estate.get("vaults", []))
    rpo = dr_ops.rpo_attainment(estate.get("instances", []))
    readiness = dr_ops.build_readiness(estate)
    chronic = job_ops.chronic_failures(enriched_jobs, estate.get("instances", []))
    # The overview quotes list prices only: actual spend needs a Cost Management round-trip
    # that would make this endpoint slow, and the Cost tab shows it properly. The billing
    # currency is reused from cache when known so the two views cannot disagree.
    rate_card = cost_ops.reference_rate_card()
    if not estate.get("demo"):
        currency = (
            costmgmt.known_currency(connection, tenant_id=_tenant(principal))
            or str(reference.cost_rates().get("currency") or "USD")
        )
        try:
            live_card = await pricing.get_rate_card(_price_region(estate, connection), currency)
            if live_card.get("instance_meters"):
                rate_card = live_card
        except (ValueError, KeyError, TypeError):  # noqa: BLE001
            rate_card = cost_ops.reference_rate_card()
    cost = cost_ops.estimate(estate, rate_card=rate_card)
    waste = cost_ops.waste(estate, rate_card=rate_card)

    actionable = 0
    if str(connection.get("id") or "") != "demo":
        actionable = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(BackupManagerChange)
                    .where(
                        BackupManagerChange.tenant_id == _tenant(principal),
                        BackupManagerChange.connection_id == str(connection.get("id") or ""),
                        BackupManagerChange.status.in_(tuple(change_ops.ACTIONABLE_STATUSES)),
                    )
                )
            ).scalar_one()
            or 0
        )

    return {
        "generated_at": estate.get("generated_at"),
        "demo": bool(estate.get("demo")),
        "scope": estate.get("scope", {}),
        "errors": estate.get("errors", {}),
        "protection": {
            "vaults": len(estate.get("vaults", [])),
            "protected_items": len(estate.get("instances", [])),
            "stopped": sum(1 for i in estate.get("instances", []) if i.get("protection_stopped")),
            "orphaned": sum(1 for i in estate.get("instances", []) if i.get("orphaned")),
            "policies": len(estate.get("policies", [])),
        },
        "jobs": job_ops.summarize(enriched_jobs),
        "chronic_failures": len(chronic),
        "rpo": {
            "attainment_pct": rpo["attainment_pct"], "breached": rpo["breached"],
            "at_risk": rpo["at_risk"], "unknown": rpo["unknown"],
        },
        "posture": {
            "average_score": posture["average_score"], "band": posture["band"],
            "red_vaults": posture["red_vaults"], "actionable_count": posture["actionable_count"],
        },
        "dr": readiness["summary"],
        "cost": {
            "monthly_total": cost["monthly_total"], "currency": cost["currency"],
            "confidence": cost["confidence"], "recoverable_monthly": waste["recoverable_monthly"],
        },
        "actionable_changes": actionable,
        "job_window_days": estate.get("job_window_days"),
    }


@router.post("/refresh")
async def refresh(
    body: ScopeBody,
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, _connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id, force=True,
    )
    return {"ok": True, "generated_at": estate.get("generated_at"), "errors": estate.get("errors", {})}


# --------------------------------------------------------------------------- snapshot
def _scope_identity(
    workload_id: str, subscription_id: str, management_group_id: str, *, required: bool = True,
) -> tuple[str, str]:
    try:
        return service.scope_identity(
            workload_id, subscription_id, management_group_id, required=required,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _job_key(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> str:
    return "|".join((tenant_id or "default", connection_id or "default", scope_kind, scope_id))


def _job_response(job: dict[str, Any] | None) -> dict[str, Any]:
    public = _refresh_jobs.public_job(job)
    if not job or not public:
        return {"job": None, "progress": [], "result": None}
    return {
        "job": public,
        "progress": list(job.get("progress") or []),
        "result": job.get("result") if job.get("status") == "done" else None,
    }


async def _actionable_changes(db: AsyncSession, tenant_id: str, connection_id: str) -> int:
    if not connection_id or connection_id == "demo":
        return 0
    return int((await db.execute(
        select(func.count()).select_from(BackupManagerChange).where(
            BackupManagerChange.tenant_id == tenant_id,
            BackupManagerChange.connection_id == connection_id,
            BackupManagerChange.status.in_(tuple(change_ops.ACTIONABLE_STATUSES)),
        )
    )).scalar_one() or 0)


# ------------------------------------------------------------------- fleet + run history
def _workloads() -> list[dict[str, Any]]:
    from app.workloads.registry import list_workloads

    return list_workloads()


def _scope_name(scope_kind: str, scope_id: str, workload_id: str) -> str:
    if scope_kind == "workload":
        for workload in _workloads():
            if str(workload.get("id") or "") == workload_id:
                return str(workload.get("name") or workload_id)
    return scope_id


def _run_payload(snapshot: dict[str, Any], scope_name: str) -> dict[str, Any]:
    """The slice of an analysis worth keeping as history.

    Deliberately NOT the whole snapshot: inventory, jobs and gap rows are thousands of records
    each, and thirty of those per scope would turn a JSON history file into hundreds of
    megabytes. The headline sections are what a trend or an audit actually reads back."""
    return {
        "generated_at": snapshot.get("generated_at", ""),
        "scope": snapshot.get("scope", {}),
        "scope_name": scope_name,
        "demo": bool(snapshot.get("demo")),
        "partial": bool(snapshot.get("partial")),
        "errors": snapshot.get("errors", {}),
        "counts": snapshot.get("counts", {}),
        "summary": snapshot.get("summary", {}),
        "cost": {k: v for k, v in (snapshot.get("cost") or {}).items() if k != "rows"},
    }


def _record_analysis(
    principal: Principal, snapshot: dict[str, Any], *, tenant: str, connection_id: str,
    scope_kind: str, scope_id: str, workload_id: str,
) -> None:
    """Persist the two lightweight artifacts a finished analysis leaves behind.

    Both are best-effort: an analysis that succeeded must not be reported as failed because a
    summary file could not be written."""
    counts = snapshot.get("counts") or {}
    protected = int(counts.get("protected_items", 0) or 0)
    gaps = int(counts.get("gaps", 0) or 0)
    eligible = protected + gaps
    scope_name = _scope_name(scope_kind, scope_id, workload_id)
    try:
        if scope_kind == "workload" and workload_id:
            fleet_store.write_row(tenant, fleet_store.summarize(
                snapshot, workload_id=workload_id, connection_id=connection_id,
            ))
        coverage_runs.save_run(
            RUNS_FEATURE, tenant, scope_kind, scope_id, _run_payload(snapshot, scope_name),
            headline=round(protected * 100 / eligible) if eligible else None,
            counts=counts, resource_count=eligible, actor=principal.subject or "",
        )
    except (OSError, TypeError, ValueError) as exc:  # noqa: BLE001 - never fail a good analysis
        log.warning("backup_manager: could not record analysis history: %s", exc)


async def _demo_snapshot(workload_id: str) -> dict[str, Any]:
    """Demo estates are synthetic and instant, so they are composed on read.

    Making the operator click Analyze for data that costs nothing to produce would be
    friction without a purpose."""
    estate = demo_data.build_demo_estate(workload_id)
    enriched_jobs = job_ops.enrich(estate.get("jobs", []))
    gaps = demo_data.demo_gaps(workload_id)
    gaps["coverage_gaps"] = []
    gaps.setdefault("vaults", [])
    gaps.setdefault("policies", [])
    compliance = policy_ops.compliance(estate.get("instances", []), estate.get("policies", []))
    compliance["tiers"] = reference.load_reference().get("tiers", [])
    cost = await analysis_ops.build_cost(
        {"id": "demo"}, estate, tenant_id="demo", is_demo=True, use_reports=False, use_actuals=False,
    )
    snapshot = analysis_ops.compose(
        estate,
        enriched_jobs=enriched_jobs,
        posture=posture_ops.build_posture(estate.get("vaults", [])),
        policies=policy_ops.analyze(estate.get("policies", []), estate.get("instances", [])),
        compliance=compliance,
        gaps=gaps,
        readiness=dr_ops.build_readiness(estate),
        rpo=dr_ops.rpo_attainment(estate.get("instances", [])),
        cost=cost,
    )
    snapshot["schema_version"] = snapshot_store.SNAPSHOT_SCHEMA_VERSION
    snapshot["demo"] = True
    return snapshot


@router.get("/snapshot")
async def get_snapshot(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Every tab's data, exactly as the last analysis computed it.

    This endpoint never talks to Azure. If the scope has not been analyzed it returns an empty
    shell with ``report_exists: false``, which is what tells the UI to offer Analyze rather
    than silently starting a multi-minute sweep on page load."""
    scope_kind, scope_id = _scope_identity(workload_id, subscription_id, management_group_id)
    if _is_demo(workload_id):
        return await _demo_snapshot(workload_id)

    connection = _connection(connection_id, workload_id)
    tenant = _tenant(principal)
    stored = snapshot_store.read_snapshot(
        tenant, str(connection.get("id") or ""), scope_kind, scope_id,
    )
    if stored is None:
        return snapshot_store.empty_snapshot(scope_kind, scope_id)
    # The change ledger moves independently of an analysis, so it is layered on live rather
    # than served at whatever value it had when the snapshot was taken.
    summary = stored.get("summary")
    if isinstance(summary, dict):
        summary["actionable_changes"] = await _actionable_changes(
            db, tenant, str(connection.get("id") or ""),
        )
    stored["age_seconds"] = snapshot_store.age_seconds(stored)
    return stored


async def run_refresh_analysis(
    *,
    principal: Principal,
    connection_id: str = "",
    workload_id: str = "",
    subscription_id: str = "",
    management_group_id: str = "",
    progress: ProgressFn,
    resolved_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run and persist one Backup Manager analysis independently of an HTTP request."""
    scope_kind, scope_id = _scope_identity(workload_id, subscription_id, management_group_id)
    if _is_demo(workload_id):
        raise ValueError("Demo scopes are generated on read and need no analysis.")
    if scope_kind == "none":
        raise ValueError("Select a workload, subscription, or management group first.")
    connection = _connection(connection_id, workload_id)
    if scope_kind == "management_group":
        await _resolve_scope_for_request(
            connection, workload_id=workload_id, subscription_id=subscription_id,
            management_group_id=management_group_id,
        )
    tenant = _tenant(principal)
    effective_connection = str(connection.get("id") or "")

    await progress("start", "Starting a server-side backup estate analysis. It continues if you navigate away.")
    if scope_kind == "management_group":
        await progress("scope", f"Resolving management group {scope_id} and its descendant subscriptions…")
        if resolved_scope is None:
            resolved_scope = await _resolve_scope_for_request(
                connection, workload_id=workload_id, subscription_id=subscription_id,
                management_group_id=management_group_id,
            )
        await progress(
            "subscriptions",
            f"Management group resolved to {resolved_scope.get('subscription_count', 0):,} subscription(s) "
            f"across {(int(resolved_scope.get('subscription_count') or 0) + service.ARG_SUBSCRIPTION_BATCH - 1) // service.ARG_SUBSCRIPTION_BATCH:,} Resource Graph batch(es).",
        )
    if _analysis_slots.locked():
        await progress("start", f"Waiting for a free analysis slot ({ANALYSIS_CONCURRENCY} run at a time)…")
    async with _analysis_slots:
        lock = snapshot_store.get_lock(tenant, effective_connection, scope_kind, scope_id)
        async with lock:
            snapshot = await analysis_ops.build_snapshot(
                connection, tenant_id=tenant, scope_kind=scope_kind, scope_id=scope_id,
                workload_id=workload_id, subscription_id=subscription_id,
                management_group_id=management_group_id, progress=progress,
                resolved_scope=resolved_scope,
            )
            await progress("save", "Saving the analysis so every tab reads the same numbers…")
            snapshot_store.write_snapshot(tenant, effective_connection, scope_kind, scope_id, snapshot)
            async with SessionLocal() as db:
                db.add(_audit(principal, "backup_manager.analyze", f"{scope_kind}:{scope_id}", {
                    "counts": snapshot.get("counts", {}),
                    "errors": sorted((snapshot.get("errors") or {}).keys()),
                }))
                await db.commit()
                snapshot["summary"]["actionable_changes"] = await _actionable_changes(
                    db, tenant, effective_connection,
                )
            _record_analysis(
                principal, snapshot, tenant=tenant, connection_id=effective_connection,
                scope_kind=scope_kind, scope_id=scope_id, workload_id=workload_id,
            )
    counts = snapshot.get("counts", {})
    await progress(
        "done",
        f"Analysis complete — {counts.get('protected_items', 0):,} protected item(s) in "
        f"{counts.get('vaults', 0):,} vault(s), {counts.get('jobs', 0):,} job(s), "
        f"{counts.get('gaps', 0):,} gap(s)."
        + (f" Some sources failed: {', '.join(sorted((snapshot.get('errors') or {}).keys()))}."
           if snapshot.get("errors") else ""),
    )
    return snapshot


@router.post("/refresh/start")
async def refresh_start(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """Start a detached analysis for this scope, idempotently.

    The job outlives the request, so navigating away or closing the tab does not abandon a
    sweep that has already spent minutes of Azure calls. Scope failures are explicit: 400 for
    an invalid or conflicting scope, 403 for an unreadable hierarchy branch, 404 when the
    selected management group is not visible, and 409 when it has no visible subscriptions.
    """
    scope_kind, scope_id = _scope_identity(workload_id, subscription_id, management_group_id)
    if _is_demo(workload_id):
        raise HTTPException(status_code=400, detail="Demo scopes are generated on read and need no analysis.")
    if scope_kind == "none":
        raise HTTPException(status_code=400, detail="Select a workload, subscription, or management group first.")

    connection = _connection(connection_id, workload_id)
    resolved_scope: dict[str, Any] | None = None
    if scope_kind == "management_group":
        resolved_scope = await _resolve_scope_for_request(
            connection, workload_id=workload_id, subscription_id=subscription_id,
            management_group_id=management_group_id,
        )
    tenant = _tenant(principal)
    effective_connection = str(connection.get("id") or "")
    key = _job_key(tenant, effective_connection, scope_kind, scope_id)

    async def runner(progress: ProgressFn) -> dict[str, Any]:
        return await run_refresh_analysis(
            principal=principal,
            connection_id=connection_id,
            workload_id=workload_id,
            subscription_id=subscription_id,
            management_group_id=management_group_id,
            progress=progress,
            resolved_scope=resolved_scope,
        )

    return _job_response(_refresh_jobs.start(key, runner))


@router.get("/refresh/job")
async def refresh_job(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """The current or most recent analysis for this scope, with its replayable progress log."""
    scope_kind, scope_id = _scope_identity(workload_id, subscription_id, management_group_id)
    if _is_demo(workload_id) or scope_kind == "none":
        return {"job": None, "progress": [], "result": None}
    connection = _connection(connection_id, workload_id)
    key = _job_key(_tenant(principal), str(connection.get("id") or ""), scope_kind, scope_id)
    return _job_response(_refresh_jobs.get_job(key))


@router.get("/refresh/jobs")
async def refresh_jobs(principal: Principal = Depends(_read)) -> dict[str, Any]:
    """Every in-flight or recent analysis for this tenant, keyed ``connection|kind|scope``.

    The Fleet grid needs live status for dozens of rows at once; polling ``/refresh/job`` per
    row would be dozens of requests per second. One call answers the whole grid."""
    tenant = _tenant(principal)
    prefix = f"{tenant}|"
    jobs: dict[str, Any] = {}
    for job in _refresh_jobs.jobs_with_prefix(prefix):
        public = _refresh_jobs.public_job(job)
        if not public:
            continue
        parts = str(public.get("key") or "").split("|")
        if len(parts) != 4:
            continue
        jobs["|".join(parts[1:])] = {
            "id": public["id"],
            "status": public["status"],
            "started_at": public["started_at"],
            "finished_at": public["finished_at"],
            "progress_count": public["progress_count"],
            "last_message": public["last_message"],
            "error": public["error"],
        }
    return {"jobs": jobs, "concurrency": ANALYSIS_CONCURRENCY}


# --------------------------------------------------------------------------- fleet
@router.get("/fleet")
async def fleet(principal: Principal = Depends(_read)) -> dict[str, Any]:
    """Latest analysis headline for every workload; never touches Azure.

    Rows come from the fleet summary store rather than the snapshot document, so they survive
    the snapshot store's scope cap and load instantly however large the estate is."""
    tenant = _tenant(principal)
    rows_by_key = fleet_store.read_rows(tenant)
    out: list[dict[str, Any]] = []
    for workload in _workloads():
        workload_id = str(workload.get("id") or "")
        connection_id = str(workload.get("connection_id") or "")
        row_key = fleet_store.key(connection_id, workload_id)
        row = rows_by_key.get(row_key) or {}
        if not row:
            # Backfill: a scope analyzed before this grid existed (or by another operator whose
            # summary row was purged) still has its stored snapshot. Derive the row from it once
            # and keep it, so the fleet is accurate the first time it is opened rather than
            # claiming a workload was never analyzed.
            stored = snapshot_store.read_snapshot(tenant, connection_id, "workload", workload_id)
            if stored:
                row = fleet_store.write_row(tenant, fleet_store.summarize(
                    stored, workload_id=workload_id, connection_id=connection_id,
                ))
        age = fleet_store.age_seconds(row) if row else None
        out.append({
            "workload_id": workload_id,
            "name": str(workload.get("name") or workload_id),
            "connection_id": connection_id,
            "criticality": str(workload.get("criticality") or ""),
            "environment": str(workload.get("environment") or ""),
            "demo": bool(row.get("demo") or _is_demo(workload_id)),
            "has_analysis": bool(row),
            "run_at": row.get("run_at", ""),
            "age_seconds": age,
            "partial": bool(row.get("partial")),
            "errors": row.get("errors", []),
            "vaults": row.get("vaults", 0),
            "protected_items": row.get("protected_items", 0),
            "stopped": row.get("stopped", 0),
            "orphaned": row.get("orphaned", 0),
            "policies": row.get("policies", 0),
            "gaps": row.get("gaps", 0),
            "pct_protected": row.get("pct_protected"),
            "failed_jobs": row.get("failed_jobs", 0),
            "chronic_failures": row.get("chronic_failures", 0),
            "rpo_attainment_pct": row.get("rpo_attainment_pct"),
            "rpo_breached": row.get("rpo_breached", 0),
            "posture_score": row.get("posture_score", 0),
            "posture_band": row.get("posture_band", ""),
            "red_vaults": row.get("red_vaults", 0),
            "vault_actions": row.get("vault_actions", 0),
            "dr_replicated": row.get("dr_replicated", 0),
            "dr_unhealthy": row.get("dr_unhealthy", 0),
            "monthly_cost": row.get("monthly_cost", 0.0),
            "recoverable_monthly": row.get("recoverable_monthly", 0.0),
            "currency": row.get("currency", ""),
            "cost_confidence": row.get("cost_confidence", ""),
        })
    # Worst first: never analyzed, then most gaps, then lowest protection, then most failures.
    out.sort(key=lambda r: (
        bool(r["has_analysis"]),
        -int(r["gaps"] or 0),
        r["pct_protected"] if r["pct_protected"] is not None else 999,
        -int(r["failed_jobs"] or 0),
        str(r["name"]).lower(),
    ))
    return {
        "workloads": out,
        "total": len(out),
        "analyzed": sum(1 for r in out if r["has_analysis"]),
        "concurrency": ANALYSIS_CONCURRENCY,
    }


# --------------------------------------------------------------------------- cleanup
class _CleanupIds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(default_factory=list, max_length=2000)


class _SnapshotKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(default_factory=list, max_length=200)


@router.get("/cleanup")
async def cleanup_list(principal: Principal = Depends(_read)) -> dict[str, Any]:
    """Saved analysis history across every scope (active + trashed) with sizes."""
    tenant = _tenant(principal)
    return {
        "runs": coverage_runs.list_all_runs(RUNS_FEATURE, tenant),
        "stats": coverage_runs.cleanup_stats(RUNS_FEATURE, tenant),
    }


@router.post("/cleanup/trash")
async def cleanup_trash(
    body: _CleanupIds, principal: Principal = Depends(_read), db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    result = coverage_runs.trash_runs(RUNS_FEATURE, _tenant(principal), body.ids)
    db.add(_audit(principal, "backup_manager.cleanup.trash", "runs", result))
    await db.commit()
    return result


@router.post("/cleanup/restore")
async def cleanup_restore(
    body: _CleanupIds, principal: Principal = Depends(_read), db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    result = coverage_runs.restore_runs(RUNS_FEATURE, _tenant(principal), body.ids)
    db.add(_audit(principal, "backup_manager.cleanup.restore", "runs", result))
    await db.commit()
    return result


@router.post("/cleanup/purge")
async def cleanup_purge(
    body: _CleanupIds, principal: Principal = Depends(_approve), db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Permanent deletion of run history \u2014 approver-gated and audited."""
    result = coverage_runs.purge_runs(RUNS_FEATURE, _tenant(principal), body.ids)
    db.add(_audit(principal, "backup_manager.cleanup.purge", "runs", result))
    await db.commit()
    return result


@router.get("/cleanup/snapshots")
async def cleanup_snapshots(principal: Principal = Depends(_read)) -> dict[str, Any]:
    """Stored analyses (the heavy documents every tab reads) with size, age and orphan state.

    A snapshot is orphaned when the workload it was taken for no longer exists, or its Azure
    connection has been removed \u2014 it can never be opened again, so it is pure dead weight
    against the store's scope cap."""
    from app.core.azure_connections import list_connections

    tenant = _tenant(principal)
    workload_ids = {str(w.get("id") or "").lower() for w in _workloads()}
    workload_names = {str(w.get("id") or "").lower(): str(w.get("name") or "") for w in _workloads()}
    connection_ids = {str(c.get("id") or "") for c in list_connections()}
    rows: list[dict[str, Any]] = []
    for row in snapshot_store.list_scopes(tenant):
        reasons: list[str] = []
        if row["scope_kind"] == "workload" and row["scope_id"] not in workload_ids:
            reasons.append("workload deleted")
        if row["connection_id"] and row["connection_id"] not in connection_ids:
            reasons.append("connection removed")
        if row["schema_stale"]:
            reasons.append("stale schema")
        rows.append({
            **row,
            "scope_name": (
                workload_names.get(row["scope_id"], row["scope_id"])
                if row["scope_kind"] == "workload"
                else row.get("scope_name") or row["scope_id"]
            ),
            "orphan_reasons": reasons,
            "orphan": bool(reasons),
        })
    return {
        "snapshots": rows,
        "stats": {
            "count": len(rows),
            "total_bytes": sum(r["size_bytes"] for r in rows),
            "orphans": sum(1 for r in rows if r["orphan"]),
            "orphan_bytes": sum(r["size_bytes"] for r in rows if r["orphan"]),
            "max_scopes": snapshot_store.MAX_SCOPES,
        },
    }


@router.post("/cleanup/snapshots/purge")
async def cleanup_snapshots_purge(
    body: _SnapshotKeys, principal: Principal = Depends(_approve), db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Drop stored analyses by store key, plus the fleet rows that pointed at them.

    Nothing here is irreversible in the Azure sense \u2014 a purged scope simply has to be
    analyzed again \u2014 but it is still an explicit, audited operator action."""
    tenant = _tenant(principal)
    prefix = f"{tenant}|"
    keys = [k for k in body.keys if k.startswith(prefix)]
    result = snapshot_store.delete_keys(keys)
    fleet_keys: list[str] = []
    for stored_key in keys:
        parts = stored_key.split("|")
        if len(parts) == 4 and parts[2] == "workload":
            fleet_keys.append(fleet_store.key(parts[1] if parts[1] != "default" else "", parts[3]))
    result["fleet_rows"] = fleet_store.delete_rows(tenant, fleet_keys)
    db.add(_audit(principal, "backup_manager.cleanup.snapshots", f"{len(keys)} scope(s)", result))
    await db.commit()
    return result


# --------------------------------------------------------------------------- inventory
@router.get("/inventory")
async def inventory(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    search: str = Query(default="", max_length=200),
    vault_id: str = Query(default="", max_length=1024),
    state: str = Query(default="", max_length=40),
    datasource_type: str = Query(default="", max_length=120),
    only_issues: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, _connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    rows = estate.get("instances", [])
    needle = search.strip().lower()
    if needle:
        rows = [
            r for r in rows
            if needle in (r.get("friendly_name") or "").lower()
            or needle in (r.get("datasource_id") or "")
            or needle in (r.get("policy_name") or "").lower()
        ]
    if vault_id:
        rows = [r for r in rows if service.canonical_id(r.get("vault_id", "")) == service.canonical_id(vault_id)]
    if state:
        rows = [r for r in rows if (r.get("protection_state") or "").lower() == state.lower()]
    if datasource_type:
        rows = [r for r in rows if (r.get("datasource_type") or "").lower() == datasource_type.lower()]
    if only_issues:
        rows = [
            r for r in rows
            if r.get("orphaned") or r.get("protection_stopped") or r.get("last_error_code")
            or (r.get("last_backup_status") or "").lower() in ("failed", "unhealthy")
        ]
    facets = {
        "datasource_types": sorted({r.get("datasource_type", "") for r in estate.get("instances", []) if r.get("datasource_type")}),
        "states": sorted({r.get("protection_state", "") for r in estate.get("instances", []) if r.get("protection_state")}),
        "vaults": [
            {"id": v["id"], "name": v["name"], "kind": v["kind"], "count": v.get("instance_count", 0)}
            for v in estate.get("vaults", [])
        ],
    }
    return {**_paged(rows, page, page_size), "facets": facets, "errors": estate.get("errors", {}),
            "demo": bool(estate.get("demo")), "generated_at": estate.get("generated_at")}


@router.get("/vaults")
async def vaults(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, _connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
    )
    return {
        "vaults": estate.get("vaults", []),
        "capacity": posture_ops.capacity(estate.get("vaults", [])),
        "errors": estate.get("errors", {}),
        "demo": bool(estate.get("demo")),
    }


@router.get("/posture")
async def posture(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, _connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
    )
    result = posture_ops.build_posture(estate.get("vaults", []))
    result["generated_at"] = estate.get("generated_at")
    result["demo"] = bool(estate.get("demo"))
    result["capacity"] = posture_ops.capacity(estate.get("vaults", []))
    return result


# --------------------------------------------------------------------------- jobs
@router.get("/jobs")
async def list_jobs(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    status: str = Query(default="", max_length=24),
    search: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, _connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    rows = job_ops.enrich(estate.get("jobs", []))
    if status:
        rows = [r for r in rows if r.get("status_bucket") == status.lower()]
    needle = search.strip().lower()
    if needle:
        rows = [
            r for r in rows
            if needle in (r.get("entity_name") or "").lower()
            or needle in (r.get("error_code") or "").lower()
            or needle in (r.get("operation") or "").lower()
        ]
    return {
        **_paged(rows, page, page_size),
        "summary": job_ops.summarize(job_ops.enrich(estate.get("jobs", []))),
        "job_window_days": estate.get("job_window_days"),
        "errors": estate.get("errors", {}),
        "demo": bool(estate.get("demo")),
    }


@router.get("/jobs/analysis")
async def job_analysis(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """Failure clusters, chronic-failure list, and backup-window congestion in one call."""
    estate, _connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    enriched = job_ops.enrich(estate.get("jobs", []))
    return {
        "clusters": job_ops.cluster_failures(enriched),
        "chronic": job_ops.chronic_failures(enriched, estate.get("instances", [])),
        "congestion": job_ops.congestion(enriched),
        "summary": job_ops.summarize(enriched),
        "job_window_days": estate.get("job_window_days"),
        "demo": bool(estate.get("demo")),
    }


# --------------------------------------------------------------------------- policies
@router.get("/policies")
async def list_policies(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, _connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    result = policy_ops.analyze(estate.get("policies", []), estate.get("instances", []))
    result["demo"] = bool(estate.get("demo"))
    return result


@router.post("/policies/retention-impact")
async def retention_impact(
    body: RetentionImpactRequest,
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    _guard_mutation_scope(body)
    estate, connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id, enrich=False,
    )
    policy = _find(estate.get("policies", []), "id", body.policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found in this scope.")
    if str(connection.get("id") or "") == "demo":
        return await policy_ops.retention_impact(
            connection, policy, estate.get("instances", []),
            proposed_retention_days=body.proposed_retention_days, exact=False,
        )
    return await policy_ops.retention_impact(
        connection, policy, estate.get("instances", []),
        proposed_retention_days=body.proposed_retention_days, exact=body.exact,
    )


# --------------------------------------------------------------------------- gaps
@router.get("/gaps")
async def list_gaps(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    include_coverage: bool = Query(default=True),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    if _is_demo(workload_id):
        result = demo_data.demo_gaps(workload_id)
        result["coverage_gaps"] = []
        return result
    estate, connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    subscriptions = set(estate.get("scope", {}).get("subscriptions") or [])
    result = await gap_ops.detect(connection, estate, subscriptions=subscriptions)
    coverage: list[dict[str, Any]] = []
    coverage_status: dict[str, Any] = {}
    if include_coverage:
        scope_kind, scope_id = _scope_identity(workload_id, subscription_id, management_group_id)
        if scope_id:
            coverage, coverage_status = gap_ops.ingest_coverage_gaps_for_scope(
                _tenant(principal), scope_kind, scope_id, sorted(subscriptions),
            )
    result["coverage_gaps"] = coverage
    result["coverage_status"] = coverage_status
    result["vaults"] = [
        {"id": v["id"], "name": v["name"], "kind": v["kind"], "location": v["location"],
         "subscription_id": v["subscription_id"], "redundancy": v.get("redundancy", "")}
        for v in estate.get("vaults", [])
    ]
    result["policies"] = [
        {"id": p["id"], "arm_id": p.get("arm_id", ""), "name": p["name"], "vault_id": p["vault_id"],
         "vault_kind": p["vault_kind"], "backup_management_type": p.get("backup_management_type", ""),
         "retention_days": p.get("retention_days")}
        for p in estate.get("policies", [])
    ]
    return result


async def _build_remediation(
    principal: Principal, body: RemediationPreviewRequest,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    _guard_mutation_scope(body)
    estate, connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id, enrich=False,
    )
    _guard_write(connection)
    subscriptions = set(estate.get("scope", {}).get("subscriptions") or [])
    detected = await gap_ops.detect(connection, estate, subscriptions=subscriptions)
    wanted = set(body.gap_ids)
    selected = [g for g in detected["gaps"] if g["gap_id"] in wanted] if wanted else []
    if not selected:
        raise HTTPException(status_code=400, detail="No matching open gaps were selected.")
    vault = _find(estate.get("vaults", []), "id", body.vault_id)
    if vault is None:
        raise HTTPException(status_code=400, detail="Select a target vault visible in this scope.")
    policy = _find(estate.get("policies", []), "id", body.policy_id) or _find(
        estate.get("policies", []), "arm_id", body.policy_id
    )
    if policy is None:
        raise HTTPException(status_code=400, detail="Select a backup policy that belongs to the target vault.")

    items = [gap_ops.plan_item(gap, vault, policy) for gap in selected]
    if body.validate_datasources:
        needs_validation = [i for i in items if i.get("requires_validation") and i.get("status") == "ready"]
        if needs_validation:
            results = await service.bounded_gather(
                [lambda i=i: gap_ops.validate_dataprotection_item(connection, i) for i in needs_validation], limit=4,
            )
            validated = {
                id(original): result
                for original, result in zip(needs_validation, service.unwrap(results))
                if isinstance(result, dict)
            }
            items = [validated.get(id(i), i) for i in items]
    return items, estate, connection


@router.post("/remediation/preview")
async def remediation_preview(
    body: RemediationPreviewRequest,
    principal: Principal = Depends(_protect_write),
) -> dict[str, Any]:
    items, _estate, _connection = await _build_remediation(principal, body)
    ready = [i for i in items if i["status"] == "ready"]
    blocked = [i for i in items if i["status"] != "ready"]
    return {
        "items": [{k: v for k, v in item.items() if k != "body"} for item in items],
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "blocked": blocked,
    }


@router.post("/remediation/submit")
async def remediation_submit(
    body: RemediationSubmitRequest,
    principal: Principal = Depends(_protect_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    items, _estate, connection = await _build_remediation(principal, body)
    ready = [i for i in items if i["status"] == "ready"]
    if not ready:
        raise HTTPException(status_code=400, detail="No selected gap can be remediated; review the blockers first.")
    plan_id = service.canonical_hash({"gaps": sorted(i["gap_id"] for i in ready), "at": service.now_iso()})[:32]
    rows: list[BackupManagerChange] = []
    for item in ready:
        row = change_ops.build_change(
            tenant_id=_tenant(principal),
            connection_id=str(connection.get("id") or ""),
            target_type="protection",
            target_id=item["target_id"],
            operation="create",
            requested_by=principal.subject,
            desired={"body": item["body"]},
            summary={**change_ops.summary_for_protection(item), "reason": body.reason},
            plan_id=plan_id,
        )
        rows.append(row)
        db.add(row)
    db.add(_audit(principal, "backup_manager.remediation.submit", plan_id, {
        "count": len(rows), "vault_id": body.vault_id, "policy_id": body.policy_id,
    }))
    await db.commit()
    return {
        "ok": True, "plan_id": plan_id, "created": len(rows),
        "changes": [change_ops.public_change(r) for r in rows],
    }


# --------------------------------------------------------------------------- protection actions
@router.post("/protection/changes")
async def protection_change(
    body: ProtectionChangeRequest,
    principal: Principal = Depends(_protect_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Draft an enable / change-policy / resume / stop-with-data-retained change.

    Deleting backup data is not reachable: the request model has no such action and the apply
    handler refuses any stop mode other than retain-data."""
    _guard_mutation_scope(body)
    estate, connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id, enrich=False,
    )
    _guard_write(connection)

    if body.action == "enable":
        gap = {
            "gap_id": "", "resource_id": body.resource_id,
            "resource_name": service.name_from_id(body.resource_id),
            "resource_type": _type_from_id(body.resource_id),
            "display_type": "", "location": "",
            "subscription_id": service.subscription_from_id(body.resource_id),
            "resource_group": service.resource_group_from_id(body.resource_id),
        }
        spec = gap_ops.ELIGIBLE_TYPES.get(gap["resource_type"])
        if not spec:
            raise HTTPException(status_code=400, detail="Backup Manager cannot enrol this resource type in a vault.")
        gap["display_type"] = spec["display"]
        vault = _find(estate.get("vaults", []), "id", body.vault_id)
        policy = _find(estate.get("policies", []), "id", body.policy_id) or _find(
            estate.get("policies", []), "arm_id", body.policy_id
        )
        if vault is None or policy is None:
            raise HTTPException(status_code=400, detail="A visible target vault and policy are required.")
        gap["location"] = vault.get("location", "")
        item = gap_ops.plan_item(gap, vault, policy)
        if item["status"] != "ready":
            raise HTTPException(status_code=400, detail=item.get("reason") or "This protection request is not valid.")
        row = change_ops.build_change(
            tenant_id=_tenant(principal), connection_id=str(connection.get("id") or ""),
            target_type="protection", target_id=item["target_id"], operation="create",
            requested_by=principal.subject, desired={"body": item["body"]},
            summary={**change_ops.summary_for_protection(item), "reason": body.reason},
        )
    else:
        instance = _find(estate.get("instances", []), "id", body.target_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="Protected item not found in this scope.")
        mechanism = "dataprotection" if instance["vault_kind"] == "backup" else "rsv_vm"
        api_version = service.DP_API if mechanism == "dataprotection" else service.RSV_BACKUP_API
        live, _status, _error = await service.arm_get(connection, instance["id"], api_version)
        before = live or {}
        common = {
            "mechanism": mechanism, "api_version": api_version, "reason": body.reason,
            "instance_name": instance.get("friendly_name", ""), "vault_name": instance.get("vault_name", ""),
            "resource_id": instance.get("datasource_id", ""),
        }
        if body.action == "stop_retain_data":
            desired_body = change_ops._stop_protection_body({"mechanism": mechanism}, before)
            row = change_ops.build_change(
                tenant_id=_tenant(principal), connection_id=str(connection.get("id") or ""),
                target_type="protection", target_id=instance["id"], operation="delete",
                requested_by=principal.subject, desired={"body": desired_body}, before=before,
                summary={**common, "kind": "stop_protection", "stop_mode": change_ops.STOP_PROTECTION_RETAIN,
                         "description": f"Stop protection for {instance.get('friendly_name')} and retain existing recovery points"},
                risk="medium",
            )
        else:
            policy_id = body.policy_id or instance.get("policy_id", "")
            policy = _find(estate.get("policies", []), "id", policy_id) or _find(
                estate.get("policies", []), "arm_id", policy_id
            )
            if policy is None:
                raise HTTPException(status_code=400, detail="Select a backup policy that belongs to this item's vault.")
            policy_arm_id = str(policy.get("arm_id") or policy.get("id") or "")
            if mechanism == "dataprotection":
                desired_body = {"properties": {**service.as_dict(before.get("properties")),
                                               "policyInfo": {"policyId": policy_arm_id}}}
            else:
                props = service.as_dict(before.get("properties"))
                desired_body = {"properties": {
                    "protectedItemType": str(props.get("protectedItemType") or "Microsoft.Compute/virtualMachines"),
                    "sourceResourceId": str(props.get("sourceResourceId") or instance.get("datasource_id") or ""),
                    "policyId": policy_arm_id,
                }}
            intent = "resume" if body.action == "resume" else "change_policy"
            row = change_ops.build_change(
                tenant_id=_tenant(principal), connection_id=str(connection.get("id") or ""),
                target_type="protection", target_id=instance["id"], operation="update",
                requested_by=principal.subject, desired={"body": desired_body}, before=before,
                summary={**common, "kind": intent, "intent": intent, "policy_id": policy_arm_id,
                         "policy_name": policy.get("name", ""),
                         "description": (
                             f"Resume protection for {instance.get('friendly_name')}"
                             if intent == "resume"
                             else f"Move {instance.get('friendly_name')} to policy {policy.get('name')}"
                         )},
            )
    db.add(row)
    db.add(_audit(principal, f"backup_manager.protection.{body.action}", row.target_id, {"change_id": row.id}))
    await db.commit()
    return {"ok": True, "change": change_ops.public_change(row)}


def _type_from_id(resource_id: str) -> str:
    parts = str(resource_id or "").strip("/").split("/")
    lower = [p.lower() for p in parts]
    try:
        index = lower.index("providers")
    except ValueError:
        return ""
    tail = parts[index + 1:]
    if len(tail) < 2:
        return ""
    segments = [tail[0]] + [tail[i] for i in range(1, len(tail), 2)]
    return "/".join(segments).lower()


@router.post("/backup-now")
async def backup_now(
    body: AdhocBackupRequest,
    principal: Principal = Depends(_ondemand),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _guard_mutation_scope(body)
    estate, connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id, enrich=False,
    )
    _guard_write(connection)
    instance = _find(estate.get("instances", []), "id", body.instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Protected item not found in this scope.")
    mechanism = "dataprotection" if instance["vault_kind"] == "backup" else "rsv_vm"
    if mechanism == "dataprotection":
        request_body: dict[str, Any] = {"backupRuleOptions": {"ruleName": "BackupDaily",
                                                              "triggerOption": {"retentionTagOverride": "Default"}}}
    else:
        expiry = (service.now() + __import__("datetime").timedelta(days=body.retain_until_days)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        request_body = {"properties": {"objectType": "IaasVMBackupRequest", "recoveryPointExpiryTimeInUTC": expiry}}
    row = change_ops.build_change(
        tenant_id=_tenant(principal), connection_id=str(connection.get("id") or ""),
        target_type="adhoc_backup", target_id=instance["id"], operation="invoke",
        requested_by=principal.subject, desired={"body": request_body},
        summary={
            "kind": "adhoc_backup", "mechanism": mechanism,
            "instance_name": instance.get("friendly_name", ""), "vault_name": instance.get("vault_name", ""),
            "retain_until_days": body.retain_until_days, "reason": body.reason,
            "description": f"Run an on-demand backup of {instance.get('friendly_name')}",
        },
    )
    db.add(row)
    db.add(_audit(principal, "backup_manager.adhoc_backup", row.target_id, {"change_id": row.id}))
    await db.commit()
    return {"ok": True, "change": change_ops.public_change(row)}


@router.post("/jobs/cancel")
async def cancel_job(
    body: JobCancelRequest,
    principal: Principal = Depends(_ondemand),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _guard_mutation_scope(body)
    estate, connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id, enrich=False,
    )
    _guard_write(connection)
    job = _find(estate.get("jobs", []), "id", body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found in this scope.")
    if job.get("vault_kind") != "recovery_services":
        raise HTTPException(status_code=400, detail="Only Recovery Services vault jobs can be cancelled here.")
    if job.get("status_bucket") != "running":
        raise HTTPException(status_code=400, detail="Only a running job can be cancelled.")
    row = change_ops.build_change(
        tenant_id=_tenant(principal), connection_id=str(connection.get("id") or ""),
        target_type="job_cancel", target_id=job["id"], operation="invoke",
        requested_by=principal.subject, desired={"body": {}},
        summary={"kind": "job_cancel", "entity_name": job.get("entity_name", ""),
                 "operation_name": job.get("operation", ""), "reason": body.reason,
                 "description": f"Cancel the running {job.get('operation')} job for {job.get('entity_name')}"},
    )
    db.add(row)
    db.add(_audit(principal, "backup_manager.job_cancel", row.target_id, {"change_id": row.id}))
    await db.commit()
    return {"ok": True, "change": change_ops.public_change(row)}


# --------------------------------------------------------------------------- vault hardening
@router.post("/vaults/harden")
async def harden_vault(
    body: VaultHardenRequest,
    principal: Principal = Depends(_vault_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _guard_mutation_scope(body)
    """Draft one change per selected control. Irreversible controls are refused outright."""
    estate, connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id,
    )
    _guard_write(connection)
    vault = _find(estate.get("vaults", []), "id", body.vault_id)
    if vault is None:
        raise HTTPException(status_code=404, detail="Vault not found in this scope.")

    rows: list[BackupManagerChange] = []
    skipped: list[dict[str, str]] = []
    # The Recovery Services vault PATCH rejects a partial monitoringSettings object, so the
    # alerts change has to merge into the live settings rather than send just the one key.
    live_props: dict[str, Any] = {}
    live_body: dict[str, Any] = {}
    alerts_read_error = ""
    if "enable_vault_alerts" in body.controls:
        api_version = service.RSV_API if vault["kind"] == "recovery_services" else service.DP_API
        live, _status, alerts_read_error = await service.arm_get(connection, vault["id"], api_version)
        if live:
            live_body = change_ops.concurrency_body(live)
            live_props = service.as_dict(live.get("properties"))
        else:
            alerts_read_error = alerts_read_error or "The vault's current settings could not be read."
    for control in dict.fromkeys(body.controls):
        if control == "enable_vault_alerts" and alerts_read_error:
            # The vault PATCH validates monitoringSettings as a whole, so drafting without the
            # live object would guarantee a failed apply. Fail here instead.
            skipped.append({"control": control, "reason": alerts_read_error})
            continue
        try:
            row = _build_harden_change(principal, connection, vault, control, body, live_props, live_body)
        except ValueError as exc:
            skipped.append({"control": control, "reason": str(exc)})
            continue
        if row is None:
            skipped.append({"control": control, "reason": "Already satisfied on this vault."})
            continue
        rows.append(row)
        db.add(row)
    if not rows:
        raise HTTPException(status_code=400, detail=skipped[0]["reason"] if skipped else "Nothing to change.")
    db.add(_audit(principal, "backup_manager.vault.harden", vault["id"], {
        "controls": list(body.controls), "created": len(rows),
    }))
    await db.commit()
    return {
        "ok": True, "created": len(rows), "skipped": skipped,
        "changes": [change_ops.public_change(r) for r in rows],
    }


def _build_harden_change(
    principal: Principal, connection: dict[str, Any], vault: dict[str, Any], control: str,
    body: VaultHardenRequest, live_props: dict[str, Any] | None = None,
    live_body: dict[str, Any] | None = None,
) -> BackupManagerChange | None:
    tenant, connection_id = _tenant(principal), str(connection.get("id") or "")
    kind = vault["kind"]
    vault_id = vault["id"]
    common = {"vault_id": vault_id, "vault_name": vault["name"], "vault_kind": kind,
              "control": control, "reason": body.reason}

    if control in ("enable_soft_delete", "extend_soft_delete_retention"):
        state = str(vault.get("soft_delete_state") or "").lower()
        if control == "enable_soft_delete" and state in ("enabled", "on", "alwayson"):
            return None
        retention = max(14, min(int(body.soft_delete_retention_days), 180))
        if kind == "recovery_services":
            payload = {"properties": {"softDeleteFeatureState": "Enabled", "enhancedSecurityState": "Enabled",
                                      "softDeleteRetentionPeriodInDays": retention}}
            summary = {**common, "arm_path": f"{vault_id}/backupconfig/vaultconfig", "arm_method": "PUT",
                       "api_version": service.RSV_VAULT_CONFIG_API, "setting": "softDeleteFeatureState",
                       "description": f"Enable soft delete on {vault['name']} with {retention}-day retention"}
        else:
            payload = {"properties": {"securitySettings": {"softDeleteSettings": {
                "state": "On", "retentionDurationInDays": retention}}}}
            summary = {**common, "arm_path": vault_id, "arm_method": "PATCH", "api_version": service.DP_API,
                       "setting": "securitySettings",
                       "description": f"Enable soft delete on {vault['name']} with {retention}-day retention"}
        return change_ops.build_change(
            tenant_id=tenant, connection_id=connection_id, target_type="vault_security",
            target_id=vault_id, operation="update", requested_by=principal.subject,
            desired={"body": payload}, summary={**summary, "api_version": summary["api_version"]},
        )

    if control == "enable_crr":
        if kind != "recovery_services":
            raise ValueError("Cross Region Restore is configured on Recovery Services vaults.")
        if str(vault.get("redundancy") or "").lower().replace("-", "") != "georedundant":
            raise ValueError("Cross Region Restore requires geo-redundant backup storage; change redundancy first.")
        payload = {"properties": {"crossRegionRestoreFlag": True}}
        return change_ops.build_change(
            tenant_id=tenant, connection_id=connection_id, target_type="vault_security",
            target_id=vault_id, operation="update", requested_by=principal.subject,
            desired={"body": payload},
            summary={**common, "arm_path": f"{vault_id}/backupstorageconfig/vaultstorageconfig",
                     "arm_method": "PATCH", "api_version": service.RSV_STORAGE_CONFIG_API,
                     "setting": "crossRegionRestoreFlag",
                     "description": f"Enable Cross Region Restore on {vault['name']}"},
        )

    if control == "set_redundancy":
        if int(vault.get("instance_count") or 0) > 0:
            raise ValueError(
                "Backup storage redundancy can only be changed before the first item is protected; "
                f"{vault['name']} already protects {vault.get('instance_count')} item(s)."
            )
        if kind != "recovery_services":
            raise ValueError("Backup vault redundancy is fixed at creation time.")
        payload = {"properties": {"storageModelType": body.redundancy}}
        return change_ops.build_change(
            tenant_id=tenant, connection_id=connection_id, target_type="vault_security",
            target_id=vault_id, operation="update", requested_by=principal.subject,
            desired={"body": payload},
            summary={**common, "arm_path": f"{vault_id}/backupstorageconfig/vaultstorageconfig",
                     "arm_method": "PATCH", "api_version": service.RSV_STORAGE_CONFIG_API,
                     "setting": "storageModelType",
                     "description": f"Set {vault['name']} backup storage redundancy to {body.redundancy}"},
        )

    if control == "enable_vault_alerts":
        if str(vault.get("monitor_alerts") or "").lower() == "enabled":
            return None
        # Both vault kinds accept the setting on the vault resource. The Recovery Services
        # alertsConfiguration sub-resource is not reliably writable across API versions, and
        # the vault PATCH validates monitoringSettings as a whole, so the live object is
        # merged rather than partially overwritten.
        monitoring = dict(service.as_dict(service.as_dict(live_props).get("monitoringSettings")))
        azure_monitor = dict(service.as_dict(monitoring.get("azureMonitorAlertSettings")))
        azure_monitor["alertsForAllJobFailures"] = "Enabled"
        monitoring["azureMonitorAlertSettings"] = azure_monitor
        payload = {"properties": {"monitoringSettings": monitoring}}
        api_version = service.RSV_API if kind == "recovery_services" else service.DP_API
        summary = {**common, "arm_path": vault_id, "arm_method": "PATCH", "api_version": api_version}
        return change_ops.build_change(
            tenant_id=tenant, connection_id=connection_id, target_type="vault_alerts",
            target_id=vault_id, operation="update", requested_by=principal.subject,
            desired={"body": payload}, before=dict(live_body or {}),
            summary={**summary, "setting": "monitoringSettings",
                     "description": f"Enable built-in backup failure alerts on {vault['name']}"},
        )

    if control == "enable_diagnostics":
        workspace = body.workspace_id or str(connection.get("log_analytics_workspace_id") or "")
        if not workspace:
            raise ValueError("A Log Analytics workspace is required to enable backup reporting.")
        if not workspace.lower().startswith("/subscriptions/"):
            raise ValueError("Provide the full ARM id of the Log Analytics workspace.")
        target = f"{vault_id}/providers/microsoft.insights/diagnosticSettings/backup-manager-reports"
        return change_ops.build_change(
            tenant_id=tenant, connection_id=connection_id, target_type="vault_diagnostics",
            target_id=target, operation="create", requested_by=principal.subject,
            desired={"body": report_ops.diagnostic_setting_body(workspace)},
            summary={**common, "workspace_id": workspace, "api_version": service.DIAG_API,
                     "categories": list(report_ops.REQUIRED_CATEGORIES),
                     "description": f"Send Backup Reports from {vault['name']} to Log Analytics"},
        )
    raise ValueError(f"Unsupported control '{control}'.")


# --------------------------------------------------------------------------- DR + drills
@router.get("/dr")
async def dr_readiness(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, _connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    readiness = dr_ops.build_readiness(estate)
    readiness["rpo"] = dr_ops.rpo_attainment(estate.get("instances", []))
    readiness["demo"] = bool(estate.get("demo"))
    return readiness


@router.get("/compliance")
async def compliance(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, _connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    result = policy_ops.compliance(estate.get("instances", []), estate.get("policies", []))
    result["tiers"] = reference.load_reference().get("tiers", [])
    result["demo"] = bool(estate.get("demo"))
    return result


@router.post("/dr/test-failover")
async def request_test_failover(
    body: TestFailoverRequest,
    principal: Principal = Depends(_drill_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Draft an approval-gated Site Recovery test failover. Never a real failover."""
    _guard_mutation_scope(body)
    estate, connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id, enrich=False,
    )
    _guard_write(connection)
    if bool(body.replicated_item_id) == bool(body.recovery_plan_id):
        raise HTTPException(status_code=400, detail="Provide exactly one of a replicated item or a recovery plan.")

    if body.replicated_item_id:
        item = _find(estate.get("replication", []), "id", body.replicated_item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Replicated item not found in this scope.")
        blocker = dr_ops.validate_drill_target(item)
        if blocker:
            raise HTTPException(status_code=400, detail=blocker)
        target_id, drill_target, target_name = item["id"], "item", item.get("friendly_name", "")
    else:
        plan = _find(estate.get("recovery_plans", []), "id", body.recovery_plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Recovery plan not found in this scope.")
        if str(plan.get("current_scenario_status") or "").lower() in ("inprogress", "running"):
            raise HTTPException(status_code=400, detail="This recovery plan already has a scenario in progress.")
        target_id, drill_target, target_name = plan["id"], "recovery_plan", plan.get("friendly_name", "")

    try:
        payload = dr_ops.build_test_failover_body(
            network_type=body.network_type, network_id=body.network_id,
            recovery_point_id=body.recovery_point_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = change_ops.build_change(
        tenant_id=_tenant(principal), connection_id=str(connection.get("id") or ""),
        target_type="asr_test_failover", target_id=target_id, operation="invoke",
        requested_by=principal.subject, desired={"body": payload},
        summary={
            "kind": "test_failover", "drill_target": drill_target, "target_name": target_name,
            "network_type": body.network_type, "network_id": body.network_id,
            "drill_id": body.drill_id, "reason": body.reason,
            "description": f"Run an isolated test failover for {target_name}",
            "cleanup_reminder": "Run test-failover cleanup afterwards or the drill resources keep billing.",
        },
        risk="high",
    )
    db.add(row)
    db.add(_audit(principal, "backup_manager.dr.test_failover", target_id, {"change_id": row.id}))
    if body.drill_id:
        drill = await db.get(BackupDrill, body.drill_id)
        if drill is not None and drill.tenant_id == _tenant(principal):
            drill.change_id = row.id
            drill.status = "in_progress"
            drill.updated_at = service.now()
    await db.commit()
    return {"ok": True, "change": change_ops.public_change(row)}


@router.post("/dr/test-failover-cleanup")
async def request_cleanup(
    body: CleanupRequest,
    principal: Principal = Depends(_drill_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _guard_mutation_scope(body)
    estate, connection = await _estate(
        principal, connection_id=body.connection_id, workload_id=body.workload_id,
        subscription_id=body.subscription_id, management_group_id=body.management_group_id, enrich=False,
    )
    _guard_write(connection)
    if bool(body.replicated_item_id) == bool(body.recovery_plan_id):
        raise HTTPException(status_code=400, detail="Provide exactly one of a replicated item or a recovery plan.")
    if body.replicated_item_id:
        item = _find(estate.get("replication", []), "id", body.replicated_item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Replicated item not found in this scope.")
        target_id, drill_target, target_name = item["id"], "item", item.get("friendly_name", "")
    else:
        plan = _find(estate.get("recovery_plans", []), "id", body.recovery_plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Recovery plan not found in this scope.")
        target_id, drill_target, target_name = plan["id"], "recovery_plan", plan.get("friendly_name", "")
    row = change_ops.build_change(
        tenant_id=_tenant(principal), connection_id=str(connection.get("id") or ""),
        target_type="asr_cleanup", target_id=target_id, operation="invoke",
        requested_by=principal.subject,
        desired={"body": dr_ops.build_cleanup_body(body.comments or "Drill complete.")},
        summary={"kind": "test_failover_cleanup", "drill_target": drill_target, "target_name": target_name,
                 "description": f"Clean up the test failover for {target_name}"},
    )
    db.add(row)
    db.add(_audit(principal, "backup_manager.dr.cleanup", target_id, {"change_id": row.id}))
    await db.commit()
    return {"ok": True, "change": change_ops.public_change(row)}


@router.get("/drills")
async def list_drills(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    status: str = Query(default="", max_length=24),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _scope_identity(workload_id, subscription_id, management_group_id)
    rows = await drill_ops.list_drills(
        db, tenant_id=_tenant(principal), connection_id=connection_id, status=status,
    )
    public = [drill_ops.public_drill(r) for r in rows]
    # The register is a live DB ledger. Site Recovery readiness comes from the completed
    # snapshot already rendered by the DR tab; opening the register must not query Azure.
    return {"drills": public, "summary": drill_ops.summarize(public, {})}


@router.post("/drills")
async def create_drill(
    body: DrillCreateRequest,
    principal: Principal = Depends(_drill_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _guard_mutation_scope(body)
    try:
        drill = drill_ops.build_drill(
            tenant_id=_tenant(principal), connection_id=body.connection_id or "default",
            name=body.name, kind=body.kind, scope_kind=body.scope_kind,
            scope_id=body.scope_id or body.workload_id, target_id=body.target_id,
            target_name=body.target_name, cadence_days=body.cadence_days, created_by=principal.subject,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(drill)
    await db.flush()  # materialise the generated id before it is referenced by the audit row
    db.add(_audit(principal, "backup_manager.drill.create", drill.id, {"kind": drill.kind, "name": drill.name}))
    await db.commit()
    return {"ok": True, "drill": drill_ops.public_drill(drill)}


@router.post("/drills/{drill_id}/outcome")
async def record_drill_outcome(
    drill_id: str,
    body: DrillOutcomeRequest,
    principal: Principal = Depends(_drill_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    drill = await db.get(BackupDrill, drill_id)
    if drill is None or drill.tenant_id != _tenant(principal):
        raise HTTPException(status_code=404, detail="Drill not found.")
    evidence_id = ""
    if body.capture_evidence:
        from app.evidence import registry as evidence_registry

        meta = evidence_registry.create_snapshot(
            tenant_id=_tenant(principal),
            name=f"Recovery drill — {drill.name}",
            scope=drill.scope_id or drill.target_name or "backup-manager",
            included=["drill"],
            retention_class="standard",
            tags=["backup-manager", "drill", drill.kind],
            content={
                "kind": "backup_manager.drill",
                "drill": drill_ops.public_drill(drill),
                "outcome": {"status": body.status, "notes": body.notes, "rto_minutes": body.rto_minutes},
                "recorded_by": principal.subject,
                "recorded_at": service.now_iso(),
            },
            created_by=principal.subject,
            demo=False,
        )
        evidence_id = str(meta.get("id") or "")
    try:
        drill_ops.record_outcome(
            drill, status=body.status, executed_by=principal.subject, notes=body.notes,
            rto_minutes=body.rto_minutes, evidence_id=evidence_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    follow_up = drill_ops.next_occurrence(drill, created_by=principal.subject) if body.status != "cancelled" else None
    if follow_up is not None:
        db.add(follow_up)
    db.add(_audit(principal, "backup_manager.drill.outcome", drill.id, {
        "status": body.status, "evidence_id": evidence_id,
    }))
    await db.commit()
    return {
        "ok": True, "drill": drill_ops.public_drill(drill),
        "next_drill": drill_ops.public_drill(follow_up) if follow_up is not None else None,
    }


# --------------------------------------------------------------------------- cost + reports
@router.get("/cost")
async def cost(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    use_reports: bool = Query(default=True),
    use_actuals: bool = Query(default=True),
    months_back: int = Query(default=1, ge=0, le=12),
    cost_type: Literal["AmortizedCost", "ActualCost"] = Query(default="AmortizedCost"),
    force: bool = Query(default=False),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """Backup cost from the best available source.

    Layers three inputs, each labelled so the caller knows what it is looking at: live retail
    list prices (forward-looking), Log Analytics consumption (per-item truth), and Cost
    Management actuals (authoritative, but only ever attributed to the vault).

    Shares its implementation with the snapshot builder, so the Cost tab cannot disagree with
    the overview when the operator picks a different period."""
    estate, connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
    )
    return await analysis_ops.build_cost(
        connection, estate,
        tenant_id=_tenant(principal),
        is_demo=str(connection.get("id") or "") == "demo",
        use_reports=use_reports, use_actuals=use_actuals,
        months_back=months_back, cost_type=cost_type, force=force,
    )


# Both helpers now live with the analysis pipeline that owns them; these names are kept so the
# API module and its tests have one obvious place to reach them.
_price_region = analysis_ops.price_region
_match_report_storage = analysis_ops.match_report_storage


@router.get("/cost/actuals")
async def cost_actuals(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    months_back: int = Query(default=1, ge=0, le=12),
    cost_type: Literal["AmortizedCost", "ActualCost"] = Query(default="AmortizedCost"),
    force: bool = Query(default=False),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """Raw Cost Management actuals for backup and Site Recovery, by vault and meter.

    Doubles as the capability probe for cost reporting: ``available`` plus ``reason``/``remedy``
    tell the UI exactly why actuals are missing when they are."""
    estate, connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    if str(connection.get("id") or "") == "demo":
        return {
            "available": False, "demo": True, "by_vault": {}, "by_meter": {}, "daily": [],
            "currency": "", "total": 0.0,
            "reason": "Demo mode does not query Azure Cost Management.",
            "remedy": "Select a live Azure connection.",
        }
    subscriptions = [s for s in (estate.get("scope", {}).get("subscriptions") or []) if s]
    result = await costmgmt.cached_actuals(
        connection, subscriptions, tenant_id=_tenant(principal),
        months_back=months_back, cost_type=cost_type, daily=True, force=force,
    )
    vault_names = {service.canonical_id(v["id"]): v.get("name", "") for v in estate.get("vaults", [])}
    result["vaults"] = sorted(
        (
            {"vault_id": vault_id, "vault_name": vault_names.get(vault_id, service.name_from_id(vault_id)),
             "cost": round(amount, 2), "in_scope": vault_id in vault_names}
            for vault_id, amount in (result.get("by_vault") or {}).items()
        ),
        key=lambda row: -row["cost"],
    )
    result.pop("rows", None)
    return result


@router.get("/prices")
async def prices(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    currency: str = Query(default="", max_length=8),
    region: str = Query(default="", max_length=64),
    force: bool = Query(default=False),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """The live Azure Retail Prices rate card the estimate is built from."""
    estate, connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id, enrich=False,
    )
    resolved_region = region or _price_region(estate, connection)
    # Default to the billing currency when it is already known, so this really is the card
    # the estimate was built from rather than one denominated differently.
    resolved_currency = (
        currency
        or costmgmt.known_currency(connection, tenant_id=_tenant(principal))
        or str(reference.cost_rates().get("currency") or "USD")
    )
    return await pricing.get_rate_card(resolved_region, resolved_currency, force=force)


@router.get("/reports")
async def backup_reports(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    days: int = Query(default=30, ge=1, le=180),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    estate, connection = await _estate(
        principal, connection_id=connection_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
    )
    if str(connection.get("id") or "") == "demo":
        return {
            "available": False, "demo": True,
            "reason": "Backup reporting queries Log Analytics, which demo mode does not simulate.",
            "remedy": "Select a live Azure connection with vault diagnostics enabled.",
            "vaults_total": len(estate.get("vaults", [])),
            "vaults_with_diagnostics": sum(1 for v in estate.get("vaults", []) if v.get("diagnostics_enabled")),
            "required_categories": list(report_ops.REQUIRED_CATEGORIES),
            "job_trend": [], "storage": [], "failure_history": [], "sla": [],
        }
    return await report_ops.build_report(connection, estate, days=days)


# --------------------------------------------------------------------------- managed changes
@router.get("/changes")
async def list_changes(
    connection_id: str = Query(default=""),
    status: str = Query(default="", max_length=24),
    view: Literal["all", "action_required"] = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tenant = _tenant(principal)
    filters = [BackupManagerChange.tenant_id == tenant]
    if connection_id:
        filters.append(BackupManagerChange.connection_id == connection_id)
    if status:
        filters.append(BackupManagerChange.status == status)
    elif view == "action_required":
        filters.append(BackupManagerChange.status.in_(tuple(change_ops.ACTIONABLE_STATUSES)))

    total = int((await db.execute(
        select(func.count()).select_from(BackupManagerChange).where(*filters)
    )).scalar_one() or 0)
    rows = list((await db.execute(
        select(BackupManagerChange)
        .where(*filters)
        .order_by(BackupManagerChange.requested_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars())

    counts_filters = [BackupManagerChange.tenant_id == tenant]
    if connection_id:
        counts_filters.append(BackupManagerChange.connection_id == connection_id)
    counts = dict(
        (await db.execute(
            select(BackupManagerChange.status, func.count())
            .where(*counts_filters)
            .group_by(BackupManagerChange.status)
        )).all()
    )
    return {
        "rows": [change_ops.public_change(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total_count": total,
        "has_more": (page - 1) * page_size + len(rows) < total,
        "status_counts": {str(k): int(v) for k, v in counts.items()},
        "pending_count": int(counts.get("pending", 0)),
        "approved_count": int(counts.get("approved", 0)),
        "applying_count": int(counts.get("applying", 0)),
        "actionable_count": int(counts.get("pending", 0)) + int(counts.get("approved", 0)),
    }


@router.post("/changes/{change_id}/decision")
async def decide_change(
    change_id: str,
    body: ChangeDecisionRequest,
    principal: Principal = Depends(_approve),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    change = await change_ops.load_change(db, change_id, tenant_id=_tenant(principal))
    if change is None:
        raise HTTPException(status_code=404, detail="Managed change not found.")
    if change.status not in ("pending", "approved"):
        raise HTTPException(status_code=409, detail=f"A {change.status} change cannot be decided.")
    if body.decision == "rejected":
        change.status = "rejected"
    else:
        if change.requires_dual_approval:
            if not change.decided_by:
                change.decided_by = principal.subject
                change.decided_at = service.now()
                change.decision_reason = body.reason
                db.add(_audit(principal, "backup_manager.change.first_approval", change.target_id, {"change_id": change.id}))
                await db.commit()
                return {"ok": True, "change": change_ops.public_change(change),
                        "awaiting_second_approver": True}
            if change.decided_by == principal.subject:
                raise HTTPException(
                    status_code=403,
                    detail="This change requires a second, different approver before it can be applied.",
                )
            change.second_approver = principal.subject
            change.second_approved_at = service.now()
        change.status = "approved"
    change.decided_by = change.decided_by or principal.subject
    change.decided_at = change.decided_at or service.now()
    change.decision_reason = body.reason
    db.add(_audit(principal, f"backup_manager.change.{body.decision}", change.target_id, {"change_id": change.id}))
    await db.commit()
    return {"ok": True, "change": change_ops.public_change(change)}


@router.post("/changes/bulk-decision")
async def bulk_decision(
    body: BulkDecisionRequest,
    principal: Principal = Depends(_approve),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tenant = _tenant(principal)
    rows = list((await db.execute(
        select(BackupManagerChange).where(
            BackupManagerChange.tenant_id == tenant,
            BackupManagerChange.id.in_(body.change_ids),
        )
    )).scalars())
    updated, skipped = [], []
    for change in rows:
        if change.status != "pending":
            skipped.append({"id": change.id, "reason": f"Status is {change.status}."})
            continue
        if body.decision == "approved" and change.requires_dual_approval:
            skipped.append({"id": change.id, "reason": "Requires two distinct approvers; approve it individually."})
            continue
        change.status = body.decision
        change.decided_by = principal.subject
        change.decided_at = service.now()
        change.decision_reason = body.reason
        updated.append(change)
    db.add(_audit(principal, f"backup_manager.changes.bulk_{body.decision}", "bulk", {
        "updated": len(updated), "skipped": len(skipped),
    }))
    await db.commit()
    return {
        "ok": True, "updated": [change_ops.public_change(c) for c in updated], "skipped": skipped,
    }


@router.post("/changes/bulk-apply")
async def bulk_apply(
    body: ChangeSelectionRequest,
    principal: Principal = Depends(_approve),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Apply approved changes in dependency order with bounded concurrency."""
    tenant = _tenant(principal)
    rows = list((await db.execute(
        select(BackupManagerChange).where(
            BackupManagerChange.tenant_id == tenant,
            BackupManagerChange.id.in_(body.change_ids),
        )
    )).scalars())
    approved = [r for r in rows if r.status == "approved"]
    skipped = [{"id": r.id, "reason": f"Status is {r.status}."} for r in rows if r.status != "approved"]
    if not approved:
        raise HTTPException(status_code=400, detail="No approved changes were selected.")

    ordered = change_ops.order_changes(approved)
    connections: dict[str, dict[str, Any]] = {}
    from app.core.azure_connections import resolve_connection

    semaphore = asyncio.Semaphore(APPLY_CONCURRENCY)

    async def run(change: BackupManagerChange) -> None:
        async with semaphore:
            connection = connections.get(change.connection_id)
            if connection is None:
                connection = resolve_connection(change.connection_id) or {}
                connections[change.connection_id] = connection
            if not connection:
                change.status = "failed"
                change.error_code = "ConnectionMissing"
                change.error_message = "The Azure connection for this change no longer exists."
                return
            try:
                submission, context = await change_ops.apply_change(connection, change)
            except (ValueError, PermissionError) as exc:
                change.status = "failed"
                change.error_code = "PreflightFailed"
                change.error_message = service.safe_error(str(exc))
                return
            if context.get("before"):
                change.before_encrypted = service.encrypted_json(context["before"])
            change_ops.mark_submitted(change, submission, principal.subject)

    # Prerequisite-bearing changes run first as a serialised prefix; the remainder fan out.
    serial = [c for c in ordered if c.depends_on]
    parallel = [c for c in ordered if not c.depends_on]
    for change in serial:
        await run(change)
    if parallel:
        await asyncio.gather(*(run(c) for c in parallel))

    db.add(_audit(principal, "backup_manager.changes.bulk_apply", "bulk", {
        "requested": len(rows), "applied": sum(1 for c in approved if c.status == "applied"),
        "applying": sum(1 for c in approved if c.status == "applying"),
        "failed": sum(1 for c in approved if c.status == "failed"),
    }))
    await db.commit()
    from app.backup_manager import cache as inventory_cache

    await inventory_cache.invalidate(tenant_id=tenant)
    return {
        "ok": True,
        "results": [change_ops.public_change(c) for c in approved],
        "skipped": skipped,
    }


@router.post("/changes/{change_id}/rollback")
async def rollback_change(
    change_id: str,
    principal: Principal = Depends(_approve),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    change = await change_ops.load_change(db, change_id, tenant_id=_tenant(principal))
    if change is None:
        raise HTTPException(status_code=404, detail="Managed change not found.")
    if change.status != "applied":
        raise HTTPException(status_code=409, detail="Only an applied change can be rolled back.")
    try:
        row = change_ops.build_rollback(change, requested_by=principal.subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(row)
    db.add(_audit(principal, "backup_manager.change.rollback_requested", change.target_id, {
        "change_id": change.id, "rollback_change_id": row.id,
    }))
    await db.commit()
    return {"ok": True, "change": change_ops.public_change(row)}


@router.get("/changes/poller")
async def poller_status(_principal: Principal = Depends(_read)) -> dict[str, Any]:
    from app.backup_manager.lro import poller

    return {"running": poller.running, "ticks": poller.ticks, "last_error": poller.last_error}


# --------------------------------------------------------------------------- reference
@router.get("/reference")
async def get_reference(_principal: Principal = Depends(_read)) -> dict[str, Any]:
    return reference.load_reference()


@router.put("/reference")
async def put_reference(
    body: ReferenceSaveRequest,
    principal: Principal = Depends(_reference_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    payload = body.model_dump()
    reason = str(payload.pop("reason", "") or "")
    doc = reference.save_reference(payload, actor=principal.subject, reason=reason)
    db.add(_audit(principal, "backup_manager.reference.save", "reference", {"version": doc.get("version")}))
    await db.commit()
    return doc


@router.get("/reference/revisions")
async def reference_revisions(_principal: Principal = Depends(_read)) -> dict[str, Any]:
    return {"revisions": reference.list_revisions()}


@router.post("/reference/restore")
async def reference_restore(
    revision_id: str = Query(min_length=1, max_length=64),
    principal: Principal = Depends(_reference_write),
) -> dict[str, Any]:
    try:
        return reference.restore_revision(revision_id, actor=principal.subject)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/reference/reset")
async def reference_reset(principal: Principal = Depends(_reference_write)) -> dict[str, Any]:
    return reference.reset_reference(actor=principal.subject)


# --------------------------------------------------------------------------- export + evidence
@router.get("/export/workbook")
async def export_workbook(
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Every Backup Manager tab as one snapshot-consistent Excel review pack.

    No Azure call is allowed here.  The analyzed snapshot is authoritative; the two ledgers
    that move independently (managed changes and drills) are read from the database and passed
    to the pure formatter as public projections.
    """
    scope_kind, scope_id = _scope_identity(workload_id, subscription_id, management_group_id)
    if scope_kind == "none":
        raise HTTPException(status_code=400, detail="Select a workload, subscription, or management group first.")

    if _is_demo(workload_id):
        connection = {"id": "demo", "display_name": "Demo", "azure_cloud": "AzureCloud"}
        snapshot = await _demo_snapshot(workload_id)
        change_rows: list[BackupManagerChange] = []
        drill_rows: list[BackupDrill] = []
        changes_truncated = False
    else:
        connection = _connection(connection_id, workload_id)
        tenant = _tenant(principal)
        effective_connection = str(connection.get("id") or "")
        snapshot = snapshot_store.read_snapshot(
            tenant, effective_connection, scope_kind, scope_id,
        )
        if snapshot is None or not snapshot.get("report_exists"):
            raise HTTPException(status_code=409, detail="Analyze backups first; no completed snapshot exists for this scope.")
        summary = snapshot.get("summary")
        if isinstance(summary, dict):
            summary["actionable_changes"] = await _actionable_changes(db, tenant, effective_connection)
        snapshot["age_seconds"] = snapshot_store.age_seconds(snapshot)

        change_rows = list((await db.execute(
            select(BackupManagerChange)
            .where(
                BackupManagerChange.tenant_id == tenant,
                BackupManagerChange.connection_id == effective_connection,
            )
            .order_by(BackupManagerChange.requested_at.desc())
            .limit(WORKBOOK_CHANGE_LIMIT + 1)
        )).scalars())
        changes_truncated = len(change_rows) > WORKBOOK_CHANGE_LIMIT
        del change_rows[WORKBOOK_CHANGE_LIMIT:]
        drill_rows = await drill_ops.list_drills(
            db, tenant_id=tenant, connection_id=effective_connection,
        )

    ledger_at = datetime.now(timezone.utc).isoformat()
    content = await asyncio.to_thread(
        export_ops.to_workbook,
        snapshot=snapshot,
        changes=[change_ops.public_change(row) for row in change_rows],
        drills=[drill_ops.public_drill(row) for row in drill_rows],
        portal_host=portal_host(connection),
        connection_label=str(connection.get("display_name") or connection.get("id") or ""),
        ledger_generated_at=ledger_at,
        changes_truncated=changes_truncated,
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="backup-manager-review-{datetime.now(timezone.utc):%Y-%m-%d}.xlsx"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/export")
async def export_csv(
    kind: Literal["instances", "jobs", "policies", "gaps", "posture", "drills"] = Query(...),
    connection_id: str = Query(default=""),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
    management_group_id: str = Query(default=""),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if kind == "drills":
        if _is_demo(workload_id):
            rows = []
        else:
            connection = _connection(connection_id, workload_id)
            rows = [drill_ops.public_drill(r) for r in await drill_ops.list_drills(
                db, tenant_id=_tenant(principal), connection_id=str(connection.get("id") or ""),
            )]
    else:
        scope_kind, scope_id = _scope_identity(workload_id, subscription_id, management_group_id)
        if scope_kind == "none":
            raise HTTPException(status_code=400, detail="Select a scope before exporting.")
        if _is_demo(workload_id):
            snapshot = await _demo_snapshot(workload_id)
        else:
            connection = _connection(connection_id, workload_id)
            snapshot = snapshot_store.read_snapshot(
                _tenant(principal), str(connection.get("id") or ""), scope_kind, scope_id,
            )
            if snapshot is None or not snapshot.get("report_exists"):
                raise HTTPException(status_code=409, detail="Analyze backups first; no completed snapshot exists for this scope.")
        if kind == "instances":
            rows = (snapshot.get("inventory") or {}).get("rows", [])
        elif kind == "jobs":
            rows = (snapshot.get("jobs") or {}).get("rows", [])
        elif kind == "policies":
            rows = (snapshot.get("policies") or {}).get("policies", [])
        elif kind == "posture":
            rows = (snapshot.get("posture") or {}).get("vaults", [])
        else:
            rows = (snapshot.get("gaps") or {}).get("gaps", [])
    content = export_ops.export(kind, rows)
    return Response(
        content=content, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="backup-manager-{kind}.csv"'},
    )


@router.post("/evidence")
async def capture_evidence(
    body: EvidenceRequest,
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Hash-stamp the completed snapshot without starting another Azure collection."""
    from app.evidence import registry as evidence_registry

    scope_kind, scope_id = _scope_identity(
        body.workload_id, body.subscription_id, body.management_group_id,
    )
    if _is_demo(body.workload_id):
        snapshot = await _demo_snapshot(body.workload_id)
        effective_connection = "demo"
    else:
        connection = _connection(body.connection_id, body.workload_id)
        effective_connection = str(connection.get("id") or "")
        snapshot = snapshot_store.read_snapshot(
            _tenant(principal), effective_connection, scope_kind, scope_id,
        )
        if snapshot is None or not snapshot.get("report_exists"):
            raise HTTPException(status_code=409, detail="Analyze backups first; no completed snapshot exists for this scope.")
    drills = [drill_ops.public_drill(r) for r in await drill_ops.list_drills(
        db, tenant_id=_tenant(principal), connection_id=effective_connection,
    )]
    estate = {
        "generated_at": snapshot.get("generated_at"),
        "scope": snapshot.get("scope", {}),
        "vaults": (snapshot.get("vaults") or {}).get("vaults", []),
        "instances": (snapshot.get("inventory") or {}).get("rows", []),
        "replication": (snapshot.get("dr") or {}).get("items", []),
        "policies": (snapshot.get("policies") or {}).get("policies", []),
    }
    payload = export_ops.evidence_payload(
        estate=estate,
        posture=snapshot.get("posture") or {},
        compliance=snapshot.get("compliance") or {},
        rpo=(snapshot.get("dr") or {}).get("rpo") or {},
        drills=drills,
        scope=snapshot.get("scope", {}),
    )
    scope_label = str((snapshot.get("scope") or {}).get("scope_name") or scope_id)
    meta = evidence_registry.create_snapshot(
        tenant_id=_tenant(principal),
        name=body.name or f"Backup recoverability — {scope_label}",
        scope=scope_label,
        included=["backup_manager"],
        retention_class="standard",
        tags=["backup-manager", "recoverability"],
        content=payload,
        created_by=principal.subject,
        demo=bool(estate.get("demo")),
    )
    db.add(_audit(principal, "backup_manager.evidence", str(meta.get("id") or ""), {
        "sha256": meta.get("sha256"), "scope": scope_label,
    }))
    await db.commit()
    return {"ok": True, "snapshot": meta}

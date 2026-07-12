"""Alerts Manager endpoints: cached analysis, refresh, and safe CSV/JSON export."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.alert_analysis import cache, demo, export
from app.alert_analysis.collector import SNAPSHOT_SCHEMA_VERSION, collect_analysis, empty_snapshot
from app.core.db import SessionLocal, get_db
from app.core.genjob import JobRegistry, ProgressFn
from app.core.security import Principal, require_permission
from app.models import AuditLog

router = APIRouter(prefix="/alert-analysis", tags=["alert-analysis"])
_read = require_permission("alert_analysis.read")
_manage = require_permission("alert_analysis.manage")
log = logging.getLogger("app.api.alert_analysis")
_refresh_jobs = JobRegistry("alert-analysis-refresh")


def _settings() -> tuple[int, float]:
    from app.core.app_settings import load_settings

    settings = load_settings()
    return (
        int(settings.get("alert_analysis_cache_ttl_s", 21600) or 21600),
        float(settings.get("alert_analysis_threshold_tolerance_pct", 10) or 10),
    )


def _decorate(snapshot: dict[str, Any], ttl_s: int) -> dict[str, Any]:
    age = cache.age_seconds(snapshot)
    result = dict(snapshot)
    result["ttl_s"] = ttl_s
    result["age_seconds"] = int(age) if age is not None else None
    result["stale"] = age is None or age >= ttl_s
    result.setdefault("report_exists", True)
    return result


def _with_decisions(
    snapshot: dict[str, Any], tenant_id: str, connection_id: str
) -> dict[str, Any]:
    from app.alert_analysis.decisions import apply_decisions, list_decisions

    return apply_decisions(snapshot, list_decisions(tenant_id, connection_id))


def _decision_connection_id(connection_id: str | None) -> str:
    from app.core.azure_connections import resolve_connection

    connection = resolve_connection(connection_id)
    return str((connection or {}).get("id") or connection_id or "default")


def _scope(
    workload_id: str | None,
    subscription_id: str | None,
    management_group_id: str | None = None,
) -> tuple[str, str]:
    if workload_id:
        return "workload", workload_id
    if subscription_id:
        return "subscription", subscription_id
    if management_group_id:
        return "management_group", management_group_id
    from app.amba.demo import DEMO_WORKLOAD_ID

    return "workload", DEMO_WORKLOAD_ID


async def _snapshot(
    principal: Principal,
    scope_kind: str,
    scope_id: str,
    *,
    force: bool,
    connection_id: str | None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    from app.core.azure_connections import connection_for_scope
    from app.workloads.registry import get_workload

    ttl_s, tolerance_pct = _settings()
    tenant_id = principal.tenant_id or "default"
    workload = get_workload(scope_id) if scope_kind == "workload" else None
    connection = connection_for_scope(scope_kind, connection_id=connection_id, workload=workload)
    effective_connection_id = str((connection or {}).get("id") or connection_id or "")

    if demo.is_demo_scope(scope_kind, scope_id):
        if progress:
            await progress("scope", "Resolving demo analysis scope…")
        snapshot = cache.read_snapshot(tenant_id, effective_connection_id, scope_kind, scope_id)
        if (
            force
            or snapshot is None
            or not cache.is_fresh(snapshot, ttl_s)
            or not snapshot.get("demo")
            or snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
            or "rationalization_score" not in snapshot
        ):
            snapshot = demo.build_demo_snapshot(scope_id)
            if progress:
                await progress("compute", "Generated deterministic demo rules, routes, overlaps, gaps, and costs.")
            cache.write_snapshot(tenant_id, effective_connection_id, scope_kind, scope_id, snapshot)
            if progress:
                await progress("save", "Saved analysis snapshot to the server cache.")
        return _with_decisions(_decorate(snapshot, ttl_s), tenant_id, effective_connection_id)

    if not force:
        snapshot = cache.read_snapshot(tenant_id, effective_connection_id, scope_kind, scope_id)
        if snapshot is not None and snapshot.get("schema_version") == SNAPSHOT_SCHEMA_VERSION:
            return _with_decisions(_decorate(snapshot, ttl_s), tenant_id, effective_connection_id)
        empty = empty_snapshot(scope_kind, scope_id)
        empty["report_exists"] = False
        return _with_decisions(_decorate(empty, ttl_s), tenant_id, effective_connection_id)

    lock = cache.get_lock(tenant_id, effective_connection_id, scope_kind, scope_id)
    async with lock:
        snapshot = await collect_analysis(
            connection,
            scope_kind=scope_kind,
            scope_id=scope_id,
            workload=workload,
            tolerance_pct=tolerance_pct,
            progress=progress,
        )
        cache.write_snapshot(tenant_id, effective_connection_id, scope_kind, scope_id, snapshot)
        if progress:
            await progress("save", "Saved analysis snapshot to the server cache.")
        return _with_decisions(_decorate(snapshot, ttl_s), tenant_id, effective_connection_id)


def _effective_connection_id(scope_kind: str, scope_id: str, connection_id: str | None) -> str:
    from app.core.azure_connections import connection_for_scope
    from app.workloads.registry import get_workload

    workload = get_workload(scope_id) if scope_kind == "workload" else None
    connection = connection_for_scope(scope_kind, connection_id=connection_id, workload=workload)
    return str((connection or {}).get("id") or connection_id or "default")


def _refresh_job_key(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> str:
    return "|".join((tenant_id or "default", connection_id or "default", scope_kind, scope_id))


async def _invalidate_live_inventory(
    principal: Principal, scope_kind: str, scope_id: str, connection_id: str | None,
) -> None:
    """Discard inventories that may have been populated before the forced ARG refresh completed."""
    from app.alerts_manager import cache as inventory_cache

    await inventory_cache.invalidate(
        kinds={"rules", "action_groups"},
        tenant_id=principal.tenant_id or "default",
        connection_id=_effective_connection_id(scope_kind, scope_id, connection_id),
    )


async def _persist_refresh(
    snapshot: dict[str, Any], principal: Principal, scope_kind: str, scope_id: str,
    db: AsyncSession, progress: ProgressFn | None = None,
) -> None:
    from app.core import coverage_runs, coverage_trends

    score = snapshot.get("rationalization_score")
    if progress:
        await progress("save", "Saving compact rationalization trend point…")
    coverage_trends.record(
        "alert_analysis", principal.tenant_id or "default", scope_kind, scope_id,
        pct=score, extra=snapshot.get("kpis") or {}, demo=bool(snapshot.get("demo")),
    )
    if progress:
        await progress("save", "Saving full analysis run history…")
    coverage_runs.save_run(
        "alert_analysis", principal.tenant_id or "default", scope_kind, scope_id, snapshot,
        headline=score, counts=snapshot.get("kpis") or {},
        resource_count=int((snapshot.get("kpis") or {}).get("resources_evaluated", 0)),
        actor=principal.subject,
    )
    if progress:
        await progress("save", "Writing refresh audit record…")
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="alert_analysis.refresh", target=f"{scope_kind}:{scope_id}",
        metadata_json={
            "rules": snapshot.get("kpis", {}).get("total_rules", 0),
            "overlaps": snapshot.get("kpis", {}).get("overlap_groups", 0),
            "gaps": snapshot.get("kpis", {}).get("gap_count", 0),
            "partial": bool(snapshot.get("partial")),
        },
    ))
    await db.commit()


def _job_response(job: dict[str, Any] | None) -> dict[str, Any]:
    public = _refresh_jobs.public_job(job)
    if not job or not public:
        return {"job": None, "progress": [], "result": None}
    return {
        "job": public,
        "progress": list(job.get("progress") or []),
        "result": job.get("result") if job.get("status") == "done" else None,
    }


@router.get("")
async def analysis(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    return await _snapshot(principal, scope_kind, scope_id, force=False, connection_id=connection_id)


@router.post("/refresh")
async def refresh(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    snapshot = await asyncio.shield(
        _snapshot(principal, scope_kind, scope_id, force=True, connection_id=connection_id)
    )
    await _invalidate_live_inventory(principal, scope_kind, scope_id, connection_id)
    await _persist_refresh(snapshot, principal, scope_kind, scope_id, db)
    return snapshot


@router.post("/refresh/start")
async def refresh_start(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """Start a detached refresh, idempotently per tenant, connection, and selected scope."""
    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    tenant_id = principal.tenant_id or "default"
    effective_connection_id = _effective_connection_id(scope_kind, scope_id, connection_id)
    key = _refresh_job_key(tenant_id, effective_connection_id, scope_kind, scope_id)

    async def runner(progress: ProgressFn) -> dict[str, Any]:
        await progress("start", "Starting server-side Alerts Manager analysis. It will continue if the browser disconnects.")
        snapshot = await _snapshot(
            principal, scope_kind, scope_id, force=True,
            connection_id=connection_id, progress=progress,
        )
        await progress("refresh", "Refreshing Rule Management inventory from the analyzed Azure state…")
        await _invalidate_live_inventory(principal, scope_kind, scope_id, connection_id)
        async with SessionLocal() as db:
            await _persist_refresh(snapshot, principal, scope_kind, scope_id, db, progress)
        await progress(
            "done",
            f"Analysis complete — {snapshot.get('kpis', {}).get('total_rules', 0):,} rules, "
            f"{snapshot.get('kpis', {}).get('overlap_groups', 0):,} overlaps, "
            f"{snapshot.get('kpis', {}).get('gap_count', 0):,} gaps.",
        )
        return snapshot

    job = _refresh_jobs.start(key, runner)
    return _job_response(job)


@router.get("/refresh/job")
async def refresh_job(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """Return the current/recent scope job, its replayable log, and completed snapshot."""
    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    effective_connection_id = _effective_connection_id(scope_kind, scope_id, connection_id)
    key = _refresh_job_key(principal.tenant_id or "default", effective_connection_id, scope_kind, scope_id)
    return _job_response(_refresh_jobs.get_job(key))


@router.get("/export")
async def export_snapshot(
    format: str = Query(default="csv", pattern="^(csv|json|xlsx)$"),
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> Response:
    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    snapshot = await _snapshot(principal, scope_kind, scope_id, force=False, connection_id=connection_id)
    if not snapshot.get("report_exists"):
        raise HTTPException(status_code=404, detail="Run an Alerts Manager analysis before exporting.")
    safe_scope = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(snapshot.get("scope_name") or scope_id)).strip("-") or "scope"
    if format == "xlsx":
        from app.core import coverage_trends

        points = coverage_trends.series("alert_analysis", principal.tenant_id or "default", scope_kind, scope_id)
        content = export.to_workbook(snapshot, points)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif format == "csv":
        content = export.to_csv(snapshot)
        media_type = "text/csv; charset=utf-8"
    else:
        content = export.to_json(snapshot)
        media_type = "application/json"
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="alert_analysis.export",
            target=f"{scope_kind}:{scope_id}",
            metadata_json={"format": format, "recipient_mode": "masked"},
        )
    )
    await db.commit()
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="alerts-manager-{safe_scope}.{format}"'},
    )


@router.get("/trend")
async def trend(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.core import coverage_trends

    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    return coverage_trends.trend("alert_analysis", principal.tenant_id or "default", scope_kind, scope_id)


@router.get("/runs")
async def runs(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.core import coverage_runs

    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    return {"runs": coverage_runs.list_runs("alert_analysis", principal.tenant_id or "default", scope_kind, scope_id)}


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: str,
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.core import coverage_runs

    run = coverage_runs.get_run("alert_analysis", principal.tenant_id or "default", run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Alerts Manager run not found.")
    return {"run": run}


@router.post("/evidence")
async def capture_evidence(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.evidence.registry import create_snapshot

    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    snapshot = await _snapshot(principal, scope_kind, scope_id, force=False, connection_id=connection_id)
    if not snapshot.get("report_exists"):
        raise HTTPException(status_code=404, detail="Run an Alerts Manager analysis before saving evidence.")
    metadata = create_snapshot(
        tenant_id=principal.tenant_id or "default",
        name=f"Alerts Manager — {snapshot.get('scope_name') or scope_id}",
        scope={"kind": scope_kind, "id": scope_id, "name": snapshot.get("scope_name") or scope_id},
        included=["inventory", "properties", "findings"],
        retention_class="standard",
        tags=["alerts-manager", "monitoring"],
        content={
            "inventory": {"rules": snapshot.get("rules", []), "action_groups": snapshot.get("action_groups", [])},
            "properties": {"kpis": snapshot.get("kpis", {})},
            "findings": {"overlaps": snapshot.get("overlaps", []), "gaps": snapshot.get("gaps", [])},
        },
        created_by=principal.subject,
        demo=bool(snapshot.get("demo")),
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="alert_analysis.evidence",
            target=metadata["id"],
            metadata_json={"scope": scope_id, "sha256": metadata["sha256"]},
        )
    )
    await db.commit()
    return {"ok": True, "snapshot": metadata}


class DecisionRequest(BaseModel):
    target_type: str
    target_id: str
    action: str
    reason: str = Field(default="", max_length=1000)
    consolidate_to: str = ""


@router.get("/decisions")
async def decision_list(
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alert_analysis.decisions import list_decisions

    return {"decisions": list_decisions(principal.tenant_id or "default", _decision_connection_id(connection_id))}


@router.post("/decisions")
async def decision_record(
    payload: DecisionRequest,
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alert_analysis.decisions import record_decision

    try:
        decision = record_decision(
            principal.tenant_id or "default",
            _decision_connection_id(connection_id),
            target_type=payload.target_type,
            target_id=payload.target_id,
            action=payload.action,
            actor=principal.subject,
            reason=payload.reason,
            consolidate_to=payload.consolidate_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="alert_analysis.decision", target=decision["id"],
        metadata_json={"decision": decision["action"], "reason": decision["reason"]},
    ))
    await db.commit()
    return {"decision": decision}


@router.delete("/decisions/{target_type}/{target_id:path}")
async def decision_delete(
    target_type: str,
    target_id: str,
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_manage),
) -> dict[str, Any]:
    from app.alert_analysis.decisions import delete_decision

    return {"ok": delete_decision(principal.tenant_id or "default", _decision_connection_id(connection_id), target_type, target_id)}


class PlanRequest(BaseModel):
    workload_id: str | None = None
    subscription_id: str | None = None
    management_group_id: str | None = None
    connection_id: str | None = None


class PlanDecision(BaseModel):
    decision: str
    reason: str = Field(default="", max_length=1000)


@router.post("/plans")
async def plan_create(
    payload: PlanRequest,
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alert_analysis.iac import generate_review_artifact
    from app.alert_analysis.plans import create_plan

    scope_kind, scope_id = _scope(payload.workload_id, payload.subscription_id, payload.management_group_id)
    snapshot = await _snapshot(principal, scope_kind, scope_id, force=False, connection_id=payload.connection_id)
    if not snapshot.get("report_exists"):
        raise HTTPException(status_code=404, detail="Run an analysis before creating a plan.")
    artifact, actions = generate_review_artifact(snapshot)
    plan = create_plan(
        tenant_id=principal.tenant_id or "default",
        connection_id=_decision_connection_id(payload.connection_id),
        scope_kind=scope_kind,
        scope_id=scope_id,
        scope_name=str(snapshot.get("scope_name") or scope_id),
        requested_by=principal.subject,
        artifact=artifact,
        actions=actions,
    )
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="alert_analysis.plan.requested", target=plan["id"],
        metadata_json={"actions": len(actions), "scope": scope_id, "safety": "no-execute"},
    ))
    await db.commit()
    return {"plan": plan}


@router.get("/plans")
async def plan_list(
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.alert_analysis.plans import list_plans

    return {"plans": list_plans(principal.tenant_id or "default")}


@router.post("/plans/{plan_id}/decision")
async def plan_decide(
    plan_id: str,
    payload: PlanDecision,
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alert_analysis.plans import decide_plan

    plan = decide_plan(principal.tenant_id or "default", plan_id, payload.decision, principal.subject, payload.reason)
    if plan is None:
        raise HTTPException(status_code=404, detail="Pending plan not found or invalid decision.")
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action=f"alert_analysis.plan.{payload.decision}", target=plan_id,
        metadata_json={"reason": payload.reason, "safety": "no-execute"},
    ))
    await db.commit()
    return {"plan": plan}


@router.delete("/plans/{plan_id}")
async def plan_delete(
    plan_id: str,
    principal: Principal = Depends(_manage),
) -> dict[str, Any]:
    from app.alert_analysis.plans import delete_plan

    return {"ok": delete_plan(principal.tenant_id or "default", plan_id)}

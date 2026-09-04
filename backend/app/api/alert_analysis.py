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


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the overview contract without shipping every analysis row to the browser."""
    result = {
        key: value for key, value in snapshot.items()
        if key not in {
            "rules", "overlaps", "gaps", "action_groups", "recipients", "decisions",
            "active_overlap_ids", "active_gap_keys",
        }
    }
    rules = list(snapshot.get("rules") or [])
    active_overlaps = [item for item in snapshot.get("overlaps") or [] if not item.get("accepted")]
    active_gaps = [item for item in snapshot.get("gaps") or [] if not item.get("accepted")]
    confidence_counts: dict[str, int] = {}
    for rule in rules:
        confidence = str((rule.get("cost") or {}).get("confidence") or "none")
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
    result["overview"] = {
        "clean_rules": sum(rule.get("finding_status") == "ok" for rule in rules),
        "cost_confidence_counts": confidence_counts,
    }
    result["section_totals"] = {
        "rules": len(rules),
        "overlaps": len(active_overlaps),
        "gaps": len(active_gaps),
        "action_groups": len(snapshot.get("action_groups") or []),
        "recipients": len(snapshot.get("recipients") or []),
    }
    result["rules"] = []
    result["overlaps"] = [
        {key: value for key, value in item.items() if key != "target_ids"}
        for item in active_overlaps[:12]
    ]
    result["gaps"] = active_gaps[:15]
    result["action_groups"] = list(snapshot.get("action_groups") or []) if snapshot.get("demo") else []
    result["recipients"] = []
    return result


def _section_page(
    snapshot: dict[str, Any], section: str, *, page: int, page_size: int,
    search: str = "", status: str = "", risk: str = "", gap_type: str = "",
    signal: str = "", sort: str = "", direction: str = "asc", group: str = "",
) -> dict[str, Any]:
    """Filter and sort one analysis section before slicing its requested page."""
    source = list(snapshot.get(section) or [])
    if section in {"overlaps", "gaps"}:
        source = [item for item in source if not item.get("accepted")]
    facets: dict[str, list[str]] = {}
    if section == "gaps":
        facets = {
            "risks": sorted({str(item.get("risk") or "") for item in source if item.get("risk")}),
            "types": sorted({str(item.get("type") or "") for item in source if item.get("type")}),
            "signals": sorted({str(item.get("signal") or "") for item in source if item.get("signal")}, key=str.lower),
        }

    query = search.strip().lower()
    if section == "rules":
        if status and status != "all":
            source = [item for item in source if item.get("finding_status") == status]
        if query:
            source = [
                item for item in source
                if query in " ".join((
                    str(item.get("name") or ""), str(item.get("type") or ""),
                    str(item.get("resource_group") or ""),
                    " ".join(str(value.get("signal_name") or "") for value in item.get("conditions") or []),
                    " ".join(str(value) for value in item.get("action_group_names") or []),
                )).lower()
            ]
    elif section == "overlaps":
        if query:
            source = [
                item for item in source
                if query in " ".join((
                    str(item.get("signal_name") or ""), str(item.get("target_id") or ""),
                    " ".join(str(value) for value in item.get("rule_names") or []),
                )).lower()
            ]
    else:
        source = [
            item for item in source
            if (not risk or risk == "all" or item.get("risk") == risk)
            and (not gap_type or gap_type == "all" or item.get("type") == gap_type)
            and (not signal or signal == "all" or item.get("signal") == signal)
            and (
                not query or query in " ".join((
                    str(item.get("type") or ""), str(item.get("resource_name") or ""),
                    str(item.get("rule_name") or ""), str(item.get("signal") or ""),
                )).lower()
            )
        ]

    reverse = direction == "desc"
    risk_order = {"critical": 0, "error": 1, "warning": 2, "informational": 3, "info": 3}
    if sort:
        if section == "rules":
            def rule_key(item: dict[str, Any]) -> tuple[Any, ...]:
                condition = (item.get("conditions") or [{}])[0]
                values = {
                    "status": str(item.get("finding_status") or ""),
                    "rule": str(item.get("name") or "").lower(),
                    "condition": f"{condition.get('signal_name', '')} {condition.get('aggregation', '')}".lower(),
                    "targets": int(item.get("effective_target_count") or 0),
                    "action_groups": len(item.get("action_group_names") or []),
                    "cost": (item.get("cost") or {}).get("monthly_usd"),
                    "firings": int(item.get("firing_30d") or item.get("firing_7d") or 0),
                }
                value = values.get(sort, "")
                if sort == "cost":
                    return (value is None, float(value or 0), str(item.get("name") or "").lower())
                return (value, str(item.get("name") or "").lower())
            if sort == "cost":
                priced = [item for item in source if (item.get("cost") or {}).get("monthly_usd") is not None]
                unpriced = [item for item in source if (item.get("cost") or {}).get("monthly_usd") is None]
                priced.sort(key=rule_key, reverse=reverse)
                source = priced + unpriced
            else:
                source.sort(key=rule_key, reverse=reverse)
        elif section == "overlaps":
            confidence_order = {"medium": 0, "high": 1}
            source.sort(key=lambda item: {
                "confidence": confidence_order.get(str(item.get("confidence") or ""), -1),
                "signal": f"{item.get('signal_name', '')} {item.get('target_id', '')}".lower(),
                "rules": len(item.get("rule_names") or []),
                "destinations": int(item.get("shared_recipient_count") or 0),
            }.get(sort, ""), reverse=reverse)
        else:
            source.sort(key=lambda item: {
                "risk": risk_order.get(str(item.get("risk") or ""), 99),
                "gap": str(item.get("type") or "").lower(),
                "resource": str(item.get("rule_name") or item.get("resource_name") or "").lower(),
                "signal": str(item.get("signal") or "").lower(),
                "recommendation": str(item.get("recommendation") or "").lower(),
            }.get(sort, ""), reverse=reverse)
    if section == "gaps" and group == "signal":
        if not sort:
            source.sort(key=lambda item: risk_order.get(str(item.get("risk") or ""), 99))
        source.sort(key=lambda item: str(item.get("signal") or "").lower())
    start = (page - 1) * page_size
    items = source[start:start + page_size]
    if section == "rules":
        items = [
            {
                **item,
                "effective_targets": [],
                "receiver_fingerprints": [],
                "duplicate_receiver_fingerprints": [],
            }
            for item in items
        ]
    return {
        "section": section,
        "items": items,
        "total": len(source),
        "page": page,
        "page_size": page_size,
        "facets": facets,
    }


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
        snapshot = await asyncio.to_thread(cache.read_snapshot, tenant_id, effective_connection_id, scope_kind, scope_id)
        if (
            force
            or snapshot is None
            or not cache.is_fresh(snapshot, ttl_s)
            or not snapshot.get("demo")
            or snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
            or "rationalization_score" not in snapshot
        ):
            snapshot = await asyncio.to_thread(demo.build_demo_snapshot, scope_id)
            if progress:
                await progress("compute", "Generated deterministic demo rules, routes, overlaps, gaps, and costs.")
            await asyncio.to_thread(cache.write_snapshot, tenant_id, effective_connection_id, scope_kind, scope_id, snapshot)
            if progress:
                await progress("save", "Saved analysis snapshot to the server cache.")
        return await asyncio.to_thread(_with_decisions, _decorate(snapshot, ttl_s), tenant_id, effective_connection_id)

    if not force:
        snapshot = await asyncio.to_thread(cache.read_snapshot, tenant_id, effective_connection_id, scope_kind, scope_id)
        if snapshot is not None and snapshot.get("schema_version") == SNAPSHOT_SCHEMA_VERSION:
            return await asyncio.to_thread(_with_decisions, _decorate(snapshot, ttl_s), tenant_id, effective_connection_id)
        empty = empty_snapshot(scope_kind, scope_id)
        empty["report_exists"] = False
        return await asyncio.to_thread(_with_decisions, _decorate(empty, ttl_s), tenant_id, effective_connection_id)

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
        await asyncio.to_thread(cache.write_snapshot, tenant_id, effective_connection_id, scope_kind, scope_id, snapshot)
        if progress:
            await progress("save", "Saved analysis snapshot to the server cache.")
        return await asyncio.to_thread(_with_decisions, _decorate(snapshot, ttl_s), tenant_id, effective_connection_id)


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
    await asyncio.to_thread(
        coverage_trends.record,
        "alert_analysis", principal.tenant_id or "default", scope_kind, scope_id,
        pct=score, extra=snapshot.get("kpis") or {}, demo=bool(snapshot.get("demo")),
    )
    if progress:
        await progress("save", "Saving full analysis run history…")
    await asyncio.to_thread(
        coverage_runs.save_run,
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


def _job_response(job: dict[str, Any] | None, *, compact: bool = False) -> dict[str, Any]:
    public = _refresh_jobs.public_job(job)
    if not job or not public:
        return {"job": None, "progress": [], "result": None}
    return {
        "job": public,
        "progress": list(job.get("progress") or []),
        "result": (
            _compact_snapshot(job["result"])
            if compact and job.get("status") == "done" and isinstance(job.get("result"), dict)
            else job.get("result") if job.get("status") == "done" else None
        ),
    }


@router.get("")
async def analysis(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    compact: bool = False,
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    snapshot = await _snapshot(principal, scope_kind, scope_id, force=False, connection_id=connection_id)
    return await asyncio.to_thread(_compact_snapshot, snapshot) if compact else snapshot


@router.get("/sections/{section}")
async def analysis_section(
    section: str,
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    search: str = Query(default="", max_length=300),
    status: str = Query(default="", max_length=40),
    risk: str = Query(default="", max_length=40),
    gap_type: str = Query(default="", max_length=120),
    signal: str = Query(default="", max_length=300),
    sort: str = Query(default="", max_length=40),
    group: str = Query(default="", max_length=40),
    direction: str = Query(default="asc", pattern="^(asc|desc)$"),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    if section not in {"rules", "overlaps", "gaps"}:
        raise HTTPException(status_code=404, detail="Unknown alert-analysis section.")
    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    snapshot = await _snapshot(principal, scope_kind, scope_id, force=False, connection_id=connection_id)
    return await asyncio.to_thread(
        _section_page, snapshot, section, page=page, page_size=page_size,
        search=search, status=status, risk=risk, gap_type=gap_type, signal=signal,
        sort=sort, direction=direction, group=group,
    )


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
    compact: bool = False,
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

    job = await _refresh_jobs.start(key, runner, tenant_id=tenant_id)
    return _job_response(job, compact=compact)


@router.get("/refresh/job")
async def refresh_job(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    compact: bool = False,
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    """Return the current/recent scope job, its replayable log, and completed snapshot."""
    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    effective_connection_id = _effective_connection_id(scope_kind, scope_id, connection_id)
    key = _refresh_job_key(principal.tenant_id or "default", effective_connection_id, scope_kind, scope_id)
    return _job_response(
        await _refresh_jobs.get_job(key, tenant_id=principal.tenant_id or "default"),
        compact=compact,
    )


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

        points = await asyncio.to_thread(coverage_trends.series, "alert_analysis", principal.tenant_id or "default", scope_kind, scope_id)
        content = await asyncio.to_thread(export.to_workbook, snapshot, points)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif format == "csv":
        content = await asyncio.to_thread(export.to_csv, snapshot)
        media_type = "text/csv; charset=utf-8"
    else:
        content = await asyncio.to_thread(export.to_json, snapshot)
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
    return await asyncio.to_thread(coverage_trends.trend, "alert_analysis", principal.tenant_id or "default", scope_kind, scope_id)


@router.get("/runs")
async def runs(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    management_group_id: str | None = Query(default=None),
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.core import coverage_runs

    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    values = await asyncio.to_thread(coverage_runs.list_runs, "alert_analysis", principal.tenant_id or "default", scope_kind, scope_id)
    return {"runs": values}


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: str,
    principal: Principal = Depends(_read),
) -> dict[str, Any]:
    from app.core import coverage_runs

    run = await asyncio.to_thread(coverage_runs.get_run, "alert_analysis", principal.tenant_id or "default", run_id)
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
    metadata = await asyncio.to_thread(
        create_snapshot,
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

    values = await asyncio.to_thread(
        list_decisions, principal.tenant_id or "default", _decision_connection_id(connection_id)
    )
    return {"decisions": values}


@router.post("/decisions")
async def decision_record(
    payload: DecisionRequest,
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alert_analysis.decisions import record_decision

    try:
        decision = await asyncio.to_thread(
            record_decision,
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

    deleted = await asyncio.to_thread(
        delete_decision,
        principal.tenant_id or "default", _decision_connection_id(connection_id), target_type, target_id,
    )
    return {"ok": deleted}


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
    def build_plan() -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
        return plan, actions

    plan, actions = await asyncio.to_thread(build_plan)
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

    return {"plans": await asyncio.to_thread(list_plans, principal.tenant_id or "default")}


@router.post("/plans/{plan_id}/decision")
async def plan_decide(
    plan_id: str,
    payload: PlanDecision,
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.alert_analysis.plans import decide_plan

    plan = await asyncio.to_thread(
        decide_plan, principal.tenant_id or "default", plan_id,
        payload.decision, principal.subject, payload.reason,
    )
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

    return {"ok": await asyncio.to_thread(delete_plan, principal.tenant_id or "default", plan_id)}

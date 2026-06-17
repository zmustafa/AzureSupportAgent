"""Assessment endpoints: catalog, runs (SSE), history, baselines, waivers, finding
ownership, remediation tickets, custom checks (+AI), schedules, portfolio, trend, export.

RBAC: reads require ``assessments.read``; mutations require ``assessments.run``. Admins
have both. Key mutations are written to the audit log (and thus flow to SIEM export)."""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from app.assessments import catalog
from app.assessments import custom_checks as cc_registry
from app.assessments.runner import run_assessment
from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AssessmentFindingState, AssessmentRun, AssessmentWaiver, AuditLog

router = APIRouter(prefix="/assessments", tags=["assessments"])
logger = logging.getLogger("app.api.assessments")

read_dep = require_permission("assessments.read")
write_dep = require_permission("assessments.run")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _audit(db: AsyncSession, principal: Principal, action: str, target: str, **meta) -> None:
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action=action,
            target=target[:512],
            metadata_json=meta or {},
        )
    )


def _run_dict(r: AssessmentRun, *, full: bool = False) -> dict:
    base = {
        "id": r.id,
        "workload_id": r.workload_id,
        "workload_name": r.workload_name,
        "connection_id": r.connection_id,
        "pillars": r.pillars or [],
        "status": r.status,
        "overall_score": r.overall_score,
        "scores": r.scores_json or {},
        "totals": r.totals_json or {},
        "severity": r.severity,
        "summary": r.summary,
        "used_ai": r.used_ai,
        "resource_count": r.resource_count,
        "catalog_version": getattr(r, "catalog_version", None),
        "schema_version": getattr(r, "schema_version", None),
        "completeness_pct": getattr(r, "completeness_pct", None),
        "confidence": getattr(r, "confidence", None),
        "baseline_run_id": r.baseline_run_id,
        "is_baseline": bool(getattr(r, "is_baseline", False)),
        "diff": r.diff_json,
        "trigger": r.trigger,
        "triggered_by": r.triggered_by,
        "duration_ms": r.duration_ms,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "deleted_at": r.deleted_at.isoformat() if getattr(r, "deleted_at", None) else None,
    }
    if full:
        base["findings"] = r.findings_json or []
        base["resources"] = r.resources_json or []
        base["compliance"] = catalog.compliance_coverage(r.findings_json or [])
    return base


async def _get_run(db: AsyncSession, principal: Principal, run_id: str) -> AssessmentRun:
    run = (
        await db.execute(
            select(AssessmentRun).where(
                AssessmentRun.id == run_id, AssessmentRun.tenant_id == principal.tenant_id
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Assessment run not found.")
    return run


# ============================ Catalog ============================
@router.get("/catalog")
async def get_catalog(_: Principal = Depends(read_dep)):
    return catalog.public_catalog()


# ============================ Run (SSE) ============================
class AssessmentRunRequest(BaseModel):
    workload_id: str
    pillars: list[str] = Field(default_factory=lambda: list(catalog.PILLARS))
    pack: str | None = None  # 'waf'|'wara'|'wasa' — overrides pillars when set
    connection_id: str | None = None
    use_ai: bool = True


@router.post("/run")
async def run_assessment_endpoint(payload: AssessmentRunRequest, principal: Principal = Depends(write_dep)):
    pillars = catalog.pack_pillars(payload.pack) if payload.pack else None
    if pillars is None:
        pillars = payload.pillars

    async def _gen():
        run_id = ""
        try:
            async for ev in run_assessment(
                workload_id=payload.workload_id,
                pillars=pillars,
                tenant_id=principal.tenant_id,
                connection_id=payload.connection_id,
                actor=principal.subject,
                trigger=(payload.pack or "manual"),
                use_ai=payload.use_ai,
            ):
                if ev.get("type") == "done":
                    run_id = ev.get("run_id", "")
                ev_type = ev.pop("type")
                yield {"event": ev_type, "data": json.dumps(ev)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Assessment run failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}
        if run_id:
            from app.core.db import SessionLocal

            async with SessionLocal() as db:
                await _audit(db, principal, "assessment.run", run_id, workload_id=payload.workload_id)
                await db.commit()

    return EventSourceResponse(_gen())


# ============================ Enqueue (background batch) ============================
# Strong refs to in-flight background tasks so they aren't garbage-collected mid-run.
_BG_TASKS: set = set()


def _spawn_assessment(
    *,
    run_id: str,
    workload_id: str,
    pillars: list[str],
    tenant_id: str,
    connection_id: str | None,
    actor: str,
    use_ai: bool,
    trigger: str = "manual",
) -> None:
    import asyncio

    from app.assessments.runner import run_assessment_to_completion

    task = asyncio.create_task(
        run_assessment_to_completion(
            run_id=run_id,
            workload_id=workload_id,
            pillars=pillars,
            tenant_id=tenant_id,
            connection_id=connection_id,
            actor=actor,
            trigger=trigger,
            use_ai=use_ai,
        )
    )
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


class AssessmentEnqueueRequest(BaseModel):
    workload_ids: list[str] = Field(default_factory=list)
    pillars: list[str] = Field(default_factory=lambda: list(catalog.PILLARS))
    pack: str | None = None  # 'waf'|'wara'|'wasa' — overrides pillars when set
    connection_id: str | None = None
    use_ai: bool = True


@router.post("/enqueue")
async def enqueue_assessments_endpoint(
    payload: AssessmentEnqueueRequest,
    principal: Principal = Depends(write_dep),
    db: AsyncSession = Depends(get_db),
):
    """Queue one background assessment run per selected workload (each covering all
    selected pillars). Returns the created run rows immediately with status='queued';
    progress is reflected by the run's status in history."""
    from app.workloads.registry import get_workload

    if not payload.workload_ids:
        raise HTTPException(status_code=400, detail="Select at least one workload.")
    pack_pillars = catalog.pack_pillars(payload.pack) if payload.pack else None
    source_pillars = pack_pillars if pack_pillars is not None else payload.pillars
    pillars = [p for p in source_pillars if p in catalog.PILLARS]
    if not pillars:
        raise HTTPException(status_code=400, detail="Select at least one assessment type.")
    trigger = payload.pack if (payload.pack and pack_pillars is not None) else "manual"

    # Create all queued run rows in the request's own session (single SQLite writer) so
    # we don't deadlock against a separate write transaction.
    new_runs: list[AssessmentRun] = []
    for wid in payload.workload_ids:
        wl = get_workload(wid)
        if wl is None:
            continue
        run = AssessmentRun(
            workload_id=wid,
            workload_name=wl.get("name", "workload"),
            tenant_id=principal.tenant_id,
            connection_id=payload.connection_id or None,
            pillars=pillars,
            status="queued",
            triggered_by=principal.subject,
            trigger=trigger,
        )
        db.add(run)
        new_runs.append(run)
    if not new_runs:
        raise HTTPException(status_code=404, detail="None of the selected workloads were found.")
    await db.flush()
    for run in new_runs:
        await _audit(db, principal, "assessment.enqueue", run.id, workload_id=run.workload_id)
    created = [_run_dict(run) for run in new_runs]
    spawn_args = [
        {
            "run_id": run.id,
            "workload_id": run.workload_id,
            "pillars": pillars,
            "tenant_id": principal.tenant_id,
            "connection_id": payload.connection_id,
            "actor": principal.subject,
            "use_ai": payload.use_ai,
            "trigger": trigger,
        }
        for run in new_runs
    ]
    await db.commit()
    # Spawn background workers only after the request's own writes are committed, so the
    # request session and the background writers don't contend on the SQLite lock.
    for args in spawn_args:
        _spawn_assessment(**args)
    return {"runs": created, "queued": len(created)}


# ============================ Runs / history ============================
@router.get("/runs")
async def list_runs_endpoint(
    principal: Principal = Depends(read_dep),
    db: AsyncSession = Depends(get_db),
    workload_id: str | None = None,
    limit: int = 50,
):
    limit = max(1, min(limit, 200))
    stmt = select(AssessmentRun).where(
        AssessmentRun.tenant_id == principal.tenant_id,
        AssessmentRun.deleted_at.is_(None),
    )
    if workload_id:
        stmt = stmt.where(AssessmentRun.workload_id == workload_id)
    stmt = stmt.order_by(desc(AssessmentRun.started_at)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return {"runs": [_run_dict(r) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run_endpoint(run_id: str, principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)):
    run = await _get_run(db, principal, run_id)
    return {"run": _run_dict(run, full=True)}


@router.delete("/runs/{run_id}")
async def delete_run_endpoint(run_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    """Soft-delete (move to trash). The run is hidden from history but recoverable."""
    run = await _get_run(db, principal, run_id)
    run.deleted_at = _now()
    if run.is_baseline:
        run.is_baseline = False
    await _audit(db, principal, "assessment.run.trash", run_id)
    await db.commit()
    return {"ok": True}


@router.post("/runs/{run_id}/cancel")
async def cancel_run_endpoint(run_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    """Request cancellation of a queued or running assessment (cooperative)."""
    from app.assessments.runner import request_cancel

    run = await _get_run(db, principal, run_id)
    if run.status not in ("queued", "running"):
        raise HTTPException(status_code=400, detail="Only queued or running assessments can be cancelled.")
    request_cancel(run_id)
    # If it hasn't started executing yet, mark it cancelled immediately so the UI updates.
    if run.status == "queued":
        run.status = "cancelled"
        run.ended_at = _now()
    await _audit(db, principal, "assessment.run.cancel", run_id)
    await db.commit()
    return {"run": _run_dict(run)}


# ============================ Trash ============================
@router.get("/trash")
async def list_trash_endpoint(principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)):
    """Soft-deleted assessment runs, most-recently-trashed first."""
    rows = (
        await db.execute(
            select(AssessmentRun)
            .where(
                AssessmentRun.tenant_id == principal.tenant_id,
                AssessmentRun.deleted_at.is_not(None),
            )
            .order_by(desc(AssessmentRun.deleted_at))
            .limit(200)
        )
    ).scalars().all()
    return {"runs": [_run_dict(r) for r in rows]}


@router.post("/runs/{run_id}/restore")
async def restore_run_endpoint(run_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    """Restore a soft-deleted run from the trash back into history."""
    run = await _get_run(db, principal, run_id)
    run.deleted_at = None
    await _audit(db, principal, "assessment.run.restore", run_id)
    await db.commit()
    return {"run": _run_dict(run)}


@router.delete("/runs/{run_id}/purge")
async def purge_run_endpoint(run_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    """Permanently delete a single trashed run."""
    run = await _get_run(db, principal, run_id)
    if run.deleted_at is None:
        raise HTTPException(status_code=400, detail="Run is not in the trash.")
    await db.delete(run)
    await _audit(db, principal, "assessment.run.purge", run_id)
    await db.commit()
    return {"ok": True}


@router.post("/trash/empty")
async def empty_trash_endpoint(principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    """Permanently delete every trashed run for the tenant."""
    rows = (
        await db.execute(
            select(AssessmentRun).where(
                AssessmentRun.tenant_id == principal.tenant_id,
                AssessmentRun.deleted_at.is_not(None),
            )
        )
    ).scalars().all()
    for r in rows:
        await db.delete(r)
    await _audit(db, principal, "assessment.trash.empty", "all", count=len(rows))
    await db.commit()
    return {"ok": True, "purged": len(rows)}


@router.post("/runs/{run_id}/baseline")
async def set_baseline_endpoint(
    run_id: str,
    payload: dict | None = None,
    principal: Principal = Depends(write_dep),
    db: AsyncSession = Depends(get_db),
):
    """Pin (or unpin) this run as the workload's baseline for drift comparison."""
    run = await _get_run(db, principal, run_id)
    make = True if payload is None else bool(payload.get("baseline", True))
    if make:
        others = (
            await db.execute(
                select(AssessmentRun).where(
                    AssessmentRun.tenant_id == principal.tenant_id,
                    AssessmentRun.workload_id == run.workload_id,
                    AssessmentRun.is_baseline.is_(True),
                )
            )
        ).scalars().all()
        for o in others:
            o.is_baseline = False
    run.is_baseline = make
    await _audit(db, principal, "assessment.baseline", run_id, baseline=make)
    await db.commit()
    return {"run": _run_dict(run)}


# ============================ Trend / portfolio ============================
@router.get("/trend")
async def trend_endpoint(
    workload_id: str,
    principal: Principal = Depends(read_dep),
    db: AsyncSession = Depends(get_db),
    limit: int = 30,
):
    """Score history (oldest→newest) for a workload, for sparklines/trend charts."""
    limit = max(1, min(limit, 100))
    rows = (
        await db.execute(
            select(AssessmentRun)
            .where(
                AssessmentRun.tenant_id == principal.tenant_id,
                AssessmentRun.workload_id == workload_id,
                AssessmentRun.status == "succeeded",
                AssessmentRun.deleted_at.is_(None),
            )
            .order_by(desc(AssessmentRun.started_at))
            .limit(limit)
        )
    ).scalars().all()
    points = [
        {
            "run_id": r.id,
            "at": r.started_at.isoformat() if r.started_at else None,
            "overall": r.overall_score,
            "scores": {p: (v or {}).get("score") for p, v in (r.scores_json or {}).items()},
        }
        for r in reversed(rows)
    ]
    return {"workload_id": workload_id, "points": points}


@router.get("/portfolio")
async def portfolio_endpoint(principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)):
    """A heatmap row per workload: latest per-pillar scores, overall, failed counts, trend."""
    rows = (
        await db.execute(
            select(AssessmentRun)
            .where(
                AssessmentRun.tenant_id == principal.tenant_id,
                AssessmentRun.status == "succeeded",
                AssessmentRun.deleted_at.is_(None),
            )
            .order_by(desc(AssessmentRun.started_at))
        )
    ).scalars().all()
    latest: dict[str, AssessmentRun] = {}
    history: dict[str, list[int]] = {}
    for r in rows:
        if r.workload_id not in latest:
            latest[r.workload_id] = r
        if r.overall_score is not None:
            history.setdefault(r.workload_id, []).append(r.overall_score)
    items = []
    for wid, r in latest.items():
        spark = list(reversed(history.get(wid, [])))[-12:]
        items.append(
            {
                "workload_id": wid,
                "workload_name": r.workload_name,
                "run_id": r.id,
                "overall_score": r.overall_score,
                "scores": {p: (v or {}).get("score") for p, v in (r.scores_json or {}).items()},
                "failed": (r.totals_json or {}).get("failed", 0),
                "severity": r.severity,
                "at": r.started_at.isoformat() if r.started_at else None,
                "sparkline": spark,
            }
        )
    items.sort(key=lambda x: (x["overall_score"] if x["overall_score"] is not None else 999))
    return {"workloads": items}


# ============================ Waivers ============================
class WaiverCreate(BaseModel):
    workload_id: str
    check_id: str
    resource_id: str | None = None
    justification: str = Field(default="", max_length=4000)
    approver: str = Field(default="", max_length=256)
    expires_at: datetime | None = None


def _waiver_dict(w: AssessmentWaiver) -> dict:
    exp_dt = w.expires_at.replace(tzinfo=w.expires_at.tzinfo or timezone.utc) if w.expires_at else None
    expired = bool(exp_dt and exp_dt <= _now())
    return {
        "id": w.id,
        "workload_id": w.workload_id,
        "check_id": w.check_id,
        "resource_id": w.resource_id,
        "justification": w.justification,
        "approver": w.approver,
        "status": "expired" if (w.status == "active" and expired) else w.status,
        "expires_at": w.expires_at.isoformat() if w.expires_at else None,
        "created_by": w.created_by,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


@router.get("/waivers")
async def list_waivers_endpoint(
    principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db), workload_id: str | None = None
):
    stmt = select(AssessmentWaiver).where(AssessmentWaiver.tenant_id == principal.tenant_id)
    if workload_id:
        stmt = stmt.where(AssessmentWaiver.workload_id == workload_id)
    rows = (await db.execute(stmt.order_by(desc(AssessmentWaiver.created_at)))).scalars().all()
    return {"waivers": [_waiver_dict(w) for w in rows]}


@router.post("/waivers")
async def create_waiver_endpoint(payload: WaiverCreate, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    w = AssessmentWaiver(
        tenant_id=principal.tenant_id,
        workload_id=payload.workload_id,
        check_id=payload.check_id,
        resource_id=payload.resource_id or None,
        justification=payload.justification,
        approver=payload.approver,
        expires_at=payload.expires_at,
        created_by=principal.subject,
    )
    db.add(w)
    await _audit(db, principal, "assessment.waiver.create", payload.check_id, workload_id=payload.workload_id)
    await db.commit()
    await db.refresh(w)
    return {"waiver": _waiver_dict(w)}


@router.delete("/waivers/{waiver_id}")
async def revoke_waiver_endpoint(waiver_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    w = (
        await db.execute(
            select(AssessmentWaiver).where(
                AssessmentWaiver.id == waiver_id, AssessmentWaiver.tenant_id == principal.tenant_id
            )
        )
    ).scalar_one_or_none()
    if w is None:
        raise HTTPException(status_code=404, detail="Waiver not found.")
    w.status = "revoked"
    w.revoked_at = _now()
    w.revoked_by = principal.subject
    await _audit(db, principal, "assessment.waiver.revoke", w.check_id, workload_id=w.workload_id)
    await db.commit()
    return {"ok": True}


# ============================ Finding ownership / state ============================
class FindingStateUpdate(BaseModel):
    workload_id: str
    check_id: str
    status: str | None = None  # open|in_progress|resolved|waived|risk_accepted
    assignee: str | None = None
    due_date: datetime | None = None
    notes: str | None = None


_VALID_STATES = {"open", "in_progress", "resolved", "waived", "risk_accepted"}


def _state_dict(s: AssessmentFindingState) -> dict:
    return {
        "workload_id": s.workload_id,
        "check_id": s.check_id,
        "status": s.status,
        "assignee": s.assignee,
        "due_date": s.due_date.isoformat() if s.due_date else None,
        "notes": s.notes,
        "ticket_url": s.ticket_url,
        "ticket_id": s.ticket_id,
        "ticket_connector": s.ticket_connector,
        "updated_by": s.updated_by,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/finding-states")
async def list_states_endpoint(
    workload_id: str, principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.execute(
            select(AssessmentFindingState).where(
                AssessmentFindingState.tenant_id == principal.tenant_id,
                AssessmentFindingState.workload_id == workload_id,
            )
        )
    ).scalars().all()
    return {"states": {s.check_id: _state_dict(s) for s in rows}}


async def _get_or_create_state(db: AsyncSession, principal: Principal, workload_id: str, check_id: str) -> AssessmentFindingState:
    s = (
        await db.execute(
            select(AssessmentFindingState).where(
                AssessmentFindingState.tenant_id == principal.tenant_id,
                AssessmentFindingState.workload_id == workload_id,
                AssessmentFindingState.check_id == check_id,
            )
        )
    ).scalar_one_or_none()
    if s is None:
        s = AssessmentFindingState(
            tenant_id=principal.tenant_id, workload_id=workload_id, check_id=check_id, updated_by=principal.subject
        )
        db.add(s)
    return s


@router.put("/finding-states")
async def update_state_endpoint(payload: FindingStateUpdate, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    if payload.status and payload.status not in _VALID_STATES:
        raise HTTPException(status_code=400, detail="Invalid status.")
    s = await _get_or_create_state(db, principal, payload.workload_id, payload.check_id)
    if payload.status is not None:
        s.status = payload.status
    if payload.assignee is not None:
        s.assignee = payload.assignee or None
    if payload.due_date is not None:
        s.due_date = payload.due_date
    if payload.notes is not None:
        s.notes = payload.notes
    s.updated_by = principal.subject
    s.updated_at = _now()
    await _audit(db, principal, "assessment.finding.update", payload.check_id, workload_id=payload.workload_id, status=s.status)
    await db.commit()
    await db.refresh(s)
    return {"state": _state_dict(s)}


# ============================ Remediation tickets ============================
class TicketRequest(BaseModel):
    run_id: str
    check_id: str
    connector_id: str


@router.post("/ticket")
async def create_ticket_endpoint(payload: TicketRequest, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    from app.assessments.tickets import create_ticket

    run = await _get_run(db, principal, payload.run_id)
    finding = next((f for f in (run.findings_json or []) if f.get("check_id") == payload.check_id), None)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found in run.")
    result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=run.workload_name or "")
    if result.get("ok"):
        s = await _get_or_create_state(db, principal, run.workload_id, payload.check_id)
        s.ticket_url = result.get("ticket_url") or s.ticket_url
        s.ticket_id = result.get("ticket_id") or s.ticket_id
        s.ticket_connector = result.get("connector_type") or s.ticket_connector
        if s.status == "open":
            s.status = "in_progress"
        s.updated_by = principal.subject
        s.updated_at = _now()
        await _audit(db, principal, "assessment.ticket.create", payload.check_id, workload_id=run.workload_id, ticket=result.get("ticket_id", ""))
        await db.commit()
    return result


# ============================ Custom checks ============================
class CustomCheckUpsert(BaseModel):
    id: str | None = None
    pillar: str = "security"
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    severity: str = "warning"
    resource_types: list[str] = Field(default_factory=list)
    kql: str = Field(default="", max_length=4000)
    remediation: str = Field(default="", max_length=2000)
    remediation_command: str = Field(default="", max_length=1000)
    frameworks: dict[str, list[str]] = Field(default_factory=dict)
    enabled: bool = True


@router.get("/custom-checks")
async def list_custom_checks_endpoint(_: Principal = Depends(read_dep)):
    return {"checks": cc_registry.list_custom_checks()}


@router.put("/custom-checks")
async def upsert_custom_check_endpoint(payload: CustomCheckUpsert, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    data = payload.model_dump(exclude_none=True)
    data["created_by"] = principal.subject
    saved = cc_registry.upsert_custom_check(data)
    await _audit(db, principal, "assessment.check.upsert", saved["id"], title=saved.get("title"))
    await db.commit()
    return {"check": saved}


@router.delete("/custom-checks/{check_id}")
async def delete_custom_check_endpoint(check_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    if not cc_registry.delete_custom_check(check_id):
        raise HTTPException(status_code=404, detail="Custom check not found.")
    await _audit(db, principal, "assessment.check.delete", check_id)
    await db.commit()
    return {"ok": True}


class GenerateCheckRequest(BaseModel):
    goal: str = Field(max_length=2000)


@router.post("/custom-checks/generate")
async def generate_custom_check_endpoint(payload: GenerateCheckRequest, _: Principal = Depends(write_dep)):
    from app.assessments.designer import generate_check

    draft = await generate_check(payload.goal)
    if draft is None:
        raise HTTPException(status_code=502, detail="The AI could not generate a check. Try rephrasing the goal.")
    return {"draft": draft}


# ============================ Schedules ============================
# Assessment scheduling has been UNIFIED into the central scheduler
# (POST/GET /admin/automations/tasks with target_type="assessment"). The legacy
# per-assessment schedule endpoints were removed; existing JSON schedules are migrated
# into ScheduledTask rows on startup by the scheduler's one-time importer.


# ============================ Export ============================
async def _resolve_subscription_names(run: AssessmentRun, findings: list) -> dict[str, str]:
    """Best-effort ``subscription_id -> display name`` map for the subscriptions referenced by a
    run's flagged resources, via a single read-only Resource Graph query against the run's
    connection. Returns ``{}`` when there's nothing to resolve, no connection, or the query
    fails — the export still succeeds with blank subscription names in that case."""
    sub_ids = {
        sid
        for f in findings
        for r in (f.get("flagged_resources") or [])
        if (sid := r.get("subscription_id"))
    }
    if not sub_ids:
        return {}
    try:
        from app.core.azure_connections import resolve_connection
        from app.exec.command_runner import run_kql_capture

        connection = resolve_connection(run.connection_id or None)
        quoted = ", ".join("'" + s.replace("'", "''") + "'" for s in sorted(sub_ids))
        kql = (
            "ResourceContainers "
            "| where type =~ 'microsoft.resources/subscriptions' "
            f"| where subscriptionId in~ ({quoted}) "
            "| project subscriptionId, name"
        )
        cap = await run_kql_capture(kql, connection, output="json")
        if not cap.ok:
            return {}
        rows = json.loads(cap.stdout or "[]")
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("value") or []
        return {
            r.get("subscriptionId", ""): r.get("name", "")
            for r in rows
            if isinstance(r, dict) and r.get("subscriptionId")
        }
    except Exception:  # noqa: BLE001 - export must never fail because name lookup did
        logger.warning("Subscription name resolution failed for run %s", run.id, exc_info=True)
        return {}


@router.get("/runs/{run_id}/export")
async def export_run_endpoint(
    run_id: str, format: str = "json", principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)
):
    run = await _get_run(db, principal, run_id)
    findings = run.findings_json or []
    if format == "csv":
        sub_names = await _resolve_subscription_names(run, findings)
        buf = io.StringIO()
        w = csv.writer(buf)
        # One row per flagged resource (findings with no resources emit a single row with
        # empty resource columns) so the export is pivot-friendly and includes resource ids.
        w.writerow([
            "workload", "check_id", "pillar", "title", "severity", "status", "flagged_count",
            "resource_name", "resource_id", "subscription_id", "subscription_name", "resource_group", "resource_type",
            "portal_url", "cis", "nist", "iso", "remediation", "remediation_command",
        ])
        for f in findings:
            fw = f.get("frameworks") or {}
            base = [
                run.workload_name or "", f.get("check_id"), f.get("pillar"), f.get("title"),
                f.get("severity"), f.get("status"), f.get("flagged_count", 0),
            ]
            frameworks_cols = [
                "; ".join(fw.get("cis", [])), "; ".join(fw.get("nist", [])), "; ".join(fw.get("iso", [])),
            ]
            remediation = f.get("remediation", "")
            template_cmd = f.get("remediation_command", "")
            resources = f.get("flagged_resources") or []
            if resources:
                for r in resources:
                    rid = r.get("id", "")
                    portal = f"https://portal.azure.com/#@/resource{rid}/overview" if rid else ""
                    sub_id = r.get("subscription_id", "")
                    # Prefer the real, per-resource command (placeholders already filled).
                    cmd = r.get("remediation_command") or template_cmd
                    w.writerow(base + [
                        r.get("name", ""), rid, sub_id, sub_names.get(sub_id, ""),
                        r.get("resource_group", ""), r.get("type", ""), portal,
                    ] + frameworks_cols + [remediation, cmd])
            else:
                w.writerow(base + ["", "", "", "", "", "", ""] + frameworks_cols + [remediation, template_cmd])
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="assessment-{run_id}.csv"'},
        )
    payload = _run_dict(run, full=True)
    # Enrich each flagged resource with a ready-to-use Azure portal deep link + subscription name.
    sub_names = await _resolve_subscription_names(run, findings)
    for f in payload.get("findings", []):
        for r in f.get("flagged_resources", []) or []:
            rid = r.get("id", "")
            r["portal_url"] = f"https://portal.azure.com/#@/resource{rid}/overview" if rid else ""
            r["subscription_name"] = sub_names.get(r.get("subscription_id", ""), "")
    if format == "pdf":
        from app.assessments.pdf_report import build_pdf

        pdf_bytes = await run_in_threadpool(build_pdf, payload)
        safe_name = (run.workload_name or "workload").replace(" ", "_")
        date = (run.ended_at or run.started_at or _now()).strftime("%Y%m%d")
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="assessment-{safe_name}-{date}.pdf"'},
        )
    return StreamingResponse(
        iter([json.dumps(payload, indent=2)]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="assessment-{run_id}.json"'},
    )


# ============================ Manual attestations ============================
class AttestationUpdate(BaseModel):
    workload_id: str
    check_id: str
    status: str = ""  # pass|fail|not_applicable, or "" to clear (revert to pending)
    note: str = Field(default="", max_length=2000)


@router.get("/attestations")
async def list_attestations_endpoint(workload_id: str, principal: Principal = Depends(read_dep)):
    from app.assessments.attestations import get_attestations

    return {"attestations": get_attestations(principal.tenant_id, workload_id)}


@router.put("/attestations")
async def set_attestation_endpoint(payload: AttestationUpdate, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)):
    """Record (or clear) a human attestation for a manual control on a workload. Takes effect
    on the next run of that workload (the control then scores pass/fail/N-A instead of pending)."""
    if payload.status and payload.status not in ("pass", "fail", "not_applicable"):
        raise HTTPException(status_code=400, detail="status must be pass, fail, not_applicable, or empty to clear.")
    from app.assessments.attestations import set_attestation

    entry = set_attestation(
        principal.tenant_id, payload.workload_id, payload.check_id,
        status=payload.status, note=payload.note, by=principal.subject,
    )
    await _audit(db, principal, "assessment.attestation.set", payload.check_id, workload_id=payload.workload_id, status=payload.status or "cleared")
    await db.commit()
    return {"attestation": entry}


# ============================ Action plan (prioritized) ============================
# Impact / effort ordinal maps so a finding can be ranked by remediation value.
_IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "": 1}
_EFFORT_DIVISOR = {"low": 1.0, "medium": 2.0, "high": 3.0, "": 2.0}
_SEV_RANK = {"critical": 4, "error": 3, "warning": 2, "info": 1}


def _priority_score(f: dict) -> float:
    """Rank a failed finding by remediation value: severity × impact × breadth ÷ effort,
    mirroring the WARA Action Plan ordering so the most valuable fixes float to the top."""
    sev = _SEV_RANK.get(f.get("severity", "info"), 1)
    impact = _IMPACT_RANK.get((f.get("impact") or "").lower(), 1)
    flagged = int(f.get("flagged_count") or 0)
    breadth = 1.0 + min(flagged, 50) / 10.0  # diminishing weight for very large blast radius
    effort = _EFFORT_DIVISOR.get((f.get("effort") or "").lower(), 2.0)
    return round(sev * impact * breadth / effort, 3)


@router.get("/runs/{run_id}/action-plan")
async def action_plan_endpoint(run_id: str, principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)):
    """A prioritized remediation plan: every failed control ranked by severity × impact ×
    blast-radius ÷ effort (highest value first), plus the manual controls still pending."""
    run = await _get_run(db, principal, run_id)
    findings = run.findings_json or []
    failed = [f for f in findings if f.get("status") == "fail"]
    items = sorted(failed, key=_priority_score, reverse=True)
    plan = [
        {
            "rank": i + 1,
            "check_id": f.get("check_id"),
            "title": f.get("title"),
            "pillar": f.get("pillar"),
            "sub_category": f.get("sub_category", ""),
            "severity": f.get("severity"),
            "impact": f.get("impact", ""),
            "effort": f.get("effort", ""),
            "flagged_count": f.get("flagged_count", 0),
            "partial": bool(f.get("partial")),
            "priority": _priority_score(f),
            "remediation": f.get("remediation", ""),
            "remediation_command": f.get("remediation_command", ""),
            "ai_rationale": f.get("ai_rationale", ""),
            "source": f.get("source", "built-in"),
        }
        for i, f in enumerate(items)
    ]
    pending_manual = [
        {"check_id": f.get("check_id"), "title": f.get("title"), "pillar": f.get("pillar"),
         "sub_category": f.get("sub_category", ""), "severity": f.get("severity"),
         "remediation": f.get("remediation", "")}
        for f in findings if f.get("status") == "manual"
    ]
    return {
        "run_id": run_id,
        "workload_name": run.workload_name,
        "overall_score": run.overall_score,
        "completeness_pct": getattr(run, "completeness_pct", None),
        "confidence": getattr(run, "confidence", None),
        "plan": plan,
        "pending_manual": pending_manual,
    }


# ============================ Per-resource rollup ============================
@router.get("/runs/{run_id}/by-resource")
async def by_resource_endpoint(run_id: str, principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)):
    """Pivot a run's failed findings by resource: for each impacted resource, the list of
    controls that flagged it (worst severity first). Complements the per-check view."""
    run = await _get_run(db, principal, run_id)
    findings = run.findings_json or []
    by_res: dict[str, dict] = {}
    for f in findings:
        if f.get("status") != "fail":
            continue
        for r in (f.get("flagged_resources") or []):
            rid = r.get("id", "")
            if not rid:
                continue
            entry = by_res.setdefault(rid, {
                "id": rid,
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "resource_group": r.get("resource_group", ""),
                "subscription_id": r.get("subscription_id", ""),
                "findings": [],
                "worst_severity": "info",
            })
            entry["findings"].append({
                "check_id": f.get("check_id"),
                "title": f.get("title"),
                "pillar": f.get("pillar"),
                "severity": f.get("severity"),
                "remediation_command": r.get("remediation_command") or f.get("remediation_command", ""),
            })
            if _SEV_RANK.get(f.get("severity", "info"), 1) > _SEV_RANK.get(entry["worst_severity"], 1):
                entry["worst_severity"] = f.get("severity", "info")
    resources = sorted(
        by_res.values(),
        key=lambda e: (_SEV_RANK.get(e["worst_severity"], 1), len(e["findings"])),
        reverse=True,
    )
    return {"run_id": run_id, "workload_name": run.workload_name, "resources": resources, "count": len(resources)}

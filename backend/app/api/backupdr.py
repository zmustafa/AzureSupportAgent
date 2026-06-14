"""Backup & DR Coverage endpoints.

Audits backup/DR posture per workload/subscription against the editable, versioned
reference, with server-side caching. Exposes IaC generation (Bicep + runbook, download-
only), Reliability-pillar finding registration, ticketing, the change-request approval
inbox, and a War Room investigate handoff context. Admin-gated."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.backupdr import cache, change_requests, demo
from app.backupdr.collector import collect_coverage
from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.models import AuditLog

router = APIRouter(prefix="/backupdr", tags=["backupdr"])
log = logging.getLogger("app.api.backupdr")


def _settings() -> tuple[int, int, int, int]:
    from app.core.app_settings import load_settings

    s = load_settings()
    ttl = int(s.get("backupdr_cache_ttl_s", 21600) or 21600)
    stale = int(s.get("backupdr_stale_drill_days", 180) or 180)
    sla = int(s.get("backupdr_last_job_sla_hours", 24) or 24)
    cap = int(s.get("backupdr_per_resource_scan_cap", 200) or 200)
    return ttl, stale, sla, cap


def _decorate(snap: dict[str, Any], ttl_s: int) -> dict[str, Any]:
    age = cache.age_seconds(snap)
    out = dict(snap)
    out["ttl_s"] = ttl_s
    out["age_seconds"] = int(age) if age is not None else None
    out["stale_cache"] = (age is None) or (age >= ttl_s)
    out.setdefault("all_resources", [])  # consistent shape for snapshots cached pre-feature
    return out


async def _get_snapshot(principal: Principal, scope_kind: str, scope_id: str, *, force: bool) -> dict[str, Any]:
    from app.core.azure_connections import get_default_connection
    from app.workloads.registry import get_workload

    ttl, stale, sla, cap = _settings()
    tenant_id = principal.tenant_id or "default"

    if demo.is_demo_scope(scope_kind, scope_id):
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if force or snap is None or not cache.is_fresh(snap, ttl) or "all_resources" not in snap or not snap.get("demo"):
            snap = demo.seed_demo(sla_hours=sla, stale_drill_days=stale, tenant_id=tenant_id, scope_id=scope_id)
        return _decorate(snap, ttl)

    if not force:
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if snap and cache.is_fresh(snap, ttl):
            return _decorate(snap, ttl)

    lock = cache.get_lock(tenant_id, scope_kind, scope_id)
    async with lock:
        if not force:
            snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
            if snap and cache.is_fresh(snap, ttl):
                return _decorate(snap, ttl)
        connection = get_default_connection()
        workload = get_workload(scope_id) if scope_kind == "workload" else None
        fresh = await collect_coverage(
            connection, scope_kind=scope_kind, scope_id=scope_id, workload=workload,
            sla_hours=sla, stale_drill_days=stale, scan_cap=cap,
        )
        cache.write_snapshot(tenant_id, scope_kind, scope_id, fresh)
        return _decorate(fresh, ttl)


def _resolve_scope_params(workload_id: str | None, subscription_id: str | None) -> tuple[str, str]:
    if workload_id:
        return "workload", workload_id
    if subscription_id:
        return "subscription", subscription_id
    return "workload", demo.DEMO_WORKLOAD_ID


# ----------------------------------------------------------------------- coverage
@router.get("/coverage")
async def coverage(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    return await _get_snapshot(principal, scope_kind, scope_id, force=False)


@router.post("/refresh")
async def refresh(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    # Shield the compute so it finishes (and writes the cache) even if the client navigates
    # away or the connection drops mid-refresh — the result is picked up on the next visit.
    snap = await asyncio.shield(_get_snapshot(principal, scope_kind, scope_id, force=True))
    # Record a compact trend point (% protected) so this scan can be charted over time.
    from app.core import coverage_trends

    sc = snap.get("scorecard", {}) or {}
    coverage_trends.record(
        "backupdr", principal.tenant_id or "default", scope_kind, scope_id,
        pct=sc.get("pct_protected"),
        extra={k: sc.get(k) for k in ("pct_offsite", "pct_recent_job", "dr_pairs")},
        demo=bool(snap.get("demo")),
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="backupdr.refresh",
            target=f"{scope_kind}:{scope_id}", metadata_json={"pct_protected": snap.get("scorecard", {}).get("pct_protected")},
        )
    )
    await db.commit()
    return snap


@router.get("/trend")
async def trend(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """% protected trend points for the scope (chart-ready). Backfills a demo series on first
    visit so a demo scope's chart isn't empty."""
    from app.core import coverage_trends

    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    tenant_id = principal.tenant_id or "default"
    if demo.is_demo_scope(scope_kind, scope_id) and not coverage_trends.series("backupdr", tenant_id, scope_kind, scope_id):
        snap = await _get_snapshot(principal, scope_kind, scope_id, force=False)
        sc = snap.get("scorecard", {}) or {}
        coverage_trends.seed_demo_series(
            "backupdr", tenant_id, scope_kind, scope_id,
            current_pct=sc.get("pct_protected"),
            extra={k: sc.get(k) for k in ("pct_offsite", "pct_recent_job", "dr_pairs")},
        )
    return coverage_trends.trend("backupdr", tenant_id, scope_kind, scope_id)



# ----------------------------------------------------------------------- reference set
@router.get("/reference")
async def get_reference(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.backupdr.reference import load_reference

    return load_reference()


class ReferenceUpdate(BaseModel):
    types: dict[str, Any]
    reason: str = "Edited"


@router.put("/reference")
async def put_reference(
    payload: ReferenceUpdate, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    from app.backupdr.reference import save_reference

    doc = save_reference(payload.types, actor=principal.subject, reason=payload.reason)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="backupdr.reference.update",
            target=f"v{doc.get('version')}", metadata_json={"type_count": len(doc.get("types", {}))},
        )
    )
    await db.commit()
    return doc


@router.get("/reference/revisions")
async def list_reference_revisions(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.backupdr.reference import list_revisions

    return {"revisions": list_revisions()}


class RestoreRequest(BaseModel):
    revision_id: str


@router.post("/reference/restore")
async def restore_reference(payload: RestoreRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.backupdr.reference import restore_revision

    doc = restore_revision(payload.revision_id, actor=principal.subject)
    if doc is None:
        return {"ok": False, "detail": "Revision not found."}
    return {"ok": True, "reference": doc}


@router.post("/reference/reset")
async def reset_reference(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.backupdr.reference import reset_to_builtin

    return {"ok": True, "reference": reset_to_builtin(actor=principal.subject)}


# ----------------------------------------------------------------------- IaC
class Gap(BaseModel):
    resource_id: str = ""
    resource_name: str = ""
    resource_type: str = ""
    resource_group: str = ""
    subscription_id: str = ""
    region: str = ""
    backup_region: str = ""
    status: str = "amber"
    failed_checks: list[str] = Field(default_factory=list)
    vault_name: str = ""
    policy: str = ""
    dr_target_region: str = ""
    severity: str = "warning"


class IacRequest(BaseModel):
    gaps: list[Gap]
    format: str = "bicep"


@router.post("/iac")
async def generate_iac_endpoint(payload: IacRequest, _: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.backupdr.iac import generate_iac

    gaps = [g.model_dump() for g in payload.gaps]
    fmt = payload.format if payload.format in ("bicep", "runbook") else "bicep"
    text = generate_iac(gaps, fmt)
    return {"format": fmt, "iac": text, "gap_count": len(gaps)}


# ----------------------------------------------------------------------- findings
class RegisterFindingsRequest(BaseModel):
    workload_id: str
    workload_name: str = ""
    gaps: list[Gap]


@router.post("/findings/register")
async def register_findings(
    payload: RegisterFindingsRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Register backup/DR gaps as Reliability-pillar findings via a lightweight AssessmentRun."""
    from datetime import datetime, timezone

    from app.models import AssessmentRun

    by_check: dict[str, dict[str, Any]] = {}
    sev_rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    for g in payload.gaps:
        primary = g.failed_checks[0] if g.failed_checks else "coverage"
        check_id = f"backupdr_{g.resource_type.replace('/', '_')}_{primary}"[:64]
        f = by_check.get(check_id)
        if f is None:
            f = {
                "check_id": check_id,
                "pillar": "reliability",
                "title": f"Backup/DR gap ({primary}) on {g.resource_type}",
                "description": f"Failed checks: {', '.join(g.failed_checks)}. RTO/RPO commitments are at risk.",
                "severity": g.severity,
                "weight": 0,
                "frameworks": {},
                "remediation": "Enable backup with an adequate policy, ensure offsite/geo redundancy, and configure + test a DR pair.",
                "remediation_command": "",
                "resource_types": [g.resource_type],
                "status": "fail",
                "flagged_count": 0,
                "flagged_resources": [],
                "ai_rationale": "",
            }
            by_check[check_id] = f
        f["flagged_resources"].append(
            {
                "id": g.resource_id, "name": g.resource_name, "type": g.resource_type,
                "resource_group": g.resource_group, "subscription_id": g.subscription_id, "remediation_command": "",
            }
        )
        f["flagged_count"] = len(f["flagged_resources"])
        if sev_rank.get(g.severity, 3) < sev_rank.get(f["severity"], 3):
            f["severity"] = g.severity

    findings = list(by_check.values())
    worst = "info"
    for f in findings:
        if sev_rank.get(f["severity"], 3) < sev_rank.get(worst, 3):
            worst = f["severity"]

    now = datetime.now(timezone.utc)
    run = AssessmentRun(
        workload_id=payload.workload_id, workload_name=payload.workload_name or payload.workload_id,
        tenant_id=principal.tenant_id, pillars=["reliability"], status="succeeded", overall_score=None,
        scores_json={}, totals_json={"passed": 0, "failed": len(findings), "na": 0, "waived": 0, "by_severity": {}},
        severity=worst, findings_json=findings, resource_count=sum(f["flagged_count"] for f in findings),
        resources_json=[], summary=f"Backup & DR Coverage: {len(findings)} reliability gap finding(s).",
        used_ai=False, triggered_by=principal.subject, trigger="backup_dr", started_at=now, ended_at=now,
    )
    db.add(run)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="backupdr.findings.register",
            target=payload.workload_id, metadata_json={"findings": len(findings)},
        )
    )
    await db.commit()
    await db.refresh(run)
    return {"ok": True, "run_id": run.id, "finding_count": len(findings)}


# ----------------------------------------------------------------------- ticketing
class TicketRequest(BaseModel):
    connector_id: str = Field(min_length=1)
    gap: Gap


@router.post("/ticket")
async def create_backupdr_ticket(
    payload: TicketRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    from app.assessments.tickets import create_ticket

    g = payload.gap
    finding = {
        "severity": g.severity,
        "title": f"Backup/DR gap: {g.resource_name}",
        "check_id": f"backupdr_{g.resource_type.replace('/', '_')}",
        "pillar": "reliability",
        "description": f"Resource: {g.resource_name} ({g.resource_type})\nFailed checks: {', '.join(g.failed_checks)}"
        + (f"\nBackup region: {g.backup_region}" if g.backup_region else ""),
        "remediation": "Enable/repair backup + policy, ensure offsite redundancy, configure + test a DR pair.",
    }
    result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=g.resource_name or "Backup/DR")
    if result.get("ok"):
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id, actor_id=principal.subject, action="backupdr.ticket.create",
                target=g.resource_id[:512], metadata_json={"ticket": result.get("ticket_id", "")},
            )
        )
        await db.commit()
    return result


# ----------------------------------------------------------------------- approval inbox
class ApprovalRequest(BaseModel):
    scope_kind: str = "workload"
    scope_id: str = ""
    scope_name: str = ""
    gaps: list[Gap]
    format: str = "bicep"


@router.post("/approval")
async def send_to_approval(
    payload: ApprovalRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    from app.backupdr.iac import generate_iac

    gaps = [g.model_dump() for g in payload.gaps]
    fmt = payload.format if payload.format in ("bicep", "runbook") else "bicep"
    iac_text = generate_iac(gaps, fmt)
    req = change_requests.create_request(
        tenant_id=principal.tenant_id, scope_kind=payload.scope_kind, scope_id=payload.scope_id,
        scope_name=payload.scope_name, gaps=gaps, iac_format=fmt, iac_text=iac_text, requested_by=principal.subject,
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="backupdr.approval.create",
            target=req["id"], metadata_json={"gaps": len(gaps), "format": fmt},
        )
    )
    await db.commit()
    return {"ok": True, "request": {k: v for k, v in req.items() if k != "gaps"}}


@router.get("/approvals")
async def list_approvals(status: str | None = Query(default=None), principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    return {"requests": change_requests.list_requests(principal.tenant_id, status=status)}


@router.get("/approvals/{request_id}")
async def get_approval(request_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    req = change_requests.get_request(principal.tenant_id, request_id)
    if req is None:
        return {"ok": False, "detail": "Not found."}
    return {"ok": True, "request": req}


class DecisionRequest(BaseModel):
    decision: str
    reason: str = ""


@router.post("/approvals/{request_id}/decide")
async def decide_approval(
    request_id: str, payload: DecisionRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    req = change_requests.decide_request(
        principal.tenant_id, request_id, decision=payload.decision, actor=principal.subject, reason=payload.reason
    )
    if req is None:
        return {"ok": False, "detail": "Invalid decision or request not found."}
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action=f"backupdr.approval.{payload.decision}",
            target=request_id, metadata_json={"reason": payload.reason},
        )
    )
    await db.commit()
    return {"ok": True, "request": {k: v for k, v in req.items() if k != "gaps"}}


@router.delete("/approvals/{request_id}")
async def delete_approval(request_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    ok = change_requests.delete_request(principal.tenant_id, request_id)
    return {"ok": ok}


# ----------------------------------------------------------------------- demo seed
@router.post("/demo/seed")
async def seed_demo_endpoint(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    _ttl, stale, sla, _cap = _settings()
    snap = demo.seed_demo(sla_hours=sla, stale_drill_days=stale, tenant_id=principal.tenant_id)
    return {"ok": True, "workload_id": demo.DEMO_WORKLOAD_ID, "pct_protected": snap.get("scorecard", {}).get("pct_protected")}

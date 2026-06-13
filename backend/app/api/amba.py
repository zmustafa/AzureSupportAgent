"""AMBA Monitoring Coverage endpoints.

Computes baseline-alert coverage per workload/subscription against the editable, versioned
AMBA reference set, with server-side caching (Resource Graph scans are slow). Exposes IaC
generation (download), Operations-pillar finding registration, ticketing, and the
change-request approval inbox. All admin-gated."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.amba import cache, change_requests, demo
from app.amba.collector import collect_coverage
from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.models import AuditLog

router = APIRouter(prefix="/amba", tags=["amba"])
log = logging.getLogger("app.api.amba")


def _settings() -> tuple[int, bool, float]:
    from app.core.app_settings import load_settings

    s = load_settings()
    ttl = int(s.get("amba_cache_ttl_s", 21600) or 21600)
    misconfig_gap = bool(s.get("amba_misconfig_counts_as_gap", True))
    tol = float(s.get("amba_threshold_tolerance_pct", 10) or 10)
    return ttl, misconfig_gap, tol


def _decorate(snap: dict[str, Any], ttl_s: int) -> dict[str, Any]:
    age = cache.age_seconds(snap)
    out = dict(snap)
    out["ttl_s"] = ttl_s
    out["age_seconds"] = int(age) if age is not None else None
    out["stale"] = (age is None) or (age >= ttl_s)
    out.setdefault("all_resources", [])  # consistent shape for snapshots cached pre-feature
    return out


async def _get_snapshot(
    principal: Principal, scope_kind: str, scope_id: str, *, force: bool
) -> dict[str, Any]:
    from app.core.azure_connections import get_default_connection
    from app.workloads.registry import get_workload

    ttl, misconfig_gap, tol = _settings()
    tenant_id = principal.tenant_id or "default"

    # Demo scope: serve/regenerate dummy data; never touches Azure.
    if demo.is_demo_scope(scope_kind, scope_id):
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if force or snap is None or not cache.is_fresh(snap, ttl) or "all_resources" not in snap or not snap.get("demo"):
            snap = demo.seed_demo(misconfig_counts_as_gap=misconfig_gap, tolerance_pct=tol, tenant_id=tenant_id, scope_id=scope_id)
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
            connection,
            scope_kind=scope_kind,
            scope_id=scope_id,
            workload=workload,
            misconfig_counts_as_gap=misconfig_gap,
            tolerance_pct=tol,
        )
        cache.write_snapshot(tenant_id, scope_kind, scope_id, fresh)
        return _decorate(fresh, ttl)


def _resolve_scope_params(workload_id: str | None, subscription_id: str | None) -> tuple[str, str]:
    if workload_id:
        return "workload", workload_id
    if subscription_id:
        return "subscription", subscription_id
    return "workload", demo.DEMO_WORKLOAD_ID  # default to the demo scope so the page is never empty


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
    # Shield so the compute finishes + caches even if the client navigates away mid-refresh.
    snap = await asyncio.shield(_get_snapshot(principal, scope_kind, scope_id, force=True))
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="amba.refresh",
            target=f"{scope_kind}:{scope_id}",
            metadata_json={"coverage_pct": snap.get("coverage_pct")},
        )
    )
    await db.commit()
    return snap


# ----------------------------------------------------------------------- reference set
@router.get("/reference")
async def get_reference(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.amba.reference import load_reference

    return load_reference()


class ReferenceUpdate(BaseModel):
    types: dict[str, Any]
    reason: str = "Edited"


@router.put("/reference")
async def put_reference(
    payload: ReferenceUpdate,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.amba.reference import save_reference

    doc = save_reference(payload.types, actor=principal.subject, reason=payload.reason)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="amba.reference.update",
            target=f"v{doc.get('version')}",
            metadata_json={"type_count": len(doc.get("types", {}))},
        )
    )
    await db.commit()
    return doc


@router.get("/reference/revisions")
async def list_reference_revisions(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.amba.reference import list_revisions

    return {"revisions": list_revisions()}


class RestoreRequest(BaseModel):
    revision_id: str


@router.post("/reference/restore")
async def restore_reference(
    payload: RestoreRequest, principal: Principal = Depends(require_admin)
) -> dict[str, Any]:
    from app.amba.reference import restore_revision

    doc = restore_revision(payload.revision_id, actor=principal.subject)
    if doc is None:
        return {"ok": False, "detail": "Revision not found."}
    return {"ok": True, "reference": doc}


@router.post("/reference/reset")
async def reset_reference(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.amba.reference import reset_to_builtin

    return {"ok": True, "reference": reset_to_builtin(actor=principal.subject)}


# ----------------------------------------------------------------------- IaC
class Gap(BaseModel):
    resource_id: str = ""
    resource_name: str = ""
    resource_type: str = ""
    resource_group: str = ""
    subscription_id: str = ""
    location: str = ""
    alert_key: str = ""
    alert_name: str = ""
    amba_category: str = ""
    severity: str = "warning"
    status: str = "missing"
    recommended: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] = Field(default_factory=dict)
    why: str = ""


class IacRequest(BaseModel):
    gaps: list[Gap]
    format: str = "bicep"


@router.post("/iac")
async def generate_iac_endpoint(
    payload: IacRequest, _: Principal = Depends(require_admin)
) -> dict[str, Any]:
    from app.amba.iac import generate_iac

    gaps = [g.model_dump() for g in payload.gaps]
    fmt = payload.format if payload.format in ("bicep", "terraform") else "bicep"
    text = generate_iac(gaps, fmt)
    return {"format": fmt, "iac": text, "gap_count": len(gaps)}


# ----------------------------------------------------------------------- findings
class RegisterFindingsRequest(BaseModel):
    workload_id: str
    workload_name: str = ""
    gaps: list[Gap]


@router.post("/findings/register")
async def register_findings(
    payload: RegisterFindingsRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Register coverage gaps as Operations-pillar findings via a lightweight AssessmentRun.

    Reuses the assessments finding-state / waiver / ticket lifecycle: each gap becomes a
    finding keyed by a synthetic, stable ``check_id`` so ownership and waivers persist."""
    from datetime import datetime, timezone

    from app.models import AssessmentRun

    # Group gaps into findings by (resource_type, alert_key) so flagged resources cluster.
    by_check: dict[str, dict[str, Any]] = {}
    sev_rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    for g in payload.gaps:
        check_id = f"amba_{g.resource_type.replace('/', '_')}_{g.alert_key}"[:64]
        f = by_check.get(check_id)
        if f is None:
            f = {
                "check_id": check_id,
                "pillar": "operations",
                "title": f"Baseline alert missing: {g.alert_name} ({g.resource_type})",
                "description": g.why or f"Recommended AMBA alert '{g.alert_name}' is not in place.",
                "severity": g.severity,
                "weight": 0,
                "frameworks": {},
                "remediation": "Create the recommended baseline alert and wire an action group.",
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
                "id": g.resource_id,
                "name": g.resource_name,
                "type": g.resource_type,
                "resource_group": g.resource_group,
                "subscription_id": g.subscription_id,
                "remediation_command": "",
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
        workload_id=payload.workload_id,
        workload_name=payload.workload_name or payload.workload_id,
        tenant_id=principal.tenant_id,
        pillars=["operations"],
        status="succeeded",
        overall_score=None,
        scores_json={},
        totals_json={"passed": 0, "failed": len(findings), "na": 0, "waived": 0, "by_severity": {}},
        severity=worst,
        findings_json=findings,
        resource_count=sum(f["flagged_count"] for f in findings),
        resources_json=[],
        summary=f"AMBA Monitoring Coverage: {len(findings)} baseline-alert gap finding(s).",
        used_ai=False,
        triggered_by=principal.subject,
        trigger="amba",
        started_at=now,
        ended_at=now,
    )
    db.add(run)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="amba.findings.register",
            target=payload.workload_id,
            metadata_json={"findings": len(findings)},
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
async def create_amba_ticket(
    payload: TicketRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.assessments.tickets import create_ticket

    g = payload.gap
    rec = g.recommended or {}
    finding = {
        "severity": g.severity,
        "title": f"Baseline alert {g.status}: {g.alert_name}",
        "check_id": f"amba_{g.resource_type.replace('/', '_')}_{g.alert_key}",
        "pillar": "operations",
        "description": (g.why or "")
        + f"\n\nResource: {g.resource_name} ({g.resource_type})"
        + f"\nRecommended: {rec.get('metric','')} {rec.get('operator','')} {rec.get('threshold','')} {rec.get('unit','')}",
        "remediation": "Create the recommended baseline alert and wire an action group.",
    }
    workload_name = g.resource_name or "Monitoring Coverage"
    result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=workload_name)
    if result.get("ok"):
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id,
                actor_id=principal.subject,
                action="amba.ticket.create",
                target=f"{g.resource_type}:{g.alert_key}",
                metadata_json={"ticket": result.get("ticket_id", "")},
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
    payload: ApprovalRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record the generated IaC for a set of gaps as a PENDING change request. Never applies."""
    from app.amba.iac import generate_iac

    gaps = [g.model_dump() for g in payload.gaps]
    fmt = payload.format if payload.format in ("bicep", "terraform") else "bicep"
    iac_text = generate_iac(gaps, fmt)
    req = change_requests.create_request(
        tenant_id=principal.tenant_id,
        scope_kind=payload.scope_kind,
        scope_id=payload.scope_id,
        scope_name=payload.scope_name,
        gaps=gaps,
        iac_format=fmt,
        iac_text=iac_text,
        requested_by=principal.subject,
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="amba.approval.create",
            target=req["id"],
            metadata_json={"gaps": len(gaps), "format": fmt},
        )
    )
    await db.commit()
    return {"ok": True, "request": {k: v for k, v in req.items() if k != "gaps"}}


@router.get("/approvals")
async def list_approvals(
    status: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
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
    request_id: str,
    payload: DecisionRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    req = change_requests.decide_request(
        principal.tenant_id, request_id, decision=payload.decision, actor=principal.subject, reason=payload.reason
    )
    if req is None:
        return {"ok": False, "detail": "Invalid decision or request not found."}
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action=f"amba.approval.{payload.decision}",
            target=request_id,
            metadata_json={"reason": payload.reason},
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
    """(Re)seed the dummy demo workload + coverage snapshot for review."""
    _ttl, misconfig_gap, tol = _settings()
    snap = demo.seed_demo(
        misconfig_counts_as_gap=misconfig_gap, tolerance_pct=tol, tenant_id=principal.tenant_id
    )
    return {"ok": True, "workload_id": demo.DEMO_WORKLOAD_ID, "coverage_pct": snap.get("coverage_pct")}

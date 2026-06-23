"""Retirement & Breaking-Change Radar endpoints.

Aggregates Service Health retirement events + Advisor service-upgrade/retirement
recommendations (deduped by tracking ID) into one workload/subscription-scoped, owner-
mapped list with a deadline countdown, plus a dedicated Azure OpenAI/Foundry model-
lifecycle lane. Exposes drill-down, migration-runbook drafting, Reliability-pillar finding
registration, ticketing, status/assign/waive, a scheduled-digest preview, the editable
reference, and a War Room handoff context. Admin-gated.

Real (non-demo) scopes are **server-side cached**: selecting a scope only ever READS the
cache and never triggers the slow Service Health / Advisor queries — a cache miss returns an
empty ``never_loaded`` snapshot so the UI prompts for Refresh. Only ``POST /radar/refresh``
recomputes under a per-scope lock. The demo scope is synthesised locally, so it stays
instant on visit."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.models import AuditLog
from app.radar import cache, demo, state

router = APIRouter(prefix="/radar", tags=["radar"])
log = logging.getLogger("app.api.radar")


def _settings() -> tuple[int, list[int]]:
    from app.core.app_settings import load_settings

    s = load_settings()
    ttl = int(s.get("radar_cache_ttl_s", 21600) or 21600)
    lead = s.get("radar_digest_lead_days") or [90, 60, 30]
    lead = [int(x) for x in lead if int(x) > 0] or [90, 60, 30]
    return ttl, lead


def _decorate(snap: dict[str, Any], ttl_s: int, tenant_id: str) -> dict[str, Any]:
    age = cache.age_seconds(snap)
    out = dict(snap)
    out["events"] = state.apply_states(tenant_id, list(out.get("events", [])))
    out["ttl_s"] = ttl_s
    out["age_seconds"] = int(age) if age is not None else None
    out["stale_cache"] = (age is None) or (age >= ttl_s)
    out.setdefault("never_loaded", False)
    return out


def _empty_radar(scope_kind: str, scope_id: str, *, connection_configured: bool) -> dict[str, Any]:
    """A 'not loaded yet' radar snapshot — empty rail/events/counts with ``never_loaded`` set so
    the UI prompts the user to press Refresh. Selecting a scope must never trigger the (slow)
    Service Health / Advisor queries; only the Refresh button recomputes and overwrites the cache."""
    from app.radar.collector import compute_radar

    snap = compute_radar([], [])
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": scope_id,
            "connection_configured": connection_configured,
            "source": "",
            "demo": False,
            "error": "",
            "never_loaded": True,
        }
    )
    return snap


async def _get_snapshot(principal: Principal, scope_kind: str, scope_id: str, *, force: bool, connection_id: str | None = None) -> dict[str, Any]:
    from app.core.azure_connections import connection_for_scope
    from app.radar.collector import collect_radar
    from app.workloads.registry import get_workload

    ttl, _lead = _settings()
    tenant_id = principal.tenant_id or "default"

    workload = get_workload(scope_id) if scope_kind == "workload" else None

    if demo.is_demo_scope(scope_kind, scope_id):
        # Demo data is synthesised locally (no Azure calls), so seeding stays instant on visit.
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if force or snap is None or not cache.is_fresh(snap, ttl) or not snap.get("demo"):
            snap = demo.seed_demo(tenant_id=tenant_id, scope_id=scope_id)
        return _decorate(snap, ttl, tenant_id)

    if not force:
        # Selecting a scope: only ever READ the cache — never trigger the (slow) Service Health /
        # Advisor queries, even when the snapshot is stale or missing. A cache miss returns an
        # empty 'not loaded yet' payload so the UI prompts the user to press Refresh; only
        # ``force`` (the Refresh button) recomputes and overwrites the cache.
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if snap:
            return _decorate(snap, ttl, tenant_id)
        connection = connection_for_scope(scope_kind, connection_id=connection_id, workload=workload)
        return _decorate(_empty_radar(scope_kind, scope_id, connection_configured=connection is not None), ttl, tenant_id)

    lock = cache.get_lock(tenant_id, scope_kind, scope_id)
    async with lock:
        connection = connection_for_scope(scope_kind, connection_id=connection_id, workload=workload)
        fresh = await collect_radar(connection, scope_kind=scope_kind, scope_id=scope_id, workload=workload, tenant_id=tenant_id)
        cache.write_snapshot(tenant_id, scope_kind, scope_id, fresh)
        return _decorate(fresh, ttl, tenant_id)


def _resolve_scope_params(workload_id: str | None, subscription_id: str | None) -> tuple[str, str]:
    if workload_id:
        return "workload", workload_id
    if subscription_id:
        return "subscription", subscription_id
    return "workload", demo.DEMO_WORKLOAD_ID


# ----------------------------------------------------------------------- overview
@router.get("/overview")
async def overview(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    return await _get_snapshot(principal, scope_kind, scope_id, force=False, connection_id=connection_id)


@router.post("/refresh")
async def refresh(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    snap = await _get_snapshot(principal, scope_kind, scope_id, force=True, connection_id=connection_id)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="radar.refresh",
            target=f"{scope_kind}:{scope_id}", metadata_json={"total": snap.get("counts", {}).get("total")},
        )
    )
    await db.commit()
    return snap


@router.get("/event/{tracking_id}")
async def event_detail(
    tracking_id: str,
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    snap = await _get_snapshot(principal, scope_kind, scope_id, force=False)
    for e in snap.get("events", []):
        if e.get("tracking_id") == tracking_id or e.get("id") == tracking_id:
            return {"ok": True, "event": e}
    return {"ok": False, "detail": "Event not found in current scope."}


# ----------------------------------------------------------------------- runbook
class RunbookRequest(BaseModel):
    event: dict[str, Any]
    architecture_id: str = ""


@router.post("/runbook")
async def generate_runbook(payload: RunbookRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.radar.runbook import draft_runbook

    result = await draft_runbook(payload.event, architecture_id=payload.architecture_id)
    return result


# ----------------------------------------------------------------------- reference
@router.get("/reference")
async def get_reference(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.radar.reference import load_reference

    return load_reference()


class ReferenceUpdate(BaseModel):
    classification_rules: list[dict[str, Any]] = Field(default_factory=list)
    model_lifecycle: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = "Edited"


@router.put("/reference")
async def put_reference(
    payload: ReferenceUpdate, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    from app.radar.reference import save_reference

    doc = save_reference(
        classification_rules=payload.classification_rules,
        model_lifecycle=payload.model_lifecycle,
        actor=principal.subject,
        reason=payload.reason,
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="radar.reference.update",
            target=f"v{doc.get('version')}",
            metadata_json={"rules": len(doc.get("classification_rules", [])), "models": len(doc.get("model_lifecycle", []))},
        )
    )
    await db.commit()
    return doc


@router.get("/reference/revisions")
async def list_reference_revisions(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.radar.reference import list_revisions

    return {"revisions": list_revisions()}


class RestoreRequest(BaseModel):
    revision_id: str


@router.post("/reference/restore")
async def restore_reference(payload: RestoreRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.radar.reference import restore_revision

    doc = restore_revision(payload.revision_id, actor=principal.subject)
    if doc is None:
        return {"ok": False, "detail": "Revision not found."}
    return {"ok": True, "reference": doc}


@router.post("/reference/reset")
async def reset_reference(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.radar.reference import reset_to_builtin

    return {"ok": True, "reference": reset_to_builtin(actor=principal.subject)}


# ----------------------------------------------------------------------- state
class StateUpdate(BaseModel):
    tracking_id: str = Field(min_length=1)
    status: str | None = None
    assignee: str | None = None
    waive_reason: str | None = None


@router.post("/state")
async def update_state(
    payload: StateUpdate, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    if payload.status and payload.status not in state.STATUSES:
        return {"ok": False, "detail": f"Invalid status. Allowed: {', '.join(state.STATUSES)}."}
    entry = state.set_state(
        principal.tenant_id or "default", payload.tracking_id,
        status=payload.status, assignee=payload.assignee, waive_reason=payload.waive_reason,
        actor=principal.subject,
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="radar.state.update",
            target=payload.tracking_id[:512], metadata_json={"status": entry.get("status")},
        )
    )
    await db.commit()
    return {"ok": True, "state": entry}


# ----------------------------------------------------------------------- findings
class RadarItem(BaseModel):
    tracking_id: str = ""
    title: str = ""
    service: str = ""
    change_type: str = "retirement"
    retirement_date: str = ""
    days_until: int | None = None
    recommended_replacement: str = ""
    migration_url: str = ""
    severity: str = "warning"
    impacted_resources: list[dict[str, Any]] = Field(default_factory=list)


class RegisterFindingsRequest(BaseModel):
    workload_id: str
    workload_name: str = ""
    items: list[RadarItem]


def _severity_for_item(it: RadarItem) -> str:
    d = it.days_until
    if d is None:
        return "warning"
    if d < 0:
        return "critical"
    if d < 30:
        return "critical"
    if d < 90:
        return "error"
    return "warning"


@router.post("/findings/register")
async def register_findings(
    payload: RegisterFindingsRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Register radar items as Reliability-pillar findings via a lightweight AssessmentRun,
    so they feed the existing assessment scoring + finding-state + waiver machinery."""
    from datetime import datetime, timezone

    from app.models import AssessmentRun

    sev_rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    findings: list[dict[str, Any]] = []
    worst = "info"
    for it in payload.items:
        sev = _severity_for_item(it)
        check_id = f"retire_{(it.service or 'azure').lower().replace(' ', '_')}_{it.tracking_id}"[:64]
        flagged = [
            {
                "id": r.get("id", ""), "name": r.get("name", ""), "type": r.get("type", ""),
                "resource_group": r.get("resource_group", ""), "subscription_id": r.get("subscription_id", ""),
                "remediation_command": "",
            }
            for r in it.impacted_resources
        ]
        findings.append(
            {
                "check_id": check_id,
                "pillar": "reliability",
                "title": f"{'Breaking change' if it.change_type == 'breaking_change' else 'Retirement'}: {it.title or it.service}",
                "description": (
                    f"Planned date: {it.retirement_date or 'TBD'}"
                    + (f" ({it.days_until} days)" if it.days_until is not None else "")
                    + f". Tracking ID {it.tracking_id}. Recommended: {it.recommended_replacement or 'see migration guidance'}."
                ),
                "severity": sev,
                "weight": 0,
                "frameworks": {},
                "remediation": it.recommended_replacement or "Plan and execute the migration before the deadline.",
                "remediation_command": "",
                "resource_types": [],
                "status": "fail",
                "flagged_count": len(flagged),
                "flagged_resources": flagged,
                "ai_rationale": "",
                "reference_url": it.migration_url,
            }
        )
        if sev_rank.get(sev, 3) < sev_rank.get(worst, 3):
            worst = sev

    now = datetime.now(timezone.utc)
    run = AssessmentRun(
        workload_id=payload.workload_id, workload_name=payload.workload_name or payload.workload_id,
        tenant_id=principal.tenant_id, pillars=["reliability"], status="succeeded", overall_score=None,
        scores_json={}, totals_json={"passed": 0, "failed": len(findings), "na": 0, "waived": 0, "by_severity": {}},
        severity=worst, findings_json=findings, resource_count=sum(f["flagged_count"] for f in findings),
        resources_json=[], summary=f"Retirement Radar: {len(findings)} reliability lifecycle finding(s).",
        used_ai=False, triggered_by=principal.subject, trigger="retirement", started_at=now, ended_at=now,
    )
    db.add(run)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="radar.findings.register",
            target=payload.workload_id, metadata_json={"findings": len(findings)},
        )
    )
    await db.commit()
    await db.refresh(run)
    return {"ok": True, "run_id": run.id, "finding_count": len(findings)}


# ----------------------------------------------------------------------- ticketing
class TicketRequest(BaseModel):
    connector_id: str = Field(min_length=1)
    item: RadarItem


@router.post("/ticket")
async def create_radar_ticket(
    payload: TicketRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    from app.assessments.tickets import create_ticket

    it = payload.item
    impacted = "\n".join(f"- {r.get('name', '')} ({r.get('type', '')})" for r in it.impacted_resources[:50])
    finding = {
        "severity": _severity_for_item(it),
        "title": f"{'Breaking change' if it.change_type == 'breaking_change' else 'Retirement'}: {it.title or it.service}",
        "check_id": f"retire_{it.tracking_id}",
        "pillar": "reliability",
        "description": (
            f"Tracking ID: {it.tracking_id}\nPlanned date: {it.retirement_date or 'TBD'}"
            + (f" ({it.days_until} days away)" if it.days_until is not None else "")
            + f"\nRecommended replacement: {it.recommended_replacement or 'see migration guidance'}"
            + (f"\nReference: {it.migration_url}" if it.migration_url else "")
            + (f"\n\nImpacted resources ({len(it.impacted_resources)}):\n{impacted}" if impacted else "")
        ),
        "remediation": it.recommended_replacement or "Plan and execute the migration before the deadline.",
    }
    result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=it.title or it.service or "Retirement")
    if result.get("ok"):
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id, actor_id=principal.subject, action="radar.ticket.create",
                target=it.tracking_id[:512], metadata_json={"ticket": result.get("ticket_id", "")},
            )
        )
        await db.commit()
    return result


# ----------------------------------------------------------------------- digest preview
@router.get("/digest/preview")
async def digest_preview(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Preview what the scheduled push would send right now (treats everything as new when
    no prior run is recorded)."""
    from app.radar.digest import select_digest_items

    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    _ttl, lead = _settings()
    snap = await _get_snapshot(principal, scope_kind, scope_id, force=False)
    sel = select_digest_items(snap, known_ids=set(), lead_days=lead)
    return {"lead_days": lead, **sel}


# ----------------------------------------------------------------------- demo
@router.post("/demo/seed")
async def seed_demo_endpoint(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    snap = demo.seed_demo(tenant_id=principal.tenant_id or "default")
    return {"ok": True, "workload_id": demo.DEMO_WORKLOAD_ID, "counts": snap.get("counts", {})}

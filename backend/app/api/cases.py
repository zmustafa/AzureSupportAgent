"""Durable Case File API — the persistent spine of an incident.

Replaces the fragile sessionStorage War Room handoff with a server-side case that
survives refreshes and reassignment, links findings → investigation → evidence →
remediation → verification, and keeps an append-only timeline. Tenant scoped; reads
need ``cases.read`` and mutations need ``cases.write``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases import store
from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import Case, CaseEvent

router = APIRouter(prefix="/cases", tags=["cases"])

read_dep = require_permission("cases.read")
write_dep = require_permission("cases.write")


def _actor(p: Principal) -> str:
    return p.email or p.subject or ""


def _case_dict(c: Case) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "summary": c.summary or "",
        "status": c.status,
        "severity": c.severity,
        "risk_score": c.risk_score,
        "confidence": c.confidence,
        "workload_id": c.workload_id or "",
        "workload_name": c.workload_name or "",
        "connection_id": c.connection_id or "",
        "architecture_id": c.architecture_id or "",
        "finding_uids": c.finding_uids or [],
        "change_event_ids": c.change_event_ids or [],
        "investigation_chat_id": c.investigation_chat_id or "",
        "investigation_message_id": c.investigation_message_id or "",
        "evidence_snapshot_ids": c.evidence_snapshot_ids or [],
        "remediation_task_id": c.remediation_task_id or "",
        "verification_json": c.verification_json,
        "assignee": c.assignee or "",
        "opened_by": c.opened_by or "",
        "opened_at": c.opened_at,
        "updated_at": c.updated_at,
        "resolved_at": c.resolved_at,
    }


def _event_dict(e: CaseEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "case_id": e.case_id,
        "kind": e.kind,
        "actor": e.actor or "",
        "message": e.message or "",
        "payload": e.payload_json or {},
        "created_at": e.created_at,
    }


class CaseCreate(BaseModel):
    title: str
    summary: str | None = None
    severity: str = "info"
    workload_id: str | None = None
    workload_name: str | None = None
    connection_id: str | None = None
    architecture_id: str | None = None
    investigation_chat_id: str | None = None
    investigation_message_id: str | None = None
    finding_uids: list[str] | None = None
    change_event_ids: list[str] | None = None
    risk_score: int | None = None
    confidence: int | None = None
    assignee: str | None = None


class CaseUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    status: str | None = None
    severity: str | None = None
    risk_score: int | None = None
    confidence: int | None = None
    assignee: str | None = None
    workload_id: str | None = None
    workload_name: str | None = None
    architecture_id: str | None = None
    investigation_chat_id: str | None = None
    investigation_message_id: str | None = None
    remediation_task_id: str | None = None
    verification_json: dict[str, Any] | None = None


class CaseAttach(BaseModel):
    field: str  # finding_uids | change_event_ids | evidence_snapshot_ids
    values: list[str]
    label: str | None = None


class CaseNote(BaseModel):
    message: str
    kind: str = "note"


@router.get("/meta")
async def case_meta(_: Principal = Depends(read_dep)) -> dict:
    """Static enums for the UI (statuses + severities)."""
    return {"statuses": list(store.STATUSES), "severities": ["info", "warning", "error", "critical"]}


@router.get("")
async def list_cases(
    status: str | None = Query(None),
    workload_id: str | None = Query(None),
    open_only: bool = Query(False),
    principal: Principal = Depends(read_dep),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cases = await store.list_cases(
        db, principal.tenant_id, status=status, workload_id=workload_id,
        include_resolved=not open_only,
    )
    open_count = sum(1 for c in cases if c.status in ("open", "investigating", "remediating", "verifying"))
    return {"cases": [_case_dict(c) for c in cases], "summary": {"total": len(cases), "open": open_count}}


@router.post("")
async def create_case(
    payload: CaseCreate,
    principal: Principal = Depends(write_dep),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="A case title is required.")
    case = await store.create_case(
        db, tenant_id=principal.tenant_id, actor=_actor(principal),
        title=payload.title, summary=payload.summary or "", severity=payload.severity,
        workload_id=payload.workload_id, workload_name=payload.workload_name,
        connection_id=payload.connection_id, architecture_id=payload.architecture_id,
        investigation_chat_id=payload.investigation_chat_id,
        investigation_message_id=payload.investigation_message_id,
        finding_uids=payload.finding_uids, change_event_ids=payload.change_event_ids,
        risk_score=payload.risk_score, confidence=payload.confidence, assignee=payload.assignee,
    )
    return _case_dict(case)


@router.get("/{case_id}")
async def get_case(
    case_id: str,
    principal: Principal = Depends(read_dep),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await store.get_case(db, principal.tenant_id, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found.")
    events = await store.list_events(db, principal.tenant_id, case_id)
    return {"case": _case_dict(case), "timeline": [_event_dict(e) for e in events]}


@router.patch("/{case_id}")
async def update_case(
    case_id: str,
    payload: CaseUpdate,
    principal: Principal = Depends(write_dep),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if payload.status and payload.status not in store.STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status '{payload.status}'.")
    case = await store.update_case(
        db, principal.tenant_id, case_id,
        fields=payload.model_dump(exclude_unset=True), actor=_actor(principal),
    )
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found.")
    return _case_dict(case)


@router.post("/{case_id}/attach")
async def attach_to_case(
    case_id: str,
    payload: CaseAttach,
    principal: Principal = Depends(write_dep),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        case = await store.attach(
            db, principal.tenant_id, case_id, field=payload.field,
            values=payload.values, actor=_actor(principal), label=payload.label or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found.")
    return _case_dict(case)


@router.post("/{case_id}/events")
async def add_case_note(
    case_id: str,
    payload: CaseNote,
    principal: Principal = Depends(write_dep),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await store.get_case(db, principal.tenant_id, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found.")
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="A note message is required.")
    ev = await store.add_event(
        db, tenant_id=principal.tenant_id, case_id=case_id,
        kind=payload.kind or "note", actor=_actor(principal), message=payload.message.strip(),
    )
    return _event_dict(ev)


@router.delete("/{case_id}")
async def delete_case(
    case_id: str,
    principal: Principal = Depends(write_dep),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ok = await store.soft_delete(db, principal.tenant_id, case_id, actor=_actor(principal))
    if not ok:
        raise HTTPException(status_code=404, detail="Case not found.")
    return {"deleted": True}

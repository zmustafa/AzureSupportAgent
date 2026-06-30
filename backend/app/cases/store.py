"""Durable Case File store.

Tenant-scoped CRUD over the ``Case`` spine and its append-only ``CaseEvent`` timeline.
Every meaningful mutation records a timeline event so an incident's history is fully
reconstructable. All functions take the request's ``AsyncSession`` and filter by
``tenant_id`` — there is no cross-tenant read path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Case, CaseEvent

# Allowed lifecycle states, in order. Used to validate transitions and drive the UI.
STATUSES = ("open", "investigating", "remediating", "verifying", "resolved", "closed")
_OPEN_STATES = ("open", "investigating", "remediating", "verifying")

# Case fields a client may update directly (everything else is system-managed).
_EDITABLE = (
    "title", "summary", "severity", "risk_score", "confidence", "assignee",
    "workload_id", "workload_name", "connection_id", "architecture_id",
    "investigation_chat_id", "investigation_message_id", "remediation_task_id",
    "verification_json",
)
# List-valued attachment fields the attach() helper can append to (deduped).
_LIST_FIELDS = ("finding_uids", "change_event_ids", "evidence_snapshot_ids")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def add_event(
    db: AsyncSession,
    *,
    tenant_id: str,
    case_id: str,
    kind: str,
    actor: str = "",
    message: str = "",
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> CaseEvent:
    """Append a timeline event to a case."""
    ev = CaseEvent(
        tenant_id=tenant_id,
        case_id=case_id,
        kind=kind,
        actor=actor or "",
        message=message or None,
        payload_json=payload or {},
    )
    db.add(ev)
    if commit:
        await db.commit()
        await db.refresh(ev)
    return ev


async def create_case(
    db: AsyncSession,
    *,
    tenant_id: str,
    title: str,
    actor: str = "",
    summary: str = "",
    severity: str = "info",
    workload_id: str | None = None,
    workload_name: str | None = None,
    connection_id: str | None = None,
    architecture_id: str | None = None,
    investigation_chat_id: str | None = None,
    investigation_message_id: str | None = None,
    finding_uids: list[str] | None = None,
    change_event_ids: list[str] | None = None,
    risk_score: int | None = None,
    confidence: int | None = None,
    assignee: str | None = None,
) -> Case:
    """Open a new case and record the opening event."""
    case = Case(
        tenant_id=tenant_id,
        title=(title or "Untitled case").strip()[:512],
        summary=(summary or "").strip() or None,
        severity=severity if severity in ("info", "warning", "error", "critical") else "info",
        status="open",
        workload_id=workload_id or None,
        workload_name=workload_name or None,
        connection_id=connection_id or None,
        architecture_id=architecture_id or None,
        investigation_chat_id=investigation_chat_id or None,
        investigation_message_id=investigation_message_id or None,
        finding_uids=list(dict.fromkeys(finding_uids or [])),
        change_event_ids=list(dict.fromkeys(change_event_ids or [])),
        risk_score=risk_score,
        confidence=confidence,
        assignee=assignee or None,
        opened_by=actor or "",
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)
    await add_event(
        db, tenant_id=tenant_id, case_id=case.id, kind="opened", actor=actor,
        message=f"Case opened: {case.title}",
        payload={"severity": case.severity, "workload_id": case.workload_id or ""},
    )
    return case


async def list_cases(
    db: AsyncSession,
    tenant_id: str,
    *,
    status: str | None = None,
    workload_id: str | None = None,
    include_resolved: bool = True,
    limit: int = 200,
) -> list[Case]:
    """List a tenant's cases (newest first), excluding soft-deleted ones."""
    stmt = select(Case).where(Case.tenant_id == tenant_id, Case.deleted_at.is_(None))
    if status:
        stmt = stmt.where(Case.status == status)
    elif not include_resolved:
        stmt = stmt.where(Case.status.in_(_OPEN_STATES))
    if workload_id:
        stmt = stmt.where(Case.workload_id == workload_id)
    stmt = stmt.order_by(Case.updated_at.desc()).limit(max(1, min(limit, 500)))
    return list((await db.execute(stmt)).scalars().all())


async def get_case(db: AsyncSession, tenant_id: str, case_id: str) -> Case | None:
    """Fetch one case (tenant scoped), or None."""
    return (
        await db.execute(
            select(Case).where(
                Case.id == case_id, Case.tenant_id == tenant_id, Case.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def list_events(db: AsyncSession, tenant_id: str, case_id: str) -> list[CaseEvent]:
    """The case's timeline, oldest first."""
    return list(
        (
            await db.execute(
                select(CaseEvent)
                .where(CaseEvent.tenant_id == tenant_id, CaseEvent.case_id == case_id)
                .order_by(CaseEvent.created_at.asc())
            )
        ).scalars().all()
    )


async def update_case(
    db: AsyncSession,
    tenant_id: str,
    case_id: str,
    *,
    fields: dict[str, Any],
    actor: str = "",
) -> Case | None:
    """Update editable fields and/or status, recording timeline events for changes."""
    case = await get_case(db, tenant_id, case_id)
    if case is None:
        return None

    changed: list[str] = []
    for key, value in fields.items():
        if key not in _EDITABLE or value is None:
            continue
        if getattr(case, key) != value:
            setattr(case, key, value)
            changed.append(key)

    new_status = fields.get("status")
    status_changed = bool(new_status) and new_status in STATUSES and new_status != case.status
    if status_changed:
        prev = case.status
        case.status = new_status
        if new_status in ("resolved", "closed") and case.resolved_at is None:
            case.resolved_at = _now()
        elif new_status in _OPEN_STATES:
            case.resolved_at = None

    if changed or status_changed:
        case.updated_at = _now()
        await db.commit()
        await db.refresh(case)
        if status_changed:
            kind = "resolved" if new_status in ("resolved", "closed") else (
                "reopened" if prev in ("resolved", "closed") else "status"
            )
            await add_event(
                db, tenant_id=tenant_id, case_id=case.id, kind=kind, actor=actor,
                message=f"Status: {prev} → {new_status}",
                payload={"from": prev, "to": new_status},
            )
        if changed:
            note = "assigned" if changed == ["assignee"] else "note"
            await add_event(
                db, tenant_id=tenant_id, case_id=case.id, kind=note, actor=actor,
                message="Updated " + ", ".join(changed),
                payload={"fields": changed},
            )
    return case


async def attach(
    db: AsyncSession,
    tenant_id: str,
    case_id: str,
    *,
    field: str,
    values: list[str],
    actor: str = "",
    label: str = "",
) -> Case | None:
    """Append one or more ids to a list-valued attachment field (deduped)."""
    if field not in _LIST_FIELDS:
        raise ValueError(f"Unknown attachment field: {field}")
    case = await get_case(db, tenant_id, case_id)
    if case is None:
        return None
    current = list(getattr(case, field) or [])
    added = [v for v in values if v and v not in current]
    if not added:
        return case
    setattr(case, field, current + added)
    case.updated_at = _now()
    await db.commit()
    await db.refresh(case)
    await add_event(
        db, tenant_id=tenant_id, case_id=case.id, kind="attach", actor=actor,
        message=f"Attached {len(added)} {label or field}",
        payload={"field": field, "added": added},
    )
    return case


async def soft_delete(db: AsyncSession, tenant_id: str, case_id: str, *, actor: str = "") -> bool:
    """Soft-delete a case (kept for audit; excluded from lists)."""
    case = await get_case(db, tenant_id, case_id)
    if case is None:
        return False
    case.deleted_at = _now()
    await db.commit()
    return True

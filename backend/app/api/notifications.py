"""Notification endpoints: in-app center (any user) + global rules (admin)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, get_principal, require_admin
from app.models import Notification, NotificationDelivery, NotificationRule

router = APIRouter(prefix="/notifications", tags=["notifications"])
logger = logging.getLogger("app.api.notifications")


def _note_row(n: Notification) -> dict[str, Any]:
    return {
        "id": n.id,
        "type": n.type,
        "source": n.source,
        "severity": n.severity,
        "title": n.title,
        "body": n.body,
        "facts": n.facts_json,
        "links": n.links_json,
        "read": n.read,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("")
async def list_notifications_endpoint(
    unread_only: bool = False,
    limit: int = 50,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """In-app center feed: events delivered to the in-app channel for this tenant."""
    delivered = (
        select(NotificationDelivery.notification_id)
        .where(
            NotificationDelivery.tenant_id == principal.tenant_id,
            NotificationDelivery.channel == "in_app",
        )
    )
    q = select(Notification).where(
        Notification.tenant_id == principal.tenant_id,
        Notification.id.in_(delivered),
    )
    if unread_only:
        q = q.where(Notification.read.is_(False))
    q = q.order_by(desc(Notification.created_at)).limit(min(limit, 200))
    rows = (await db.execute(q)).scalars().all()
    return {"notifications": [_note_row(n) for n in rows]}


@router.get("/unread-count")
async def unread_count_endpoint(
    principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)
):
    delivered = (
        select(NotificationDelivery.notification_id)
        .where(
            NotificationDelivery.tenant_id == principal.tenant_id,
            NotificationDelivery.channel == "in_app",
        )
    )
    count = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.tenant_id == principal.tenant_id,
                Notification.id.in_(delivered),
                Notification.read.is_(False),
            )
        )
    ).scalar() or 0
    return {"count": int(count)}


@router.post("/{notification_id}/read")
async def mark_read_endpoint(
    notification_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    n = await db.get(Notification, notification_id)
    if n is None or n.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Notification not found.")
    n.read = True
    await db.commit()
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read_endpoint(
    principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)
):
    await db.execute(
        update(Notification)
        .where(Notification.tenant_id == principal.tenant_id, Notification.read.is_(False))
        .values(read=True)
    )
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- rules
class RuleUpsert(BaseModel):
    id: str | None = None
    name: str = Field(max_length=200)
    enabled: bool = True
    event_types: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    min_severity: str = "warning"
    in_app: bool = True
    connector_ids: list[str] = Field(default_factory=list)


def _rule_row(r: NotificationRule) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "enabled": r.enabled,
        "event_types": r.event_types,
        "sources": r.sources,
        "min_severity": r.min_severity,
        "in_app": r.in_app,
        "connector_ids": r.connector_ids,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("/rules")
async def list_rules_endpoint(
    principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.execute(
            select(NotificationRule)
            .where(NotificationRule.tenant_id == principal.tenant_id)
            .order_by(NotificationRule.created_at)
        )
    ).scalars().all()
    return {"rules": [_rule_row(r) for r in rows]}


@router.put("/rules")
async def upsert_rule_endpoint(
    payload: RuleUpsert,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if payload.id:
        rule = await db.get(NotificationRule, payload.id)
        # Treat a cross-tenant rule id as if it doesn't exist so existence can't
        # be probed and the foreign rule can't be edited.
        if rule is None or rule.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="Rule not found.")
    else:
        rule = NotificationRule(tenant_id=principal.tenant_id, created_by=principal.subject)
        db.add(rule)
    rule.name = payload.name
    rule.enabled = payload.enabled
    rule.event_types = payload.event_types
    rule.sources = payload.sources
    rule.min_severity = payload.min_severity
    rule.in_app = payload.in_app
    rule.connector_ids = payload.connector_ids
    rule.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rule)
    return {"rule": _rule_row(rule)}


@router.delete("/rules/{rule_id}")
async def delete_rule_endpoint(
    rule_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rule = await db.get(NotificationRule, rule_id)
    if rule is None or rule.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Rule not found.")
    await db.delete(rule)
    await db.commit()
    return {"ok": True}

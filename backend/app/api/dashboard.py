"""Dashboard personalization and cache-only summary endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, get_principal
from app.models import AssessmentRun, Case, Chat, MissionRun, RecentItem

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_MAX_STORED = 50
_RETENTION_DAYS = 90

# A recent item never grants access.  This registry is both an internal-route allowlist
# and the permission that must still be held when the history is read later.
_KIND_RULES: dict[str, tuple[str, str]] = {
    "chat": ("/c", "chat.use"),
    "workload": ("/workloads", "workloads.read"),
    "workload_group": ("/workloads/groups", "workloads.read"),
    "mission": ("/mission-control", "missions.read"),
    "assessment": ("/assessments", "assessments.read"),
    "architecture": ("/architectures", "architectures.read"),
    "know_me": ("/knowme", "architectures.read"),
    "fmea": ("/fmea", "architectures.read"),
    "case": ("/cases", "cases.read"),
    "insight": ("/insights", "insights.read"),
    "evidence": ("/evidence", "evidence.read"),
    "graph": ("/graph", "graph.read"),
    "inventory": ("/inventory", "inventory.read"),
    "ownership": ("/ownership", "ownership.read"),
    "tag_intelligence": ("/tagintel", "tagintel.read"),
    "change_explorer": ("/change-explorer", "changeexplorer.read"),
    "iam": ("/iam", "iam.read"),
    "entra": ("/entra", "entra.read"),
    "policy": ("/policy", "policy.read"),
    "coverage": ("/coverage", "coverage.read"),
    "alerts_manager": ("/alerts-manager", "alerts_manager.read"),
    "telemetry": ("/telemetry", "coverage.read"),
    "backup_dr": ("/backupdr", "coverage.read"),
    "backup_manager": ("/backup-manager", "backup_manager.read"),
    "resiliency": ("/resiliency", "resiliency.read"),
    "capability": ("/capability", "connections.read"),
    "radar": ("/radar", "radar.read"),
    "reservations": ("/reservations", "reservations.read"),
    "quota": ("/quota", "quota.read"),
    "telemetry_intelligence": ("/telemetry-intel", "teleintel.read"),
    "performance": ("/performance", "perfprofile.read"),
    "monitor": ("/monitor", "monitor.view"),
    "stats": ("/stats", "monitor.view"),
    "automation": ("/automations", "tasks.read"),
}
_DB_ENTITY_MODELS = {
    "chat": Chat,
    "assessment": AssessmentRun,
    "mission": MissionRun,
    "case": Case,
}


class RecentItemTouch(BaseModel):
    kind: str = Field(min_length=1, max_length=48)
    item_key: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    subtitle: str = Field(default="", max_length=256)
    route: str = Field(min_length=1, max_length=1024)
    connection_id: str | None = Field(default=None, max_length=128)
    workload_id: str | None = Field(default=None, max_length=128)


class PinRequest(BaseModel):
    pinned: bool


def _clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _permission_and_route(kind: str, route: str) -> tuple[str, str]:
    rule = _KIND_RULES.get(kind)
    if rule is None:
        raise HTTPException(status_code=422, detail="Unsupported recent-item kind.")
    if any(ord(char) < 32 for char in route) or "\\" in route or route.startswith("//"):
        raise HTTPException(status_code=422, detail="Recent-item route is not a safe internal route.")
    parsed = urlsplit(route)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.fragment:
        raise HTTPException(status_code=422, detail="Recent-item route must be an internal application path.")
    prefix, permission = rule
    if parsed.path != prefix and not parsed.path.startswith(f"{prefix}/"):
        raise HTTPException(status_code=422, detail="Recent-item route does not match its kind.")
    normalized = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return permission, normalized


def _public(row: RecentItem, permission: str) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "item_key": row.item_key,
        "title": row.title,
        "subtitle": row.subtitle,
        "route": row.route,
        "permission": permission,
        "connection_id": row.connection_id,
        "workload_id": row.workload_id,
        "pinned": row.pinned,
        "visit_count": row.visit_count,
        "last_visited_at": row.last_visited_at.isoformat(),
    }


async def _existing_entity_keys(
    db: AsyncSession, principal: Principal, rows: list[RecentItem],
) -> dict[str, set[str]]:
    """Resolve DB-owned entities in batches; file-backed/scoped destinations remain valid.

    This makes a trashed chat/run/case disappear from recents without turning a GET into a
    cleanup write. File-backed features already fail closed in their own route loaders.
    """
    found: dict[str, set[str]] = {}
    for kind, model in _DB_ENTITY_MODELS.items():
        keys = {row.item_key for row in rows if row.kind == kind}
        if not keys:
            continue
        identity_column = model.workload_id if kind == "mission" else model.id
        statement = select(identity_column).where(
            identity_column.in_(keys), model.tenant_id == principal.tenant_id,
        )
        if kind == "chat":
            statement = statement.where(model.user_id == principal.subject, model.archived.is_(False))
        elif hasattr(model, "deleted_at"):
            statement = statement.where(model.deleted_at.is_(None))
        found[kind] = set((await db.execute(statement)).scalars().all())
    return found


async def _prune(db: AsyncSession, principal: Principal) -> None:
    # Keep the store bounded even when a user pins every destination. Pinned items
    # win over unpinned history, then recency decides which rows survive. If pinned
    # history alone exceeds the cap, its oldest row is evicted rather than allowing
    # this personalization table to grow without limit.
    overflow = (
        await db.execute(
            select(RecentItem.id)
            .where(
                RecentItem.tenant_id == principal.tenant_id,
                RecentItem.user_id == principal.subject,
            )
            .order_by(RecentItem.pinned.desc(), RecentItem.last_visited_at.desc())
            .offset(_MAX_STORED)
        )
    ).scalars().all()
    if overflow:
        await db.execute(delete(RecentItem).where(RecentItem.id.in_(overflow)))


@router.get("/recent-items")
async def list_recent_items(
    limit: int = Query(default=8, ge=1, le=20),
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)
    rows = list((await db.execute(
        select(RecentItem)
        .where(
            RecentItem.tenant_id == principal.tenant_id,
            RecentItem.user_id == principal.subject,
            RecentItem.last_visited_at >= cutoff,
        )
        .order_by(RecentItem.pinned.desc(), RecentItem.last_visited_at.desc())
        .limit(_MAX_STORED)
    )).scalars().all())
    existing = await _existing_entity_keys(db, principal, rows)
    visible: list[dict] = []
    for row in rows:
        rule = _KIND_RULES.get(row.kind)
        if rule is None or not principal.has(rule[1]):
            continue
        if row.kind in existing and row.item_key not in existing[row.kind]:
            continue
        visible.append(_public(row, rule[1]))
        if len(visible) >= limit:
            break
    return {"items": visible}


@router.put("/recent-items")
async def touch_recent_item(
    payload: RecentItemTouch,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    permission, route = _permission_and_route(payload.kind, payload.route)
    if not principal.has(permission):
        raise HTTPException(status_code=403, detail="This destination is outside your current access.")
    title = _clean_text(payload.title)
    if not title:
        raise HTTPException(status_code=422, detail="Recent-item title cannot be blank.")
    item_key = payload.item_key.strip()
    now = datetime.now(timezone.utc)
    where = (
        RecentItem.tenant_id == principal.tenant_id,
        RecentItem.user_id == principal.subject,
        RecentItem.kind == payload.kind,
        RecentItem.item_key == item_key,
    )
    row = (await db.execute(select(RecentItem).where(*where))).scalar_one_or_none()
    if row is None:
        row = RecentItem(
            tenant_id=principal.tenant_id,
            user_id=principal.subject,
            kind=payload.kind,
            item_key=item_key,
            title=title,
            subtitle=_clean_text(payload.subtitle),
            route=route,
            connection_id=payload.connection_id or None,
            workload_id=payload.workload_id or None,
            last_visited_at=now,
        )
        db.add(row)
    else:
        row.title = title
        row.subtitle = _clean_text(payload.subtitle)
        row.route = route
        row.connection_id = payload.connection_id or None
        row.workload_id = payload.workload_id or None
        row.last_visited_at = now
        row.visit_count += 1
    await db.flush()
    await _prune(db, principal)
    try:
        await db.commit()
    except IntegrityError:
        # Two tabs may touch the same new entity concurrently. The unique key is the
        # authority; retry as an update rather than surfacing a spurious conflict.
        await db.rollback()
        row = (await db.execute(select(RecentItem).where(*where))).scalar_one_or_none()
        if row is None:
            # A concurrent clear/remove can win between the conflicting insert and
            # this retry. Return a retryable conflict instead of leaking NoResultFound.
            raise HTTPException(status_code=409, detail="Recent history changed; retry the visit.")
        row.title = title
        row.subtitle = _clean_text(payload.subtitle)
        row.route = route
        row.connection_id = payload.connection_id or None
        row.workload_id = payload.workload_id or None
        row.last_visited_at = now
        row.visit_count += 1
        await db.commit()
    await db.refresh(row)
    return {"item": _public(row, permission)}


@router.patch("/recent-items/{item_id}/pin")
async def pin_recent_item(
    item_id: str,
    payload: PinRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(RecentItem).where(
        RecentItem.id == item_id,
        RecentItem.tenant_id == principal.tenant_id,
        RecentItem.user_id == principal.subject,
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Recent item not found.")
    rule = _KIND_RULES.get(row.kind)
    if rule is None or not principal.has(rule[1]):
        raise HTTPException(status_code=403, detail="This destination is outside your current access.")
    row.pinned = payload.pinned
    await db.commit()
    return {"ok": True, "pinned": row.pinned}


@router.delete("/recent-items/{item_id}")
async def remove_recent_item(
    item_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(delete(RecentItem).where(
        RecentItem.id == item_id,
        RecentItem.tenant_id == principal.tenant_id,
        RecentItem.user_id == principal.subject,
    ))
    await db.commit()
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="Recent item not found.")
    return {"ok": True}


@router.delete("/recent-items")
async def clear_recent_items(
    include_pinned: bool = Query(default=False),
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    conditions = [
        RecentItem.tenant_id == principal.tenant_id,
        RecentItem.user_id == principal.subject,
    ]
    if not include_pinned:
        conditions.append(RecentItem.pinned.is_(False))
    result = await db.execute(delete(RecentItem).where(*conditions))
    await db.commit()
    return {"ok": True, "deleted": int(result.rowcount or 0)}


__all__ = ["router"]

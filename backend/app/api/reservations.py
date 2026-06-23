"""Azure Reservations Monitor endpoints.

Tracks reservation orders for the default connection's identity, flags those expiring
within (or expired within the last) ``reservations_window_days`` days, and previews the
weekly digest. Reservation orders are tenant/billing-scoped, so there is a single live
scope (``tenant``) plus a synthetic ``demo`` scope. Admin-gated.

Real scopes are **server-side cached**: selecting the view only READS the cache and never
triggers the (multi-call) Capacity queries — a miss returns an empty ``never_loaded``
snapshot so the UI prompts for Refresh. Only ``POST /reservations/refresh`` recomputes
under a per-scope lock. The demo scope is synthesised locally, so it stays instant."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog
from app.reservations import cache, demo

router = APIRouter(prefix="/reservations", tags=["reservations"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("reservations.read")
log = logging.getLogger("app.api.reservations")

_LIVE_SCOPE = "tenant"


def _settings() -> tuple[int, int]:
    from app.core.app_settings import load_settings

    s = load_settings()
    ttl = int(s.get("reservations_cache_ttl_s", 21600) or 21600)
    window = int(s.get("reservations_window_days", 60) or 60)
    return ttl, window


def _decorate(snap: dict[str, Any], ttl_s: int, *, scope_id: str) -> dict[str, Any]:
    age = cache.age_seconds(snap)
    out = dict(snap)
    out["scope_id"] = scope_id
    out["ttl_s"] = ttl_s
    out["age_seconds"] = int(age) if age is not None else None
    out["stale_cache"] = (age is None) or (age >= ttl_s)
    out.setdefault("never_loaded", False)
    out.setdefault("items", [])
    return out


def _empty(window_days: int, *, connection_configured: bool) -> dict[str, Any]:
    from app.reservations.collector import empty_snapshot

    return empty_snapshot(connection_configured=connection_configured, window_days=window_days, never_loaded=True)


async def _get_snapshot(principal: Principal, *, use_demo: bool, force: bool, connection_id: str | None = None) -> dict[str, Any]:
    from app.core.azure_connections import resolve_connection

    ttl, window = _settings()
    tenant_id = principal.tenant_id or "default"
    # Reservation orders are billing/tenant-scoped, so two connections (tenants) return genuinely
    # different data — key the cache by the chosen connection so picking a tenant never shows
    # another tenant's reservations.
    scope_id = demo.DEMO_SCOPE_ID if use_demo else f"{_LIVE_SCOPE}:{connection_id or 'default'}"

    if use_demo:
        snap = cache.read_snapshot(tenant_id, scope_id)
        if force or snap is None or not cache.is_fresh(snap, ttl) or not snap.get("demo"):
            snap = demo.seed_demo(window_days=window)
            cache.write_snapshot(tenant_id, scope_id, snap)
        return _decorate(snap, ttl, scope_id=scope_id)

    if not force:
        # Selecting the view only ever READS the cache; a miss prompts the user to Refresh.
        snap = cache.read_snapshot(tenant_id, scope_id)
        if snap:
            return _decorate(snap, ttl, scope_id=scope_id)
        connection = resolve_connection(connection_id)
        return _decorate(_empty(window, connection_configured=connection is not None), ttl, scope_id=scope_id)

    lock = cache.get_lock(tenant_id, scope_id)
    async with lock:
        from app.reservations.collector import collect_reservations

        connection = resolve_connection(connection_id)
        fresh = await collect_reservations(connection, window_days=window)
        cache.write_snapshot(tenant_id, scope_id, fresh)
        return _decorate(fresh, ttl, scope_id=scope_id)


@router.get("/overview")
async def overview(
    demo: bool = Query(default=False),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return await _get_snapshot(principal, use_demo=demo, force=False, connection_id=connection_id)


@router.post("/refresh")
async def refresh(
    demo: bool = Query(default=False),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    snap = await _get_snapshot(principal, use_demo=demo, force=True, connection_id=connection_id)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="reservations.refresh",
            target="demo" if demo else "tenant",
            metadata_json={"total": snap.get("counts", {}).get("total"), "error": snap.get("error", "")[:200]},
        )
    )
    await db.commit()
    return snap


@router.get("/digest/preview")
async def digest_preview(
    demo: bool = Query(default=False),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Preview exactly what the weekly digest would send right now (read-only)."""
    from app.reservations.digest import render_html, select_digest_items

    _ttl, window = _settings()
    snap = await _get_snapshot(principal, use_demo=demo, force=False, connection_id=connection_id)
    sel = select_digest_items(snap, window_days=window)
    html = render_html(sel["items"], window_days=window)
    return {**sel, "html": html, "never_loaded": snap.get("never_loaded", False), "error": snap.get("error", "")}

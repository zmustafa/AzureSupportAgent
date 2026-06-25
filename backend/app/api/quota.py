"""Quota Monitor endpoints.

Discovers and reports Azure quota posture for a SINGLE selected subscription (the default scope),
across all regions the subscription reports — or a region/category subset the operator picks.

Like Reservations/Radar, real scopes are **server-side cached**: opening the view only READS the
cache (no ARM calls); a miss returns a ``never_loaded`` snapshot so the UI prompts for a scan.
``POST /quota/scan`` recomputes under a per-scope lock, writes the cache, and records a compact
``QuotaScanRun`` history point. There are NO scheduled scans — refresh is always manual."""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog, QuotaScanRun
from app.quota import cache, demo
from app.quota.base import registry
import app.quota.collectors  # noqa: F401 - registers collectors

router = APIRouter(prefix="/quota", tags=["quota"])

require_read = require_permission("quota.read")
require_run = require_permission("quota.run")
log = logging.getLogger("app.api.quota")

_WARN_PLUS = {"Critical", "Warning"}


def _ttl() -> int:
    from app.core.app_settings import load_settings

    return int(load_settings().get("quota_cache_ttl_s", 21600) or 21600)


def _scope_id(subscription_id: str, *, demo_mode: bool) -> str:
    return demo.DEMO_SCOPE_ID if demo_mode else f"sub:{subscription_id or 'none'}"


def _csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _decorate(snap: dict[str, Any], *, scope_id: str) -> dict[str, Any]:
    ttl = _ttl()
    age = cache.age_seconds(snap)
    out = dict(snap)
    out["scope_id"] = scope_id
    out["ttl_s"] = ttl
    out["age_seconds"] = int(age) if age is not None else None
    out["stale_cache"] = (age is None) or (age >= ttl)
    out.setdefault("never_loaded", False)
    out.setdefault("results", [])
    return out


# ----------------------------------------------------------------------- read endpoints
@router.get("/meta")
async def meta(principal: Principal = Depends(require_read)) -> dict[str, Any]:
    """Collector catalog + categories + active thresholds (drives the UI legend/filters)."""
    from app.quota.risk import load_thresholds

    return {
        "collectors": registry.meta(),
        "categories": registry.categories(),
        "thresholds": load_thresholds(),
        "capacity_note": (
            "Quota approval does not guarantee real-time Azure regional/SKU capacity."
        ),
    }


@router.get("/subscriptions")
async def subscriptions(
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Subscriptions visible to the connection's identity (for the scope picker)."""
    from app.azure.arm import list_subscriptions
    from app.azure.credentials import get_arm_token
    from app.core.azure_connections import resolve_connection

    conn = resolve_connection(connection_id)
    if conn is None:
        return {"subscriptions": [], "error": "No Azure connection configured."}
    token, terr = await get_arm_token(conn)
    if terr or not token:
        return {"subscriptions": [], "error": terr or "No ARM token."}
    subs, err = await list_subscriptions(token)
    return {"subscriptions": subs, "error": err or ""}


@router.get("/regions")
async def regions(
    subscription_id: str = Query(...),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Regions the subscription reports (so the operator can scan all or a subset)."""
    from app.azure.credentials import get_arm_token
    from app.core.azure_connections import resolve_connection
    from app.quota import providers as prov

    conn = resolve_connection(connection_id)
    if conn is None:
        return {"regions": [], "error": "No Azure connection configured."}
    token, terr = await get_arm_token(conn)
    if terr or not token:
        return {"regions": [], "error": terr or "No ARM token."}
    regs, err = await prov.list_regions(token, subscription_id)
    return {"regions": regs, "error": err or ""}


@router.get("/providers")
async def providers_status(
    subscription_id: str = Query(...),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Resource-provider registration status for the required providers."""
    from app.azure.credentials import get_arm_token
    from app.core.azure_connections import resolve_connection
    from app.quota import providers as prov

    conn = resolve_connection(connection_id)
    if conn is None:
        return {"providers": [], "error": "No Azure connection configured."}
    token, terr = await get_arm_token(conn)
    if terr or not token:
        return {"providers": [], "error": terr or "No ARM token."}
    regs, err = await prov.provider_registration(token, subscription_id)
    return {"providers": regs, "error": err or ""}


@router.get("/overview")
async def overview(
    subscription_id: str = Query(default=""),
    connection_id: str | None = Query(default=None),
    demo_mode: bool = Query(default=False, alias="demo"),
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Latest cached snapshot (READ-ONLY — never triggers a scan). Miss → never_loaded."""
    tenant_id = principal.tenant_id or "default"
    scope_id = _scope_id(subscription_id, demo_mode=demo_mode)

    if demo_mode:
        snap = cache.read_snapshot(tenant_id, scope_id)
        if snap is None or not snap.get("demo") or cache.age_seconds(snap) is None:
            snap = demo.seed_demo()
            cache.write_snapshot(tenant_id, scope_id, snap)
        return _decorate(snap, scope_id=scope_id)

    snap = cache.read_snapshot(tenant_id, scope_id)
    if snap:
        return _decorate(snap, scope_id=scope_id)
    from app.core.azure_connections import resolve_connection
    from app.quota.scan import empty_snapshot

    conn = resolve_connection(connection_id)
    empty = empty_snapshot(connection_configured=conn is not None, never_loaded=True)
    empty["subscription_id"] = subscription_id
    return _decorate(empty, scope_id=scope_id)


# ----------------------------------------------------------------------- scan
@router.post("/scan")
async def scan(
    subscription_id: str = Query(default=""),
    connection_id: str | None = Query(default=None),
    demo_mode: bool = Query(default=False, alias="demo"),
    regions: str | None = Query(default=None, description="Comma-separated region names (default: all)."),
    categories: str | None = Query(default=None, description="Comma-separated categories (default: all)."),
    include_unused: bool = Query(default=False, description="Include zero-usage rows for every category."),
    principal: Principal = Depends(require_run),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run a quota scan for one subscription under a per-scope lock; cache + record history."""
    tenant_id = principal.tenant_id or "default"
    scope_id = _scope_id(subscription_id, demo_mode=demo_mode)

    if demo_mode:
        snap = demo.seed_demo()
        cache.write_snapshot(tenant_id, scope_id, snap)
        return _decorate(snap, scope_id=scope_id)

    if not subscription_id:
        from app.quota.scan import empty_snapshot

        return _decorate(empty_snapshot(connection_configured=True, error="No subscription selected."), scope_id=scope_id)

    region_list = _csv_list(regions)
    category_list = _csv_list(categories)

    lock = cache.get_lock(tenant_id, scope_id)
    async with lock:
        from app.azure.arm import list_subscriptions
        from app.azure.credentials import get_arm_token
        from app.core.azure_connections import resolve_connection
        from app.quota.scan import empty_snapshot, run_scan

        conn = resolve_connection(connection_id)
        if conn is None:
            return _decorate(empty_snapshot(connection_configured=False), scope_id=scope_id)

        # Resolve a friendly subscription name (best-effort).
        sub_name = subscription_id
        token, terr = await get_arm_token(conn)
        if not terr and token:
            subs, _e = await list_subscriptions(token)
            for s in subs:
                if s.get("id") == subscription_id:
                    sub_name = s.get("name", subscription_id)
                    break

        started = datetime.now(timezone.utc)
        snap = await run_scan(
            conn, subscription_id, sub_name,
            regions=region_list, categories=category_list,
            tenant_id=tenant_id, tenant_name=principal.display_name or "",
            include_unused=include_unused,
        )
        cache.write_snapshot(tenant_id, scope_id, snap)

        # --- record a compact history point + diff vs previous run --------------------
        if not snap.get("error"):
            await _record_run(db, principal, conn, connection_id, scope_id, snap, started)

    return _decorate(snap, scope_id=scope_id)


@router.post("/scan/stream")
async def scan_stream(
    subscription_id: str = Query(default=""),
    connection_id: str | None = Query(default=None),
    demo_mode: bool = Query(default=False, alias="demo"),
    regions: str | None = Query(default=None, description="Comma-separated region names (default: all)."),
    categories: str | None = Query(default=None, description="Comma-separated categories (default: all)."),
    include_unused: bool = Query(default=False, description="Include zero-usage rows for every category."),
    principal: Principal = Depends(require_run),
    db: AsyncSession = Depends(get_db),
):
    """Run a quota scan, streaming live progress as SSE so the UI can show an activity-log popup
    (like FMEA generation). Emits ``status`` events during the scan, then a final ``done`` event
    carrying the decorated snapshot. Caches the snapshot + records history on success."""
    tenant_id = principal.tenant_id or "default"
    scope_id = _scope_id(subscription_id, demo_mode=demo_mode)

    async def _gen():
        # Demo: synthesize instantly with a couple of staged status lines.
        if demo_mode:
            yield {"event": "status", "data": json.dumps({"phase": "auth", "message": "🔑 Demo mode — using sample data…"})}
            snap = demo.seed_demo()
            cache.write_snapshot(tenant_id, scope_id, snap)
            yield {"event": "status", "data": json.dumps({"phase": "done", "message": "✅ Demo snapshot ready."})}
            yield {"event": "done", "data": json.dumps(_decorate(snap, scope_id=scope_id), default=str)}
            return

        if not subscription_id:
            yield {"event": "error", "data": json.dumps({"message": "No subscription selected."})}
            return

        region_list = _csv_list(regions)
        category_list = _csv_list(categories)

        lock = cache.get_lock(tenant_id, scope_id)
        async with lock:
            from app.azure.arm import list_subscriptions
            from app.azure.credentials import get_arm_token
            from app.core.azure_connections import resolve_connection
            from app.quota.scan import scan_events

            conn = resolve_connection(connection_id)
            if conn is None:
                yield {"event": "error", "data": json.dumps({"message": "No Azure connection configured."})}
                return

            # Resolve a friendly subscription name (best-effort).
            sub_name = subscription_id
            token, terr = await get_arm_token(conn)
            if not terr and token:
                subs, _e = await list_subscriptions(token)
                for s in subs:
                    if s.get("id") == subscription_id:
                        sub_name = s.get("name", subscription_id)
                        break

            started = datetime.now(timezone.utc)
            snap: dict[str, Any] | None = None
            try:
                async for kind, payload in scan_events(
                    conn, subscription_id, sub_name,
                    regions=region_list, categories=category_list,
                    tenant_id=tenant_id, tenant_name=principal.display_name or "",
                    include_unused=include_unused,
                ):
                    if kind == "status":
                        yield {"event": "status", "data": json.dumps(payload)}
                    elif kind == "snapshot":
                        snap = payload
            except Exception as exc:  # noqa: BLE001 - never crash the stream
                log.exception("Quota scan stream failed")
                yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}
                return

            if snap is None:
                yield {"event": "error", "data": json.dumps({"message": "Scan produced no result."})}
                return

            cache.write_snapshot(tenant_id, scope_id, snap)
            if not snap.get("error"):
                try:
                    await _record_run(db, principal, conn, connection_id, scope_id, snap, started)
                except Exception:  # noqa: BLE001 - history is best-effort
                    log.warning("Quota scan history record failed", exc_info=True)

        yield {"event": "done", "data": json.dumps(_decorate(snap, scope_id=scope_id), default=str)}

    return EventSourceResponse(_gen())


async def _record_run(
    db: AsyncSession, principal: Principal, conn: dict[str, Any], connection_id: str | None,
    scope_id: str, snap: dict[str, Any], started: datetime,
) -> None:
    tenant_id = principal.tenant_id or "default"
    sub_id = snap.get("subscription_id", "")
    counts = snap.get("counts", {})
    risk_keys = sorted({
        f"{r.get('region', '')}|{r.get('provider_namespace', '')}|{r.get('quota_name', '')}".lower()
        for r in snap.get("results", [])
        if r.get("risk_level") in _WARN_PLUS
    })

    # Diff vs the previous run for the same subscription.
    prev = (
        await db.scalars(
            select(QuotaScanRun)
            .where(QuotaScanRun.tenant_id == tenant_id, QuotaScanRun.subscription_id == sub_id, QuotaScanRun.demo == False)  # noqa: E712
            .order_by(QuotaScanRun.started_at.desc())
            .limit(1)
        )
    ).first()
    diff = None
    if prev is not None:
        prev_keys = set(prev.risk_keys_json or [])
        cur = set(risk_keys)
        diff = {"new_at_risk": sorted(cur - prev_keys), "recovered": sorted(prev_keys - cur)}

    ended = datetime.now(timezone.utc)
    run = QuotaScanRun(
        tenant_id=tenant_id,
        connection_id=connection_id,
        subscription_id=sub_id,
        subscription_name=snap.get("subscription_name", ""),
        scope=scope_id,
        trigger="manual",
        status=snap.get("status", "succeeded"),
        regions_json=snap.get("regions_scanned", []),
        categories_json=snap.get("categories_scanned", []),
        total_results=int(counts.get("total", 0)),
        critical_count=int(counts.get("Critical", 0)),
        warning_count=int(counts.get("Warning", 0)),
        watch_count=int(counts.get("Watch", 0)),
        counts_json=counts,
        provider_errors_json=snap.get("provider_errors", []),
        risk_keys_json=risk_keys,
        diff_json=diff,
        demo=False,
        triggered_by=principal.subject,
        started_at=started,
        ended_at=ended,
        duration_ms=int((ended - started).total_seconds() * 1000),
    )
    db.add(run)
    db.add(AuditLog(
        tenant_id=tenant_id,
        actor_id=principal.subject,
        action="quota.scan",
        target=f"sub:{sub_id}",
        metadata_json={
            "regions": len(snap.get("regions_scanned", [])),
            "critical": int(counts.get("Critical", 0)),
            "warning": int(counts.get("Warning", 0)),
            "status": snap.get("status", "succeeded"),
        },
    ))
    await db.commit()


# ----------------------------------------------------------------------- history
@router.get("/runs")
async def runs(
    subscription_id: str = Query(default=""),
    limit: int = Query(default=30, ge=1, le=200),
    principal: Principal = Depends(require_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Compact scan history for trend charting (newest first)."""
    tenant_id = principal.tenant_id or "default"
    stmt = select(QuotaScanRun).where(QuotaScanRun.tenant_id == tenant_id)
    if subscription_id:
        stmt = stmt.where(QuotaScanRun.subscription_id == subscription_id)
    stmt = stmt.order_by(QuotaScanRun.started_at.desc()).limit(limit)
    rows = list(await db.scalars(stmt))
    return {"runs": [_run_to_dict(r) for r in reversed(rows)]}


def _run_to_dict(r: QuotaScanRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "subscription_id": r.subscription_id,
        "subscription_name": r.subscription_name,
        "status": r.status,
        "regions": r.regions_json or [],
        "categories": r.categories_json or [],
        "total_results": r.total_results,
        "critical_count": r.critical_count,
        "warning_count": r.warning_count,
        "watch_count": r.watch_count,
        "counts": r.counts_json or {},
        "provider_errors": r.provider_errors_json or [],
        "diff": r.diff_json,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "duration_ms": r.duration_ms,
    }


# ----------------------------------------------------------------------- export
@router.get("/export")
async def export(
    subscription_id: str = Query(default=""),
    demo_mode: bool = Query(default=False, alias="demo"),
    fmt: str = Query(default="csv", alias="format"),
    principal: Principal = Depends(require_read),
) -> Response:
    """Export the latest cached snapshot results as CSV or JSON."""
    tenant_id = principal.tenant_id or "default"
    scope_id = _scope_id(subscription_id, demo_mode=demo_mode)
    snap = cache.read_snapshot(tenant_id, scope_id) or (demo.seed_demo() if demo_mode else None)
    results = (snap or {}).get("results", [])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if fmt.lower() == "json":
        body = json.dumps(snap or {"results": []}, indent=2, default=str)
        return Response(
            content=body, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="quota-{stamp}.json"'},
        )

    cols = [
        "subscription_name", "region", "provider_namespace", "service_name", "quota_category",
        "quota_name", "sku_family", "current_usage", "limit", "remaining", "percent_used", "unit",
        "adjustable_status", "source_type", "collection_status", "risk_level", "recommendation",
        "last_checked_utc", "error_message",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for r in results:
        writer.writerow({c: r.get(c, "") for c in cols})
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="quota-{stamp}.csv"'},
    )

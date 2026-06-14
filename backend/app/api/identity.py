"""Identity posture dashboard endpoints.

Surfaces the recurring identity CritSit causes — expiring secrets/certs, ownerless app
registrations, conditional-access gaps, privileged users without MFA, and Key Vault
object expiry — each with severity, owning workload (where resolvable) and one-click
ticket / investigate handoffs.

Results are **server-side cached** (the Graph aggregation is slow). Visiting the page only
ever READS the cache from ``backend/.data/identity_cache.json`` — it never recomputes, even
when the snapshot is stale or missing. Only ``POST /identity/refresh`` (the dashboard Refresh
button) recomputes under a per-tenant lock and overwrites the cache. Admin-gated."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.identity import cache
from app.identity.collector import collect_identity
from app.models import AuditLog

router = APIRouter(prefix="/identity", tags=["identity"])
log = logging.getLogger("app.api.identity")

# Group names a caller may carry over from the last-good snapshot on a partial failure.
_GROUPS = ("expiring_credentials", "ownerless_apps", "ca_gaps", "users_without_mfa", "keyvault_expiry")


def _clamp_days(days: int) -> int:
    """Expiry window in days — any positive value up to a year (presets: 30/60/90)."""
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 90
    return max(1, min(365, d))


def _settings() -> tuple[int, int]:
    from app.core.app_settings import load_settings

    s = load_settings()
    ttl = int(s.get("identity_cache_ttl_s", 21600) or 21600)
    cap = int(s.get("identity_mfa_scan_cap", 50) or 50)
    return ttl, cap


def _decorate(snap: dict[str, Any], ttl_s: int) -> dict[str, Any]:
    """Attach freshness metadata the dashboard renders (Updated X ago / stale badge)."""
    age = cache.age_seconds(snap)
    out = dict(snap)
    out["ttl_s"] = ttl_s
    out["age_seconds"] = int(age) if age is not None else None
    out["stale"] = (age is None) or (age >= ttl_s)
    out.setdefault("never_loaded", False)
    return out


def _empty_overview(tenant_id: str, days: int, connection: dict[str, Any] | None) -> dict[str, Any]:
    """A 'not loaded yet' identity snapshot — empty groups/KPIs with ``never_loaded`` set so the
    UI prompts the user to press Refresh. Visiting the page must never trigger the (slow) Graph
    aggregation; only the Refresh button recomputes."""
    return {
        "generated_at": "",
        "days": int(days),
        "tenant_id": tenant_id,
        "connection_configured": connection is not None,
        "kpis": {
            "expiring_secrets": 0,
            "expiring_certs": 0,
            "ownerless_apps": 0,
            "users_without_mfa": 0,
            "ca_gaps": 0,
            "keyvault_expiring": 0,
        },
        "group_severity": {g: "ok" for g in _GROUPS},
        "groups": {g: [] for g in _GROUPS},
        "errors": {},
        "meta": {},
        "never_loaded": True,
    }


def _merge_last_good(new: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any]:
    """Stale-while-error: for any group that failed AND came back empty, keep the last
    known-good values (clearly annotated) so a transient Graph error doesn't blank the UI."""
    if not prev:
        return new
    for g in _GROUPS:
        if g in new.get("errors", {}) and not new["groups"].get(g) and prev.get("groups", {}).get(g):
            new["groups"][g] = prev["groups"][g]
            new["errors"][g] = f"{new['errors'][g]} (showing last-known values)"
            # Keep KPIs consistent with the carried-over group.
            _recount_kpis(new)
    return new


def _recount_kpis(snap: dict[str, Any]) -> None:
    from app.identity.collector import KIND_CERT, KIND_SECRET

    g = snap["groups"]
    snap["kpis"] = {
        "expiring_secrets": sum(1 for f in g["expiring_credentials"] if f["kind"] == KIND_SECRET),
        "expiring_certs": sum(1 for f in g["expiring_credentials"] if f["kind"] == KIND_CERT),
        "ownerless_apps": len(g["ownerless_apps"]),
        "users_without_mfa": len(g["users_without_mfa"]),
        "ca_gaps": len(g["ca_gaps"]),
        "keyvault_expiring": len(g["keyvault_expiry"]),
    }


async def _get_snapshot(principal: Principal, days: int, *, force: bool) -> dict[str, Any]:
    from app.core.azure_connections import get_default_connection

    ttl, mfa_cap = _settings()
    connection = get_default_connection()
    tenant_id = (connection or {}).get("tenant_id") or principal.tenant_id or "default"

    if not force:
        # Plain page visit: only ever READ the cache — never trigger the (slow) Graph
        # aggregation, even when the snapshot is stale or missing. A cache miss returns an
        # empty 'not loaded yet' payload so the UI prompts the user to press Refresh; only
        # ``force`` (the Refresh button) recomputes and overwrites the cache.
        snap = cache.read_snapshot(tenant_id, days)
        if snap:
            return _decorate(snap, ttl)
        return _decorate(_empty_overview(tenant_id, days, connection), ttl)

    lock = cache.get_lock(tenant_id, days)
    async with lock:
        prev = cache.read_snapshot(tenant_id, days)
        fresh = await collect_identity(
            connection, days=days, mfa_cap=mfa_cap, include_keyvault=True, tenant_id=tenant_id
        )
        fresh = _merge_last_good(fresh, prev)
        cache.write_snapshot(tenant_id, days, fresh)
        return _decorate(fresh, ttl)


@router.get("/overview")
async def overview(
    days: int = Query(default=90),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Return the identity snapshot for the expiry window — cached when fresh."""
    return await _get_snapshot(principal, _clamp_days(days), force=False)


@router.post("/refresh")
async def refresh(
    days: int = Query(default=90),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Force a recompute and overwrite the cache (the dashboard Refresh button)."""
    d = _clamp_days(days)
    snap = await _get_snapshot(principal, d, force=True)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="identity.refresh",
            target=f"days={d}",
            metadata_json={"kpis": snap.get("kpis", {})},
        )
    )
    await db.commit()
    return snap


# --------------------------------------------------------------------------- app registrations
def _empty_appregs(tenant_id: str) -> dict[str, Any]:
    """A 'not loaded yet' snapshot — no apps, never_loaded flag set so the UI prompts the
    user to press Refresh. Visiting the page must never trigger the (slow) Graph pull."""
    return {
        "generated_at": "",
        "tenant_id": tenant_id,
        "connection_configured": False,
        "source": "",
        "note": "",
        "apps": [],
        "facets": {"audiences": [], "permissions": [], "owners": []},
        "summary": {
            "total": 0, "withSecrets": 0, "withCerts": 0, "expiringSoon": 0, "expired": 0,
            "highRisk": 0, "ownerless": 0, "applicationPerms": 0, "delegatedPerms": 0,
        },
        "cached": False,
        "never_loaded": True,
        "fetched_at": "",
        "age_seconds": None,
    }


async def _appregs_snapshot(principal: Principal, *, connection_id: str | None, force: bool) -> dict[str, Any]:
    """Server-cached app-registrations snapshot. The cache is permanent and visiting the page
    only ever READS it — it never recomputes. Only ``force`` (the Refresh button) re-pulls from
    Graph. On a cache miss with ``force=False`` we return an empty 'not loaded yet' snapshot."""
    from app.identity import appregs, appregs_cache

    connection, tenant_id, cid = _appregs_target(principal, connection_id)

    if not force:
        hit = appregs_cache.get(tenant_id, cid)
        if hit:
            return {**hit["payload"], "cached": True, "never_loaded": False, "fetched_at": hit["fetched_at"], "age_seconds": hit["age_seconds"]}
        # No cache yet — do NOT compute on a plain page visit.
        return _empty_appregs(tenant_id)

    snap = await appregs.collect_app_registrations(connection, tenant_id=tenant_id)
    fetched_at = appregs_cache.set_(tenant_id, cid, snap)
    return {**snap, "cached": False, "never_loaded": False, "fetched_at": fetched_at, "age_seconds": 0}


def _appregs_target(principal: Principal, connection_id: str | None) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve (connection, tenant_id, cache-connection-id) for app-registrations."""
    from app.core.azure_connections import get_default_connection

    connection = get_default_connection()
    tenant_id = (connection or {}).get("tenant_id") or principal.tenant_id or "default"
    cid = connection_id or (connection or {}).get("id") or ""
    return connection, tenant_id, cid


def _appregs_job_key(tenant_id: str, cid: str) -> str:
    return f"{tenant_id}|{cid}"


@router.get("/app-registrations")
async def app_registrations(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """List Entra ID app registrations with credential counts, permission split and owners.
    Served from the permanent server-side cache (computed on first miss)."""
    return await _appregs_snapshot(principal, connection_id=connection_id, force=False)


@router.post("/app-registrations/refresh")
async def refresh_app_registrations(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Force a re-pull of app registrations and overwrite the cache (the Refresh button)."""
    snap = await _appregs_snapshot(principal, connection_id=connection_id, force=True)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="identity.app_registrations.refresh",
            target=f"apps={snap.get('summary', {}).get('total', 0)}",
            metadata_json={"summary": snap.get("summary", {}), "source": snap.get("source", "")},
        )
    )
    await db.commit()
    return snap


@router.post("/app-registrations/refresh/start")
async def start_app_registrations_refresh(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Kick off a BACKGROUND refresh (the live Entra enumeration can take 10–30 minutes). The
    job keeps running even if the browser navigates away; progress is streamed separately via
    ``/app-registrations/refresh/stream``. Returns the (possibly already-running) job."""
    from app.identity import appregs_job

    connection, tenant_id, cid = _appregs_target(principal, principal and connection_id)
    key = _appregs_job_key(tenant_id, cid)
    already = appregs_job.is_running(key)
    job = appregs_job.start_job(key=key, tenant_id=tenant_id, connection=connection, connection_id=cid)
    if not already:
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id,
                actor_id=principal.subject,
                action="identity.app_registrations.refresh.start",
                target=key,
                metadata_json={"job_id": job["id"]},
            )
        )
        await db.commit()
    return {**(appregs_job.public_job(job) or {}), "already_running": already}


@router.get("/app-registrations/job")
async def app_registrations_job(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Current background-refresh job status for this scope (for reconnect on page visit)."""
    from app.identity import appregs_job

    _conn, tenant_id, cid = _appregs_target(principal, connection_id)
    key = _appregs_job_key(tenant_id, cid)
    return {"job": appregs_job.public_job(appregs_job.get_job(key))}


@router.get("/app-registrations/refresh/stream")
async def stream_app_registrations_refresh(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
):
    """SSE progress stream for the background refresh job: start → progress* (+ ping
    heartbeats) → done(snapshot) | error. Auto-starts a job if none is running, so a single
    call both launches and follows the refresh. Disconnecting does NOT stop the job."""
    from app.identity import appregs_job

    connection, tenant_id, cid = _appregs_target(principal, connection_id)
    key = _appregs_job_key(tenant_id, cid)
    if not appregs_job.is_running(key):
        appregs_job.start_job(key=key, tenant_id=tenant_id, connection=connection, connection_id=cid)
    return EventSourceResponse(appregs_job.stream(key))


class IdentityFinding(BaseModel):
    id: str = ""
    kind: str = ""
    title: str = ""
    detail: str = ""
    severity: str = "info"
    subject: str = ""
    subject_id: str = ""
    expires_at: str | None = None
    days_left: int | None = None
    workload_id: str | None = None
    workload_name: str | None = None
    remediation: str = ""


class TicketRequest(BaseModel):
    connector_id: str = Field(min_length=1)
    finding: IdentityFinding


@router.post("/ticket")
async def create_identity_ticket(
    payload: TicketRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a remediation ticket for an identity finding via a Jira/ServiceNow connector."""
    from app.assessments.tickets import create_ticket

    f = payload.finding
    finding = {
        "severity": f.severity,
        "title": f.title,
        "check_id": f.id or f.kind,
        "pillar": "Identity",
        "description": f.detail
        + (f"\n\nSubject: {f.subject}" if f.subject else "")
        + (f"\nExpires: {f.expires_at}" if f.expires_at else ""),
        "remediation": f.remediation,
    }
    workload_name = f.workload_name or f.subject or "Identity"
    result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=workload_name)
    if result.get("ok"):
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id,
                actor_id=principal.subject,
                action="identity.ticket.create",
                target=(f.id or f.kind)[:512],
                metadata_json={"ticket": result.get("ticket_id", ""), "connector": result.get("connector_type", "")},
            )
        )
        await db.commit()
    return result

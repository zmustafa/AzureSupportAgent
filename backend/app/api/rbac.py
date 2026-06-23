"""RBAC (access review) API — per-scope access scanner with server-side cache.

A standalone, admin-gated dashboard that answers *who can access what* across Azure RBAC
(control + data plane), Entra directory roles, group-derived access, service-principal ownership
and PIM. Ported from the standalone all-azure-access scanner; surfaced as a top-level "RBAC"
section between Inventory and Azure Policy.

Read endpoints serve the **per-scope server cache** only — visiting the page never triggers a
scan. ``POST /rbac/refresh`` recomputes a single scope (or the directory layer, or everything)
as a background job with live SSE progress, so one subscription can be refreshed while the rest
stay served from cache."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog
from app.rbac import cache, compose, demo, export, job, pivots, schema, store
from app.rbac import scopes as scope_filters

router = APIRouter(prefix="/rbac", tags=["rbac"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("rbac.read")
log = logging.getLogger("app.api.rbac")

# Master-row tab filters (one grid, many lenses — see RbacView).
_TAB_FILTERS = {
    "all": lambda r: True,
    "effective": lambda r: True,
    "privileged": lambda r: bool(r.get("roleIsPrivileged")),
    "data_plane": lambda r: bool(r.get("roleHasDataActions")),
    "group": lambda r: r.get("accessPath") == schema.PATH_GROUP or r.get("principalType") == "Group",
    "owners": lambda r: r.get("accessPath") == schema.PATH_OWNER,
    "entra": lambda r: r.get("surface") == schema.SURFACE_ENTRA,
    "azure": lambda r: r.get("surface") == schema.SURFACE_AZURE_RBAC,
}


def _target(principal: Principal, connection_id: str | None) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve (connection, tenant_id, connection-id) for the active access scan."""
    from app.core.azure_connections import resolve_connection

    connection = resolve_connection(connection_id)
    tenant_id = (connection or {}).get("tenant_id") or principal.tenant_id or "default"
    cid = connection_id or (connection or {}).get("id") or ""
    return connection, tenant_id, cid


def _ttl_s() -> int:
    from app.core.app_settings import load_settings

    return int(load_settings().get("rbac_cache_ttl_s", 21600) or 21600)


def _max_rows() -> int:
    from app.core.app_settings import load_settings

    return int(load_settings().get("rbac_max_rows", 5000) or 5000)


# --------------------------------------------------------------------------- overview
@router.get("/overview")
async def overview(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """KPIs + per-scope freshness + collector status. Reads cache only (never scans)."""
    connection, tenant_id, _cid = _target(principal, connection_id)
    ov = compose.compute_overview(tenant_id)
    ttl = _ttl_s()
    for s in ov["scopes"]:
        age = s.get("age_seconds")
        s["stale"] = (age is None) or (age >= ttl)
    ov["ttl_s"] = ttl
    ov["connection_configured"] = connection is not None
    return ov


# --------------------------------------------------------------------------- access grid
@router.get("/access")
async def access(
    tab: str = Query("all"),
    scope: str | None = None,
    surface: str | None = None,
    principal_type: str | None = None,
    search: str | None = None,
    privileged_only: bool = False,
    scope_id: str | None = None,
    subscription_ids: str | None = None,
    workload_id: str | None = None,
    offset: int = 0,
    limit: int = 200,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Paged + filtered normalized access rows for a tab (the shared 46-column grid)."""
    connection, tenant_id, _cid = _target(principal, connection_id)
    rows = compose.build_master_rows(tenant_id)
    # Azure-scope (management group / subscription tree) and/or workload narrowing.
    if scope_id or subscription_ids or workload_id:
        sub_id_list = [s for s in (subscription_ids or "").split(",") if s.strip()]
        rows = await scope_filters.filter_rows(
            rows,
            scope_id=scope_id or "",
            subscription_ids=sub_id_list,
            workload_id=workload_id or "",
            connection=connection,
        )
    tab_filter = _TAB_FILTERS.get(tab, _TAB_FILTERS["all"])
    rows = [r for r in rows if tab_filter(r)]
    if scope:
        sl = scope.lower()
        rows = [r for r in rows if sl in str(r.get("scope", "")).lower() or sl in str(r.get("scopeDisplayName", "")).lower() or sl in str(r.get("subscriptionName", "")).lower()]
    if surface:
        rows = [r for r in rows if r.get("surface") == surface]
    if principal_type:
        rows = [r for r in rows if (r.get("effectivePrincipalType") or r.get("principalType")) == principal_type]
    if privileged_only:
        rows = [r for r in rows if r.get("roleIsPrivileged")]
    if search:
        q = search.lower()
        rows = [
            r
            for r in rows
            if q in str(r.get("effectivePrincipalName", "")).lower()
            or q in str(r.get("principalDisplayName", "")).lower()
            or q in str(r.get("effectivePrincipalUserPrincipalName", "")).lower()
            or q in str(r.get("roleName", "")).lower()
            or q in str(r.get("scope", "")).lower()
        ]
    # Privileged first, then by role name — most-interesting rows on top.
    rows.sort(key=lambda r: (not r.get("roleIsPrivileged"), r.get("roleName", ""), r.get("effectivePrincipalName", "")))
    total = len(rows)
    page = rows[max(0, offset) : max(0, offset) + min(limit, _max_rows())]
    return {"total": total, "offset": offset, "limit": limit, "rows": page, "columns": list(schema.COLUMNS)}


# --------------------------------------------------------------------------- scopes
@router.get("/scope-tree")
async def scope_tree(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """The management-group → subscription tree (with per-node grant counts) used by the scope
    filter. Built from the cache only — visiting never triggers an Azure call."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return scope_filters.build_scope_tree(tenant_id)


# --------------------------------------------------------------------------- scopes
@router.get("/scopes")
async def scopes(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Cached scopes with freshness (drives per-scope refresh buttons) + directory freshness."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    ov = compose.compute_overview(tenant_id)
    ttl = _ttl_s()
    for s in ov["scopes"]:
        age = s.get("age_seconds")
        s["stale"] = (age is None) or (age >= ttl)
    return {"scopes": ov["scopes"], "directory": ov["directory"], "ttl_s": ttl}


# --------------------------------------------------------------------------- roles & principals
@router.get("/roles")
async def roles(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Role definitions + principal directory from the cached directory layer."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    directory = cache.read_directory(tenant_id)
    return {
        "role_defs": directory.get("role_defs", []),
        "principals": directory.get("principals", []),
    }


# --------------------------------------------------------------------------- insights
@router.get("/pivots")
async def insights(
    scope_id: str | None = None,
    subscription_ids: str | None = None,
    workload_id: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """The 13 precomputed pivot summaries for the Insights tab (honors the scope/workload filter)."""
    connection, tenant_id, _cid = _target(principal, connection_id)
    rows = compose.build_master_rows(tenant_id)
    if scope_id or subscription_ids or workload_id:
        sub_id_list = [s for s in (subscription_ids or "").split(",") if s.strip()]
        rows = await scope_filters.filter_rows(
            rows,
            scope_id=scope_id or "",
            subscription_ids=sub_id_list,
            workload_id=workload_id or "",
            connection=connection,
        )
    return {"pivots": pivots.compute_pivots(rows), "labels": pivots.PIVOT_LABELS}


# --------------------------------------------------------------------------- diagnostics
@router.get("/diagnostics")
async def diagnostics(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Collector statuses + any rows that carry an error/partial collection status."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    ov = compose.compute_overview(tenant_id)
    rows = compose.build_master_rows(tenant_id)
    errors = [
        {
            "collector": r.get("collector", ""),
            "scope": r.get("scope", ""),
            "status": r.get("collectionStatus", ""),
            "errorCode": r.get("errorCode", ""),
            "errorMessage": r.get("errorMessage", ""),
        }
        for r in rows
        if r.get("collectionStatus") in schema.ATTENTION_STATUSES or r.get("errorMessage")
    ]
    return {"collectors": ov["collectors"], "errors": errors, "directory": ov["directory"]}


# --------------------------------------------------------------------------- refresh
class RefreshBody(BaseModel):
    scope: str | None = None  # a subscription/MG scope id; omit for directory/all modes
    mode: str = "scope"  # scope | directory | all
    display_name: str | None = None


def _job_scope_key(body: RefreshBody) -> str:
    if body.mode == "all":
        return job.SCOPE_ALL
    if body.mode == "directory":
        return job.SCOPE_DIRECTORY
    return body.scope or job.SCOPE_ALL


@router.post("/refresh")
async def refresh(
    body: RefreshBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Start a background refresh for one scope (or the directory, or everything). Per-scope:
    refreshing one subscription leaves every other scope served from cache."""
    connection, tenant_id, cid = _target(principal, connection_id)
    scope_key = _job_scope_key(body)
    already = job.is_running(job.job_key(tenant_id, scope_key))
    started = job.start_job(
        tenant_id=tenant_id,
        connection=connection,
        scope=scope_key,
        mode=body.mode,
        display_name=body.display_name or "",
        connection_id=cid,
        triggered_by=principal.subject,
    )
    if not already:
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id,
                actor_id=principal.subject,
                action="rbac.refresh",
                target=f"{body.mode}:{scope_key}",
                metadata_json={"job_id": started["id"]},
            )
        )
        await db.commit()
    return {**(job.public_job(started) or {}), "already_running": already}


@router.get("/job")
async def job_status(
    scope: str | None = None,
    mode: str = "scope",
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Current background-refresh job for a scope (reconnect on page visit)."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    scope_key = job.SCOPE_ALL if mode == "all" else (job.SCOPE_DIRECTORY if mode == "directory" else (scope or job.SCOPE_ALL))
    return {"job": job.public_job(job.get_job(job.job_key(tenant_id, scope_key)))}


@router.get("/refresh/stream")
async def refresh_stream(
    scope: str | None = None,
    mode: str = "scope",
    display_name: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
):
    """SSE progress for a scope's background refresh; auto-starts a job if none is running."""
    connection, tenant_id, cid = _target(principal, connection_id)
    scope_key = job.SCOPE_ALL if mode == "all" else (job.SCOPE_DIRECTORY if mode == "directory" else (scope or job.SCOPE_ALL))
    key = job.job_key(tenant_id, scope_key)
    if not job.is_running(key):
        job.start_job(
            tenant_id=tenant_id,
            connection=connection,
            scope=scope_key,
            mode=mode,
            display_name=display_name or "",
            connection_id=cid,
            triggered_by=principal.subject,
        )
    return EventSourceResponse(job.stream(key))


# --------------------------------------------------------------------------- history / drift
@router.get("/runs")
async def runs(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return {"runs": await store.list_runs(tenant_id)}


@router.get("/run/{run_id}")
async def run_detail(
    run_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    _conn, tenant_id, _cid = _target(principal, connection_id)
    run = await store.get_run(tenant_id, run_id)
    return {"run": run}


# --------------------------------------------------------------------------- export
@router.get("/export")
async def export_rows(
    fmt: str = Query("csv", pattern="^(csv|json)$"),
    tab: str = "all",
    scope_id: str | None = None,
    subscription_ids: str | None = None,
    workload_id: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> Response:
    """Download the access rows for a tab as CSV or JSON (46-column scanner parity). Honors the
    active scope/workload filter so the export matches what's on screen."""
    connection, tenant_id, _cid = _target(principal, connection_id)
    rows = compose.build_master_rows(tenant_id)
    if scope_id or subscription_ids or workload_id:
        sub_id_list = [s for s in (subscription_ids or "").split(",") if s.strip()]
        rows = await scope_filters.filter_rows(
            rows,
            scope_id=scope_id or "",
            subscription_ids=sub_id_list,
            workload_id=workload_id or "",
            connection=connection,
        )
    tab_filter = _TAB_FILTERS.get(tab, _TAB_FILTERS["all"])
    rows = [r for r in rows if tab_filter(r)]
    if fmt == "json":
        return Response(content=export.to_json(rows), media_type="application/json", headers={"Content-Disposition": f"attachment; filename=rbac-access-{tab}.json"})
    return Response(content=export.to_csv(rows), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=rbac-access-{tab}.csv"})


@router.get("/export/workbook")
async def export_workbook(
    scope_id: str | None = None,
    subscription_ids: str | None = None,
    workload_id: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> Response:
    """Download a comprehensive multi-sheet Excel workbook (Summary, Effective Access, the
    privileged/group/owner/Entra/Key-Vault lenses, Scopes, Role Definitions, Principals,
    Insights pivots, Diagnostics). Honors the active scope/workload filter."""
    connection, tenant_id, _cid = _target(principal, connection_id)
    rows = compose.build_master_rows(tenant_id)
    if scope_id or subscription_ids or workload_id:
        sub_id_list = [s for s in (subscription_ids or "").split(",") if s.strip()]
        rows = await scope_filters.filter_rows(
            rows,
            scope_id=scope_id or "",
            subscription_ids=sub_id_list,
            workload_id=workload_id or "",
            connection=connection,
        )
    overview = compose.compute_overview(tenant_id)
    pivots_data = pivots.compute_pivots(rows)
    directory = cache.read_directory(tenant_id)
    content = export.to_workbook(
        rows=rows,
        overview=overview,
        pivots=pivots_data,
        pivot_labels=pivots.PIVOT_LABELS,
        directory=directory,
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=rbac-access-review.xlsx"},
    )


# --------------------------------------------------------------------------- demo
@router.post("/demo/seed")
async def demo_seed(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Seed the synthetic demo access dataset (the local reviewable path) for this tenant."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    summary = demo.seed_demo(tenant_id)
    try:
        await store.save_run(tenant_id, scope="__all__", trigger="manual", triggered_by=principal.subject, demo=True)
    except Exception:  # noqa: BLE001
        log.warning("rbac demo run record failed", exc_info=True)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="rbac.demo.seed",
            target="demo",
            metadata_json=summary,
        )
    )
    await db.commit()
    return {"ok": True, **summary, "overview": compose.compute_overview(tenant_id)}


@router.post("/demo/purge")
async def demo_purge(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Remove the demo access snapshot for this tenant (only demo-flagged slices; real scans
    cached under the same tenant are preserved)."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    removed = cache.purge_demo(tenant_id)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="rbac.demo.purge",
            target="demo",
            metadata_json={"scopes_removed": removed},
        )
    )
    await db.commit()
    return {"ok": True, "scopes_removed": removed}

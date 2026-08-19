"""IAM (access review) API — per-scope access scanner with server-side cache.

A standalone, admin-gated dashboard that answers *who can access what* across Azure RBAC
(control + data plane), Entra directory roles, group-derived access, service-principal ownership
and PIM. Ported from the standalone all-azure-access scanner; surfaced as a top-level "IAM"
section between Inventory and Azure Policy.

Read endpoints serve the **per-scope server cache** only — visiting the page never triggers a
scan. ``POST /iam/refresh`` recomputes a single scope (or the directory layer, or everything)
as a background job with live SSE progress, so one subscription can be refreshed while the rest
stay served from cache.

Renamed from ``/rbac``: the screen covers access models that are not RBAC (Key Vault access
policies, classic administrators, PIM eligibility). ``app.main`` also mounts this router under
the legacy ``/rbac`` prefix for one release — see ``legacy_router`` below."""
from __future__ import annotations

import asyncio
import time as _time
import datetime as _dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.iam import attribution, bypass, cache, campaigns, compose, cpu, dataplane, demo, diff, effective, escalation, export, findings, flow, frameworks, importer, job, leavers, pivots, progress, remediation, rightsize, scanner_jobs, scanners, schema, signals, simulator, store, usage
from app.iam import scopes as scope_filters
from app.iam import resource_access as resource_access_mod
from app.iam import score as score_mod
from app.models import AuditLog

# No prefix here on purpose: ``app.main`` mounts this router twice — once at ``/iam`` (the
# documented surface) and once at the legacy ``/rbac`` prefix, hidden from the schema and
# tagged with a Deprecation header by :func:`deprecated_rbac_alias`.
router = APIRouter(tags=["iam"])


async def deprecated_rbac_alias(response: Response) -> None:
    """Mark a response served from the legacy ``/rbac`` prefix.

    Saved automations, MCP clients and bookmarks still call ``/api/rbac/*``; breaking them on
    a rename would be gratuitous. Remove this alias one release after the frontend moves."""
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</api/iam>; rel="successor-version"'


# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("iam.read")
# Mutating the cached dataset (importing a scanner run, purging imports) is a separate
# capability from reading it — an auditor should not be able to overwrite what they review.
require_write = require_permission("iam.write")
# Certification is a third capability. An auditor reads the review; they do not get to record
# the decisions they are auditing, and a reader does not get to close a campaign.
require_review = require_permission("iam.review")
# Modelling a change is a fourth capability. It is read-only and cheap, but it produces a very
# confident-looking artifact, so it is kept separate from plain `iam.read`.
require_simulate = require_permission("iam.simulate")
log = logging.getLogger("app.api.iam")

# Master-row tab filters (one grid, many lenses — see IamView).
_TAB_FILTERS = {
    "all": lambda r: True,
    "effective": lambda r: True,
    "privileged": lambda r: bool(r.get("roleIsPrivileged")),
    "data_plane": lambda r: bool(r.get("roleHasDataActions")),
    "group": lambda r: r.get("accessPath") == schema.PATH_GROUP or r.get("principalType") == "Group",
    "owners": lambda r: r.get("accessPath") == schema.PATH_OWNER,
    "entra": lambda r: r.get("surface") == schema.SURFACE_ENTRA,
    "azure": lambda r: r.get("surface") == schema.SURFACE_AZURE_RBAC,
    # PIM lenses. These exist as server-side filters because the PIM tab needs ALL eligible
    # grants, not the eligible ones that happen to land in a page: filtering a 200-row page of a
    # 5,506-row estate client-side showed "Eligible assignments (3)" beside a KPI reading 137.
    "eligible": lambda r: r.get("assignmentState") == schema.STATE_ELIGIBLE,
    "elevated": lambda r: bool(r.get("activationExpiresOn")),
}


def _apply_grid_filters(
    rows: list[dict[str, Any]],
    *,
    tab: str = "all",
    scope: str | None = None,
    surface: str | None = None,
    principal_type: str | None = None,
    privileged_only: bool = False,
    disabled_only: bool = False,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """The access grid's row filters, in one place.

    Shared by ``GET /access`` and ``GET /export`` DELIBERATELY. They used to filter separately
    and the export understood strictly fewer parameters, so a download quietly contained rows
    the grid above it did not show. An export that disagrees with the screen it was launched
    from is worse than no export: it is the artifact that gets attached to the audit."""
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
    if disabled_only:
        # KNOWN-disabled only. `unknown` is never included: a cache that predates the
        # account-state collector would otherwise turn this lens into "show me everything".
        rows = [r for r in rows if schema.is_disabled(r)]
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
    return rows


def _target(
    principal: Principal,
    connection_id: str | None,
    workload_id: str | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve the active access scan, preserving a workload's canonical tenant ownership."""
    from app.core.azure_connections import connection_for_scope, resolve_connection
    from app.workloads.registry import get_workload

    if workload_id:
        workload = get_workload(workload_id)
        if not workload:
            raise HTTPException(status_code=404, detail="Workload not found")
        connection = connection_for_scope(
            "workload", connection_id=connection_id, workload=workload
        )
        if workload.get("connection_id") and not connection:
            raise HTTPException(
                status_code=409,
                detail="Workload connection is missing or disabled",
            )
    else:
        connection = resolve_connection(connection_id)
    tenant_id = (connection or {}).get("tenant_id") or principal.tenant_id or "default"
    cid = (connection or {}).get("id") or connection_id or ""
    return connection, tenant_id, cid


def _ttl_s() -> int:
    from app.core.app_settings import load_settings

    return int(load_settings().get("iam_cache_ttl_s", 21600) or 21600)


def _max_rows() -> int:
    from app.core.app_settings import load_settings

    return int(load_settings().get("iam_max_rows", 5000) or 5000)


# --------------------------------------------------------------------------- overview
@router.get("/overview")
async def overview(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """KPIs + per-scope freshness + collector status. Reads cache only (never scans)."""
    connection, tenant_id, _cid = _target(principal, connection_id)
    # Off the loop like every other compose call in this module. "Reads cache only" is not the
    # same as "is cheap": after any write the memo is gone and this recomposes the whole estate,
    # and this endpoint is polled by the screen that is open while a refresh runs.
    ov = await asyncio.to_thread(compose.compute_overview, tenant_id)
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
    disabled_only: bool = False,
    scope_id: str | None = None,
    subscription_ids: str | None = None,
    workload_id: str | None = None,
    offset: int = 0,
    limit: int = 200,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Paged + filtered normalized access rows for a tab (the shared 46-column grid)."""
    connection, tenant_id, _cid = _target(principal, connection_id, workload_id)
    rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)
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
    rows = _apply_grid_filters(
        rows,
        tab=tab,
        scope=scope,
        surface=surface,
        principal_type=principal_type,
        privileged_only=privileged_only,
        disabled_only=disabled_only,
        search=search,
    )
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
    return await asyncio.to_thread(scope_filters.build_scope_tree, tenant_id)


# --------------------------------------------------------------------------- scopes
@router.get("/scopes")
async def scopes(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Cached scopes with freshness (drives per-scope refresh buttons) + directory freshness."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    ov = await asyncio.to_thread(compose.compute_overview, tenant_id)
    ttl = _ttl_s()
    for s in ov["scopes"]:
        age = s.get("age_seconds")
        # A delta refresh can confirm a scope is unchanged without re-collecting it. Staleness
        # is therefore measured from the more recent of "collected" and "verified" — otherwise
        # every delta-maintained scope shows a stale warning on data we KNOW is current, and the
        # reader is pushed into full refreshes, which is the cost delta refresh exists to avoid.
        # `age_seconds` itself is left as the real collection age; the UI shows both.
        effective = age
        verified = s.get("verified_age_seconds")
        if verified is not None and (effective is None or verified < effective):
            effective = verified
        s["stale"] = (effective is None) or (effective >= ttl)
    return {"scopes": ov["scopes"], "directory": ov["directory"], "ttl_s": ttl}


# --------------------------------------------------------------------------- roles & principals
@router.get("/roles")
async def roles(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Role definitions + principal directory from the cached directory layer."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    directory = await asyncio.to_thread(cache.read_directory, tenant_id)
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
    connection, tenant_id, _cid = _target(principal, connection_id, workload_id)
    rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)
    if scope_id or subscription_ids or workload_id:
        sub_id_list = [s for s in (subscription_ids or "").split(",") if s.strip()]
        rows = await scope_filters.filter_rows(
            rows,
            scope_id=scope_id or "",
            subscription_ids=sub_id_list,
            workload_id=workload_id or "",
            connection=connection,
        )
    return {"pivots": await asyncio.to_thread(pivots.compute_pivots, rows), "labels": pivots.PIVOT_LABELS}


# --------------------------------------------------------------------------- access map
@router.get("/flow")
async def access_flow(
    scope_id: str | None = None,
    subscription_ids: str | None = None,
    workload_id: str | None = None,
    principal_id: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Pivot-ready access facts for the Access Map.

    Returns a deduplicated projection rather than master rows: the grid's 66-column rows are
    far too heavy to re-send every time the operator reorders the columns, and the diagram only
    reads about eighteen of them. Denies and eligibility are kept distinguishable rather than
    folded in, because a flow diagram cannot express either honestly on its own.
    """
    connection, tenant_id, _cid = _target(principal, connection_id, workload_id)
    rows = await cpu.run(compose.build_master_rows, tenant_id, label="access map rows")
    if scope_id or subscription_ids or workload_id:
        sub_id_list = [s for s in (subscription_ids or "").split(",") if s.strip()]
        rows = await scope_filters.filter_rows(
            rows,
            scope_id=scope_id or "",
            subscription_ids=sub_id_list,
            workload_id=workload_id or "",
            connection=connection,
        )
    if principal_id:
        needle = principal_id.strip().lower()
        rows = [
            r for r in rows
            if needle in {
                str(r.get("principalId") or "").lower(),
                str(r.get("effectivePrincipalId") or "").lower(),
            }
        ]
    facts = await cpu.run(flow.build_facts, rows, label="access map facts")
    return await cpu.run(flow.encode, facts, label="access map encode")


# --------------------------------------------------------------------------- diagnostics
@router.get("/diagnostics")
async def diagnostics(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Collector statuses + any rows that carry an error/partial collection status."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    ov = await asyncio.to_thread(compose.compute_overview, tenant_id)
    rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)
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
def _scope_key_for(mode: str, scope: str | None) -> str:
    """Map (mode, scope) onto the job key that serialises concurrent refreshes.

    `delta` shares SCOPE_ALL deliberately: both write across every subscription slice, so
    letting them run concurrently would have two writers racing over the same cache entries.
    All three refresh endpoints go through here — when this lived inline in each of them, a new
    mode worked in one and silently fell through to the default in the others."""
    if mode in ("all", "delta"):
        return job.SCOPE_ALL
    if mode == "directory":
        return job.SCOPE_DIRECTORY
    return scope or job.SCOPE_ALL


class RefreshBody(BaseModel):
    scope: str | None = None  # a subscription/MG scope id; omit for directory/all modes
    mode: str = "scope"  # scope | directory | all | delta
    display_name: str | None = None


class PinBody(BaseModel):
    pinned: bool = True
    reason: str = ""


class CampaignBody(BaseModel):
    name: str
    selector: dict[str, Any]
    description: str = ""
    baseline_run_id: str = ""
    reviewer_strategy: str = "owner"
    reviewer_fallback_id: str = ""
    due_at: str = ""
    reminder_days: list[int] | None = None


class DecisionBody(BaseModel):
    decision: str
    reason: str = ""
    delegated_to: str = ""


class RemediationBody(BaseModel):
    format: str = "az"
    target_role: str = "Reader"


class SimulateBody(BaseModel):
    changes: list[dict[str, Any]]
    name: str = ""


def _job_scope_key(body: RefreshBody) -> str:
    return _scope_key_for(body.mode, body.scope)


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
                action="iam.refresh",
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
    scope_key = _scope_key_for(mode, scope)
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
    scope_key = _scope_key_for(mode, scope)
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


@router.post("/run/{run_id}/pin")
async def pin_run(
    run_id: str,
    body: PinBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_review),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Retain a run's full rows indefinitely so it can serve as a baseline or evidence."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    if body.pinned:
        result = await store.pin_run(tenant_id, run_id, reason=body.reason)
        if result is None:
            raise HTTPException(status_code=404, detail="no such run")
    else:
        if not await store.unpin_run(tenant_id, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        result = await store.get_run(tenant_id, run_id)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject,
            action="iam.run.pin", target=run_id,
            metadata_json={"pinned": body.pinned, "reason": body.reason},
        )
    )
    await db.commit()
    return {"run": result}


@router.get("/diff")
async def access_diff(
    from_run: str = "",
    to_run: str = "",
    change_class: str = Query("", alias="class"),
    scope_id: str = "",
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """What changed between two runs, classified and attributed.

    With no run ids this serves the cached diff for the latest run. `available: false` means
    there was nothing to compare against — which is NOT the same as nothing having changed, and
    the UI must not render it as an all-clear."""
    _conn, tenant_id, _cid = _target(principal, connection_id)

    if from_run and to_run:
        before = await store.run_rows(tenant_id, from_run)
        after = await store.run_rows(tenant_id, to_run)
        if before is None or after is None:
            # Retention is one unpinned run plus whatever was pinned. Saying which side is
            # missing is the difference between "pin your baselines" and "this feature is broken".
            missing = [n for n, r in ((from_run, before), (to_run, after)) if r is None]
            return {
                "changes": [], "counts_by_class": {}, "total": 0, "truncated": False,
                "available": False,
                "note": (
                    f"Full rows are no longer retained for run(s) {', '.join(missing)}. Only the "
                    f"most recent run and pinned runs keep them — pin a run to compare against it later."
                ),
            }
        payload = diff.compute(before, after)
        payload["available"] = True
        payload["baseline_run_id"] = from_run
    else:
        payload = cache.read_drift(tenant_id)

    changes = payload.get("changes", [])
    if change_class:
        changes = [c for c in changes if c.get("class") == change_class]
    if scope_id:
        needle = scope_id.lower()
        changes = [c for c in changes if str(c.get("scope", "")).lower().startswith(needle)]
    total = len(changes)
    return {
        **payload,
        "changes": changes[offset: offset + limit],
        "filtered_total": total,
        "classes": list(diff.CHANGE_CLASSES),
    }


# --------------------------------------------------------------------------- scanners
@router.get("/scanners")
async def list_scanners(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """The scanner cards: what each one covers, and what it found last time it looked.

    **Reading never records a run.** Every card is computed with `persist=False`, so opening
    this screen cannot consume the delta — otherwise the first person to look each morning
    would turn everyone else's "3 new" into "0 new" and the feature would quietly stop
    reporting anything."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    results = await asyncio.to_thread(findings.evaluate, tenant_id)
    all_findings = [f.public() for r in results for f in r.findings]

    cards: list[dict[str, Any]] = []
    for spec in scanners.registry():
        card = scanners.run(spec, tenant_id, all_findings, results, persist=False)
        cards.append({
            **spec.public(),
            **scanners.summarise(card),
            "due": scanners.due(spec, tenant_id),
        })
    return {
        "scanners": cards,
        "severities": list(signals.SEVERITIES),
        "immediate_signal_ids": list(scanners.ALWAYS_IMMEDIATE),
    }


@router.get("/scanners/{scanner_id}/findings")
async def scanner_findings(
    scanner_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Everything one scanner reports. Served separately from the cards because shipping the
    findings inline made a nine-card response megabytes wide for data nothing rendered."""
    spec = scanners.get(scanner_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown scanner '{scanner_id}'")
    _conn, tenant_id, _cid = _target(principal, connection_id)
    results = await asyncio.to_thread(findings.evaluate, tenant_id)
    all_findings = [f.public() for r in results for f in r.findings]
    card = scanners.run(spec, tenant_id, all_findings, results, persist=False)
    ledger = scanners.read_ledger(tenant_id)
    selected = scanners.select(spec, all_findings) if not card["blocked"] else []
    for f in selected:
        entry = ledger.get(str(f.get("id"))) or {}
        f["first_seen"] = entry.get("first_seen", "")
        f["age_days"] = scanners.age_days(entry) if entry else None
    return {**spec.public(), **card, "findings": selected}


@router.post("/scanners/{scanner_id}/run")
async def run_scanner(
    scanner_id: str,
    notify: bool = True,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Run one scanner now: records the baseline and delivers the delta.

    This is the only scanner path that writes. It is a POST for exactly that reason."""
    spec = scanners.get(scanner_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown scanner '{scanner_id}'")
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return await scanner_jobs.run_scanner(tenant_id, spec, notify_enabled=notify)


@router.post("/scanners/run")
async def run_scanners(
    force: bool = False,
    notify: bool = True,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Run every scanner whose cadence has elapsed (or all of them with `force`)."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    ran = await scanner_jobs.run_due(tenant_id, force=force, notify_enabled=notify)
    return {"ran": ran, "count": len(ran)}


# --------------------------------------------------------------------------- resource access
@router.get("/resource/access-summary")
async def resource_access_summary(
    resource_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Who can reach ONE resource at all, and whether RBAC is the only way in.

    Distinct from ``/resource-access``, which answers the narrower *"who can perform this
    ACTION here"*. Both were briefly mounted on the same path with the same function name, and
    the first registration silently shadowed the other — so the action-level pivot would have
    started returning this payload with nothing failing anywhere.

    Serves the Inventory drawer. Inherited access is the substance of the answer, not a
    footnote: almost nobody is assigned at a resource — they are Owner on the subscription and
    reach it from there — so a view showing only assignments written AT the resource would
    report "nobody" for something anyone can delete.

    Runs off the event loop: it walks the whole master row set against one scope."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return await asyncio.to_thread(resource_access_mod.for_resource, tenant_id, resource_id)


@router.get("/principal/{principal_id}/timeline")
async def principal_timeline(
    principal_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Chronological access events for one principal across every retained run."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    runs_list = await store.list_runs(tenant_id, limit=30)
    events = diff.timeline_for(principal_id, runs_list)
    return {
        "principal_id": principal_id,
        "events": events,
        "runs_considered": len(runs_list),
        "limitations": [
            "Runs recorded before classified diffing was added contribute nothing to this "
            "timeline. A gap here means the history was not captured, not that nothing happened.",
        ],
    }


@router.post("/attribute")
async def attribute_drift(
    days: int = Query(30, ge=1, le=attribution.ACTIVITY_LOG_RETENTION_DAYS),
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Join the latest diff to the Activity Log: who made each change, when, from where.

    Separate from the refresh because it is per-subscription, slow, and has its own freshness.
    An unattributed change stays `unknown` — never guessed."""
    conn, tenant_id, _cid = _target(principal, connection_id)
    payload = cache.read_drift(tenant_id)
    changes = payload.get("changes", [])
    if not changes:
        return {**payload, "attribution": {}, "note": "No changes to attribute."}

    subs = sorted({
        schema.parse_scope(str(c.get("scope", ""))).get("subscriptionId", "")
        for c in changes
    } - {""})
    events, note = await attribution.collect_authorization_events(subs, conn, days=days)
    stats = attribution.attribute_all(changes, events)
    payload["changes"] = changes
    payload["attribution"] = {**stats, "days": days, "note": note}
    payload["note"] = note
    await asyncio.to_thread(cache.write_drift, tenant_id, payload)
    return payload


# --------------------------------------------------------------------------- frameworks
@router.get("/frameworks")
async def framework_mapping(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Which controls this run produced evidence for — and which it could not measure."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    results = findings.evaluate(tenant_id)
    return frameworks.map_results(results)


# --------------------------------------------------------------------------- CIEM
@router.get("/usage")
async def usage_summary(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Exercised actions per principal, with its OWN freshness.

    Usage is collected by a separate job from the access refresh, so this deliberately reports a
    different `generated_at` from `/iam/scopes`. `measured: false` means nobody has run a usage
    scan — which is not the same as nothing having been used."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    payload = cache.read_usage(tenant_id)
    meta = cache.read_usage_meta(tenant_id)
    return {
        "measured": usage.is_measured(payload),
        "generated_at": meta.get("generated_at", ""),
        "window_days": payload.get("window_days", 0),
        "source": payload.get("source", ""),
        "status": payload.get("status", ""),
        "event_count": payload.get("event_count", 0),
        "principal_count": len(payload.get("principals") or []),
        "notes": payload.get("notes") or [],
        "limitations": payload.get("limitations") or usage.LIMITATIONS,
    }


@router.post("/usage/refresh")
async def refresh_usage(
    days: int = Query(usage.DEFAULT_WINDOW_DAYS, ge=1, le=usage.MAX_WINDOW_DAYS),
    connection_id: str | None = None,
    principal: Principal = Depends(require_write),
) -> dict[str, Any]:
    """Run the usage sweep. Slow — per subscription over the Activity Log."""
    from app.iam import orchestrator

    conn, tenant_id, _cid = _target(principal, connection_id)
    written = await orchestrator.refresh_usage(tenant_id, conn, days=days)
    return {"ok": True, "usage": written}


@router.get("/rightsizing")
async def rightsizing(
    connection_id: str | None = None,
    force: bool = False,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Granted vs used, with a narrower proposal where one is defensible.

    Every figure carries its denominator, its window and its confidence. Break-glass principals
    are reported but never get a removal recommendation, and data-plane roles are excluded
    entirely while data-plane logging is unavailable.

    Served from the version-stamped cache. This used to recompute on EVERY request — 7.3 seconds
    per page load on a realistic tenant, cold and warm alike, while the refresh path was already
    writing a perfectly good copy to disk that nothing read.

    Runs OFF the event loop. It is pure CPU over a large role catalogue, and the first version
    took 40 seconds inline — which did not merely make this endpoint slow, it stalled every other
    request in the process until SQLite began reporting "database is locked" on unrelated session
    writes. Even at two seconds a synchronous CPU burn has no business in an async handler."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return await cpu.run(rightsize.analyse_for_tenant, tenant_id, force=force, label="right-sizing")


# --------------------------------------------------------------------------- simulator
@router.post("/simulate")
async def simulate(
    body: SimulateBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_simulate),
) -> dict[str, Any]:
    """Model a set of access changes over the cached snapshot. No Azure call in this path.

    An unknown change kind or a malformed change is a **400** — never silently skipped, because
    an ignored change produces a reassuring "nothing happens" result from a typo. A change whose
    referent has since been deleted is a **409**: the request was valid when it was written and
    the world moved."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)
    directory = await asyncio.to_thread(cache.read_directory, tenant_id)
    usage_meta = cache.read_usage_meta(tenant_id)

    age = None
    if usage_meta.get("generated_at"):
        try:
            stamp = _dt.datetime.fromisoformat(str(usage_meta["generated_at"]).replace("Z", "+00:00"))
            age = (_dt.datetime.now(_dt.timezone.utc) - stamp).days
        except ValueError:
            age = None

    try:
        return simulator.simulate(
            rows, body.changes,
            role_index=effective.build_role_index(directory.get("role_defs", [])),
            owned_scopes=_owned_scopes(tenant_id),
            usage_age_days=age,
        )
    except simulator.InvalidChange as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except simulator.MissingReferent as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _owned_scopes(tenant_id: str) -> set[str]:
    """Scopes with a recorded owner, for the orphaned-resources cross-reference.

    Best-effort: ownership is an optional feature, and a missing registry must not turn a
    simulation into an error."""
    try:
        from app.ownership import registry as ownership_registry

        return {
            str(e.get("resource_id") or e.get("scope") or "")
            for e in ownership_registry.list_assignments(tenant_id)  # type: ignore[attr-defined]
        } - {""}
    except Exception:  # noqa: BLE001
        return set()


@router.get("/simulate/kinds")
async def simulate_kinds(
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return {"kinds": list(simulator.CHANGE_KINDS), "seed": simulator.SEED}


# --------------------------------------------------------------------------- review campaigns
@router.get("/campaigns")
async def list_campaigns(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return {
        "campaigns": await campaigns.list_campaigns(tenant_id),
        "strategies": list(campaigns.STRATEGIES),
        "decisions": list(campaigns.DECISIONS),
    }


@router.post("/campaigns")
async def create_campaign(
    body: CampaignBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_review),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a certification campaign from a selector over the current snapshot."""
    _conn, tenant_id, cid = _target(principal, connection_id)
    due = None
    if body.due_at:
        try:
            due = _dt.datetime.fromisoformat(body.due_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"unparseable due_at: {body.due_at}") from exc

    finding_rows = (await findings.list_findings(tenant_id, limit=500)).get("findings", [])
    try:
        campaign = await campaigns.create(
            tenant_id,
            name=body.name,
            selector=body.selector,
            description=body.description,
            baseline_run_id=body.baseline_run_id,
            reviewer_strategy=body.reviewer_strategy,
            reviewer_fallback_id=body.reviewer_fallback_id,
            due_at=due,
            reminder_days=body.reminder_days,
            connection_id=cid,
            created_by=principal.subject,
            findings=finding_rows,
        )
    except campaigns.CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject,
            action="iam.campaign.create", target=campaign["id"],
            metadata_json={"name": campaign["name"], "selector": body.selector, "items": campaign["stats"].get("total", 0)},
        )
    )
    await db.commit()
    return {"campaign": campaign}


@router.get("/campaigns/{campaign_id}")
async def campaign_detail(
    campaign_id: str,
    reviewer_id: str = "",
    undecided_only: bool = False,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    _conn, tenant_id, _cid = _target(principal, connection_id)
    campaign = await campaigns.get_campaign(tenant_id, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="no such campaign")
    items = await campaigns.list_items(
        tenant_id, campaign_id, reviewer_id=reviewer_id, undecided_only=undecided_only
    )
    return {"campaign": campaign, "items": items}


@router.post("/campaigns/{campaign_id}/activate")
async def activate_campaign(
    campaign_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_review),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _conn, tenant_id, _cid = _target(principal, connection_id)
    try:
        campaign = await campaigns.activate(tenant_id, campaign_id)
    except campaigns.CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject,
                    action="iam.campaign.activate", target=campaign_id, metadata_json={}))
    await db.commit()
    return {"campaign": campaign}


@router.post("/campaigns/{campaign_id}/items/{item_id}/decide")
async def decide_item(
    campaign_id: str,
    item_id: str,
    body: DecisionBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_review),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record one certification decision. Nothing here writes to Azure."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    try:
        result = await campaigns.decide(
            tenant_id, campaign_id, item_id,
            decision=body.decision, reason=body.reason,
            decided_by=principal.subject, delegated_to=body.delegated_to,
        )
    except campaigns.CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject,
                    action="iam.campaign.decide", target=f"{campaign_id}/{item_id}",
                    metadata_json={"decision": body.decision, "reason": body.reason}))
    await db.commit()
    return result


@router.post("/campaigns/{campaign_id}/refresh")
async def refresh_campaign(
    campaign_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_review),
) -> dict[str, Any]:
    """Re-check every item against the current snapshot and re-present what moved."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    try:
        changed = await campaigns.refresh_against(tenant_id, campaign_id)
        confirmed = await campaigns.auto_confirm_applied(tenant_id, campaign_id)
    except campaigns.CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**changed, **confirmed}


@router.post("/campaigns/{campaign_id}/complete")
async def complete_campaign(
    campaign_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_review),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Close a campaign. Undecided items are NOT approved — they are counted and reported."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    try:
        campaign = await campaigns.complete(tenant_id, campaign_id)
    except campaigns.CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject,
                    action="iam.campaign.complete", target=campaign_id,
                    metadata_json=campaign.get("stats", {})))
    await db.commit()
    return {"campaign": campaign}


@router.post("/campaigns/{campaign_id}/remediation")
async def campaign_remediation(
    campaign_id: str,
    body: RemediationBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_review),
) -> dict[str, Any]:
    """Generate the ordered remediation script for this campaign's decisions.

    Generated on demand and never stored — a saved script goes stale against a moving estate and
    the assignment id it references may already belong to something else."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    if body.format not in remediation.FORMATS:
        raise HTTPException(status_code=400, detail=f"format must be one of {remediation.FORMATS}")
    campaign = await campaigns.get_campaign(tenant_id, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="no such campaign")

    decided = await campaigns.decided_rows(tenant_id, campaign_id)
    actions = []
    for entry in decided:
        action = remediation.for_decision(
            entry["row"], entry["decision"], body.format, target_role=body.target_role
        )
        if action:
            actions.append(action)
    if not actions:
        return {"bundle": None, "note": "No revoke or reduce decisions have been recorded yet."}

    try:
        bundle = remediation.build_bundle(
            actions, body.format, title=f"Access review: {campaign['name']}",
            run_id=campaign.get("baseline_run_id", ""), campaign_id=campaign_id,
        )
    except remediation.SecretLeak as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await campaigns.mark_remediation(tenant_id, campaign_id, [e["item_id"] for e in decided], "generated")
    return {"bundle": bundle}


@router.post("/campaigns/{campaign_id}/evidence")
async def campaign_evidence(
    campaign_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_review),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Write the immutable, hashed evidence snapshot for a completed campaign."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    campaign = await campaigns.get_campaign(tenant_id, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="no such campaign")
    if campaign["status"] not in (campaigns.COMPLETED, campaigns.EXPIRED):
        raise HTTPException(
            status_code=400,
            detail="complete the campaign first — an evidence pack for a running review is a moving target",
        )

    items = await campaigns.list_items(tenant_id, campaign_id, limit=campaigns.MAX_ITEMS)
    run = await store.get_run(tenant_id, campaign["baseline_run_id"]) if campaign["baseline_run_id"] else None
    content = campaigns.evidence_content(
        campaign, items, run=run, framework_map=frameworks.map_results(findings.evaluate(tenant_id))
    )
    from app.evidence import registry as evidence_registry

    snapshot = evidence_registry.create_snapshot(
        tenant_id=tenant_id,
        name=f"IAM access review — {campaign['name']}",
        scope={"kind": "iam_campaign", "campaign_id": campaign_id},
        included=["findings", "activity"],
        retention_class="audit",
        tags=["iam", "access-review", "certification"],
        content=content,
        created_by=principal.subject,
    )
    db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject,
                    action="iam.campaign.evidence", target=campaign_id,
                    metadata_json={"evidence_id": snapshot.get("id", ""), "sha256": snapshot.get("sha256", "")}))
    await db.commit()
    return {"evidence": snapshot, "digest": campaigns.content_digest(content)}


# --------------------------------------------------------------------------- disabled access
# The filter vocabulary lives in `app.iam.leavers`, not here, because three callers need it and
# only one of them is an HTTP endpoint: the report, the export, and the review-campaign
# selector. When it lived beside the endpoints, the campaign selector understood two of the
# sixteen filters and silently created a review 26 times larger than the screen it came from.
ON_PREM_ANY = leavers.ON_PREM_ANY
ON_PREM_CLOUD = leavers.ON_PREM_CLOUD
ON_PREM_SYNCED = leavers.ON_PREM_SYNCED
ON_PREM_UNKNOWN = leavers.ON_PREM_UNKNOWN
SIGNIN_KINDS = leavers.SIGNIN_KINDS

_signin_at = leavers.signin_at


def _apply_leavers_filters(identities: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    """Thin wrapper over :func:`leavers.filter_identities`, kept for call-site readability."""
    return leavers.filter_identities(identities, kw)


def _leavers_counts(identities: list[dict[str, Any]], signin_kind: str = "any") -> dict[str, Any]:
    return leavers.count_identities(identities, signin_kind)


class LeaversQuery(BaseModel):
    """Every filter this screen can apply, in ONE object.

    A single model rather than a dozen loose parameters because the report endpoint and the
    export endpoint must accept exactly the same set — the moment one of them understands fewer
    filters, a download silently contains rows the screen was not showing."""

    tier: str | None = None
    principal_type: str | None = None
    privileged_only: bool = False
    on_prem_synced: bool = False          # legacy boolean; `on_prem` supersedes it
    on_prem: str | None = None            # "" | cloud | onprem | unknown
    via_group_only: bool = False
    soft_deleted: bool = False
    has_owned_sp: bool = False
    pim_eligible: bool = False
    never_used: bool = False
    dormancy: str | None = None
    signin_kind: str = "any"
    subscription: str | None = None
    role: str | None = None
    plane: str | None = None
    group: str | None = None
    search: str | None = None
    #: Explicit selection from the screen. An ADDITIONAL constraint on the filters, never a
    #: replacement, so a stale id cannot resurrect an identity a later scan has excluded.
    principal_ids: list[str] | None = None


def _leavers_query(**kw: Any) -> LeaversQuery:
    return LeaversQuery(**kw)


def _leavers_filter_summary(q: LeaversQuery) -> dict[str, Any]:
    """The applied filters, for the workbook's Summary sheet. A download with no record of what
    was filtered out cannot be audited."""
    return {k: v for k, v in q.model_dump().items() if v not in (None, "", False, "any")}


@router.get("/leavers")
async def leavers_report(
    tier: str | None = None,
    principal_type: str | None = None,
    privileged_only: bool = False,
    on_prem_synced: bool = False,
    on_prem: str | None = None,
    via_group_only: bool = False,
    soft_deleted: bool = False,
    has_owned_sp: bool = False,
    pim_eligible: bool = False,
    never_used: bool = False,
    dormancy: str | None = None,
    signin_kind: str = "any",
    subscription: str | None = None,
    role: str | None = None,
    plane: str | None = None,
    group: str | None = None,
    search: str | None = None,
    principal_ids: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Disabled accounts that still hold access, rolled up per person.

    Person-centric on purpose — every other lens in this feature is one row per grant, and a
    leaver with Contributor on four subscriptions is one offboarding task, not four findings.

    ``measured`` is the gate the UI must render on: false means account state has never been
    collected for this tenant, and an empty ``identities`` list then means "we have not looked",
    not "nobody". Those are opposite findings.

    ``counts`` is computed over the whole FILTERED set, server-side, so a group header can
    never disagree with the section under it."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    q = _leavers_query(
        tier=tier, principal_type=principal_type, privileged_only=privileged_only,
        on_prem_synced=on_prem_synced, on_prem=on_prem, via_group_only=via_group_only,
        soft_deleted=soft_deleted, has_owned_sp=has_owned_sp, pim_eligible=pim_eligible,
        never_used=never_used, dormancy=dormancy, signin_kind=signin_kind,
        subscription=subscription, role=role, plane=plane, group=group, search=search,
        principal_ids=[p for p in (principal_ids or "").split(",") if p.strip()] or None,
    )
    report = await cpu.run(leavers.build_leavers, tenant_id, label="disabled access report")
    filtered = _apply_leavers_filters(report.get("identities") or [], **q.model_dump())
    out = dict(report)
    out["identities"] = filtered
    out["counts"] = _leavers_counts(filtered, q.signin_kind)
    # The unfiltered population, so the screen can say "12 of 34" rather than implying the
    # filter found everything there is.
    out["total_identities"] = len(report.get("identities") or [])
    out["filtered"] = len(filtered) != out["total_identities"]
    # Every value the filter dropdowns can offer, derived from the UNFILTERED set so choosing
    # one option never empties the others out of existence.
    everything = report.get("identities") or []
    out["facets"] = {
        "subscriptions": sorted({s for i in everything for s in (i.get("subscriptions") or [])}),
        "roles": sorted({str(i.get("highestRole") or "") for i in everything} - {""}),
        "planes": sorted({p for i in everything for p in (i.get("planes") or [])}),
        "groups": sorted({g for i in everything for g in (i.get("groupsGrantingAccess") or [])}),
        "signin_kinds": list(SIGNIN_KINDS),
    }
    return out


@router.get("/leavers/export")
async def leavers_export(
    fmt: str = Query("csv", pattern="^(csv|xlsx)$"),
    shape: str = Query("identities", pattern="^(identities|grants)$"),
    tier: str | None = None,
    principal_type: str | None = None,
    privileged_only: bool = False,
    on_prem_synced: bool = False,
    on_prem: str | None = None,
    via_group_only: bool = False,
    soft_deleted: bool = False,
    has_owned_sp: bool = False,
    pim_eligible: bool = False,
    never_used: bool = False,
    dormancy: str | None = None,
    signin_kind: str = "any",
    subscription: str | None = None,
    role: str | None = None,
    plane: str | None = None,
    group: str | None = None,
    search: str | None = None,
    principal_ids: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> Response:
    """Download the disabled-access report.

    ``shape=identities`` is one row per person — "who do I go and offboard". ``shape=grants`` is
    one row per assignment in the full schema — "what exactly do I delete", and it round-trips
    through the same writers as the main access export so a remediation script can be built from
    it without a second collection.

    Both shapes go through :func:`_apply_leavers_filters`, so the file always contains exactly
    what the screen showed."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    q = _leavers_query(
        tier=tier, principal_type=principal_type, privileged_only=privileged_only,
        on_prem_synced=on_prem_synced, on_prem=on_prem, via_group_only=via_group_only,
        soft_deleted=soft_deleted, has_owned_sp=has_owned_sp, pim_eligible=pim_eligible,
        never_used=never_used, dormancy=dormancy, signin_kind=signin_kind,
        subscription=subscription, role=role, plane=plane, group=group, search=search,
        principal_ids=[p for p in (principal_ids or "").split(",") if p.strip()] or None,
    )
    report = await cpu.run(leavers.build_leavers, tenant_id, label="disabled access export")
    filtered = _apply_leavers_filters(report.get("identities") or [], **q.model_dump())
    keep = {str(i.get("principalId", "")).lower() for i in filtered}
    rows = await cpu.run(compose.build_master_rows, tenant_id, label="disabled access rows")
    grants = [
        r for r in leavers.disabled_grant_rows(rows)
        if str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower() in keep
    ]
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

    if fmt == "xlsx":
        payload = dict(report)
        payload["identities"] = filtered
        applied = _leavers_filter_summary(q)
        # Evaluate every argument BEFORE handing the call to the worker: a threaded call whose
        # arguments are computed in the argument list runs those arguments on the event loop.
        body = await cpu.run(
            export.to_disabled_workbook,
            report=payload, grants=grants, tenant_id=tenant_id, filters=applied,
            label="disabled access workbook",
        )
        return Response(
            content=body,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=iam-disabled-access-{stamp}.xlsx"},
        )

    if shape == "grants":
        body_csv = await cpu.run(export.to_csv, grants, label="disabled access grants csv")
        return Response(
            content=body_csv,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=iam-disabled-access-grants-{stamp}.csv"},
        )
    tiers = report.get("tiers") or {}
    body_csv = await cpu.run(export.to_identity_csv, filtered, tiers, label="disabled access csv")
    return Response(
        content=body_csv,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=iam-disabled-access-identities-{stamp}.csv"},
    )


@router.get("/leavers/remediation")
async def leavers_remediation(
    fmt: str = Query("az"),
    tier: str | None = None,
    principal_type: str | None = None,
    privileged_only: bool = False,
    on_prem_synced: bool = False,
    on_prem: str | None = None,
    via_group_only: bool = False,
    soft_deleted: bool = False,
    has_owned_sp: bool = False,
    pim_eligible: bool = False,
    never_used: bool = False,
    dormancy: str | None = None,
    signin_kind: str = "any",
    subscription: str | None = None,
    role: str | None = None,
    plane: str | None = None,
    group: str | None = None,
    search: str | None = None,
    principal_ids: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """The ordered revocation script for the identities currently selected.

    Read-only, and it does NOT create a campaign. "What would it cost me to clean these three
    up" is a question people ask before committing to a review, and forcing a campaign first
    made it unanswerable.

    Nothing here writes to Azure. The script is text for the operator to read and run through
    their own change process, and every step carries a dry run, a ``breaks if`` and a rollback.

    Ordering is not cosmetic: group-derived access is revoked BEFORE direct assignments, because
    revoking a direct grant while the principal still inherits the same access through a group
    looks successful and changes nothing — which is how "we revoked it" and "they still have it"
    end up both being true."""
    if fmt not in remediation.FORMATS:
        raise HTTPException(status_code=400, detail=f"format must be one of {remediation.FORMATS}")
    _conn, tenant_id, _cid = _target(principal, connection_id)
    q = _leavers_query(
        tier=tier, principal_type=principal_type, privileged_only=privileged_only,
        on_prem_synced=on_prem_synced, on_prem=on_prem, via_group_only=via_group_only,
        soft_deleted=soft_deleted, has_owned_sp=has_owned_sp, pim_eligible=pim_eligible,
        never_used=never_used, dormancy=dormancy, signin_kind=signin_kind,
        subscription=subscription, role=role, plane=plane, group=group, search=search,
        principal_ids=[p for p in (principal_ids or "").split(",") if p.strip()] or None,
    )

    def _build() -> dict[str, Any]:
        report = leavers.build_leavers(tenant_id)
        if not report.get("measured"):
            return {
                "measured": False,
                "reason": report.get("reason", ""),
                "script": "",
                "action_count": 0,
                "identities": 0,
            }
        picked = leavers.filter_identities(report.get("identities") or [], q.model_dump())
        keep = {str(i.get("principalId", "")).lower() for i in picked}
        rows = [
            r for r in leavers.disabled_grant_rows(compose.build_master_rows(tenant_id))
            if str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower() in keep
        ]
        actions = [a for a in (remediation.revoke_assignment(r, fmt) for r in rows) if a]
        bundle = remediation.build_bundle(
            actions,
            fmt,
            title=f"Remove access held by {len(keep)} disabled account(s)",
        )
        bundle["measured"] = True
        bundle["identities"] = len(keep)
        bundle["grants"] = len(rows)
        # How many steps land on each API. The action count alone is misleading once duplicate
        # group memberships are folded — 527 grants collapse to far fewer steps — and an operator
        # needs to know up front that some steps need Graph rather than ARM.
        planes: dict[str, int] = {}
        for a in bundle["actions"]:
            key = str(a.get("plane") or remediation.PLANE_AZURE_RBAC)
            planes[key] = planes.get(key, 0) + 1
        bundle["planes"] = planes
        # The same caveats the screen carries. A script pasted into a change record outlives
        # every banner that was on screen when it was generated.
        bundle["limitations"] = report.get("limitations") or []
        return bundle

    try:
        return await cpu.run(_build, label="disabled access remediation")
    except remediation.SecretLeak as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# --------------------------------------------------------------------------- export
@router.get("/export")
async def export_rows(
    fmt: str = Query("csv", pattern="^(csv|json|scanner)$"),
    tab: str = "all",
    scope_id: str | None = None,
    subscription_ids: str | None = None,
    workload_id: str | None = None,
    scope: str | None = None,
    surface: str | None = None,
    principal_type: str | None = None,
    privileged_only: bool = False,
    disabled_only: bool = False,
    search: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> Response:
    """Download the access rows for a tab.

    ``csv`` / ``json`` carry every column this product knows; ``scanner`` projects back down to
    the frozen 46 the standalone all-azure-access scanner emits, so a round trip through
    ``POST /iam/import`` is byte-identical.

    Takes the SAME filter parameters as ``GET /access`` and applies them through the same
    function. It previously understood only the scope/workload narrowing, so a download taken
    with a search term or the privileged toggle active silently contained every other row too —
    and this is the artifact that gets attached to an access review."""
    connection, tenant_id, _cid = _target(principal, connection_id, workload_id)
    rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)
    if scope_id or subscription_ids or workload_id:
        sub_id_list = [s for s in (subscription_ids or "").split(",") if s.strip()]
        rows = await scope_filters.filter_rows(
            rows,
            scope_id=scope_id or "",
            subscription_ids=sub_id_list,
            workload_id=workload_id or "",
            connection=connection,
        )
    rows = _apply_grid_filters(
        rows,
        tab=tab,
        scope=scope,
        surface=surface,
        principal_type=principal_type,
        privileged_only=privileged_only,
        disabled_only=disabled_only,
        search=search,
    )
    if fmt == "scanner":
        body = export.to_json(rows, columns=schema.SCANNER_COLUMNS)
        return Response(content=body, media_type="application/json", headers={"Content-Disposition": "attachment; filename=allAzureAccess.json"})
    if fmt == "json":
        return Response(content=export.to_json(rows), media_type="application/json", headers={"Content-Disposition": f"attachment; filename=iam-access-{tab}.json"})
    return Response(content=export.to_csv(rows), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=iam-access-{tab}.csv"})


@router.get("/export/workbook")
async def export_workbook(
    scope_id: str | None = None,
    subscription_ids: str | None = None,
    workload_id: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> Response:
    """Download the full multi-sheet Excel workbook.

    Carries the access lenses AND the analysis: the posture score, every finding (plus the
    checks that could not run), the scanner cards, right-sizing, the shadow-access sweep, the
    escalation paths with their blind spots, and the data-plane coverage statement. A workbook
    with only the role-assignment grid hands somebody a list and calls it an access review.

    Honors the active scope/workload filter for the access sheets. The analysis is tenant-wide
    by construction, so it is NOT filtered — a finding about a scope you filtered out is still
    true, and silently dropping it would make the export read cleaner than the tenant is."""
    connection, tenant_id, _cid = _target(principal, connection_id, workload_id)
    rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)
    if scope_id or subscription_ids or workload_id:
        sub_id_list = [s for s in (subscription_ids or "").split(",") if s.strip()]
        rows = await scope_filters.filter_rows(
            rows,
            scope_id=scope_id or "",
            subscription_ids=sub_id_list,
            workload_id=workload_id or "",
            connection=connection,
        )
    overview = await asyncio.to_thread(compose.compute_overview, tenant_id)
    pivots_data = await asyncio.to_thread(pivots.compute_pivots, rows)
    directory = await asyncio.to_thread(cache.read_directory, tenant_id)

    # Every one of these is served from a version-stamped cache, so gathering them costs
    # milliseconds rather than re-running the engines.
    results = await asyncio.to_thread(findings.evaluate, tenant_id)
    findings_payload = await findings.list_findings(tenant_id, cap=None)
    all_findings = [f.public() for r in results for f in r.findings]
    scanner_cards = await asyncio.to_thread(
        lambda: [
            {**spec.public(), **scanners.summarise(
                scanners.run(spec, tenant_id, all_findings, results, persist=False)
            ), "due": scanners.due(spec, tenant_id)}
            for spec in scanners.registry()
        ]
    )

    # Everything the workbook needs is built INSIDE the thread. Passing `rightsize.analyse(...)`
    # or `escalation.graph_for_tenant(...)` as an ARGUMENT to `to_thread` evaluates it on the
    # event loop first — the call is threaded, its arguments are not — so the two most expensive
    # computations in the export were running exactly where they must not.
    def _build_workbook() -> bytes:
        return export.to_workbook(
            rows=rows,
            overview=overview,
            pivots=pivots_data,
            pivot_labels=pivots.PIVOT_LABELS,
            directory=directory,
            findings=findings_payload,
            rightsizing=rightsize.analyse_for_tenant(tenant_id),
            bypass=cache.read_bypass(tenant_id),
            escalation=escalation.graph_for_tenant(
                tenant_id, compose.build_master_rows(tenant_id),
                effective.build_role_index(directory.get("role_defs", [])),
                identities=directory.get("identities", {}),
                federated=directory.get("federated", []),
            ),
            scanners=scanner_cards,
            score=score_mod.compute(results),
            dataplane=dataplane.public_catalogue(),
            leavers=leavers.build_leavers(tenant_id),
        )

    content = await cpu.run(_build_workbook, label="workbook export")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=iam-access-review.xlsx"},
    )


# --------------------------------------------------------------------------- effective access
def _effective_context_sync(tenant_id: str):
    rows = compose.build_master_rows(tenant_id)
    directory = cache.read_directory(tenant_id)
    return rows, effective.build_role_index(directory.get("role_defs", []))


async def _effective_context(tenant_id: str):
    """(composed rows, role index) for a tenant, built off the event loop.

    Both come from cache, so an evaluation never issues an Azure call — the answer is only as
    current as the last refresh, which the caller surfaces alongside it. "From cache" still
    means gunzipping every scope sidecar and indexing every role definition whenever a write has
    invalidated the memo, which is precisely what happens throughout a refresh."""
    return await asyncio.to_thread(_effective_context_sync, tenant_id)


@router.get("/effective")
async def effective_access(
    principal_id: str,
    scope: str,
    action: str = "",
    plane: str = "",
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Can this principal perform this action on this scope, and why?

    Omit ``action`` for the role-level grant set instead of a single verdict. The verdict is one
    of allowed / denied / not_granted / **indeterminate** — the last is returned whenever an
    unevaluated ABAC condition or an unresolved role definition sits in the path, because a
    confident yes that turns out to be conditional is worse than admitting the uncertainty."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    rows, role_index = await _effective_context(tenant_id)
    if not action:
        out = effective.effective_actions(rows, role_index, principal_id=principal_id, scope=scope)
        out["generated_at"] = (await asyncio.to_thread(compose.compute_overview, tenant_id)).get("generated_at", "")
        return out
    if plane and plane not in (effective.PLANE_CONTROL, effective.PLANE_DATA):
        raise HTTPException(status_code=400, detail="plane must be 'control' or 'data'")
    dec = effective.evaluate(
        rows, role_index, principal_id=principal_id, scope=scope, action=action, plane=plane
    )
    return dec.public()


@router.get("/principal/{principal_id}/access")
async def principal_access(
    principal_id: str,
    scope: str = "/",
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Principal 360: everything this principal can reach, as roles rather than expanded actions.

    Role-level on purpose — a tenant-wide action expansion is tens of thousands of strings that
    nobody reads, and the per-action question is what ``/iam/effective`` is for."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    rows, role_index = await _effective_context(tenant_id)
    return effective.effective_actions(rows, role_index, principal_id=principal_id, scope=scope)


@router.get("/resource-access")
async def resource_access(
    scope: str,
    action: str,
    plane: str = "",
    limit: int = Query(200, ge=1, le=500),
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """The inverse pivot: everyone who can perform ``action`` on ``scope``.

    Each candidate goes through the same evaluator rather than a "who holds a matching role"
    query, so a principal blocked by a deny assignment does not appear in the allowed list."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    rows, role_index = await _effective_context(tenant_id)
    return await asyncio.to_thread(
        effective.who_can, rows, role_index, scope=scope, action=action, plane=plane, limit=limit
    )


@router.get("/escalation")
async def escalation_graph(
    scope_id: str = "",
    principal_id: str = "",
    min_confidence: str = "low",
    force: bool = False,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """*"Alice is not an Owner. Can Alice become one?"* — as a directed graph.

    Always publishes ``limitations``: an escalation map that could not see managed identities
    reporting no paths is the most dangerous false negative in this product, because it reads as
    an all-clear on exactly the thing the reader came to check.

    ``force=true`` discards the cached graph and rebuilds it. That is a ~40-second operation on
    a realistic tenant, so it is opt-in: the cache is invalidated automatically whenever the
    underlying rows change, and force exists for the case where somebody wants to be certain
    rather than for routine use.

    Runs off the event loop — a 40-second CPU burn in an async handler stalls every other
    request in the process, which this product has already learned the hard way."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    if min_confidence not in (escalation.CONF_LOW, escalation.CONF_MEDIUM, escalation.CONF_HIGH):
        raise HTTPException(status_code=400, detail="min_confidence must be low, medium or high")
    rows, role_index = await _effective_context(tenant_id)
    directory = await asyncio.to_thread(cache.read_directory, tenant_id)
    if not principal_id and not scope_id:
        # The unfiltered graph is the expensive one and is shared with /findings and /score.
        return await cpu.run(
            escalation.graph_for_tenant,
            tenant_id, rows, role_index,
            identities=directory.get("identities", {}),
            federated=directory.get("federated", []),
            min_confidence=min_confidence,
            force=force,
            label="escalation graph",
        )
    return await cpu.run(
        escalation.detect,
        rows, role_index,
        identities=directory.get("identities", {}),
        federated=directory.get("federated", []),
        min_confidence=min_confidence,
        principal_id=principal_id,
        scope_filter=scope_id,
        label="escalation detect",
    )


# --------------------------------------------------------------------------- derived caches
@router.get("/cache")
async def cache_status(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """What is cached, how old it is, and how long it took to build.

    Published because the alternative to knowing is guessing: a screen that renders in 300 ms
    when the same screen took 40 seconds an hour ago invites the reasonable suspicion that it is
    showing something stale. Every entry carries the source-data version it was built from, so
    "current" is a fact rather than a claim."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    version = cache.cache_version()
    esc = cache.read_escalation_meta(tenant_id)
    usage_meta = cache.read_usage_meta(tenant_id)
    typical, basis = progress.estimate(tenant_id, "all")
    return {
        "source_version": version,
        "entries": [
            {
                "key": "escalation",
                "label": "Escalation graph",
                "generated_at": esc.get("generated_at", ""),
                "built_from_version": esc.get("cache_version"),
                "current": esc.get("cache_version") == version,
                "duration_seconds": esc.get("duration_seconds"),
                "size": {"nodes": esc.get("nodes"), "edges": esc.get("edges"),
                         "paths": esc.get("paths")},
            },
            {
                "key": "usage",
                "label": "Activity-log usage",
                "generated_at": usage_meta.get("generated_at", ""),
                # Usage is deliberately allowed to be older than the access snapshot; it has its
                # own collection job, and pretending otherwise would imply one freshness for two
                # very different things.
                "built_from_version": None,
                "current": bool(usage_meta),
                "duration_seconds": None,
                "size": {"events": usage_meta.get("event_count"),
                         "principals": usage_meta.get("principal_count")},
            },
        ],
        "refresh_estimate": {
            "typical_seconds": round(typical, 1) if typical is not None else None,
            "basis": basis,
        },
    }


@router.post("/cache/rebuild")
async def rebuild_cache(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Rebuild the derived caches from the rows already collected. No Azure call.

    Distinct from a refresh, and the distinction matters: a refresh re-reads Azure and takes
    minutes, while this recomputes from the snapshot already on disk. Somebody who suspects a
    stale *derived* result wants this one."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    rows, role_index = await _effective_context(tenant_id)
    directory = await asyncio.to_thread(cache.read_directory, tenant_id)
    started = _time.monotonic()
    graph = await cpu.run(
        escalation.graph_for_tenant,
        tenant_id, rows, role_index,
        identities=directory.get("identities", {}),
        federated=directory.get("federated", []),
        force=True,
        label="escalation graph (forced rebuild)",
    )
    return {
        "ok": True,
        "rebuilt": ["escalation"],
        "duration_seconds": round(_time.monotonic() - started, 1),
        "size": {"nodes": len(graph.get("nodes") or []), "edges": len(graph.get("edges") or []),
                 "paths": len(graph.get("paths") or [])},
    }


@router.get("/identities")
async def managed_identities(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """The managed-identity inventory, joined to what each identity holds.

    Answers "which resource IS this GUID service principal?" — currently unanswerable in any
    Azure-native view and the most common complaint about every RBAC report."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    directory = await asyncio.to_thread(cache.read_directory, tenant_id)
    identities = directory.get("identities", {})
    rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)

    grants: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        pid = str(r.get("effectivePrincipalId", "") or r.get("principalId", "")).lower()
        if pid in identities:
            grants.setdefault(pid, []).append(r)

    fic_by_identity: dict[str, list[dict[str, Any]]] = {}
    for f in directory.get("federated", []):
        fic_by_identity.setdefault(str(f.get("identityResourceId", "")).lower(), []).append(f)

    out = []
    for pid, ident in identities.items():
        held = grants.get(pid, [])
        out.append({
            **ident,
            "roles": sorted({str(r.get("roleName", "")) for r in held}),
            "privileged": any(r.get("roleIsPrivileged") for r in held),
            "assignmentCount": len(held),
            "federatedCredentials": fic_by_identity.get(
                str(ident.get("identityResourceId", "")).lower(), []
            ),
        })
    out.sort(key=lambda i: (not i["privileged"], str(i.get("identityName", ""))))
    return {"identities": out, "total": len(out), "federated_total": len(directory.get("federated", []))}


@router.get("/bypass")
async def bypass_surface(
    family: str = "",
    severity: str = "",
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Shadow access: the doors that are not RBAC.

    Reports **the door, not the room** — `listClusterAdminCredential` is an Azure control-plane
    action and is in scope; the ClusterRoleBindings behind it are not. That distinction is in
    `limitations` on every response, because a reader must never infer from this that a cluster's
    internal authorization has been assessed."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    payload = cache.read_bypass(tenant_id)
    meta = cache.read_bypass_meta(tenant_id)
    rows = payload["rows"]
    if family:
        rows = [r for r in rows if r["family"] == family]
    if severity:
        rows = [r for r in rows if r["severity"] == severity]
    rows.sort(key=lambda r: (schema.SEVERITY_RANK.get(r["severity"], 3), r["resourceName"]))
    return {
        "rows": rows,
        "summary": payload["summary"],
        "generated_at": meta.get("generated_at", ""),
        "status": meta.get("status", ""),
        "collectors": meta.get("collectors", []),
        "never_loaded": not meta,
        "families": bypass.FAMILIES,
    }


# --------------------------------------------------------------------------- findings
@router.get("/findings")
async def list_findings(
    severity: str = "",
    pillar: str = "",
    signal_id: str = "",
    framework: str = "",
    state: str = "",
    include_suppressed: bool = False,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Findings from the signal registry, with human state overlaid.

    Served from its own endpoint rather than embedded in the refresh/run responses: embedding
    `findings[:200]` per scanner made the equivalent Entra payload 1.3 MB where serving it here
    keeps it at ~10 KB.

    **Viewing never records anything.** If it did, everything would be marked seen and the next
    real run would report "nothing changed" because somebody looked."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return await findings.list_findings(
        tenant_id,
        severity=severity, pillar=pillar, signal_id=signal_id, framework=framework,
        state=state, include_suppressed=include_suppressed, limit=limit, offset=offset,
    )


@router.get("/score")
async def posture_score(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Posture score with its coverage.

    The score is never returned without coverage, and the grade is withheld below the coverage
    floor — a letter derived from a third of the checks would be quoted without the caveat."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return findings.compute_score(tenant_id)


@router.get("/dataplane")
async def data_plane_catalogue(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Which services hold data, which doors reach it, and which of them we cannot read.

    Published so the answer to "does this cover my estate?" is inspectable rather than implied.
    ``rbac_is_complete: false`` means Azure role assignments do NOT describe access to that
    service — its grants live in a system that needs a data-plane credential an ARM/Graph
    connection does not have — and every such entry carries the reason."""
    catalogue = dataplane.public_catalogue()
    return {
        "services": catalogue,
        "readable": [s for s in catalogue if s["rbac_is_complete"]],
        "not_readable": [s for s in catalogue if not s["rbac_is_complete"]],
        "tiers": [
            {"key": dataplane.TIER_CREDENTIAL,
             "label": "Reaches a credential",
             "desc": "Reading a secret, key or certificate grants the identity it authenticates."},
            {"key": dataplane.TIER_WRITE, "label": "Can modify or destroy data",
             "desc": "Reaches resource CONTENTS, which no resource lock protects."},
            {"key": dataplane.TIER_READ, "label": "Can read data", "desc": "Discloses the data itself."},
            {"key": dataplane.TIER_META, "label": "Metadata only",
             "desc": "Lists names and properties, never the value."},
        ],
    }


@router.get("/signals")
async def list_signals(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """The signal catalogue and the pillar weights — the same registry the score projects from."""
    return {
        "signals": [s.public() for s in signals.all_signals()],
        "pillars": signals.PILLARS,
        "severities": list(signals.SEVERITIES),
    }


class FindingStateBody(BaseModel):
    state: str
    reason: str = ""


@router.post("/findings/{fingerprint}/state")
async def set_finding_state(
    fingerprint: str,
    body: FindingStateBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Suppress, accept or progress a finding.

    Stored against the fingerprint so it survives re-evaluation. A collection run never touches
    this — a rescan must not silently clear somebody's risk acceptance."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    try:
        out = await findings.set_state(
            tenant_id, fingerprint, state=body.state, reason=body.reason, actor=principal.subject
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="iam.finding.state",
            target=fingerprint,
            metadata_json={"state": body.state, "reason": body.reason[:200]},
        )
    )
    await db.commit()
    return {"ok": True, **out}


# --------------------------------------------------------------------------- import
@router.post("/import")
async def import_scanner_run(
    file: UploadFile = File(...),
    label: str = Form(""),
    connection_id: str | None = Form(None),
    principal: Principal = Depends(require_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Import a standalone all-azure-access scanner run (``allAzureAccess.json`` / ``.csv`` /
    ``results.zip``).

    The app's service principal often cannot read the directory, billing or the management-group
    hierarchy; the scanner runs as a human who can. This lets that human produce the data and
    this product analyse it without widening the app's permissions. Imported rows are flagged so
    no screen can present them as a live scan."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    payload = await file.read()
    try:
        summary = importer.import_rows(tenant_id, payload, file.filename or "upload", label=label)
    except importer.ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await store.save_run(tenant_id, scope="__imported__", trigger="import", triggered_by=principal.subject)
    except Exception:  # noqa: BLE001
        log.warning("iam import run record failed", exc_info=True)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="iam.import",
            target=summary.get("source", ""),
            metadata_json={k: v for k, v in summary.items() if k != "unknown_columns"},
        )
    )
    await db.commit()
    return {"ok": True, **summary, "overview": await asyncio.to_thread(compose.compute_overview, tenant_id)}


@router.post("/import/purge")
async def import_purge(
    connection_id: str | None = None,
    principal: Principal = Depends(require_write),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Drop every imported slice for this tenant. Live scans and demo data are untouched."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    removed = importer.purge_imported(tenant_id)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="iam.import.purge",
            target="imported",
            metadata_json={"scopes_removed": removed},
        )
    )
    await db.commit()
    return {"ok": True, "scopes_removed": removed}


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
        log.warning("iam demo run record failed", exc_info=True)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="iam.demo.seed",
            target="demo",
            metadata_json=summary,
        )
    )
    await db.commit()
    return {"ok": True, **summary, "overview": await asyncio.to_thread(compose.compute_overview, tenant_id)}


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
            action="iam.demo.purge",
            target="demo",
            metadata_json={"scopes_removed": removed},
        )
    )
    await db.commit()
    return {"ok": True, "scopes_removed": removed}

"""Telemetry Coverage (Diagnostic Settings / Log Coverage Auditor) endpoints.

Audits diagnostic-settings coverage per workload/subscription against the editable,
versioned recommended-category reference, with server-side caching. Exposes IaC
generation (Bicep + Azure Policy assignment, download-only), Operations-pillar finding
registration, ticketing, the change-request approval inbox, and the approved-workspace
standardization list. Admin-gated."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog
from app.telemetry import cache, change_requests, demo
from app.telemetry.collector import _empty_snapshot, collect_coverage, list_workspaces

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

# Viewing coverage + running scans requires coverage.read; curating the reference set, the
# approved workspaces, and approving/deleting change requests requires coverage.manage. The
# `require_admin` alias is the read tier; manage endpoints opt into `_manage`. Admins pass
# either way.
require_admin = require_permission("coverage.read")
_manage = require_permission("coverage.manage")
log = logging.getLogger("app.api.telemetry")


def _settings() -> tuple[int, list[str], int]:
    from app.core.app_settings import load_settings

    s = load_settings()
    ttl = int(s.get("telemetry_cache_ttl_s", 21600) or 21600)
    approved = list(s.get("telemetry_approved_workspaces", []) or [])
    cap = int(s.get("telemetry_per_resource_scan_cap", 200) or 200)
    return ttl, approved, cap


def _decorate(snap: dict[str, Any], ttl_s: int) -> dict[str, Any]:
    age = cache.age_seconds(snap)
    out = dict(snap)
    out["ttl_s"] = ttl_s
    out["age_seconds"] = int(age) if age is not None else None
    out["stale"] = (age is None) or (age >= ttl_s)
    out.setdefault("all_resources", [])  # consistent shape for snapshots cached pre-feature
    out.setdefault("report_exists", True)  # a saved/computed snapshot exists for this scope
    return out


async def _get_snapshot(
    principal: Principal, scope_kind: str, scope_id: str, *, force: bool, compute: bool = True,
    connection_id: str | None = None,
    progress: "Callable[[int, int, str], Awaitable[None]] | None" = None,
) -> dict[str, Any]:
    from app.core.azure_connections import connection_for_scope
    from app.workloads.registry import get_workload

    ttl, approved, cap = _settings()
    tenant_id = principal.tenant_id or "default"

    if demo.is_demo_scope(scope_kind, scope_id):
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if force or snap is None or not cache.is_fresh(snap, ttl) or "all_resources" not in snap or not snap.get("demo"):
            snap = demo.seed_demo(tenant_id=tenant_id, scope_id=scope_id)
        return _decorate(snap, ttl)

    if not force:
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if snap and cache.is_fresh(snap, ttl):
            return _decorate(snap, ttl)

    # Cached-only mode (page loads, PDF/evidence): never trigger a live Azure scan — that can
    # hang and leave the view stuck on "Loading…". Return any saved snapshot (even stale), else
    # a lightweight "no report yet" sentinel the UI renders as an empty state. Computing a fresh
    # scan happens only on an explicit Refresh (force=True).
    if not compute:
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if snap:
            return _decorate(snap, ttl)
        empty = _empty_snapshot(scope_kind, scope_id, error="")
        empty["report_exists"] = False
        return _decorate(empty, ttl)

    lock = cache.get_lock(tenant_id, scope_kind, scope_id)
    async with lock:
        if not force:
            snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
            if snap and cache.is_fresh(snap, ttl):
                return _decorate(snap, ttl)
        workload = get_workload(scope_id) if scope_kind == "workload" else None
        connection = connection_for_scope(scope_kind, connection_id=connection_id, workload=workload)
        fresh = await collect_coverage(
            connection,
            scope_kind=scope_kind,
            scope_id=scope_id,
            workload=workload,
            approved_workspaces=approved,
            scan_cap=cap,
            progress=progress,
        )
        cache.write_snapshot(tenant_id, scope_kind, scope_id, fresh)
        return _decorate(fresh, ttl)


def _resolve_scope_params(workload_id: str | None, subscription_id: str | None) -> tuple[str, str]:
    if workload_id:
        return "workload", workload_id
    if subscription_id:
        return "subscription", subscription_id
    return "workload", demo.DEMO_WORKLOAD_ID


# ----------------------------------------------------------------- reports (PDF + evidence)
_REPORT_FEATURE = "telemetry"


async def latest_snapshot(
    principal: Principal, workload_id: str | None, subscription_id: str | None
) -> tuple[str, str, dict[str, Any]]:
    """Latest cached coverage snapshot for a scope (no forced re-scan). Public so the
    combined estate report can gather all three coverage features."""
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    snap = await _get_snapshot(principal, scope_kind, scope_id, force=False, compute=False)
    return scope_kind, scope_id, snap


@router.get("/coverage/pdf")
async def coverage_pdf(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> Any:
    from app.core.coverage_report_helpers import coverage_pdf_response

    scope_kind, scope_id, snap = await latest_snapshot(principal, workload_id, subscription_id)
    return await coverage_pdf_response(
        _REPORT_FEATURE, snap, tenant_id=principal.tenant_id or "default",
        scope_kind=scope_kind, scope_id=scope_id,
    )


@router.post("/coverage/evidence")
async def coverage_evidence(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.core.coverage_report_helpers import capture_coverage_evidence

    scope_kind, scope_id, snap = await latest_snapshot(principal, workload_id, subscription_id)
    meta = capture_coverage_evidence(
        _REPORT_FEATURE, snap, tenant_id=principal.tenant_id or "default", actor=principal.subject,
    )
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action=f"{_REPORT_FEATURE}.coverage.evidence", target=meta["id"],
        metadata_json={"sha256": meta["sha256"], "scope": scope_id, "name": meta["name"]},
    ))
    await db.commit()
    return {"ok": True, "snapshot": meta}


# ----------------------------------------------------------------------- coverage
@router.get("/coverage")
async def coverage(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    return await _get_snapshot(principal, scope_kind, scope_id, force=False, compute=False, connection_id=connection_id)


@router.get("/fleet")
async def fleet(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Latest cached telemetry coverage for every active workload; never scans Azure."""
    from app.workloads.registry import list_workloads

    ttl, _approved, _cap = _settings()
    tenant_id = principal.tenant_id or "default"
    rows: list[dict[str, Any]] = []
    for workload in list_workloads():
        snapshot = cache.read_snapshot(tenant_id, "workload", workload["id"])
        age = cache.age_seconds(snapshot) if snapshot else None
        kpis = (snapshot or {}).get("kpis") or {}
        resources = int(kpis.get("total_resources_in_reference", 0) or 0)
        rows.append({
            "workload_id": workload["id"],
            "name": workload.get("name", ""),
            "connection_id": workload.get("connection_id", ""),
            "criticality": workload.get("criticality", ""),
            "environment": workload.get("environment", ""),
            "has_scan": snapshot is not None,
            "run_at": (snapshot or {}).get("generated_at", ""),
            "coverage_pct": (snapshot or {}).get("coverage_pct") if resources > 0 else None,
            "resources": resources,
            "with_any_diag": int(kpis.get("with_any_diag", 0) or 0),
            "with_all_categories": int(kpis.get("with_all_categories", 0) or 0),
            "unknown_destinations": int(kpis.get("unknown_destinations", 0) or 0),
            "unreadable": int(kpis.get("unreadable", 0) or 0),
            "gaps": len((snapshot or {}).get("gaps") or []),
            "demo": bool((snapshot or {}).get("demo", False)),
            "age_seconds": int(age) if age is not None else None,
            "stale": snapshot is None or age is None or age >= ttl,
            "error": str((snapshot or {}).get("error") or ""),
        })
    rows.sort(key=lambda row: (
        not row["has_scan"],
        row["coverage_pct"] is None,
        row["coverage_pct"] if row["coverage_pct"] is not None else 999,
        -row["gaps"],
        row["name"].lower(),
        row["workload_id"],
    ))
    return {
        "workloads": rows,
        "ttl_s": ttl,
        "total": len(rows),
        "scanned": sum(1 for row in rows if row["has_scan"]),
    }


@router.post("/refresh")
async def refresh(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    # Shield so the compute finishes + caches even if the client navigates away mid-refresh.
    snap = await asyncio.shield(_get_snapshot(principal, scope_kind, scope_id, force=True, connection_id=connection_id))
    # Record a compact trend point so this scan can be charted over time.
    from app.core import coverage_trends, coverage_runs

    coverage_trends.record(
        "telemetry", principal.tenant_id or "default", scope_kind, scope_id,
        pct=snap.get("coverage_pct"), extra=snap.get("kpis") or {}, demo=bool(snap.get("demo")),
    )
    coverage_runs.save_run(
        "telemetry", principal.tenant_id or "default", scope_kind, scope_id, snap,
        headline=snap.get("coverage_pct"), counts=snap.get("kpis") or {},
        resource_count=len(snap.get("all_resources") or []), actor=principal.subject,
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="telemetry.refresh",
            target=f"{scope_kind}:{scope_id}",
            metadata_json={"coverage_pct": snap.get("coverage_pct")},
        )
    )
    await db.commit()
    return snap


@router.post("/refresh/stream")
async def refresh_stream(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
):
    """TP4 — live coverage scan over SSE: start → progress* (scanned X of N) → done(snapshot).
    The scan runs as a shielded background task (drained via a queue) so it finishes + caches
    even if the client disconnects; the result is also recorded as a trend point + saved run."""
    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    tenant_id = principal.tenant_id or "default"

    async def _gen():
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def _progress(done: int, total: int, name: str) -> None:
            await queue.put({"done": done, "total": total, "resource": name})

        async def _run() -> dict[str, Any]:
            try:
                return await _get_snapshot(
                    principal, scope_kind, scope_id, force=True, connection_id=connection_id, progress=_progress,
                )
            finally:
                await queue.put(None)  # sentinel

        try:
            yield {"event": "start", "data": json.dumps({"scope_kind": scope_kind, "scope_id": scope_id})}
            task = asyncio.create_task(asyncio.shield(_run()))
            while True:
                ev = await queue.get()
                if ev is None:
                    break
                yield {"event": "progress", "data": json.dumps(ev)}
            snap = await task
            from app.core import coverage_trends, coverage_runs

            coverage_trends.record(
                "telemetry", tenant_id, scope_kind, scope_id,
                pct=snap.get("coverage_pct"), extra=snap.get("kpis") or {}, demo=bool(snap.get("demo")),
            )
            coverage_runs.save_run(
                "telemetry", tenant_id, scope_kind, scope_id, snap,
                headline=snap.get("coverage_pct"), counts=snap.get("kpis") or {},
                resource_count=len(snap.get("all_resources") or []), actor=principal.subject,
            )
            yield {"event": "done", "data": json.dumps(snap)}
        except Exception as exc:  # noqa: BLE001
            log.exception("telemetry refresh stream failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


@router.get("/trend")
async def trend(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Coverage-% trend points for the scope (chart-ready). Backfills a demo series on first
    visit so a demo scope's chart isn't empty."""
    from app.core import coverage_trends

    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    tenant_id = principal.tenant_id or "default"
    if demo.is_demo_scope(scope_kind, scope_id) and not coverage_trends.series("telemetry", tenant_id, scope_kind, scope_id):
        snap = await _get_snapshot(principal, scope_kind, scope_id, force=False)
        coverage_trends.seed_demo_series(
            "telemetry", tenant_id, scope_kind, scope_id,
            current_pct=snap.get("coverage_pct"), extra=snap.get("kpis") or {},
        )
    return coverage_trends.trend("telemetry", tenant_id, scope_kind, scope_id)


# ----------------------------------------------------------------------- run history
@router.get("/runs")
async def list_runs(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Saved scan history for the scope (newest first). Each 'Refresh now' adds a run."""
    from app.core import coverage_runs

    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    return {"runs": coverage_runs.list_runs("telemetry", principal.tenant_id or "default", scope_kind, scope_id)}


@router.get("/runs/trash")
async def list_trashed_runs(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    from app.core import coverage_runs

    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    return {"runs": coverage_runs.list_trashed_runs("telemetry", principal.tenant_id or "default", scope_kind, scope_id)}


@router.get("/run/{run_id}")
async def get_run(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Re-open a saved run's full snapshot (renders exactly like a fresh scan)."""
    from app.core import coverage_runs

    run = coverage_runs.get_run("telemetry", principal.tenant_id or "default", run_id)
    if run is None:
        return {"ok": False, "detail": "Run not found."}
    return {"ok": True, "run": _decorate(run, _settings()[0])}


@router.delete("/run/{run_id}")
async def delete_run(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, bool]:
    from app.core import coverage_runs

    return {"ok": coverage_runs.delete_run("telemetry", principal.tenant_id or "default", run_id)}


@router.post("/run/{run_id}/restore")
async def restore_run(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, bool]:
    from app.core import coverage_runs

    return {"ok": coverage_runs.restore_run("telemetry", principal.tenant_id or "default", run_id)}


@router.delete("/run/{run_id}/purge")
async def purge_run(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, bool]:
    from app.core import coverage_runs

    return {"ok": coverage_runs.purge_run("telemetry", principal.tenant_id or "default", run_id)}


@router.post("/runs/trash/empty")
async def empty_trash(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, int]:
    from app.core import coverage_runs

    scope_kind, scope_id = _resolve_scope_params(workload_id, subscription_id)
    return {"purged": coverage_runs.empty_trash("telemetry", principal.tenant_id or "default", scope_kind, scope_id)}


# ----------------------------------------------------------------------- cleanup (cross-scope)
class _CleanupIds(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.get("/cleanup")
async def cleanup_list(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """All saved runs across EVERY scope (active + trashed) with size — drives the Cleanup tab."""
    from app.core import coverage_runs

    tid = principal.tenant_id or "default"
    return {"runs": coverage_runs.list_all_runs("telemetry", tid), "stats": coverage_runs.cleanup_stats("telemetry", tid)}


@router.post("/cleanup/trash")
async def cleanup_trash(body: _CleanupIds, principal: Principal = Depends(require_admin)) -> dict[str, int]:
    from app.core import coverage_runs

    return coverage_runs.trash_runs("telemetry", principal.tenant_id or "default", body.ids)


@router.post("/cleanup/restore")
async def cleanup_restore(body: _CleanupIds, principal: Principal = Depends(require_admin)) -> dict[str, int]:
    from app.core import coverage_runs

    return coverage_runs.restore_runs("telemetry", principal.tenant_id or "default", body.ids)


@router.post("/cleanup/purge")
async def cleanup_purge(body: _CleanupIds, principal: Principal = Depends(require_admin)) -> dict[str, int]:
    from app.core import coverage_runs

    return coverage_runs.purge_runs("telemetry", principal.tenant_id or "default", body.ids)



@router.get("/locate")
async def locate_in_architecture(
    resource_id: str = Query(...), _: Principal = Depends(require_admin)
) -> dict[str, Any]:
    """Find an architecture node whose arm_id matches a resource id (canvas drill-back)."""
    from app.architectures.registry import list_architectures

    rid = (resource_id or "").strip().lower()
    if not rid:
        return {"found": False}
    for arch in list_architectures():
        for node in arch.get("nodes", []) or []:
            if str(node.get("arm_id", "")).lower() == rid:
                return {
                    "found": True,
                    "architecture_id": arch.get("id"),
                    "architecture_name": arch.get("name"),
                    "node_id": node.get("id"),
                }
    return {"found": False}


@router.get("/workspaces")
async def workspaces(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Log Analytics workspaces in the tenant + the current approved list (for Admin)."""
    from app.core.azure_connections import get_default_connection

    _ttl, approved, _cap = _settings()
    try:
        ws = await list_workspaces(get_default_connection())
    except Exception as exc:  # noqa: BLE001
        log.info("workspace list failed: %s", exc)
        ws = []
    return {"workspaces": ws, "approved": approved}


# ----------------------------------------------------------------------- reference set
@router.get("/reference")
async def get_reference(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.telemetry.reference import load_reference

    return load_reference()


class ReferenceUpdate(BaseModel):
    types: dict[str, Any]
    reason: str = "Edited"


@router.put("/reference")
async def put_reference(
    payload: ReferenceUpdate,
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.telemetry.reference import save_reference

    doc = save_reference(payload.types, actor=principal.subject, reason=payload.reason)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="telemetry.reference.update",
            target=f"v{doc.get('version')}",
            metadata_json={"type_count": len(doc.get("types", {}))},
        )
    )
    await db.commit()
    return doc


@router.get("/reference/revisions")
async def list_reference_revisions(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.telemetry.reference import list_revisions

    return {"revisions": list_revisions()}


class RestoreRequest(BaseModel):
    revision_id: str


@router.post("/reference/restore")
async def restore_reference(payload: RestoreRequest, principal: Principal = Depends(_manage)) -> dict[str, Any]:
    from app.telemetry.reference import restore_revision

    doc = restore_revision(payload.revision_id, actor=principal.subject)
    if doc is None:
        return {"ok": False, "detail": "Revision not found."}
    return {"ok": True, "reference": doc}


@router.post("/reference/reset")
async def reset_reference(principal: Principal = Depends(_manage)) -> dict[str, Any]:
    from app.telemetry.reference import reset_to_builtin

    return {"ok": True, "reference": reset_to_builtin(actor=principal.subject)}


# ----------------------------------------------------------------------- approved workspaces
class ApprovedWorkspaces(BaseModel):
    workspaces: list[str]


@router.put("/approved-workspaces")
async def set_approved_workspaces(
    payload: ApprovedWorkspaces,
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.core.app_settings import save_settings

    cleaned = [str(w).strip() for w in payload.workspaces if str(w).strip()]
    save_settings({"telemetry_approved_workspaces": cleaned})
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="telemetry.approved_workspaces.update",
            target="",
            metadata_json={"count": len(cleaned)},
        )
    )
    await db.commit()
    return {"ok": True, "approved": cleaned}


# ----------------------------------------------------------------------- IaC
class Gap(BaseModel):
    resource_id: str = ""
    resource_name: str = ""
    resource_type: str = ""
    resource_group: str = ""
    subscription_id: str = ""
    location: str = ""
    status: str = "none"
    missing_categories: list[str] = Field(default_factory=list)
    missing_audit_categories: list[str] = Field(default_factory=list)
    has_drift: bool = False
    drift_workspaces: list[str] = Field(default_factory=list)
    severity: str = "warning"


class IacRequest(BaseModel):
    gaps: list[Gap]
    format: str = "bicep"
    workspace_id: str = ""


@router.post("/iac")
async def generate_iac_endpoint(payload: IacRequest, _: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.telemetry.iac import generate_iac

    gaps = [g.model_dump() for g in payload.gaps]
    fmt = payload.format if payload.format in ("bicep", "policy") else "bicep"
    workspace = payload.workspace_id
    if not workspace:
        _ttl, approved, _cap = _settings()
        workspace = approved[0] if approved else ""
    text = generate_iac(gaps, fmt, workspace_id=workspace)
    return {"format": fmt, "iac": text, "gap_count": len(gaps)}


# ----------------------------------------------------------------------- findings
class RegisterFindingsRequest(BaseModel):
    workload_id: str
    workload_name: str = ""
    gaps: list[Gap]


@router.post("/findings/register")
async def register_findings(
    payload: RegisterFindingsRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Register telemetry gaps as Operations-pillar findings via a lightweight AssessmentRun."""
    from datetime import datetime, timezone

    from app.models import AssessmentRun

    by_check: dict[str, dict[str, Any]] = {}
    sev_rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    for g in payload.gaps:
        kind = "no_diagnostics" if g.status == "none" else "incomplete_diagnostics"
        check_id = f"telemetry_{g.resource_type.replace('/', '_')}_{kind}"[:64]
        f = by_check.get(check_id)
        if f is None:
            title = (
                f"No diagnostic settings on {g.resource_type}"
                if g.status == "none"
                else f"Incomplete diagnostic settings on {g.resource_type}"
            )
            f = {
                "check_id": check_id,
                "pillar": "operations",
                "title": title,
                "description": (
                    "Resource has no diagnostic settings — logs are not being collected."
                    if g.status == "none"
                    else "Diagnostic settings are missing recommended categories or ship to a non-approved destination."
                ),
                "severity": g.severity,
                "weight": 0,
                "frameworks": {},
                "remediation": "Enable diagnostic settings shipping the recommended categories to an approved Log Analytics workspace.",
                "remediation_command": "",
                "resource_types": [g.resource_type],
                "status": "fail",
                "flagged_count": 0,
                "flagged_resources": [],
                "ai_rationale": "",
            }
            by_check[check_id] = f
        f["flagged_resources"].append(
            {
                "id": g.resource_id,
                "name": g.resource_name,
                "type": g.resource_type,
                "resource_group": g.resource_group,
                "subscription_id": g.subscription_id,
                "remediation_command": "",
            }
        )
        f["flagged_count"] = len(f["flagged_resources"])
        if sev_rank.get(g.severity, 3) < sev_rank.get(f["severity"], 3):
            f["severity"] = g.severity

    findings = list(by_check.values())
    worst = "info"
    for f in findings:
        if sev_rank.get(f["severity"], 3) < sev_rank.get(worst, 3):
            worst = f["severity"]

    now = datetime.now(timezone.utc)
    run = AssessmentRun(
        workload_id=payload.workload_id,
        workload_name=payload.workload_name or payload.workload_id,
        tenant_id=principal.tenant_id,
        pillars=["operations"],
        status="succeeded",
        overall_score=None,
        scores_json={},
        totals_json={"passed": 0, "failed": len(findings), "na": 0, "waived": 0, "by_severity": {}},
        severity=worst,
        findings_json=findings,
        resource_count=sum(f["flagged_count"] for f in findings),
        resources_json=[],
        summary=f"Telemetry Coverage: {len(findings)} diagnostic-settings gap finding(s).",
        used_ai=False,
        triggered_by=principal.subject,
        trigger="telemetry",
        started_at=now,
        ended_at=now,
    )
    db.add(run)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="telemetry.findings.register",
            target=payload.workload_id,
            metadata_json={"findings": len(findings)},
        )
    )
    await db.commit()
    await db.refresh(run)
    return {"ok": True, "run_id": run.id, "finding_count": len(findings)}


# ----------------------------------------------------------------------- ticketing
class TicketRequest(BaseModel):
    connector_id: str = Field(min_length=1)
    gap: Gap


@router.post("/ticket")
async def create_telemetry_ticket(
    payload: TicketRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.assessments.tickets import create_ticket

    g = payload.gap
    missing = ", ".join(g.missing_categories) or "(destination drift)"
    finding = {
        "severity": g.severity,
        "title": (
            f"No diagnostic settings: {g.resource_name}"
            if g.status == "none"
            else f"Telemetry gap: {g.resource_name}"
        ),
        "check_id": f"telemetry_{g.resource_type.replace('/', '_')}",
        "pillar": "operations",
        "description": f"Resource: {g.resource_name} ({g.resource_type})\nMissing categories: {missing}"
        + (f"\nDrift workspaces: {', '.join(g.drift_workspaces)}" if g.drift_workspaces else ""),
        "remediation": "Enable diagnostic settings with the recommended categories to an approved workspace.",
    }
    result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=g.resource_name or "Telemetry")
    if result.get("ok"):
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id,
                actor_id=principal.subject,
                action="telemetry.ticket.create",
                target=g.resource_id[:512],
                metadata_json={"ticket": result.get("ticket_id", "")},
            )
        )
        await db.commit()
    return result


# ----------------------------------------------------------------------- approval inbox
class ApprovalRequest(BaseModel):
    scope_kind: str = "workload"
    scope_id: str = ""
    scope_name: str = ""
    gaps: list[Gap]
    format: str = "bicep"
    workspace_id: str = ""


@router.post("/approval")
async def send_to_approval(
    payload: ApprovalRequest,
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.telemetry.iac import generate_iac

    gaps = [g.model_dump() for g in payload.gaps]
    fmt = payload.format if payload.format in ("bicep", "policy") else "bicep"
    workspace = payload.workspace_id
    if not workspace:
        _ttl, approved, _cap = _settings()
        workspace = approved[0] if approved else ""
    iac_text = generate_iac(gaps, fmt, workspace_id=workspace)
    req = change_requests.create_request(
        tenant_id=principal.tenant_id,
        scope_kind=payload.scope_kind,
        scope_id=payload.scope_id,
        scope_name=payload.scope_name,
        gaps=gaps,
        iac_format=fmt,
        iac_text=iac_text,
        requested_by=principal.subject,
    )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="telemetry.approval.create",
            target=req["id"],
            metadata_json={"gaps": len(gaps), "format": fmt},
        )
    )
    await db.commit()
    return {"ok": True, "request": {k: v for k, v in req.items() if k != "gaps"}}


@router.get("/approvals")
async def list_approvals(
    status: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return {"requests": change_requests.list_requests(principal.tenant_id, status=status)}


@router.get("/approvals/{request_id}")
async def get_approval(request_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    req = change_requests.get_request(principal.tenant_id, request_id)
    if req is None:
        return {"ok": False, "detail": "Not found."}
    return {"ok": True, "request": req}


class DecisionRequest(BaseModel):
    decision: str
    reason: str = ""


@router.post("/approvals/{request_id}/decide")
async def decide_approval(
    request_id: str,
    payload: DecisionRequest,
    principal: Principal = Depends(_manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    req = change_requests.decide_request(
        principal.tenant_id, request_id, decision=payload.decision, actor=principal.subject, reason=payload.reason
    )
    if req is None:
        return {"ok": False, "detail": "Invalid decision or request not found."}
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action=f"telemetry.approval.{payload.decision}",
            target=request_id,
            metadata_json={"reason": payload.reason},
        )
    )
    await db.commit()
    return {"ok": True, "request": {k: v for k, v in req.items() if k != "gaps"}}


@router.delete("/approvals/{request_id}")
async def delete_approval(request_id: str, principal: Principal = Depends(_manage)) -> dict[str, Any]:
    ok = change_requests.delete_request(principal.tenant_id, request_id)
    return {"ok": ok}


# ----------------------------------------------------------------------- demo seed
@router.post("/demo/seed")
async def seed_demo_endpoint(principal: Principal = Depends(_manage)) -> dict[str, Any]:
    snap = demo.seed_demo(tenant_id=principal.tenant_id)
    return {"ok": True, "workload_id": demo.DEMO_WORKLOAD_ID, "coverage_pct": snap.get("coverage_pct")}

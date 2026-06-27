"""Performance Profiler endpoints — profile a workload against the AMBA reference.

Returns a heatmap + per-resource performance scores + a ranked bottleneck list + an AI
narrative. A live SSE refresh streams per-resource progress ("profiling N resources…").
Findings register under the Performance pillar; ticketing + War-Room handoff included.
Admin-gated. Read-only — uses az monitor metrics list only."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog
from app.perfprofile import cache, demo

router = APIRouter(prefix="/performance", tags=["performance"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("perfprofile.read")
log = logging.getLogger("app.api.perfprofile")


def _settings() -> tuple[int, str, str, int]:
    from app.core.app_settings import load_settings

    s = load_settings()
    ttl = int(s.get("perfprofile_cache_ttl_s", 21600) or 21600)
    ts = str(s.get("perfprofile_window", "P1D") or "P1D")
    grain = str(s.get("perfprofile_interval", "PT15M") or "PT15M")
    cap = int(s.get("perfprofile_scan_cap", 200) or 200)
    return ttl, ts, grain, cap


def _decorate(snap: dict[str, Any], ttl_s: int) -> dict[str, Any]:
    age = cache.age_seconds(snap)
    out = dict(snap)
    out["ttl_s"] = ttl_s
    out["age_seconds"] = int(age) if age is not None else None
    out["stale_cache"] = (age is None) or (age >= ttl_s)
    # Historical runs saved before the "All Resources" tab existed have no all_resources list;
    # default it so the frontend renders an empty tab instead of crashing.
    out.setdefault("all_resources", [])
    return out


def _scope(workload_id: str | None, subscription_id: str | None) -> tuple[str, str]:
    if workload_id:
        return "workload", workload_id
    if subscription_id:
        return "subscription", subscription_id
    return "workload", demo.DEMO_WORKLOAD_ID


def _conn_and_workload(scope_kind: str, scope_id: str, connection_id: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve the workload (when the scope is a workload) and the Azure connection to profile
    it with. An explicit ``connection_id`` (the Azure-tenant picker) wins; otherwise the
    workload's OWN connection, falling back to the default only when it has none."""
    from app.core.azure_connections import connection_for_scope
    from app.workloads.registry import get_workload

    workload = get_workload(scope_id) if scope_kind == "workload" else None
    return connection_for_scope(scope_kind, connection_id=connection_id, workload=workload), workload


async def _get_snapshot(principal: Principal, scope_kind: str, scope_id: str, *, force: bool) -> dict[str, Any]:
    from app.perfprofile.collector import profile_workload

    ttl, window, interval, cap = _settings()
    tenant_id = principal.tenant_id or "default"

    if demo.is_demo_scope(scope_kind, scope_id):
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if force or snap is None or not cache.is_fresh(snap, ttl) or not snap.get("demo"):
            snap = demo.seed_demo(tenant_id=tenant_id, scope_id=scope_id)
        return _decorate(snap, ttl)

    if not force:
        snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
        if snap and cache.is_fresh(snap, ttl):
            return _decorate(snap, ttl)

    lock = cache.get_lock(tenant_id, scope_kind, scope_id)
    async with lock:
        if not force:
            snap = cache.read_snapshot(tenant_id, scope_kind, scope_id)
            if snap and cache.is_fresh(snap, ttl):
                return _decorate(snap, ttl)
        connection, workload = _conn_and_workload(scope_kind, scope_id)
        fresh = await profile_workload(
            connection, scope_kind=scope_kind, scope_id=scope_id, workload=workload,
            timespan=window, interval=interval, scan_cap=cap,
        )
        cache.write_snapshot(tenant_id, scope_kind, scope_id, fresh)
        return _decorate(fresh, ttl)


def _sli_context(scope_kind: str, scope_id: str, tenant_id: str) -> str:
    if scope_kind != "workload":
        return ""
    try:
        from app.teleintel.resolver import sli_context_for_workload

        return sli_context_for_workload(scope_id, tenant_id)
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------------------------------------------------- profile (read-only)
@router.get("/profile")
async def profile(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Return the LATEST stored run for the scope WITHOUT profiling. Page load uses this +
    /runs; a new profile only runs when the user clicks Run (/refresh/stream)."""
    from app.perfprofile import runs

    scope_kind, scope_id = _scope(workload_id, subscription_id)
    latest = runs.latest_run(principal.tenant_id or "default", scope_kind, scope_id)
    if latest is None:
        return {
            "scope_kind": scope_kind, "scope_id": scope_id, "scope_name": scope_id,
            "no_runs": True, "resources": [], "bottlenecks": [], "top_bottleneck": None,
            "scorecard": {"workload_score": 100, "resources_profiled": 0, "breaching": 0, "approaching": 0, "healthy": 0, "bottleneck_count": 0},
        }
    return latest


# ----------------------------------------------------------------------- fleet (all workloads)
@router.get("/fleet")
async def fleet(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Summarize the LATEST performance-profile run for EVERY active workload — drives the
    Fleet view's mass overview + mass-launch grid. Reads stored runs only (no profiling)."""
    import time
    from datetime import datetime

    from app.perfprofile import runs
    from app.workloads.registry import list_workloads

    ttl, default_window, _interval, _cap = _settings()
    tenant_id = principal.tenant_id or "default"
    workloads = list_workloads()
    scopes = [("workload", w["id"]) for w in workloads]
    latest = runs.latest_runs_for_scopes(tenant_id, scopes)

    rows: list[dict[str, Any]] = []
    for w in workloads:
        summ = latest.get(f"workload:{w['id']}")
        run_at = (summ or {}).get("run_at") or ""
        age: float | None = None
        if run_at:
            try:
                dt = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
                age = max(0.0, time.time() - dt.timestamp())
            except (ValueError, TypeError):
                age = None
        rows.append({
            "workload_id": w["id"],
            "name": w.get("name", ""),
            "connection_id": w.get("connection_id", ""),
            "criticality": w.get("criticality", ""),
            "environment": w.get("environment", ""),
            "has_runs": summ is not None,
            "run_id": (summ or {}).get("id", ""),
            "run_at": run_at,
            "window": (summ or {}).get("window", ""),
            "workload_score": (summ or {}).get("workload_score"),
            "resources_profiled": (summ or {}).get("resources_profiled", 0),
            "breaching": (summ or {}).get("breaching", 0),
            "approaching": (summ or {}).get("approaching", 0),
            "healthy": (summ or {}).get("healthy", 0),
            "top_bottleneck": (summ or {}).get("top_bottleneck"),
            "demo": (summ or {}).get("demo", False),
            "age_seconds": int(age) if age is not None else None,
            "stale": (summ is None) or (age is None) or (age >= ttl),
        })
    # Worst first: never-profiled last, then most breaching, then lowest score.
    rows.sort(
        key=lambda r: (
            not r["has_runs"],
            -(r["breaching"] or 0),
            r["workload_score"] if r["workload_score"] is not None else 999,
        )
    )
    return {
        "workloads": rows,
        "ttl_s": ttl,
        "default_window": default_window,
        "total": len(rows),
        "profiled": sum(1 for r in rows if r["has_runs"]),
    }


# ----------------------------------------------------------------------- run history
@router.get("/runs")
async def list_runs(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    from app.perfprofile import runs

    scope_kind, scope_id = _scope(workload_id, subscription_id)
    return {"runs": runs.list_runs(principal.tenant_id or "default", scope_kind, scope_id)}


@router.get("/runs/trash")
async def list_trashed_runs(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Trashed (soft-deleted) profile runs for the scope — restorable until purged."""
    from app.perfprofile import runs

    scope_kind, scope_id = _scope(workload_id, subscription_id)
    return {"runs": runs.list_trashed_runs(principal.tenant_id or "default", scope_kind, scope_id)}


@router.post("/runs/trash/empty")
async def empty_trash(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Permanently delete every trashed run for the scope."""
    from app.perfprofile import runs

    scope_kind, scope_id = _scope(workload_id, subscription_id)
    deleted = runs.empty_trash(principal.tenant_id or "default", scope_kind, scope_id)
    if deleted:
        db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="performance.trash.empty", target=f"{scope_kind}:{scope_id}"))
        await db.commit()
    return {"ok": True, "deleted": deleted}


@router.get("/run/{run_id}")
async def get_run(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.perfprofile import runs

    run = runs.get_run(principal.tenant_id or "default", run_id)
    if run is None:
        return {"ok": False, "detail": "Run not found."}
    return {"ok": True, "run": run}


# --------------------------------------------------------------- report (PDF + Evidence)
def _safe_filename(text: str, *, fallback: str = "performance") -> str:
    import re

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-")
    return slug[:80] or fallback


async def _resolve_run_snapshot(
    principal: Principal, run_id: str | None, workload_id: str | None, subscription_id: str | None
) -> dict[str, Any] | None:
    """The snapshot to report on: a specific run by id, else the latest run for the scope."""
    from app.perfprofile import runs

    tenant_id = principal.tenant_id or "default"
    if run_id:
        return runs.get_run(tenant_id, run_id)
    scope_kind, scope_id = _scope(workload_id, subscription_id)
    if demo.is_demo_scope(scope_kind, scope_id):
        snap = await _get_snapshot(principal, scope_kind, scope_id, force=False)
        return snap
    return runs.latest_run(tenant_id, scope_kind, scope_id)


def _trend_for(snap: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    from app.core import coverage_trends

    return coverage_trends.trend(
        "performance", tenant_id,
        str(snap.get("scope_kind") or "workload"), str(snap.get("scope_id") or ""),
    )


async def _performance_pdf_response(snap: dict[str, Any], tenant_id: str):
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import Response

    from app.core.performance_pdf import build_performance_pdf

    trend = _trend_for(snap, tenant_id)
    pdf = await run_in_threadpool(build_performance_pdf, snap, trend)
    scope_name = snap.get("scope_name") or snap.get("scope_id") or "scope"
    fname = f"performance-profile-{_safe_filename(scope_name)}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/run/{run_id}/pdf")
async def run_pdf(run_id: str, principal: Principal = Depends(require_admin)) -> Any:
    """Branded PDF for one specific profile run."""
    snap = await _resolve_run_snapshot(principal, run_id, None, None)
    if snap is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Run not found.")
    return await _performance_pdf_response(snap, principal.tenant_id or "default")


@router.get("/pdf")
async def latest_pdf(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> Any:
    """Branded PDF for the latest profile run of a scope (no run_id needed)."""
    snap = await _resolve_run_snapshot(principal, None, workload_id, subscription_id)
    if snap is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="No profile run for this scope yet — run the profiler first.")
    return await _performance_pdf_response(snap, principal.tenant_id or "default")


def _capture_evidence(snap: dict[str, Any], *, tenant_id: str, actor: str) -> dict[str, Any]:
    from app.core.performance_pdf import build_evidence_content
    from app.evidence import registry

    name, scope, included, tags, content = build_evidence_content(snap)
    return registry.create_snapshot(
        tenant_id=tenant_id, name=name, scope=scope, included=included,
        retention_class="standard", tags=tags, content=content,
        created_by=actor or "system", demo=bool(snap.get("demo")),
    )


@router.post("/run/{run_id}/evidence")
async def run_evidence(
    run_id: str, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Capture one specific profile run as an immutable Evidence Locker snapshot."""
    from fastapi import HTTPException

    snap = await _resolve_run_snapshot(principal, run_id, None, None)
    if snap is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    meta = _capture_evidence(snap, tenant_id=principal.tenant_id or "default", actor=principal.subject)
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="performance.evidence", target=meta["id"],
        metadata_json={"sha256": meta["sha256"], "scope": snap.get("scope_id"), "name": meta["name"]},
    ))
    await db.commit()
    return {"ok": True, "snapshot": meta}


@router.post("/evidence")
async def latest_evidence(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Capture the latest profile run of a scope as an Evidence Locker snapshot."""
    from fastapi import HTTPException

    snap = await _resolve_run_snapshot(principal, None, workload_id, subscription_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="No profile run for this scope yet — run the profiler first.")
    meta = _capture_evidence(snap, tenant_id=principal.tenant_id or "default", actor=principal.subject)
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="performance.evidence", target=meta["id"],
        metadata_json={"sha256": meta["sha256"], "scope": snap.get("scope_id"), "name": meta["name"]},
    ))
    await db.commit()
    return {"ok": True, "snapshot": meta}


@router.delete("/run/{run_id}")
async def delete_run(
    run_id: str, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Soft-delete: move a profile run to the Trash (restorable until purged)."""
    from app.perfprofile import runs

    ok = runs.delete_run(principal.tenant_id or "default", run_id)
    if ok:
        db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="performance.run.delete", target=run_id))
        await db.commit()
    return {"ok": ok}


@router.post("/run/{run_id}/restore")
async def restore_run(
    run_id: str, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Restore a trashed profile run back into history."""
    from app.perfprofile import runs

    ok = runs.restore_run(principal.tenant_id or "default", run_id)
    if ok:
        db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="performance.run.restore", target=run_id))
        await db.commit()
    return {"ok": ok}


@router.delete("/run/{run_id}/purge")
async def purge_run(
    run_id: str, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Permanently delete a single trashed profile run (hard delete)."""
    from app.perfprofile import runs

    if runs.get_run(principal.tenant_id or "default", run_id, include_deleted=True) is None:
        return {"ok": False, "detail": "Run not found."}
    ok = runs.purge_run(principal.tenant_id or "default", run_id)
    if ok:
        db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="performance.run.purge", target=run_id))
        await db.commit()
    return {"ok": ok}


@router.get("/trend")
async def trend(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Performance-score trend points for the scope (chart-ready). On a demo scope with no
    history yet, backfills a believable rising series so the chart isn't empty."""
    from app.core import coverage_trends
    from app.perfprofile import runs

    scope_kind, scope_id = _scope(workload_id, subscription_id)
    tenant_id = principal.tenant_id or "default"
    if demo.is_demo_scope(scope_kind, scope_id) and not coverage_trends.series("performance", tenant_id, scope_kind, scope_id):
        latest = runs.latest_run(tenant_id, scope_kind, scope_id)
        score = (latest or {}).get("scorecard", {}).get("workload_score") if latest else None
        if score is None:
            # No runs yet for the demo scope — seed the demo profile to get a current score.
            snap = demo.seed_demo(tenant_id=tenant_id, scope_id=scope_id)
            score = snap.get("scorecard", {}).get("workload_score")
        coverage_trends.seed_demo_series("performance", tenant_id, scope_kind, scope_id, current_pct=score)
    return coverage_trends.trend("performance", tenant_id, scope_kind, scope_id)


@router.post("/refresh/stream")
async def refresh_stream(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    window: str | None = Query(default=None),
    start_time: str | None = Query(default=None),
    end_time: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
):
    """Live profiling over SSE: start → progress* → done. Saves the result to run history
    and returns it (with run id) in the done payload."""
    from app.perfprofile import runs
    from app.perfprofile.collector import profile_workload
    from app.perfprofile.narrative import narrate

    scope_kind, scope_id = _scope(workload_id, subscription_id)
    _ttl, default_window, interval, cap = _settings()
    eff_window = (window or default_window).strip()
    st = (start_time or "").strip()
    et = (end_time or "").strip()
    tenant_id = principal.tenant_id or "default"

    async def _gen():
        try:
            yield {"event": "start", "data": json.dumps({"scope_kind": scope_kind, "scope_id": scope_id, "window": eff_window})}
            if demo.is_demo_scope(scope_kind, scope_id):
                snap = demo.build_demo_snapshot(scope_id=scope_id)
                if st and et:
                    snap["window"] = f"{st} → {et}"
                    snap["requested_start"] = st
                    snap["requested_end"] = et
                else:
                    snap["window"] = eff_window
                    snap["requested_window"] = eff_window
                for r in snap.get("resources", []):
                    yield {"event": "progress", "data": json.dumps({"resource": r["resource_name"], "type": r["resource_type"]})}
            else:
                # PP4 — stream per-resource progress LIVE (not replayed after the scan): the
                # collector's progress callback pushes onto a queue that we drain concurrently
                # while the profile runs in a background task.
                connection, workload = _conn_and_workload(scope_kind, scope_id, connection_id)
                queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

                async def _collect(name: str, rtype: str):
                    await queue.put({"resource": name, "type": rtype})

                async def _run() -> dict[str, Any]:
                    try:
                        return await profile_workload(
                            connection, scope_kind=scope_kind, scope_id=scope_id, workload=workload,
                            timespan=eff_window, interval=interval, scan_cap=cap,
                            start_time=st, end_time=et, progress=_collect,
                        )
                    finally:
                        await queue.put(None)  # sentinel: scan finished

                task = asyncio.create_task(_run())
                while True:
                    ev = await queue.get()
                    if ev is None:
                        break
                    yield {"event": "progress", "data": json.dumps(ev)}
                snap = await task
            snap = dict(snap)
            snap["narrative"] = await narrate(snap, sli_context=_sli_context(scope_kind, scope_id, tenant_id))
            cache.write_snapshot(tenant_id, scope_kind, scope_id, snap)
            stored = runs.save_run(tenant_id, scope_kind, scope_id, snap, actor=principal.subject)
            yield {"event": "done", "data": json.dumps(stored)}
        except Exception as exc:  # noqa: BLE001
            log.exception("performance refresh failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


@router.post("/refresh")
async def refresh(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    window: str | None = Query(default=None),
    start_time: str | None = Query(default=None),
    end_time: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Non-streaming profile + save to history (returns the stored run)."""
    from app.perfprofile import runs
    from app.perfprofile.collector import profile_workload
    from app.perfprofile.narrative import narrate

    scope_kind, scope_id = _scope(workload_id, subscription_id)
    _ttl, default_window, interval, cap = _settings()
    eff_window = (window or default_window).strip()
    st = (start_time or "").strip()
    et = (end_time or "").strip()
    tenant_id = principal.tenant_id or "default"

    if demo.is_demo_scope(scope_kind, scope_id):
        snap = demo.build_demo_snapshot(scope_id=scope_id)
        snap["window"] = f"{st} → {et}" if (st and et) else eff_window
        if st and et:
            snap["requested_start"] = st
            snap["requested_end"] = et
        else:
            snap["requested_window"] = eff_window
    else:
        connection, workload = _conn_and_workload(scope_kind, scope_id, connection_id)
        snap = await profile_workload(
            connection, scope_kind=scope_kind, scope_id=scope_id, workload=workload,
            timespan=eff_window, interval=interval, scan_cap=cap, start_time=st, end_time=et,
        )
    snap = dict(snap)
    snap["narrative"] = await narrate(snap, sli_context=_sli_context(scope_kind, scope_id, tenant_id))
    cache.write_snapshot(tenant_id, scope_kind, scope_id, snap)
    stored = runs.save_run(tenant_id, scope_kind, scope_id, snap, actor=principal.subject)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id, actor_id=principal.subject, action="performance.refresh",
            target=f"{scope_kind}:{scope_id}", metadata_json={"run_id": stored.get("id"), "score": snap.get("scorecard", {}).get("workload_score")},
        )
    )
    await db.commit()
    return stored


@router.get("/resource")
async def resource_detail(
    resource_id: str = Query(...),
    run_id: str | None = Query(default=None),
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Resource drill-down from a stored run (run_id), else the scope's latest run."""
    from app.perfprofile import runs

    tenant_id = principal.tenant_id or "default"
    snap = runs.get_run(tenant_id, run_id) if run_id else None
    if snap is None:
        scope_kind, scope_id = _scope(workload_id, subscription_id)
        snap = runs.latest_run(tenant_id, scope_kind, scope_id)
    for r in (snap or {}).get("resources", []):
        if r.get("resource_id") == resource_id:
            return {"ok": True, "resource": r}
    return {"ok": False, "detail": "Resource not in that run."}



# ----------------------------------------------------------------------- findings
class Bottleneck(BaseModel):
    resource_id: str = ""
    resource_name: str = ""
    resource_type: str = ""
    metric: str = ""
    metric_name: str = ""
    severity: str = "warning"
    state: str = "approaching"
    observed: float | None = None
    threshold: float | None = None
    unit: str = ""
    pct_of_threshold: float | None = None
    why: str = ""


class RegisterFindingsRequest(BaseModel):
    workload_id: str
    workload_name: str = ""
    bottlenecks: list[Bottleneck]


@router.post("/findings/register")
async def register_findings(
    payload: RegisterFindingsRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Register performance bottlenecks as Performance-pillar findings via a lightweight
    AssessmentRun (feeds the existing scoring / finding-state / waivers)."""
    from datetime import datetime, timezone

    from app.models import AssessmentRun

    sev_rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    by_check: dict[str, dict[str, Any]] = {}
    for b in payload.bottlenecks:
        check_id = f"perf_{b.resource_type.replace('/', '_')}_{b.metric}"[:64]
        sev = "error" if b.state == "breaching" else "warning"
        f = by_check.get(check_id)
        if f is None:
            f = {
                "check_id": check_id, "pillar": "performance",
                "title": f"{b.metric_name} {'breaching' if b.state == 'breaching' else 'approaching'} AMBA threshold on {b.resource_type}",
                "description": f"{b.metric} observed {b.observed}{b.unit} vs threshold {b.threshold}{b.unit} ({b.pct_of_threshold}% of threshold). {b.why}",
                "severity": sev, "weight": 0, "frameworks": {},
                "remediation": "Scale up/out the resource or tune the workload before it breaches under peak load.",
                "remediation_command": "", "resource_types": [b.resource_type], "status": "fail",
                "flagged_count": 0, "flagged_resources": [], "ai_rationale": "",
            }
            by_check[check_id] = f
        f["flagged_resources"].append(
            {"id": b.resource_id, "name": b.resource_name, "type": b.resource_type,
             "resource_group": "", "subscription_id": "", "remediation_command": ""}
        )
        f["flagged_count"] = len(f["flagged_resources"])
        if sev_rank.get(sev, 3) < sev_rank.get(f["severity"], 3):
            f["severity"] = sev

    findings = list(by_check.values())
    worst = "info"
    for f in findings:
        if sev_rank.get(f["severity"], 3) < sev_rank.get(worst, 3):
            worst = f["severity"]

    now = datetime.now(timezone.utc)
    run = AssessmentRun(
        workload_id=payload.workload_id, workload_name=payload.workload_name or payload.workload_id,
        tenant_id=principal.tenant_id, pillars=["performance"], status="succeeded", overall_score=None,
        scores_json={}, totals_json={"passed": 0, "failed": len(findings), "na": 0, "waived": 0, "by_severity": {}},
        severity=worst, findings_json=findings, resource_count=sum(f["flagged_count"] for f in findings),
        resources_json=[], summary=f"Performance Profiler: {len(findings)} performance finding(s).",
        used_ai=False, triggered_by=principal.subject, trigger="perfprofile", started_at=now, ended_at=now,
    )
    db.add(run)
    db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="performance.findings.register", target=payload.workload_id, metadata_json={"findings": len(findings)}))
    await db.commit()
    await db.refresh(run)
    return {"ok": True, "run_id": run.id, "finding_count": len(findings)}


# ----------------------------------------------------------------------- ticketing
class TicketRequest(BaseModel):
    connector_id: str = Field(min_length=1)
    bottleneck: Bottleneck


@router.post("/ticket")
async def create_perf_ticket(
    payload: TicketRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    from app.assessments.tickets import create_ticket

    b = payload.bottleneck
    finding = {
        "severity": "error" if b.state == "breaching" else "warning",
        "title": f"Performance: {b.metric_name} on {b.resource_name}",
        "check_id": f"perf_{b.metric}",
        "pillar": "performance",
        "description": f"{b.resource_name} ({b.resource_type})\n{b.metric} observed {b.observed}{b.unit} vs AMBA threshold {b.threshold}{b.unit} ({b.pct_of_threshold}% of threshold).\n{b.why}",
        "remediation": "Scale up/out or tune the workload before peak load breaches the threshold.",
    }
    result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=b.resource_name or "Performance")
    if result.get("ok"):
        db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="performance.ticket.create", target=b.resource_id[:512], metadata_json={"ticket": result.get("ticket_id", "")}))
        await db.commit()
    return result


# ----------------------------------------------------------------------- demo
@router.post("/demo/seed")
async def seed_demo_endpoint(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    snap = demo.seed_demo(tenant_id=principal.tenant_id or "default")
    return {"ok": True, "workload_id": demo.DEMO_WORKLOAD_ID, "scorecard": snap.get("scorecard", {})}

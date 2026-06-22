"""Azure Workload Change Explorer endpoints.

Read-only: analyze the changes made to a workload's Azure resources in a time window, across a
scope mode (workload / workload+dependencies / tenant-wide), and return a tab-ready run. Runs
are persisted per (tenant, workload) with history + trash. Admin-gated. No writes to Azure.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.changeexplorer import demo, export as export_mod, runs as runs_store, service
from app.core.security import Principal, require_admin

router = APIRouter(prefix="/changeexplorer", tags=["changeexplorer"])
log = logging.getLogger("app.api.changeexplorer")


def _resolve(workload_id: str, connection_id: str | None):
    """Resolve (workload_dict, connection_dict|None). The demo workload short-circuits to a
    synthetic definition; real workloads come from the registry with their own connection."""
    if demo.is_demo(workload_id):
        return demo.demo_workload(), None
    from app.core.azure_connections import connection_for_workload, resolve_connection
    from app.workloads.registry import get_workload

    wl = get_workload(workload_id)
    if wl is None:
        return None, None
    conn = resolve_connection(connection_id) if connection_id else connection_for_workload(wl)
    return wl, conn


def _resolve_subscription(sub_id: str, sub_name: str, connection_id: str | None):
    """Resolve a SUBSCRIPTION scope into a synthetic single-subscription 'workload' + the Azure
    connection to query it with (the selected connection, else the default — the same one the
    subscription picker uses). Mirrors how the Performance Profiler scopes a subscription."""
    from app.core.azure_connections import get_default_connection, resolve_connection

    conn = resolve_connection(connection_id) if connection_id else get_default_connection()
    wl = {
        "id": f"sub:{sub_id}",
        "name": sub_name or f"Subscription {sub_id[:8]}…",
        "nodes": [{"kind": "subscription", "id": sub_id, "subscription_id": sub_id}],
    }
    return wl, conn


@router.get("/workloads")
async def list_workloads(_: Principal = Depends(require_admin)):
    """Workloads for the picker — the demo scenario first, then the registry's workloads."""
    from app.workloads.registry import list_workloads as reg_list

    out = [{"id": demo.DEMO_WORKLOAD_ID, "name": f"{demo.DEMO_WORKLOAD_NAME} (demo)", "demo": True,
            "connection_id": ""}]
    for w in reg_list():
        out.append({"id": w.get("id", ""), "name": w.get("name", ""), "demo": False,
                    "connection_id": w.get("connection_id", "")})
    return {"workloads": out}


class AnalyzeReq(BaseModel):
    workload_id: str = ""
    subscription_id: str = ""
    subscription_name: str = ""
    connection_id: str = ""
    start_time: str = ""
    end_time: str = ""
    scope_mode: str = "workload"   # workload | workload_dependencies | tenant


def _resolve_scope(req: "AnalyzeReq"):
    """Resolve an analyze request to (workload, connection, run_key, force_demo)."""
    if req.subscription_id and not req.workload_id:
        workload, conn = _resolve_subscription(req.subscription_id, req.subscription_name, req.connection_id or None)
        return workload, conn, f"sub:{req.subscription_id}", False
    workload, conn = _resolve(req.workload_id, req.connection_id or None)
    return workload, conn, req.workload_id, demo.is_demo(req.workload_id)


@router.post("/analyze")
async def analyze(req: AnalyzeReq, principal: Principal = Depends(require_admin)):
    """Run a change analysis and persist it. Returns the full tab-ready run. Scopes to either a
    workload (``workload_id``) or a subscription (``subscription_id``), matching the unified
    scope picker."""
    workload, conn, run_key, force_demo = _resolve_scope(req)
    if workload is None:
        raise HTTPException(status_code=404, detail="Workload not found.")
    scope_mode = req.scope_mode if req.scope_mode in ("workload", "workload_dependencies", "tenant") else "workload"
    actor = principal.display_name or principal.email or principal.subject
    run = await service.analyze(
        tenant_id=principal.tenant_id, workload=workload, connection=conn,
        start_iso=req.start_time, end_iso=req.end_time, scope_mode=scope_mode,
        requested_by=actor, force_demo=force_demo,
    )
    runs_store.save_run(principal.tenant_id, run_key, run)
    return run


@router.post("/analyze/stream")
async def analyze_stream(req: AnalyzeReq, principal: Principal = Depends(require_admin)):
    """Live change analysis over SSE: ``start`` → ``progress*`` (collecting / classifying / AI
    analyzing) → ``done`` (the full run). The run is persisted before ``done`` is emitted, so it
    survives even if the browser disconnects mid-stream (the client reloads it from history)."""
    workload, conn, run_key, force_demo = _resolve_scope(req)
    if workload is None:
        raise HTTPException(status_code=404, detail="Workload not found.")
    scope_mode = req.scope_mode if req.scope_mode in ("workload", "workload_dependencies", "tenant") else "workload"
    actor = principal.display_name or principal.email or principal.subject
    tenant_id = principal.tenant_id

    async def _gen():
        try:
            yield {"event": "start", "data": json.dumps({"workloadName": workload.get("name", ""), "scopeMode": scope_mode})}
            async for ev in service.analyze_stream(
                tenant_id=tenant_id, workload=workload, connection=conn,
                start_iso=req.start_time, end_iso=req.end_time, scope_mode=scope_mode,
                requested_by=actor, force_demo=force_demo,
            ):
                if ev.get("phase") == "done":
                    run = ev["run"]
                    runs_store.save_run(tenant_id, run_key, run)
                    yield {"event": "done", "data": json.dumps(run)}
                else:
                    yield {"event": "progress", "data": json.dumps(ev)}
        except Exception as exc:  # noqa: BLE001
            log.exception("change analysis stream failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


@router.get("/runs")
async def list_runs(workload_id: str, principal: Principal = Depends(require_admin)):
    return {"runs": runs_store.list_runs(principal.tenant_id, workload_id)}


@router.get("/runs/trash")
async def list_trash(workload_id: str, principal: Principal = Depends(require_admin)):
    return {"runs": runs_store.list_trashed(principal.tenant_id, workload_id)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, principal: Principal = Depends(require_admin)):
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str, principal: Principal = Depends(require_admin)):
    return {"deleted": runs_store.soft_delete(principal.tenant_id, run_id)}


@router.post("/runs/{run_id}/restore")
async def restore_run(run_id: str, principal: Principal = Depends(require_admin)):
    return {"restored": runs_store.restore(principal.tenant_id, run_id)}


@router.delete("/runs/{run_id}/purge")
async def purge_run(run_id: str, principal: Principal = Depends(require_admin)):
    return {"purged": runs_store.purge(principal.tenant_id, run_id)}


@router.get("/runs/{run_id}/export")
async def export_run(run_id: str, format: str = "csv", principal: Principal = Depends(require_admin)):
    """Export a run. ``format`` = csv | csv_high | json | exec | technical | rca | servicenow | queries."""
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    events = run.get("events", [])
    name = (run.get("workloadName", "changes") or "changes").replace(" ", "_")
    if format == "csv":
        return {"filename": f"{name}_changes.csv", "mime": "text/csv", "content": export_mod.to_csv(events)}
    if format == "csv_high":
        return {"filename": f"{name}_high_risk.csv", "mime": "text/csv", "content": export_mod.to_csv(events, high_risk_only=True)}
    if format == "json":
        return {"filename": f"{name}_run.json", "mime": "application/json", "content": export_mod.to_json(run)}
    if format == "exec":
        return {"filename": f"{name}_executive.md", "mime": "text/markdown", "content": export_mod.executive_summary(run)}
    if format == "technical":
        return {"filename": f"{name}_technical.md", "mime": "text/markdown", "content": export_mod.technical_summary(run)}
    if format == "rca":
        return {"filename": f"{name}_rca.md", "mime": "text/markdown", "content": export_mod.rca_summary(run)}
    if format == "servicenow":
        return {"filename": f"{name}_servicenow.txt", "mime": "text/plain", "content": export_mod.servicenow_text(run)}
    if format == "queries":
        return {"queries": export_mod.validation_queries(run)}
    raise HTTPException(status_code=400, detail="Unknown export format.")

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
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.changeexplorer import (
    compare as compare_mod,
    demo,
    export as export_mod,
    nlquery,
    runs as runs_store,
    service,
)
from app.core.security import Principal, require_permission

router = APIRouter(prefix="/changeexplorer", tags=["changeexplorer"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("changeexplorer.read")
log = logging.getLogger("app.api.changeexplorer")


def _light_run(run: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of a run with the heavy per-event ``rawEventJson`` stripped (P2), so
    the streamed / fetched payload is small. The drawer lazy-loads raw JSON on demand. The stored
    run (and exports) keep the full data."""
    out = dict(run)
    out["events"] = [{**e, "rawEventJson": None, "_hasRaw": bool(e.get("rawEventJson"))}
                     for e in (run.get("events") or [])]
    return out


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


@router.get("/fleet")
async def fleet(principal: Principal = Depends(require_admin)):
    """Summarize the LATEST change-analysis run for EVERY active workload — drives the Fleet
    view's mass overview + mass-launch grid. Reads stored runs only (no analysis)."""
    import time
    from datetime import datetime

    from app.workloads.registry import list_workloads as reg_list

    tenant_id = principal.tenant_id
    workloads = reg_list()
    latest = runs_store.latest_runs_for_workloads(tenant_id, [w.get("id", "") for w in workloads])

    rows: list[dict[str, Any]] = []
    for w in workloads:
        summ = latest.get(w.get("id", ""))
        run_at = (summ or {}).get("completedAt") or (summ or {}).get("createdAt") or ""
        age: float | None = None
        if run_at:
            try:
                dt = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
                age = max(0.0, time.time() - dt.timestamp())
            except (ValueError, TypeError):
                age = None
        rows.append({
            "workload_id": w.get("id", ""),
            "name": w.get("name", ""),
            "connection_id": w.get("connection_id", ""),
            "environment": w.get("environment", ""),
            "has_runs": summ is not None,
            "run_id": (summ or {}).get("runId", ""),
            "run_at": run_at,
            "start_time": (summ or {}).get("startTime", ""),
            "end_time": (summ or {}).get("endTime", ""),
            "scope_mode": (summ or {}).get("scopeMode", ""),
            "status": (summ or {}).get("status", ""),
            "total_changes": (summ or {}).get("totalChanges", 0),
            "critical_count": (summ or {}).get("criticalCount", 0),
            "high_count": (summ or {}).get("highCount", 0),
            "medium_count": (summ or {}).get("mediumCount", 0),
            "low_count": (summ or {}).get("lowCount", 0),
            "informational_count": (summ or {}).get("informationalCount", 0),
            "demo": (summ or {}).get("demo", False),
            "age_seconds": int(age) if age is not None else None,
        })
    # Worst first: never-analyzed last, then most critical, then most high, then most changes.
    rows.sort(
        key=lambda r: (
            not r["has_runs"],
            -(r["critical_count"] or 0),
            -(r["high_count"] or 0),
            -(r["total_changes"] or 0),
        )
    )
    return {
        "workloads": rows,
        "total": len(rows),
        "analyzed": sum(1 for r in rows if r["has_runs"]),
    }


class AnalyzeReq(BaseModel):
    workload_id: str = ""
    subscription_id: str = ""
    subscription_name: str = ""
    connection_id: str = ""
    start_time: str = ""
    end_time: str = ""
    scope_mode: str = "workload"   # workload | workload_dependencies | tenant
    run_ai: bool = True            # when False, skip the (slow) AI pass — run it later on demand


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
        requested_by=actor, force_demo=force_demo, run_ai=req.run_ai,
    )
    runs_store.save_run(principal.tenant_id, run_key, run)
    return _light_run(run)


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
                requested_by=actor, force_demo=force_demo, run_ai=req.run_ai,
            ):
                if ev.get("phase") == "done":
                    run = ev["run"]
                    runs_store.save_run(tenant_id, run_key, run)
                    yield {"event": "done", "data": json.dumps(_light_run(run))}
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
async def get_run(run_id: str, light: bool = True, principal: Principal = Depends(require_admin)):
    """Fetch a run. ``light=True`` (default) strips the heavy per-event ``rawEventJson`` blob from
    the events so the initial payload is small + fast (P2); the raw JSON is fetched on demand via
    ``/runs/{id}/changes/{changeId}/raw`` when a reviewer expands it. ``light=False`` returns the
    full run (used by exports that embed raw data)."""
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if not light:
        return run
    # Shallow-copy + strip rawEventJson from each event (keeps the stored run intact).
    out = dict(run)
    out["events"] = [{**e, "rawEventJson": None, "_hasRaw": bool(e.get("rawEventJson"))} for e in (run.get("events") or [])]
    return out


@router.get("/runs/{run_id}/changes/{change_id}/raw")
async def get_change_raw(run_id: str, change_id: str, principal: Principal = Depends(require_admin)):
    """Lazy-load the raw event JSON for a single change (P2) — only fetched when the drawer's
    'Raw event JSON' section is expanded."""
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    for e in run.get("events") or []:
        if e.get("changeId") == change_id:
            return {"rawEventJson": e.get("rawEventJson")}
    raise HTTPException(status_code=404, detail="Change not found.")


@router.post("/runs/{run_id}/ai-enrich")
async def ai_enrich_run(run_id: str, principal: Principal = Depends(require_admin)):
    """Run the AI enrichment pass over an already-persisted run that was analyzed without AI
    (the 'Perform AI analysis' checkbox was off). Re-sharpens narrative + risk, rebuilds every
    derived view, persists the updated run, and returns it. Read-only w.r.t. Azure."""
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run.get("aiAnalyzed"):
        return _light_run(run)  # already enriched — no-op
    final: dict[str, Any] = run
    async for ev in service.ai_enrich_run(run):
        if ev.get("phase") == "done":
            final = ev["run"]
    runs_store.update_run(principal.tenant_id, final)
    return _light_run(final)


@router.post("/runs/{run_id}/ai-enrich/stream")
async def ai_enrich_run_stream(run_id: str, principal: Principal = Depends(require_admin)):
    """Streaming variant of ``ai-enrich`` over SSE: ``progress*`` → ``done`` (the updated run).
    The enriched run is persisted before ``done`` is emitted."""
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    tenant_id = principal.tenant_id

    async def _gen():
        try:
            if run.get("aiAnalyzed"):
                yield {"event": "done", "data": json.dumps(_light_run(run))}
                return
            async for ev in service.ai_enrich_run(run):
                if ev.get("phase") == "done":
                    updated = ev["run"]
                    runs_store.update_run(tenant_id, updated)
                    yield {"event": "done", "data": json.dumps(_light_run(updated))}
                else:
                    yield {"event": "progress", "data": json.dumps(ev)}
        except Exception as exc:  # noqa: BLE001
            log.exception("change AI enrichment stream failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


class AskReq(BaseModel):
    question: str
    run_id: str = ""
    workload_id: str = ""


@router.post("/ask")
async def ask(req: AskReq, principal: Principal = Depends(require_admin)):
    """Natural-language change search. Parses a question like 'show me all VMs modified yesterday'
    into a structured filter spec (time window resolved deterministically; the rest AI-grounded
    against the run's facets), applies it to the loaded run's events, and returns the matches.

    When the parsed time window falls OUTSIDE the loaded run's window, no events are returned and
    a ``suggested_window`` is surfaced so the client can offer a one-click re-scan. Read-only."""
    from datetime import datetime, timezone

    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required.")
    run = runs_store.get_run(principal.tenant_id, req.run_id) if req.run_id else None
    if run is None and req.workload_id:
        hist = runs_store.list_runs(principal.tenant_id, req.workload_id)
        if hist:
            run = runs_store.get_run(principal.tenant_id, hist[0]["runId"])
    if run is None:
        return {"available": False, "answer": "Run a change analysis first, then ask about it."}

    facets = run.get("facets", {}) or {}
    spec = await nlquery.parse_query(req.question, now=datetime.now(timezone.utc), facets=facets)
    window = spec.get("time_window")
    in_window = nlquery.window_in_run(window, run.get("startTime", ""), run.get("endTime", ""))
    matched = nlquery.apply_spec(run.get("events", []) or [], spec) if in_window else []
    return {
        "available": True,
        "spec": spec,
        "matched_ids": [e.get("changeId", "") for e in matched],
        "match_count": len(matched),
        "in_window": in_window,
        "suggested_window": (window if not in_window else None),
        "run_id": run.get("runId", ""),
        "explanation": spec.get("explanation", ""),
    }


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


@router.get("/runs/{run_id}/report.pdf")
async def report_pdf(run_id: str, principal: Principal = Depends(require_admin)):
    """Board-ready incident-report PDF for a run (E1)."""
    from starlette.concurrency import run_in_threadpool

    from app.changeexplorer.pdf_report import build_change_report_pdf

    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    pdf = await run_in_threadpool(build_change_report_pdf, run)
    name = (run.get("workloadName", "changes") or "changes").replace(" ", "_")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}_incident_report.pdf"'})


@router.get("/runs/{run_id}/compare/{other_id}")
async def compare_runs_endpoint(run_id: str, other_id: str, principal: Principal = Depends(require_admin)):
    """Compare two runs (E2). ``run_id`` is the baseline (A), ``other_id`` the later run (B)."""
    a = runs_store.get_run(principal.tenant_id, run_id)
    b = runs_store.get_run(principal.tenant_id, other_id)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="One or both runs not found.")
    return compare_mod.compare_runs(a, b)


class CaseUpdate(BaseModel):
    pinned: list[str] | None = None
    notes: dict[str, str] | None = None
    case_summary: str | None = None


@router.post("/runs/{run_id}/case")
async def set_case(run_id: str, req: CaseUpdate, principal: Principal = Depends(require_admin)):
    """Persist the investigator case file for a run (D1): pinned change ids + per-change notes +
    a case summary. Partial updates merge with what's stored."""
    payload: dict[str, Any] = {}
    if req.pinned is not None:
        payload["pinned"] = req.pinned
    if req.notes is not None:
        payload["notes"] = req.notes
    if req.case_summary is not None:
        payload["caseSummary"] = req.case_summary
    saved = runs_store.set_case(principal.tenant_id, run_id, payload)
    if saved is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"caseFile": saved}

"""FMEA (Failure Mode and Effects Analysis) API.

A standalone router (prefix ``/fmea``) that mirrors the Know-Me endpoints: an index for the
FMEA page, CRUD by ``fmea_id``, soft-delete Trash (delete/restore/purge/empty), revision
history, CSV/Markdown export, and AI generation (whole document + per-table) streamed over
SSE by transforming an architecture's Memory. Tenant-scoped; admin-write via the existing
``architectures.*`` permissions so it slots into the current role model.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.architectures import registry as arch_registry
from app.core.genjob import JobRegistry
from app.core.security import Principal, require_permission
from app.fmea import compute
from app.fmea import registry as freg
from app.workloads.registry import get_workload

logger = logging.getLogger("app.api.fmea")

router = APIRouter(prefix="/fmea", tags=["fmea"])
get_principal = require_permission("architectures.read")
_write = require_permission("architectures.write")

# Background-survivable FMEA generation (mirrors Know-Me): generate / regenerate / per-table
# regen run as detached jobs keyed by document so navigating away never loses the draft.
_fmea_jobs = JobRegistry("fmea")


def _actor(principal: Principal) -> str:
    return principal.display_name or principal.email or principal.subject


def _arch_or_404(architecture_id: str, principal: Principal, *, include_deleted: bool = False) -> dict[str, Any]:
    arch = arch_registry.get_architecture(architecture_id, include_deleted=include_deleted)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    arch_tenant = arch.get("tenant_id") or ""
    if arch_tenant and arch_tenant != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return arch


def _fmea_or_404(fmea_id: str, principal: Principal, *, allow_deleted: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch an FMEA by id + its source architecture, tenant-checked. Raises 404 otherwise.
    Returns (doc, arch). ``arch`` may be {} if the architecture was deleted."""
    doc = freg.get_fmea(fmea_id)
    if doc is None or (doc.get("tenant_id") or "") not in ("", principal.tenant_id):
        raise HTTPException(status_code=404, detail="FMEA not found.")
    if doc.get("deleted_at") and not allow_deleted:
        raise HTTPException(status_code=404, detail="This FMEA is in the Trash.")
    arch = arch_registry.get_architecture(doc.get("architecture_id", ""), include_deleted=True) or {}
    return doc, arch


def _fmea_response(arch: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc.get("id"),
        "fmea": doc,
        "summary": compute.summarize(doc),
        "architecture": {
            "id": doc.get("architecture_id", ""),
            "name": arch.get("name", ""),
            "workload_id": arch.get("workload_id", ""),
            "workload_name": arch.get("workload_name", "") or doc.get("workload_name", ""),
            "updated_at": arch.get("updated_at", ""),
        },
    }


# ------------------------------------------------------------------------- models
class FmeaRowIn(BaseModel):
    id: str | None = None
    item: str = ""
    function: str = ""
    failure_mode: str = ""
    effects: str = ""
    causes: str = ""
    control_prevention: str = ""
    control_detection: str = ""
    recommended_actions: str = ""
    owner: str = ""
    date_due: str = ""
    action_results: str = ""
    date_completed: str = ""
    severity: int | None = 0
    occurrence: int | None = 0
    detection: int | None = 0
    severity_post: int | None = 0
    occurrence_post: int | None = 0
    detection_post: int | None = 0


class FmeaTableIn(BaseModel):
    id: str | None = None
    name: str = "Untitled table"
    scope_ref: str = ""
    rows: list[FmeaRowIn] = []


class FmeaUpsert(BaseModel):
    title: str | None = None
    scope_note: str | None = None
    tables: list[FmeaTableIn] | None = None
    status: str | None = None


class FmeaCreate(BaseModel):
    architecture_id: str
    title: str = ""
    scope_note: str = ""


class FmeaGenerateRequest(BaseModel):
    architecture_id: str | None = None
    extra_context: str = ""
    focus: str = ""


# ------------------------------------------------------------------------- index + trash
@router.get("")
async def list_fmea_endpoint(principal: Principal = Depends(get_principal)):
    """Index for the FMEA page: every FMEA document (drafts + published), the architectures
    that have a Memory (so the UI can offer 'create a new FMEA'), and the trash count."""
    from app.architectures import memory as mem

    archs = {a["id"]: a for a in arch_registry.list_architectures(principal.tenant_id)}
    memories = {m.get("architecture_id", "") for m in mem.list_memories(principal.tenant_id)}

    def _doc(doc: dict[str, Any]) -> dict[str, Any]:
        aid = doc.get("architecture_id", "")
        arch = archs.get(aid)
        summary = compute.summarize(doc)
        return {
            "id": doc.get("id"),
            "architecture_id": aid,
            "architecture_name": (arch or {}).get("name", "") or "(deleted architecture)",
            "architecture_exists": arch is not None,
            "workload_id": doc.get("workload_id", "") or (arch or {}).get("workload_id", ""),
            "workload_name": (arch or {}).get("workload_name", "") or doc.get("workload_name", ""),
            "title": doc.get("title", ""),
            "status": doc.get("status", "draft"),
            "source": doc.get("source", ""),
            "table_count": len(doc.get("tables", []) or []),
            "row_count": summary["total_rows"],
            "top_rpn": summary["top_rpn"],
            "counts": summary["counts"],
            "updated_at": doc.get("updated_at", ""),
            "updated_by": doc.get("updated_by", ""),
        }

    all_docs = freg.list_fmea(principal.tenant_id)
    documents = [_doc(d) for d in all_docs]
    by_arch: dict[str, int] = {}
    for d in documents:
        by_arch[d["architecture_id"]] = by_arch.get(d["architecture_id"], 0) + 1
    buildable: list[dict[str, Any]] = []
    for aid in memories:
        arch = archs.get(aid)
        buildable.append({
            "architecture_id": aid,
            "architecture_name": (arch or {}).get("name", "") or "(deleted architecture)",
            "architecture_exists": arch is not None,
            "workload_id": (arch or {}).get("workload_id", ""),
            "workload_name": (arch or {}).get("workload_name", ""),
            "fmea_count": by_arch.get(aid, 0),
        })
    buildable.sort(key=lambda r: (r["workload_name"].lower() or r["architecture_name"].lower()))
    documents.sort(key=lambda r: r["updated_at"], reverse=True)
    trash_count = len(freg.list_fmea(principal.tenant_id, only_deleted=True))
    return {"documents": documents, "buildable": buildable, "trash_count": trash_count}


@router.get("/trash")
async def list_fmea_trash_endpoint(principal: Principal = Depends(get_principal)):
    """List soft-deleted FMEA documents (the Trash)."""
    archs = {a["id"]: a for a in arch_registry.list_architectures(principal.tenant_id, include_deleted=True)}
    out: list[dict[str, Any]] = []
    for d in freg.list_fmea(principal.tenant_id, only_deleted=True):
        arch = archs.get(d.get("architecture_id", ""))
        out.append({
            "id": d.get("id"),
            "architecture_id": d.get("architecture_id", ""),
            "workload_name": (arch or {}).get("workload_name", "") or d.get("workload_name", ""),
            "title": d.get("title", ""),
            "status": d.get("status", "draft"),
            "deleted_at": d.get("deleted_at", ""),
            "deleted_by": d.get("deleted_by", ""),
            "updated_at": d.get("updated_at", ""),
        })
    return {"items": out}


@router.post("/trash/empty")
async def empty_fmea_trash_endpoint(principal: Principal = Depends(_write)):
    return {"purged": freg.empty_trash(principal.tenant_id)}


# ------------------------------------------------------------------------- create + CRUD
@router.post("")
async def create_fmea_endpoint(payload: FmeaCreate, principal: Principal = Depends(_write)):
    """Create a NEW (empty draft) FMEA document for an architecture."""
    arch = _arch_or_404(payload.architecture_id, principal)
    doc = freg.create_fmea(
        architecture_id=payload.architecture_id,
        workload_id=arch.get("workload_id", ""),
        workload_name=arch.get("workload_name", ""),
        connection_id=arch.get("connection_id", ""),
        title=payload.title or "",
        scope_note=payload.scope_note or "",
        tenant_id=principal.tenant_id,
        actor=_actor(principal),
    )
    return _fmea_response(arch, doc)


@router.get("/{fmea_id}")
async def get_fmea_endpoint(fmea_id: str, principal: Principal = Depends(get_principal)):
    """Return one FMEA document by id + risk summary. Includes ``has_memory`` /
    ``memory_updated_at`` so the UI can flag a stale FMEA whose source Memory changed."""
    from app.architectures import memory as mem

    doc, arch = _fmea_or_404(fmea_id, principal, allow_deleted=True)
    memory = mem.get_memory(doc.get("architecture_id", ""))
    return {
        **_fmea_response(arch, doc),
        "has_memory": memory is not None,
        "memory_updated_at": (memory or {}).get("updated_at", ""),
    }


@router.put("/{fmea_id}")
async def upsert_fmea_endpoint(fmea_id: str, payload: FmeaUpsert, principal: Principal = Depends(_write)):
    """Save an FMEA (tables / title / scope_note / status) — snapshots a revision."""
    doc, arch = _fmea_or_404(fmea_id, principal)
    tables = [t.model_dump() for t in payload.tables] if payload.tables is not None else None
    saved = freg.update_fmea(
        fmea_id,
        title=payload.title,
        scope_note=payload.scope_note,
        tables=tables,
        status=payload.status,
        source="edited",
        tenant_id=principal.tenant_id,
        actor=_actor(principal),
    )
    return _fmea_response(arch, saved)


@router.delete("/{fmea_id}")
async def delete_fmea_endpoint(fmea_id: str, principal: Principal = Depends(_write)):
    """Move an FMEA to the Trash (soft-delete)."""
    _fmea_or_404(fmea_id, principal, allow_deleted=True)
    freg.soft_delete(fmea_id, _actor(principal))
    return {"ok": True}


@router.post("/{fmea_id}/restore")
async def restore_fmea_endpoint(fmea_id: str, principal: Principal = Depends(_write)):
    """Restore an FMEA from the Trash."""
    _fmea_or_404(fmea_id, principal, allow_deleted=True)
    restored = freg.restore(fmea_id)
    if restored is None:
        raise HTTPException(status_code=404, detail="Nothing to restore.")
    return {"ok": True, "fmea": restored}


@router.delete("/{fmea_id}/purge")
async def purge_fmea_endpoint(fmea_id: str, principal: Principal = Depends(_write)):
    """Permanently delete an FMEA (and its revisions)."""
    _fmea_or_404(fmea_id, principal, allow_deleted=True)
    freg.purge(fmea_id)
    return {"ok": True}


# ------------------------------------------------------------------------- revisions
@router.get("/{fmea_id}/revisions")
async def list_fmea_revisions_endpoint(fmea_id: str, principal: Principal = Depends(get_principal)):
    from app.fmea import revisions

    _fmea_or_404(fmea_id, principal, allow_deleted=True)
    return {"revisions": revisions.list_revisions(fmea_id)}


@router.post("/{fmea_id}/revisions/{revision_id}/restore")
async def restore_fmea_revision_endpoint(
    fmea_id: str, revision_id: str, principal: Principal = Depends(_write)
):
    doc, arch = _fmea_or_404(fmea_id, principal)
    restored = freg.restore_revision(fmea_id, revision_id, _actor(principal))
    if restored is None:
        raise HTTPException(status_code=404, detail="FMEA or revision not found.")
    return _fmea_response(arch, restored)


# ------------------------------------------------------------------------- export
_CSV_COLUMNS = [
    ("item", "System / Item / Process Step"), ("function", "Function"),
    ("failure_mode", "Potential Failure Mode"), ("effects", "Effects of Failure"),
    ("severity", "Severity"), ("causes", "Causes"), ("occurrence", "Occurrence"),
    ("control_prevention", "Current Controls — Prevention"),
    ("control_detection", "Current Controls — Detection"), ("detection", "Detection"),
    ("rpn", "RPN"), ("recommended_actions", "Recommended Actions"), ("owner", "Owner"),
    ("date_due", "Date Due"), ("action_results", "Action Results"),
    ("date_completed", "Date Completed"), ("severity_post", "Severity"),
    ("occurrence_post", "Occurrence"), ("detection_post", "Detection"),
    ("rpn_post", "RPN"),
]


@router.get("/{fmea_id}/export")
async def export_fmea_endpoint(fmea_id: str, format: str = "csv", principal: Principal = Depends(get_principal)):
    """Export the FMEA as a richly-formatted Excel workbook (format=xlsx) or a CSV
    (format=csv) — both mirror the on-screen worksheet layout."""
    from fastapi.responses import Response

    doc, arch = _fmea_or_404(fmea_id, principal, allow_deleted=True)
    wl_name = arch.get("workload_name", "") or doc.get("workload_name", "")
    base = re.sub(r"[^a-z0-9]+", "-", (doc.get("title") or wl_name or "fmea").lower()).strip("-") or "fmea"

    if format == "xlsx":
        from app.fmea import excel as fexcel

        data = await asyncio.to_thread(fexcel.build_fmea_xlsx, doc, wl_name)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{base}-fmea.xlsx"'},
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([doc.get("title") or f"FMEA — {wl_name}"])
    for table in doc.get("tables", []) or []:
        writer.writerow([])
        writer.writerow([table.get("name", "Table")] + ([table.get("scope_ref", "")] if table.get("scope_ref") else []))
        writer.writerow([label for _key, label in _CSV_COLUMNS])
        for row in table.get("rows", []) or []:
            writer.writerow([_csv_cell(row.get(key)) for key, _label in _CSV_COLUMNS])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{base}-fmea.csv"'},
    )


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


# ------------------------------------------------------------------------- AI generation
async def _run_generate_fmea(
    progress: "Callable[[str, str], Awaitable[None]]",
    *, architecture_id: str, arch: dict[str, Any], principal: Principal,
    extra_context: str, focus: str, target_fmea_id: str | None, target_table_id: str | None = None,
) -> dict[str, Any]:
    """Background-job runner: transform an architecture's Memory into FMEA tables and PERSIST.
    Reports ``progress(phase, message)``; returns the full FMEA response (the job's ``done``
    payload). Runs detached from any SSE subscriber so it survives the browser navigating away."""
    from app.architectures import memory as mem
    from app.fmea import generator as fgen
    from app.knowme import context as kctx
    from app.knowme import sections as km

    workload_id = arch.get("workload_id") or ""
    connection_id = arch.get("connection_id") or ""
    tenant_id = principal.tenant_id

    memory = mem.get_memory(architecture_id)
    if memory is None or not any(str(s.get("content") or "").strip() for s in memory.get("sections", []) or []):
        raise ValueError("No architecture memory to transform. Generate the Memory first.")

    wl = get_workload(workload_id) if workload_id else None
    wl_name = arch.get("workload_name", "") or (wl or {}).get("name", "")
    await progress("scope", "🔎 Resolving the workload's Azure scope…")
    facts = km.scope_facts(wl, arch)
    n_subs = len(facts.get("subscriptions") or [])
    n_res = len(facts.get("resources") or [])
    n_rg = len(facts.get("resource_groups") or [])
    await progress("scope", f"🔎 Scope resolved: {n_res} resource(s) across {n_rg} resource group(s), {n_subs} subscription(s).")
    n_sections = sum(1 for s in memory.get("sections", []) or [] if str(s.get("content") or "").strip())
    await progress("memory", f"🧠 Reading the Architecture Memory ({n_sections} filled section(s))…")
    await progress("evidence", "📊 Pulling posture evidence (assessments, coverage)…")
    evidence = await kctx.gather_evidence(architecture_id, workload_id, tenant_id, connection_id)
    ev_assess = (evidence.get("assessment") or {})
    ev_findings = len(ev_assess.get("findings", []) or [])
    ev_cov = sorted((evidence.get("coverage") or {}).keys())
    ev_bits = []
    if ev_findings:
        ev_bits.append(f"{ev_findings} assessment finding(s)")
    if ev_cov:
        ev_bits.append(f"coverage: {', '.join(ev_cov)}")
    ev_msg = ("📊 Evidence gathered — " + "; ".join(ev_bits) + ".") if ev_bits else "📊 No prior posture evidence found — grounding on memory + scope only."
    await progress("evidence", ev_msg)

    await progress("ai", "🤖 Handing context to the model — enumerating failure modes & scoring risk…")
    queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    async def _gen_progress(phase: str, message: str) -> None:
        await queue.put({"phase": phase, "message": message})

    gen_task = asyncio.create_task(
        fgen.generate_fmea(
            workload_name=wl_name, memory=memory, facts=facts, progress=_gen_progress,
            extra_context=extra_context, evidence_block=evidence.get("block", ""), focus=focus,
        )
    )
    while not gen_task.done() or not queue.empty():
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=0.25)
        except asyncio.TimeoutError:
            continue
        await progress(ev.get("phase", "ai"), ev.get("message", ""))
    result = await gen_task
    if result is None or not result.get("tables"):
        raise RuntimeError("The AI could not draft the FMEA. Try again.")

    new_tables = result["tables"]
    n_tables = len(new_tables)
    n_rows = sum(len(t.get("rows", []) or []) for t in new_tables)
    conf = result.get("confidence")
    conf_txt = f" · confidence {round(float(conf) * 100)}%" if isinstance(conf, (int, float)) else ""
    await progress("save", f"💾 Recomputing RPN & saving {n_tables} table(s) · {n_rows} failure mode(s){conf_txt}…")
    ai_meta = {
        "confidence": result.get("confidence"),
        "passes": result.get("passes"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": _actor(principal),
    }

    if target_fmea_id:
        existing = freg.get_fmea(target_fmea_id) or {}
        tables = list(existing.get("tables", []) or [])
        if target_table_id:
            # Replace the rows of one specific table with the freshly-generated first table.
            gen_rows = (new_tables[0] or {}).get("rows", []) if new_tables else []
            for t in tables:
                if t.get("id") == target_table_id:
                    t["rows"] = gen_rows
                    break
        else:
            tables = new_tables
        doc = freg.update_fmea(
            target_fmea_id, workload_id=workload_id, workload_name=wl_name, connection_id=connection_id,
            tables=tables, source="hybrid", ai=ai_meta,
            tenant_id=tenant_id, actor=_actor(principal), reason="Generated with AI",
        )
    else:
        created = freg.create_fmea(
            architecture_id=architecture_id, workload_id=workload_id, workload_name=wl_name,
            connection_id=connection_id, tenant_id=tenant_id, actor=_actor(principal),
        )
        doc = freg.update_fmea(
            created["id"], workload_id=workload_id, workload_name=wl_name, connection_id=connection_id,
            tables=new_tables, source="ai", ai=ai_meta,
            tenant_id=tenant_id, actor=_actor(principal), reason="Generated with AI",
        )
    return _fmea_response(arch, doc)


@router.post("/generate/stream")
async def generate_fmea_stream_endpoint(
    payload: FmeaGenerateRequest = Body(default_factory=FmeaGenerateRequest),
    principal: Principal = Depends(_write),
):
    """Create a NEW FMEA for an architecture by transforming its Memory. Runs as a detached,
    navigation-surviving job; this call starts it (idempotently) and follows it via SSE → done."""
    if not payload.architecture_id:
        raise HTTPException(status_code=400, detail="architecture_id is required.")
    arch = _arch_or_404(payload.architecture_id, principal)
    extra_context = payload.extra_context or ""
    focus = payload.focus or ""
    key = f"new:{payload.architecture_id}"

    def _runner(progress):
        return _run_generate_fmea(
            progress, architecture_id=payload.architecture_id, arch=arch, principal=principal,
            extra_context=extra_context, focus=focus, target_fmea_id=None,
        )

    _fmea_jobs.start(key, _runner)
    return EventSourceResponse(_fmea_jobs.stream(key))


@router.post("/{fmea_id}/generate/stream")
async def regenerate_fmea_stream_endpoint(
    fmea_id: str,
    payload: FmeaGenerateRequest = Body(default_factory=FmeaGenerateRequest),
    principal: Principal = Depends(_write),
):
    """Regenerate an EXISTING FMEA document from its architecture's Memory. Detached,
    navigation-surviving job keyed by fmea_id; SSE follows it → done."""
    doc, arch = _fmea_or_404(fmea_id, principal)
    if not arch:
        raise HTTPException(status_code=400, detail="The source architecture no longer exists.")
    extra_context = payload.extra_context or ""
    focus = payload.focus or ""
    key = f"fmea:{fmea_id}"

    def _runner(progress):
        return _run_generate_fmea(
            progress, architecture_id=doc.get("architecture_id", ""), arch=arch, principal=principal,
            extra_context=extra_context, focus=focus, target_fmea_id=fmea_id,
        )

    _fmea_jobs.start(key, _runner)
    return EventSourceResponse(_fmea_jobs.stream(key))


@router.get("/{fmea_id}/generate/job")
async def fmea_generate_job_endpoint(fmea_id: str, principal: Principal = Depends(get_principal)):
    """Current generation-job status for an FMEA doc (for reconnect on page visit)."""
    return {"job": _fmea_jobs.public_job(_fmea_jobs.get_job(f"fmea:{fmea_id}"))}


@router.post("/{fmea_id}/tables/{table_id}/generate/stream")
async def regenerate_fmea_table_stream_endpoint(
    fmea_id: str, table_id: str,
    payload: FmeaGenerateRequest = Body(default_factory=FmeaGenerateRequest),
    principal: Principal = Depends(_write),
):
    """Regenerate the rows of ONE table from the architecture's Memory. SSE → done."""
    doc, arch = _fmea_or_404(fmea_id, principal)
    if not arch:
        raise HTTPException(status_code=400, detail="The source architecture no longer exists.")
    table = next((t for t in doc.get("tables", []) or [] if t.get("id") == table_id), None)
    if table is None:
        raise HTTPException(status_code=404, detail="Table not found.")
    focus = payload.focus or table.get("name", "") or table.get("scope_ref", "")
    extra_context = payload.extra_context or ""
    key = f"tbl:{fmea_id}:{table_id}"

    def _runner(progress):
        return _run_generate_fmea(
            progress, architecture_id=doc.get("architecture_id", ""), arch=arch, principal=principal,
            extra_context=extra_context, focus=focus,
            target_fmea_id=fmea_id, target_table_id=table_id,
        )

    _fmea_jobs.start(key, _runner)
    return EventSourceResponse(_fmea_jobs.stream(key))



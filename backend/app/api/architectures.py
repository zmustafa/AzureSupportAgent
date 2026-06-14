"""Architecture endpoints: CRUD over the registry, the manual-builder catalog, and AI
reverse-engineering of an application architecture from a workload's resource inventory.

Any authenticated user may manage architectures (tenant-scoped). Reverse-engineering runs
read-only Azure Resource Graph queries via the existing command runner.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.architectures import catalog
from app.architectures import registry as arch_registry
from app.core.azure_connections import resolve_connection
from app.core.security import Principal, get_principal
from app.workloads.registry import get_workload

router = APIRouter(prefix="/architectures", tags=["architectures"])
logger = logging.getLogger("app.api.architectures")


def _actor(principal: Principal) -> str:
    """A human-readable author label (display name → email → subject id) recorded as the
    creator/modifier and in the activity log, so users see who did what (not a raw id)."""
    return principal.display_name or principal.email or principal.subject


# --------------------------------------------------------------------------- models
class ArchNode(BaseModel):
    id: str
    arm_id: str = ""
    name: str = ""
    type: str = ""
    category: str = "other"
    layer: str = "shared"
    resource_group: str = ""
    subscription_id: str = ""
    location: str = ""
    sku: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    group_id: str = ""
    x: float = 0.0
    y: float = 0.0


class ArchEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str = ""
    kind: str = "depends_on"
    dashed: bool = False


class ArchGroup(BaseModel):
    id: str
    name: str = ""
    kind: str = "custom"
    color: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0


class ArchitectureUpsert(BaseModel):
    id: str | None = None
    name: str = Field(default="Architecture", max_length=200)
    description: str = Field(default="", max_length=4000)
    # None (not "") so a partial save (e.g. the canvas, which doesn't send these) leaves
    # the existing values untouched. The upsert merge skips None but treats "" as a real
    # value — so defaulting to "" here would clobber a workload link on every diagram save.
    # Use the dedicated /workload endpoint to set or clear the link.
    workload_id: str | None = None
    workload_name: str | None = None
    connection_id: str | None = None
    source: str = "manual"
    state: str | None = None
    category_id: str | None = None
    nodes: list[ArchNode] = Field(default_factory=list)
    edges: list[ArchEdge] = Field(default_factory=list)
    groups: list[ArchGroup] = Field(default_factory=list)
    ai: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- catalog
@router.get("/catalog")
async def get_catalog(_: Principal = Depends(get_principal)):
    return catalog.public_catalog()


# --------------------------------------------- AI generation jobs (background, async)
# NOTE: these /jobs routes MUST be declared before GET /{architecture_id} so that
# "/architectures/jobs" isn't captured as an architecture id.
class GenerateJobsRequest(BaseModel):
    workload_ids: list[str] = Field(default_factory=list)
    connection_id: str | None = None


@router.post("/jobs")
async def create_generation_jobs_endpoint(
    payload: GenerateJobsRequest, principal: Principal = Depends(get_principal)
):
    """Queue one background AI reverse-engineering job per selected workload. Returns the
    job records immediately; poll GET /architectures/jobs for live progress."""
    from app.architectures.jobs import manager

    if not payload.workload_ids:
        raise HTTPException(status_code=400, detail="Select at least one workload.")
    created: list[dict[str, Any]] = []
    for wid in payload.workload_ids:
        wl = get_workload(wid)
        if wl is None:
            continue
        job = manager.create(
            tenant_id=principal.tenant_id,
            workload_id=wid,
            workload_name=wl.get("name", "workload"),
            connection_id=payload.connection_id or wl.get("connection_id") or "",
            created_by=_actor(principal),
        )
        created.append(job)
    if not created:
        raise HTTPException(status_code=404, detail="None of the selected workloads were found.")
    return {"jobs": created, "queued": len(created)}


@router.get("/jobs")
async def list_generation_jobs_endpoint(principal: Principal = Depends(get_principal)):
    from app.architectures.jobs import manager

    return {"jobs": manager.list(principal.tenant_id)}


@router.post("/jobs/{job_id}/cancel")
async def cancel_generation_job_endpoint(job_id: str, principal: Principal = Depends(get_principal)):
    from app.architectures.jobs import manager

    if not manager.cancel(job_id, principal.tenant_id):
        raise HTTPException(status_code=404, detail="Job not found or already finished.")
    return {"ok": True}


@router.delete("/jobs/{job_id}")
async def dismiss_generation_job_endpoint(job_id: str, principal: Principal = Depends(get_principal)):
    from app.architectures.jobs import manager

    if not manager.dismiss(job_id, principal.tenant_id):
        raise HTTPException(status_code=404, detail="Job not found or still running.")
    return {"ok": True}


# ------------------------------------------------- Collections (Categories / Solutions)
# NOTE: declared before GET /{architecture_id} so "/architectures/collections" isn't
# captured as an architecture id.
class CollectionUpsert(BaseModel):
    id: str | None = None
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=2000)
    color: str = "#6b7280"
    icon: str = Field(default="📁", max_length=8)
    order: int | None = None


class ReorderRequest(BaseModel):
    ordered_ids: list[str] = Field(default_factory=list)


@router.get("/collections")
async def list_collections_endpoint(principal: Principal = Depends(get_principal)):
    from app.architectures import collections as coll_registry

    return {"collections": coll_registry.list_collections(principal.tenant_id)}


@router.put("/collections")
async def upsert_collection_endpoint(
    payload: CollectionUpsert, principal: Principal = Depends(get_principal)
):
    from app.architectures import collections as coll_registry

    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="A category name is required.")
    data = payload.model_dump(exclude_none=True)
    data["tenant_id"] = principal.tenant_id
    if not payload.id:
        data["created_by"] = principal.subject
    saved = coll_registry.upsert_collection(data)
    return {"collection": saved}


@router.post("/collections/reorder")
async def reorder_collections_endpoint(
    payload: ReorderRequest, _: Principal = Depends(get_principal)
):
    from app.architectures import collections as coll_registry

    coll_registry.reorder_collections(payload.ordered_ids)
    return {"ok": True}


@router.delete("/collections/{collection_id}")
async def delete_collection_endpoint(
    collection_id: str, principal: Principal = Depends(get_principal)
):
    from app.architectures import collections as coll_registry

    # Reassign member architectures to Uncategorized so none are orphaned.
    reassigned = arch_registry.clear_category(collection_id, _actor(principal))
    if not coll_registry.delete_collection(collection_id):
        raise HTTPException(status_code=404, detail="Category not found.")
    return {"ok": True, "reassigned": reassigned}


# --------------------------------------------------------------------------- CRUD
@router.get("")
async def list_architectures_endpoint(principal: Principal = Depends(get_principal)):
    return {"architectures": arch_registry.list_architectures(principal.tenant_id)}


# ----------------------------------------------------------------------------- Trash
# Declared BEFORE GET /{architecture_id} so "/architectures/trash" isn't captured as an
# architecture id. Soft-deleted architectures live here until restored or purged.
@router.get("/trash")
async def list_trashed_architectures_endpoint(principal: Principal = Depends(get_principal)):
    """Architectures currently in the Trash (soft-deleted, restorable)."""
    return {"architectures": arch_registry.list_trashed_architectures(principal.tenant_id)}


@router.post("/trash/empty")
async def empty_architecture_trash_endpoint(principal: Principal = Depends(get_principal)):
    """Permanently delete every architecture in the Trash (tenant-scoped)."""
    deleted = arch_registry.empty_architecture_trash(principal.tenant_id)
    return {"ok": True, "deleted": deleted}


# --------------------------------------------------------------- Architecture Memory
# Declared BEFORE GET /{architecture_id} so "/architectures/memories" and
# "/architectures/memory/catalog" aren't captured as an architecture id.
@router.get("/memory/catalog")
async def memory_catalog_endpoint(_: Principal = Depends(get_principal)):
    """The section catalog (groups + keys + labels + hints) — the single source of truth
    shared by the editor, the AI generator, and rendering."""
    from app.architectures import memory as mem

    return {"sections": mem.SECTION_CATALOG, "default_keys": mem.DEFAULT_SECTION_KEYS}


@router.get("/memories")
async def list_memories_endpoint(principal: Principal = Depends(get_principal)):
    """All architecture memories (tenant-scoped), each joined to its architecture name +
    workload for the standalone Memory index."""
    from app.architectures import memory as mem

    archs = {a["id"]: a for a in arch_registry.list_architectures(principal.tenant_id)}
    out: list[dict[str, Any]] = []
    for m in mem.list_memories(principal.tenant_id):
        arch = archs.get(m.get("architecture_id", ""))
        sections = m.get("sections", []) or []
        filled = sum(1 for s in sections if str(s.get("content") or "").strip())
        out.append({
            "id": m.get("id"),
            "architecture_id": m.get("architecture_id"),
            "architecture_name": (arch or {}).get("name", "") or "(deleted architecture)",
            "architecture_exists": arch is not None,
            "workload_id": m.get("workload_id", "") or (arch or {}).get("workload_id", ""),
            "workload_name": (arch or {}).get("workload_name", ""),
            "title": m.get("title", ""),
            "section_count": len(sections),
            "filled_count": filled,
            "enabled_for_investigations": m.get("enabled_for_investigations", True),
            "source": m.get("source", "manual"),
            "updated_at": m.get("updated_at", ""),
            "updated_by": m.get("updated_by", ""),
        })
    return {"memories": out}


@router.get("/{architecture_id}")
async def get_architecture_endpoint(architecture_id: str, _: Principal = Depends(get_principal)):
    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"architecture": arch}


@router.put("")
async def upsert_architecture_endpoint(
    payload: ArchitectureUpsert, principal: Principal = Depends(get_principal)
):
    data = payload.model_dump()
    data["tenant_id"] = principal.tenant_id
    if not payload.id:
        data["created_by"] = _actor(principal)
    saved = arch_registry.upsert_architecture(data, actor=_actor(principal), reason="Edited")
    return {"architecture": saved}


@router.delete("/{architecture_id}")
async def delete_architecture_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    """Soft-delete: move the architecture to the Trash (restorable until purged)."""
    if not arch_registry.delete_architecture(architecture_id, actor=_actor(principal)):
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"ok": True}


@router.post("/{architecture_id}/restore")
async def restore_architecture_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    """Restore a trashed architecture back into the active list."""
    arch = arch_registry.restore_architecture(architecture_id, actor=_actor(principal))
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not in trash.")
    return {"architecture": arch}


@router.delete("/{architecture_id}/purge")
async def purge_architecture_endpoint(architecture_id: str, _: Principal = Depends(get_principal)):
    """Permanently delete a single trashed architecture (hard delete)."""
    arch = arch_registry.get_architecture(architecture_id, include_deleted=True)
    if arch is None or not arch.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Architecture not in trash.")
    arch_registry.purge_architecture(architecture_id)
    return {"ok": True}


# ----------------------------------------------------- lightweight state/category setters
# These update a SINGLE field via read-modify-write so a list-card quick action can't
# clobber the diagram (the full PUT always serializes nodes/edges).
class StateUpdate(BaseModel):
    state: str


class CategoryUpdate(BaseModel):
    category_id: str = ""


@router.post("/{architecture_id}/state")
async def set_architecture_state_endpoint(
    architecture_id: str, payload: StateUpdate, principal: Principal = Depends(get_principal)
):
    try:
        saved = arch_registry.set_state(architecture_id, payload.state, _actor(principal))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"architecture": saved}


@router.post("/{architecture_id}/category")
async def set_architecture_category_endpoint(
    architecture_id: str, payload: CategoryUpdate, principal: Principal = Depends(get_principal)
):
    saved = arch_registry.set_category(architecture_id, payload.category_id, _actor(principal))
    if saved is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"architecture": saved}


class WorkloadUpdate(BaseModel):
    workload_id: str = ""


@router.post("/{architecture_id}/workload")
async def set_architecture_workload_endpoint(
    architecture_id: str, payload: WorkloadUpdate, principal: Principal = Depends(get_principal)
):
    """Link (or unlink) the architecture to a workload. An empty workload_id unlinks it;
    the diagram is never modified — only the association changes."""
    workload_name = ""
    if payload.workload_id:
        wl = get_workload(payload.workload_id)
        if wl is None:
            raise HTTPException(status_code=404, detail="Workload not found.")
        workload_name = wl.get("name", "")
    saved = arch_registry.set_workload(
        architecture_id, payload.workload_id, workload_name, _actor(principal)
    )
    if saved is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"architecture": saved}


class RebuildRequest(BaseModel):
    # Optional override; defaults to the architecture's currently-linked workload.
    workload_id: str | None = None
    connection_id: str | None = None


@router.post("/{architecture_id}/rebuild")
async def rebuild_architecture_endpoint(
    architecture_id: str, payload: RebuildRequest, principal: Principal = Depends(get_principal)
):
    """Queue a background job that re-reverse-engineers this architecture from the current
    Azure state of a workload, overwriting its diagram in place (id/name/state preserved).
    Poll GET /architectures/jobs for live progress; the job's target_architecture_id is this id."""
    from app.architectures.jobs import manager

    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    workload_id = payload.workload_id or arch.get("workload_id") or ""
    if not workload_id:
        raise HTTPException(
            status_code=400,
            detail="This architecture isn't linked to a workload. Pick one to rebuild from.",
        )
    wl = get_workload(workload_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="Workload not found.")
    # Keep the link in sync if the caller rebuilt from a different workload.
    if workload_id != arch.get("workload_id"):
        arch_registry.set_workload(
            architecture_id, workload_id, wl.get("name", ""), _actor(principal)
        )
    job = manager.create(
        tenant_id=principal.tenant_id,
        workload_id=workload_id,
        workload_name=wl.get("name", "workload"),
        connection_id=payload.connection_id or wl.get("connection_id") or "",
        created_by=_actor(principal),
        target_architecture_id=architecture_id,
    )
    return {"job": job}


# ------------------------------------------------------------- Architecture Memory (CRUD)
class MemorySectionIn(BaseModel):
    key: str
    label: str = ""
    content: str = ""
    needs_review: bool = False


class MemoryUpsert(BaseModel):
    title: str = ""
    sections: list[MemorySectionIn] | None = None
    enabled_for_investigations: bool | None = None


class MemoryGenerateRequest(BaseModel):
    # Optional operator-provided grounding (a pasted runbook, RCA, or notes) folded into
    # the AI draft as authoritative context.
    extra_context: str = ""


class MemorySectionGenerateRequest(BaseModel):
    extra_context: str = ""


def _arch_meta(architecture_id: str, arch: dict[str, Any]) -> dict[str, Any]:
    """The architecture summary block returned alongside a memory (incl. timestamps so
    the UI can detect a stale memory generated before the diagram last changed)."""
    return {
        "id": architecture_id,
        "name": arch.get("name", ""),
        "workload_id": arch.get("workload_id", ""),
        "workload_name": arch.get("workload_name", ""),
        "updated_at": arch.get("updated_at", ""),
        "ai": arch.get("ai") or None,
    }


def _memory_response(architecture_id: str, arch: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
    from app.architectures import memory as mem

    return {
        "memory": memory,
        "markdown": mem.render_markdown(
            memory, arch.get("name", ""), arch.get("workload_name", "")
        ),
        "architecture": _arch_meta(architecture_id, arch),
    }


@router.get("/{architecture_id}/memory")
async def get_memory_endpoint(architecture_id: str, _: Principal = Depends(get_principal)):
    """Return the architecture's memory (or null if none exists yet) + rendered markdown."""
    from app.architectures import memory as mem

    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    memory = mem.get_memory(architecture_id)
    if memory is None:
        return {"memory": None, "markdown": "", "architecture": _arch_meta(architecture_id, arch)}
    return _memory_response(architecture_id, arch, memory)


@router.put("/{architecture_id}/memory")
async def upsert_memory_endpoint(
    architecture_id: str, payload: MemoryUpsert, principal: Principal = Depends(get_principal)
):
    """Create or update the architecture's memory (sections / title / enabled flag)."""
    from app.architectures import memory as mem

    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    sections = (
        [s.model_dump() for s in payload.sections] if payload.sections is not None else None
    )
    memory = mem.upsert_memory(
        architecture_id,
        workload_id=arch.get("workload_id", ""),
        title=payload.title,
        sections=sections,
        enabled_for_investigations=payload.enabled_for_investigations,
        tenant_id=principal.tenant_id,
        actor=_actor(principal),
    )
    return _memory_response(architecture_id, arch, memory)


@router.delete("/{architecture_id}/memory")
async def delete_memory_endpoint(architecture_id: str, _: Principal = Depends(get_principal)):
    from app.architectures import memory as mem

    if not mem.delete_memory(architecture_id):
        raise HTTPException(status_code=404, detail="No memory to delete.")
    return {"ok": True}


@router.get("/{architecture_id}/memory/revisions")
async def list_memory_revisions_endpoint(architecture_id: str, _: Principal = Depends(get_principal)):
    """Revision history for an architecture's memory (newest first)."""
    from app.architectures import memory_revisions

    return {"revisions": memory_revisions.list_revisions(architecture_id)}


@router.get("/{architecture_id}/memory/revisions/{revision_id}")
async def get_memory_revision_endpoint(
    architecture_id: str, revision_id: str, _: Principal = Depends(get_principal)
):
    """Full content of one memory revision, for read-only preview (does not restore)."""
    from app.architectures import memory as mem
    from app.architectures import memory_revisions

    rev = memory_revisions.get_revision(architecture_id, revision_id)
    if rev is None:
        raise HTTPException(status_code=404, detail="Revision not found.")
    arch = arch_registry.get_architecture(architecture_id) or {}
    clean = {k: v for k, v in rev.items() if k != "sig"}
    # Render the revision's markdown so the preview pane can show it just like the editor.
    markdown = mem.render_markdown(clean, arch.get("name", ""), arch.get("workload_name", ""))
    return {"revision": clean, "markdown": markdown}


@router.post("/{architecture_id}/memory/revisions/{revision_id}/restore")
async def restore_memory_revision_endpoint(
    architecture_id: str, revision_id: str, principal: Principal = Depends(get_principal)
):
    """Restore a past memory revision onto the live memory (the current version is
    snapshotted first, so nothing is lost)."""
    from app.architectures import memory as mem

    restored = mem.restore_revision(architecture_id, revision_id, _actor(principal))
    if restored is None:
        raise HTTPException(status_code=404, detail="Memory or revision not found.")
    arch = arch_registry.get_architecture(architecture_id) or {}
    return _memory_response(architecture_id, arch, restored)


async def _gather_weakness_signals(architecture_id: str, workload_id: str, tenant_id: str, connection_id: str) -> list[str]:
    """Best-effort weakness signals to seed 'Known gaps': latest assessment failures +
    idle/orphaned inventory resources. Never raises — returns whatever it can gather."""
    signals: list[str] = []
    # 1) Latest succeeded assessment run for the workload → failing findings.
    try:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models import AssessmentRun

        async with SessionLocal() as db:
            run = (
                await db.execute(
                    select(AssessmentRun)
                    .where(
                        AssessmentRun.tenant_id == tenant_id,
                        AssessmentRun.workload_id == workload_id,
                        AssessmentRun.status == "succeeded",
                        AssessmentRun.deleted_at.is_(None),
                    )
                    .order_by(AssessmentRun.started_at.desc())
                    .limit(1)
                )
            ).scalars().first()
        if run is not None:
            for f in (run.findings_json or [])[:60]:
                if isinstance(f, dict) and f.get("status") == "fail":
                    sev = f.get("severity", "")
                    title = f.get("title") or f.get("check_id") or "finding"
                    signals.append(f"Assessment: {title}" + (f" ({sev})" if sev else ""))
    except Exception:  # noqa: BLE001
        pass
    # 2) Idle/orphaned inventory resources (reuse the optimization analysis).
    try:
        from app.inventory import cache as inv_cache
        from app.inventory import cost as inv_cost
        from app.inventory.optimization import analyze_resources

        hit = inv_cache.get(tenant_id, connection_id or "")
        if hit:
            resources = (hit.get("payload") or {}).get("resources") or []
            report = analyze_resources(resources, inv_cost.peek_cost(tenant_id, connection_id or ""))
            for it in report.get("items", [])[:20]:
                signals.append(f"Idle/orphaned: {it.get('category_label')} — {it.get('name')}")
    except Exception:  # noqa: BLE001
        pass
    return signals


@router.post("/{architecture_id}/memory/generate/stream")
async def generate_memory_stream_endpoint(
    architecture_id: str,
    payload: MemoryGenerateRequest = Body(default_factory=MemoryGenerateRequest),
    principal: Principal = Depends(get_principal),
):
    """AI-draft the memory sections from the architecture + live resources + weakness
    signals (assessment findings, idle resources). SSE: status… → done{memory, markdown}."""
    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    workload_id = arch.get("workload_id") or ""
    connection_id = arch.get("connection_id") or ""
    extra_context = payload.extra_context or ""
    # Gather weakness signals up-front (fast cache/DB reads) so the SSE generator only
    # streams the slow Resource Graph + AI steps.
    weakness_signals = await _gather_weakness_signals(
        architecture_id, workload_id, principal.tenant_id, connection_id
    )

    async def _gen():
        try:
            from app.architectures import memory as mem
            from app.architectures.memory_designer import generate_memory

            wl = get_workload(workload_id) if workload_id else None
            wl_name = arch.get("workload_name", "") or (wl or {}).get("name", "")
            resources: list[dict[str, Any]] = []
            if wl is not None:
                yield {"event": "status", "data": json.dumps({"phase": "query", "message": "Querying Azure Resource Graph for live resources…"})}
                from app.architectures.reverse import dump_resources

                conn = resolve_connection(connection_id or wl.get("connection_id") or None)
                dump = await dump_resources(wl, conn)
                resources = dump.get("resources") or []
            else:
                yield {"event": "status", "data": json.dumps({"phase": "query", "message": "No linked workload — drafting from the diagram only…"})}

            yield {"event": "status", "data": json.dumps({"phase": "ai", "message": f"Drafting memory from the architecture + {len(resources)} resource(s)…"})}
            # Bridge the designer's per-section progress callback into this SSE stream via
            # a queue: run generation as a task that pushes status lines, and drain them
            # here so the UI narrates each section as the model writes it.
            queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()

            async def _progress(phase: str, message: str) -> None:
                await queue.put({"phase": phase, "message": message})

            gen_task = asyncio.create_task(
                generate_memory(arch, resources, weakness_signals, wl_name, progress=_progress, extra_context=extra_context)
            )
            while not gen_task.done() or not queue.empty():
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                yield {"event": "status", "data": json.dumps(ev)}
            result = await gen_task
            if result is None:
                yield {"event": "error", "data": json.dumps({"message": "The AI could not draft the memory. Try again."})}
                return

            yield {"event": "status", "data": json.dumps({"phase": "save", "message": "💾 Validating sections & saving memory…"})}
            # Merge AI content into the existing (or default) section list, preserving
            # author order; fill empty sections, append any new catalog sections.
            existing = mem.get_memory(architecture_id)
            sections = list((existing or {}).get("sections") or mem.default_sections())
            present = {s["key"] for s in sections}
            ai_sections = result["sections"]
            for s in sections:
                if not str(s.get("content") or "").strip() and ai_sections.get(s["key"]):
                    s["content"] = ai_sections[s["key"]]
            for key, content in ai_sections.items():
                if key not in present:
                    sections.append({"key": key, "label": mem.section_label(key), "content": content})

            memory = mem.upsert_memory(
                architecture_id,
                workload_id=workload_id,
                sections=sections,
                source="ai" if existing is None else "hybrid",
                ai={
                    "confidence": result.get("confidence"),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "generated_by": _actor(principal),
                    "resource_count": len(resources),
                },
                tenant_id=principal.tenant_id,
                actor=_actor(principal),
                reason="Generated with AI",
            )
            yield {"event": "done", "data": json.dumps(_memory_response(architecture_id, arch, memory))}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Architecture memory generation failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


@router.post("/{architecture_id}/memory/sections/{section_key}/generate")
async def generate_memory_section_endpoint(
    architecture_id: str,
    section_key: str,
    payload: MemorySectionGenerateRequest = Body(default_factory=MemorySectionGenerateRequest),
    principal: Principal = Depends(get_principal),
):
    """AI-(re)draft a SINGLE memory section and persist it, leaving the rest untouched.

    Returns the updated memory (so the client can refresh just that section)."""
    from app.architectures import memory as mem
    from app.architectures.memory_designer import generate_memory

    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    workload_id = arch.get("workload_id") or ""
    connection_id = arch.get("connection_id") or ""
    weakness_signals = await _gather_weakness_signals(
        architecture_id, workload_id, principal.tenant_id, connection_id
    )
    wl = get_workload(workload_id) if workload_id else None
    wl_name = arch.get("workload_name", "") or (wl or {}).get("name", "")
    resources: list[dict[str, Any]] = []
    if wl is not None:
        from app.architectures.reverse import dump_resources

        conn = resolve_connection(connection_id or wl.get("connection_id") or None)
        dump = await dump_resources(wl, conn)
        resources = dump.get("resources") or []

    result = await generate_memory(
        arch, resources, weakness_signals, wl_name,
        only_keys=[section_key], extra_context=payload.extra_context or "",
    )
    content = (result or {}).get("sections", {}).get(section_key) if result else None
    if not content:
        raise HTTPException(status_code=502, detail="The AI could not draft this section. Try again.")

    existing = mem.get_memory(architecture_id)
    sections = list((existing or {}).get("sections") or mem.default_sections())
    found = False
    for s in sections:
        if s.get("key") == section_key:
            s["content"] = content
            s.pop("needs_review", None)  # a fresh draft clears the review flag
            found = True
            break
    if not found:
        sections.append({"key": section_key, "label": mem.section_label(section_key), "content": content})

    memory = mem.upsert_memory(
        architecture_id,
        workload_id=workload_id,
        sections=sections,
        source="hybrid" if existing is not None else "ai",
        tenant_id=principal.tenant_id,
        actor=_actor(principal),
        reason=f"Regenerated section: {mem.section_label(section_key)}",
    )
    return _memory_response(architecture_id, arch, memory)


# ------------------------------------------------------------- clone + revision history
@router.post("/{architecture_id}/clone")
async def clone_architecture_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    """Duplicate an architecture into a fresh Draft copy."""
    cloned = arch_registry.clone_architecture(
        architecture_id, actor=_actor(principal), tenant_id=principal.tenant_id
    )
    if cloned is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"architecture": cloned}


@router.get("/{architecture_id}/revisions")
async def list_revisions_endpoint(architecture_id: str, _: Principal = Depends(get_principal)):
    from app.architectures import revisions

    return {"revisions": revisions.list_revisions(architecture_id)}


@router.get("/{architecture_id}/revisions/{revision_id}")
async def get_revision_endpoint(
    architecture_id: str, revision_id: str, _: Principal = Depends(get_principal)
):
    """Full content of one revision, for read-only preview (does not restore)."""
    from app.architectures import revisions

    rev = revisions.get_revision(architecture_id, revision_id)
    if rev is None:
        raise HTTPException(status_code=404, detail="Revision not found.")
    # Strip the internal dedup signature; return content + metadata for the preview.
    return {"revision": {k: v for k, v in rev.items() if k != "sig"}}


@router.post("/{architecture_id}/revisions/{revision_id}/restore")
async def restore_revision_endpoint(
    architecture_id: str, revision_id: str, principal: Principal = Depends(get_principal)
):
    restored = arch_registry.restore_revision(architecture_id, revision_id, _actor(principal))
    if restored is None:
        raise HTTPException(status_code=404, detail="Architecture or revision not found.")
    return {"architecture": restored}


@router.get("/{architecture_id}/activity")
async def list_activity_endpoint(architecture_id: str, _: Principal = Depends(get_principal)):
    """The management activity log (status/category changes, edits, clone, restore, AI)."""
    from app.architectures import activity

    return {"activity": activity.list_activity(architecture_id)}


# --------------------------------------------------------- workload inventory (debug)
@router.get("/workload/{workload_id}/inventory")
async def workload_inventory_endpoint(workload_id: str, _: Principal = Depends(get_principal)):
    """The resource inventory (with compacted properties) that would be sent to the AI."""
    from app.architectures.reverse import dump_resources

    wl = get_workload(workload_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="Workload not found.")
    conn = resolve_connection(wl.get("connection_id") or None)
    dump = await dump_resources(wl, conn)
    return {"count": dump["count"], "error": dump["error"], "resources": dump["resources"]}


# ----------------------------------------------- AI: reverse-engineer from a workload
class FromWorkloadRequest(BaseModel):
    workload_id: str
    connection_id: str | None = None
    save: bool = True


@router.post("/from-workload")
async def from_workload_endpoint(payload: FromWorkloadRequest, principal: Principal = Depends(get_principal)):
    """Reverse-engineer an architecture from a workload (SSE: status… → done{architecture})."""

    async def _gen():
        try:
            wl = get_workload(payload.workload_id)
            if wl is None:
                yield {"event": "error", "data": json.dumps({"message": "Workload not found."})}
                return
            conn_id = payload.connection_id or wl.get("connection_id") or ""
            conn = resolve_connection(conn_id or None)
            wl_name = wl.get("name", "workload")

            yield {"event": "status", "data": json.dumps({"phase": "scope", "message": f"Resolving scope for '{wl_name}'…"})}
            from app.architectures.reverse import dump_resources

            yield {"event": "status", "data": json.dumps({"phase": "query", "message": "Querying Azure Resource Graph for resources + properties…"})}
            dump = await dump_resources(wl, conn)
            if dump["error"]:
                yield {"event": "error", "data": json.dumps({"message": dump["error"]})}
                return
            resources = dump["resources"]
            if not resources:
                yield {"event": "error", "data": json.dumps({"message": "No resources found in this workload's scope."})}
                return

            yield {"event": "status", "data": json.dumps({"phase": "ai", "message": f"Reverse-engineering architecture from {len(resources)} resource(s)…"})}
            from app.architectures.designer import generate_architecture

            result = await generate_architecture(wl_name, resources)
            if result is None:
                yield {"event": "error", "data": json.dumps({"message": "The AI could not infer an architecture. Try again."})}
                return

            arch_payload = {
                "name": result["name"] or f"{wl_name} architecture",
                "description": result["description"],
                "workload_id": payload.workload_id,
                "workload_name": wl_name,
                "connection_id": conn_id,
                "tenant_id": principal.tenant_id,
                "source": "ai",
                "nodes": result["nodes"],
                "edges": result["edges"],
                "groups": result["groups"],
                "ai": {
                    "rationale": result["rationale"],
                    "confidence": result["confidence"],
                    "resource_count": len(resources),
                    "generated_by": _actor(principal),
                },
            }
            if payload.save:
                arch_payload["created_by"] = _actor(principal)
                saved = arch_registry.upsert_architecture(arch_payload, actor=_actor(principal), reason="Generated by AI")
            else:
                saved = arch_payload
            yield {"event": "done", "data": json.dumps({"architecture": saved})}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Architecture reverse-engineering failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


class EnhanceRequest(BaseModel):
    goal: str = Field(default="", max_length=2000)


@router.post("/{architecture_id}/enhance")
async def enhance_architecture_endpoint(
    architecture_id: str, payload: EnhanceRequest, principal: Principal = Depends(get_principal)
):
    """AI-refine an existing diagram (grounded on the source workload's inventory)."""
    from app.architectures.designer import enhance_architecture
    from app.architectures.reverse import dump_resources

    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    resources: list[dict[str, Any]] = []
    wl = get_workload(arch.get("workload_id") or "")
    if wl is not None:
        conn = resolve_connection(arch.get("connection_id") or wl.get("connection_id") or None)
        dump = await dump_resources(wl, conn)
        resources = dump["resources"]
    result = await enhance_architecture(arch, resources, payload.goal)
    if result is None:
        raise HTTPException(status_code=502, detail="The AI could not enhance this diagram. Try again.")
    saved = arch_registry.upsert_architecture({
        "id": architecture_id,
        "name": result["name"],
        "description": result["description"],
        "nodes": result["nodes"],
        "edges": result["edges"],
        "groups": result["groups"],
        "source": "ai",
        "ai": {**(arch.get("ai") or {}), "rationale": result["rationale"], "confidence": result["confidence"]},
    }, actor=_actor(principal), reason="AI enhanced")
    return {"architecture": saved}


# ----------------------------------------------------------------- AI Q&A about a diagram
class AskRequest(BaseModel):
    question: str = Field(max_length=2000)


@router.post("/{architecture_id}/ask")
async def ask_architecture_endpoint(
    architecture_id: str, payload: AskRequest, _: Principal = Depends(get_principal)
):
    """Grounded Q&A about an architecture (SPOFs, blast radius, zone-redundancy, etc.)."""
    from app.architectures.designer import answer_question

    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="Ask a question.")
    try:
        answer = await answer_question(arch, payload.question)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Architecture Q&A failed")
        raise HTTPException(status_code=502, detail="The AI could not answer. Try again.") from exc
    return {"answer": answer or "(no answer)"}


# --------------------------------------------------------------- Drift vs. live Azure (ARG)
@router.post("/{architecture_id}/drift")
async def architecture_drift_endpoint(
    architecture_id: str, principal: Principal = Depends(get_principal)
):
    """Compare the diagram's resources against the workload's live Azure Resource Graph
    inventory: which diagram nodes are gone, and which live resources aren't on the diagram.

    When the architecture is linked to a workload, drift is scoped to that workload. Otherwise
    (e.g. an older reverse-engineered diagram with no workload link) it falls back to the
    (subscription, resource group) scope the diagram's own ARM-id nodes already live in."""
    from app.architectures.reverse import dump_resources, live_resources_in_diagram_scope

    arch = arch_registry.get_architecture(architecture_id)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    nodes = arch.get("nodes") or []
    arm_ids = [str(n.get("arm_id", "")) for n in nodes if n.get("arm_id")]
    wl = get_workload(arch.get("workload_id") or "")

    if wl is not None:
        conn = resolve_connection(arch.get("connection_id") or wl.get("connection_id") or None)
        dump = await dump_resources(wl, conn)
        if dump.get("error"):
            raise HTTPException(status_code=502, detail=str(dump["error"]))
        live = dump.get("resources") or []
    else:
        # No workload link — derive the comparison scope from the diagram's own resources.
        if not arm_ids:
            raise HTTPException(
                status_code=400,
                detail="This architecture has no Azure-linked resources, so drift can't be computed. Reverse-engineer it from a workload to enable drift.",
            )
        conn = resolve_connection(arch.get("connection_id") or None)
        scoped = await live_resources_in_diagram_scope(arm_ids, conn)
        if scoped.get("error"):
            raise HTTPException(status_code=502, detail=str(scoped["error"]))
        live = scoped.get("resources") or []

    live_by_id = {str(r.get("id", "")).lower(): r for r in live if r.get("id")}
    diagram_ids = {str(n.get("arm_id", "")).lower() for n in nodes if n.get("arm_id")}

    removed = []  # on the diagram, no longer in Azure
    for n in nodes:
        aid = str(n.get("arm_id", "")).lower()
        if aid and aid not in live_by_id:
            removed.append({"id": n.get("id"), "name": n.get("name"), "type": n.get("type"), "arm_id": n.get("arm_id")})
    added = []  # in Azure, not on the diagram
    for aid, r in live_by_id.items():
        if aid not in diagram_ids:
            added.append({"name": r.get("name"), "type": r.get("type"), "arm_id": r.get("id"), "resource_group": r.get("resourceGroup")})

    matched = len(diagram_ids & set(live_by_id.keys()))
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "live_count": len(live),
        "diagram_count": len(diagram_ids),
        "matched": matched,
        "removed": removed,   # ghost nodes (in diagram, not in Azure)
        "added": added,       # new resources (in Azure, not in diagram)
        "in_sync": not removed and not added,
    }

"""Architecture endpoints: CRUD over the registry, the manual-builder catalog, and AI
reverse-engineering of an application architecture from a workload's resource inventory.

Any authenticated user may manage architectures (tenant-scoped). Reverse-engineering runs
read-only Azure Resource Graph queries via the existing command runner.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.architectures import catalog
from app.architectures import registry as arch_registry
from app.core.azure_connections import resolve_connection
from app.core.genjob import JobRegistry
from app.core.security import Principal, require_permission
from app.workloads.registry import get_workload, list_workloads

# KP5/KU4 — background-survivable Know-Me generation. The generate/regenerate run as detached
# jobs keyed by document (so navigating away doesn't lose the draft); SSE subscribers follow.
_knowme_jobs = JobRegistry("knowme")


router = APIRouter(prefix="/architectures", tags=["architectures"])

# Viewing architectures requires architectures.read; AI-generating, editing, organizing
# (collections), deleting/restoring/purging, and editing memory requires
# architectures.write. The `get_principal` alias is the read tier (so existing call sites
# stay correct); write endpoints opt into `_write`. Admins always pass via require_permission.
get_principal = require_permission("architectures.read")
_write = require_permission("architectures.write")
logger = logging.getLogger("app.api.architectures")


def _tenant_arch_or_404(
    architecture_id: str, principal: Principal, *, include_deleted: bool = False
) -> dict[str, Any]:
    """Load an architecture and verify the caller's tenant owns it.

    Centralizes the per-id IDOR guard for every architecture endpoint that takes
    `architecture_id`. Empty `tenant_id` on a registry row is treated as a legacy
    pre-multi-tenancy record visible to any tenant (matches the list endpoint behavior).
    Otherwise a mismatch raises 404 (not 403) so a cross-tenant probe cannot confirm
    existence.
    """
    arch = arch_registry.get_architecture(architecture_id, include_deleted=include_deleted)
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    arch_tenant = arch.get("tenant_id") or ""
    if arch_tenant and arch_tenant != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return arch


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
    payload: GenerateJobsRequest, principal: Principal = Depends(_write)
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
async def cancel_generation_job_endpoint(job_id: str, principal: Principal = Depends(_write)):
    from app.architectures.jobs import manager

    if not manager.cancel(job_id, principal.tenant_id):
        raise HTTPException(status_code=404, detail="Job not found or already finished.")
    return {"ok": True}


@router.delete("/jobs/{job_id}")
async def dismiss_generation_job_endpoint(job_id: str, principal: Principal = Depends(_write)):
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
    payload: CollectionUpsert, principal: Principal = Depends(_write)
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
    payload: ReorderRequest, _: Principal = Depends(_write)
):
    from app.architectures import collections as coll_registry

    coll_registry.reorder_collections(payload.ordered_ids)
    return {"ok": True}


@router.delete("/collections/{collection_id}")
async def delete_collection_endpoint(
    collection_id: str, principal: Principal = Depends(_write)
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
async def empty_architecture_trash_endpoint(principal: Principal = Depends(_write)):
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


@router.get("/know-me")
async def list_know_me_endpoint(principal: Principal = Depends(get_principal)):
    """Index for the standalone Know-Me page. Returns every Know-Me DOCUMENT (a workload can
    have many — drafts + published), plus the set of architectures that have a Memory (so the
    UI can offer 'create a new Know-Me'), plus the trash count. Tenant-scoped."""
    from app.architectures import memory as mem
    from app.knowme import registry as kreg

    archs = {a["id"]: a for a in arch_registry.list_architectures(principal.tenant_id)}
    # Active (non-trashed) workload ids — an architecture whose backing workload was deleted
    # is "orphaned" even though the architecture itself still exists.
    active_wl = {w["id"] for w in list_workloads()}
    memories = {m.get("architecture_id", "") for m in mem.list_memories(principal.tenant_id)}

    def _workload_exists(wid: str) -> bool:
        # A standalone architecture (no workload_id) is never workload-orphaned.
        return (not wid) or (wid in active_wl)

    def _doc(km_doc: dict[str, Any]) -> dict[str, Any]:
        aid = km_doc.get("architecture_id", "")
        arch = archs.get(aid)
        sections = km_doc.get("sections", []) or []
        todos = km_doc.get("todos", []) or []
        wid = km_doc.get("workload_id", "") or (arch or {}).get("workload_id", "")
        return {
            "id": km_doc.get("id"),
            "architecture_id": aid,
            "architecture_name": (arch or {}).get("name", "") or "(deleted architecture)",
            "architecture_exists": arch is not None,
            "workload_id": wid,
            "workload_exists": _workload_exists(wid),
            "workload_name": (arch or {}).get("workload_name", "") or km_doc.get("workload_name", ""),
            "title": km_doc.get("title", ""),
            "description": km_doc.get("description", ""),
            "status": km_doc.get("status", "draft"),
            "is_reference": bool(km_doc.get("is_reference", False)),
            "source": km_doc.get("source", ""),
            "section_count": len(sections),
            "filled_count": sum(1 for s in sections if str(s.get("content") or "").strip()),
            "open_todos": sum(1 for t in todos if t.get("status") != "done"),
            "updated_at": km_doc.get("updated_at", ""),
            "updated_by": km_doc.get("updated_by", ""),
            "deleted_at": km_doc.get("deleted_at", ""),
        }

    all_docs = kreg.list_know_me(principal.tenant_id)
    documents = [_doc(d) for d in all_docs]
    by_arch: dict[str, int] = {}
    for d in documents:
        by_arch[d["architecture_id"]] = by_arch.get(d["architecture_id"], 0) + 1
    # Architectures the user can build a (new) Know-Me from: those with a Memory.
    buildable: list[dict[str, Any]] = []
    for aid in memories:
        arch = archs.get(aid)
        wid = (arch or {}).get("workload_id", "")
        # Don't offer "+ New Know-Me" for an architecture whose backing workload was deleted —
        # its card would otherwise linger on the index after the workload is gone. Any Know-Me
        # documents it already has still surface (via `documents`, flagged orphaned) so they
        # can be reviewed/trashed.
        if wid and wid not in active_wl:
            continue
        buildable.append({
            "architecture_id": aid,
            "architecture_name": (arch or {}).get("name", "") or "(deleted architecture)",
            "architecture_exists": arch is not None,
            "workload_id": wid,
            "workload_exists": _workload_exists(wid),
            "workload_name": (arch or {}).get("workload_name", ""),
            "know_me_count": by_arch.get(aid, 0),
        })
    buildable.sort(key=lambda r: (r["workload_name"].lower() or r["architecture_name"].lower()))
    documents.sort(key=lambda r: r["updated_at"], reverse=True)
    trash_count = len(kreg.list_know_me(principal.tenant_id, only_deleted=True))
    return {"documents": documents, "buildable": buildable, "trash_count": trash_count}


@router.get("/know-me/trash")
async def list_know_me_trash_endpoint(principal: Principal = Depends(get_principal)):
    """List soft-deleted Know-Me documents (the Trash)."""
    from app.knowme import registry as kreg

    archs = {a["id"]: a for a in arch_registry.list_architectures(principal.tenant_id, include_deleted=True)}
    out: list[dict[str, Any]] = []
    for d in kreg.list_know_me(principal.tenant_id, only_deleted=True):
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


@router.post("/know-me/trash/empty")
async def empty_know_me_trash_endpoint(principal: Principal = Depends(_write)):
    from app.knowme import registry as kreg

    return {"purged": kreg.empty_trash(principal.tenant_id)}


@router.delete("/know-me/orphans/{architecture_id}")
async def purge_know_me_orphan_endpoint(architecture_id: str, principal: Principal = Depends(_write)):
    """Permanently remove an ORPHANED Know-Me group — its leftover Architecture Memory plus
    any Know-Me documents — for an architecture that no longer exists. These surface on the
    index as "(deleted architecture) · orphaned" and can't be cleaned via the normal
    architecture-scoped delete (that 404s once the architecture is gone). Tenant-checked.

    Refuses if the architecture still EXISTS (active, not trashed) for this tenant — use the
    normal delete instead — so this can only ever clean up true orphans. A soft-deleted /
    trashed architecture counts as orphaned here (the index lists only ACTIVE architectures,
    so a trashed one already shows as "orphaned")."""
    from app.architectures import memory as mem
    from app.architectures import memory_revisions
    from app.knowme import registry as kreg

    # Active-only lookup (NOT include_deleted): a trashed architecture is "orphaned" from the
    # Know-Me index's perspective, so it's purgeable here. Only a live architecture is refused.
    arch = arch_registry.get_architecture(architecture_id)
    if arch is not None and (arch.get("tenant_id") or "") in ("", principal.tenant_id):
        raise HTTPException(
            status_code=400,
            detail="That architecture still exists — delete its Know-Me documents and Memory the normal way.",
        )

    purged_docs = 0
    found = False
    # Purge any Know-Me documents (active or trashed) for this orphaned architecture, tenant-scoped.
    for d in kreg.list_know_me(principal.tenant_id, include_deleted=True, architecture_id=architecture_id):
        found = True
        if kreg.purge(d["id"]):
            purged_docs += 1

    # Delete the leftover Memory (+ its revisions) if it belongs to this tenant.
    memory = mem.get_memory(architecture_id)
    if memory is not None and (memory.get("tenant_id") or "") in ("", principal.tenant_id):
        found = True
        mem.delete_memory(architecture_id)
        try:
            memory_revisions.delete_for(architecture_id)
        except Exception:  # noqa: BLE001 — best-effort revision cleanup
            pass

    if not found:
        raise HTTPException(status_code=404, detail="No orphaned Know-Me data found for that architecture.")
    return {"ok": True, "purged_documents": purged_docs}



@router.get("/{architecture_id}")
async def get_architecture_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    arch = _tenant_arch_or_404(architecture_id, principal)
    return {"architecture": arch}


@router.put("")
async def upsert_architecture_endpoint(
    payload: ArchitectureUpsert, principal: Principal = Depends(_write)
):
    data = payload.model_dump()
    data["tenant_id"] = principal.tenant_id
    if not payload.id:
        data["created_by"] = _actor(principal)
    saved = arch_registry.upsert_architecture(data, actor=_actor(principal), reason="Edited")
    return {"architecture": saved}


@router.delete("/{architecture_id}")
async def delete_architecture_endpoint(architecture_id: str, principal: Principal = Depends(_write)):
    """Soft-delete: move the architecture to the Trash (restorable until purged)."""
    _tenant_arch_or_404(architecture_id, principal)
    if not arch_registry.delete_architecture(architecture_id, actor=_actor(principal)):
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"ok": True}


@router.post("/{architecture_id}/restore")
async def restore_architecture_endpoint(architecture_id: str, principal: Principal = Depends(_write)):
    """Restore a trashed architecture back into the active list."""
    _tenant_arch_or_404(architecture_id, principal, include_deleted=True)
    arch = arch_registry.restore_architecture(architecture_id, actor=_actor(principal))
    if arch is None:
        raise HTTPException(status_code=404, detail="Architecture not in trash.")
    return {"architecture": arch}


@router.delete("/{architecture_id}/purge")
async def purge_architecture_endpoint(architecture_id: str, principal: Principal = Depends(_write)):
    """Permanently delete a single trashed architecture (hard delete)."""
    arch = _tenant_arch_or_404(architecture_id, principal, include_deleted=True)
    if not arch.get("deleted_at"):
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
    architecture_id: str, payload: StateUpdate, principal: Principal = Depends(_write)
):
    _tenant_arch_or_404(architecture_id, principal)
    try:
        saved = arch_registry.set_state(architecture_id, payload.state, _actor(principal))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"architecture": saved}


@router.post("/{architecture_id}/category")
async def set_architecture_category_endpoint(
    architecture_id: str, payload: CategoryUpdate, principal: Principal = Depends(_write)
):
    _tenant_arch_or_404(architecture_id, principal)
    saved = arch_registry.set_category(architecture_id, payload.category_id, _actor(principal))
    if saved is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"architecture": saved}


class WorkloadUpdate(BaseModel):
    workload_id: str = ""


@router.post("/{architecture_id}/workload")
async def set_architecture_workload_endpoint(
    architecture_id: str, payload: WorkloadUpdate, principal: Principal = Depends(_write)
):
    """Link (or unlink) the architecture to a workload. An empty workload_id unlinks it;
    the diagram is never modified — only the association changes."""
    _tenant_arch_or_404(architecture_id, principal)
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
    architecture_id: str, payload: RebuildRequest, principal: Principal = Depends(_write)
):
    """Queue a background job that re-reverse-engineers this architecture from the current
    Azure state of a workload, overwriting its diagram in place (id/name/state preserved).
    Poll GET /architectures/jobs for live progress; the job's target_architecture_id is this id."""
    from app.architectures.jobs import manager

    arch = _tenant_arch_or_404(architecture_id, principal)
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


class KnowMeSectionIn(BaseModel):
    key: str
    label: str = ""
    content: str = ""


class KnowMeTodoIn(BaseModel):
    id: str
    field_key: str = ""
    label: str = ""
    section_key: str = ""
    status: str = "open"
    value: str = ""
    assignee: str = ""
    note: str = ""
    choices: list[str] | None = None
    allow_custom: bool = True
    choice_source: str = ""
    multi: bool = False


class KnowMeUpsert(BaseModel):
    title: str | None = None
    description: str | None = None
    sections: list[KnowMeSectionIn] | None = None
    todos: list[KnowMeTodoIn] | None = None
    status: str | None = None


class KnowMeGenerateRequest(BaseModel):
    # Optional human-provided grounding folded into the Know-Me draft.
    extra_context: str = ""


class KnowMeSectionGenerateRequest(BaseModel):
    extra_context: str = ""


def _know_me_response(architecture_id: str, arch: dict[str, Any], km_doc: dict[str, Any]) -> dict[str, Any]:
    from app.knowme import sections as km

    wl_name = arch.get("workload_name", "") or km_doc.get("workload_name", "")
    return {
        "id": km_doc.get("id"),
        "know_me": km_doc,
        "markdown": km.render_markdown(km_doc, wl_name),
        "architecture": _arch_meta(architecture_id, arch),
    }


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
async def get_memory_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    """Return the architecture's memory (or null if none exists yet) + rendered markdown."""
    from app.architectures import memory as mem

    arch = _tenant_arch_or_404(architecture_id, principal)
    memory = mem.get_memory(architecture_id)
    if memory is None:
        return {"memory": None, "markdown": "", "architecture": _arch_meta(architecture_id, arch)}
    return _memory_response(architecture_id, arch, memory)


@router.put("/{architecture_id}/memory")
async def upsert_memory_endpoint(
    architecture_id: str, payload: MemoryUpsert, principal: Principal = Depends(_write)
):
    """Create or update the architecture's memory (sections / title / enabled flag)."""
    from app.architectures import memory as mem

    arch = _tenant_arch_or_404(architecture_id, principal)
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
async def delete_memory_endpoint(architecture_id: str, principal: Principal = Depends(_write)):
    from app.architectures import memory as mem

    _tenant_arch_or_404(architecture_id, principal)
    if not mem.delete_memory(architecture_id):
        raise HTTPException(status_code=404, detail="No memory to delete.")
    return {"ok": True}


@router.get("/{architecture_id}/memory/revisions")
async def list_memory_revisions_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    """Revision history for an architecture's memory (newest first)."""
    from app.architectures import memory_revisions

    _tenant_arch_or_404(architecture_id, principal)
    return {"revisions": memory_revisions.list_revisions(architecture_id)}


@router.get("/{architecture_id}/memory/revisions/{revision_id}")
async def get_memory_revision_endpoint(
    architecture_id: str, revision_id: str, principal: Principal = Depends(get_principal)
):
    """Full content of one memory revision, for read-only preview (does not restore)."""
    from app.architectures import memory as mem
    from app.architectures import memory_revisions

    arch = _tenant_arch_or_404(architecture_id, principal)
    rev = memory_revisions.get_revision(architecture_id, revision_id)
    if rev is None:
        raise HTTPException(status_code=404, detail="Revision not found.")
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

    arch = _tenant_arch_or_404(architecture_id, principal)
    restored = mem.restore_revision(architecture_id, revision_id, _actor(principal))
    if restored is None:
        raise HTTPException(status_code=404, detail="Memory or revision not found.")
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
    arch = _tenant_arch_or_404(architecture_id, principal)
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
            # Merge AI content into the existing (or default) section list. See
            # ``mem.merge_ai_sections`` — a full "Generate with AI" overwrites every section
            # the model returned content for (a fully-populated memory used to silently keep
            # its old content and appear "not saved").
            existing = mem.get_memory(architecture_id)
            sections = mem.merge_ai_sections(
                (existing or {}).get("sections"), result["sections"]
            )

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

    arch = _tenant_arch_or_404(architecture_id, principal)
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


# ============================================================ Workload Know-Me
# A Know-Me is a support-facing reference transformed from an architecture's Memory. Each is
# keyed by its OWN id (a workload can have many — drafts + a published reference) and links
# back to its source architecture / workload.
def _km_or_404(km_id: str, principal: Principal, *, allow_deleted: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch a Know-Me by id + its source architecture, tenant-checked. Raises 404 otherwise.
    Returns (km_doc, arch). ``arch`` may be {} if the architecture was deleted."""
    from app.knowme import registry as kreg

    km_doc = kreg.get_know_me(km_id)
    if km_doc is None or (km_doc.get("tenant_id") or "") not in ("", principal.tenant_id):
        raise HTTPException(status_code=404, detail="Know-Me not found.")
    if km_doc.get("deleted_at") and not allow_deleted:
        raise HTTPException(status_code=404, detail="This Know-Me is in the Trash.")
    arch = arch_registry.get_architecture(km_doc.get("architecture_id", ""), include_deleted=True) or {}
    return km_doc, arch


@router.post("/{architecture_id}/know-me")
async def create_know_me_endpoint(
    architecture_id: str, payload: KnowMeUpsert, principal: Principal = Depends(_write)
):
    """Create a NEW (empty draft) Know-Me document for an architecture."""
    from app.knowme import registry as kreg

    arch = _tenant_arch_or_404(architecture_id, principal)
    km_doc = kreg.create_know_me(
        architecture_id=architecture_id,
        workload_id=arch.get("workload_id", ""),
        workload_name=arch.get("workload_name", ""),
        connection_id=arch.get("connection_id", ""),
        title=payload.title or "",
        tenant_id=principal.tenant_id,
        actor=_actor(principal),
    )
    return _know_me_response(architecture_id, arch, km_doc)


@router.get("/know-me/{km_id}")
async def get_know_me_endpoint(km_id: str, principal: Principal = Depends(get_principal)):
    """Return one Know-Me document by id + rendered markdown. Includes ``memory_updated_at``
    so the UI can flag a Know-Me as stale when its source Memory changed after generation."""
    from app.architectures import memory as mem

    km_doc, arch = _km_or_404(km_id, principal, allow_deleted=True)
    memory = mem.get_memory(km_doc.get("architecture_id", ""))
    return {
        **_know_me_response(km_doc.get("architecture_id", ""), arch, km_doc),
        "has_memory": memory is not None,
        "memory_updated_at": (memory or {}).get("updated_at", ""),
    }


@router.put("/know-me/{km_id}")
async def upsert_know_me_endpoint(km_id: str, payload: KnowMeUpsert, principal: Principal = Depends(_write)):
    """Save a Know-Me (sections / todos / title / description / status) — snapshots a revision."""
    from app.knowme import registry as kreg

    km_doc, arch = _km_or_404(km_id, principal)
    sections = [s.model_dump() for s in payload.sections] if payload.sections is not None else None
    todos = [t.model_dump() for t in payload.todos] if payload.todos is not None else None
    saved = kreg.update_know_me(
        km_id,
        title=payload.title,
        description=payload.description,
        sections=sections,
        todos=todos,
        status=payload.status,
        source="edited",
        tenant_id=principal.tenant_id,
        actor=_actor(principal),
    )
    return _know_me_response(km_doc.get("architecture_id", ""), arch, saved)


@router.post("/know-me/{km_id}/reference")
async def set_know_me_reference_endpoint(
    km_id: str, payload: dict[str, Any] | None = Body(default=None), principal: Principal = Depends(_write)
):
    """Mark (or unmark) this Know-Me as the canonical reference for its workload. Only one
    document per workload can be the reference."""
    from app.knowme import registry as kreg

    km_doc, arch = _km_or_404(km_id, principal)
    is_ref = True if payload is None else bool(payload.get("is_reference", True))
    saved = kreg.set_reference(km_id, is_reference=is_ref, actor=_actor(principal))
    if saved is None:
        raise HTTPException(status_code=404, detail="Know-Me not found.")
    return _know_me_response(km_doc.get("architecture_id", ""), arch, saved)


@router.delete("/know-me/{km_id}")
async def delete_know_me_endpoint(km_id: str, principal: Principal = Depends(_write)):
    """Move a Know-Me to the Trash (soft-delete)."""
    from app.knowme import registry as kreg

    _km_or_404(km_id, principal, allow_deleted=True)
    kreg.soft_delete(km_id, _actor(principal))
    return {"ok": True}


@router.post("/know-me/{km_id}/restore")
async def restore_know_me_endpoint(km_id: str, principal: Principal = Depends(_write)):
    """Restore a Know-Me from the Trash."""
    from app.knowme import registry as kreg

    _km_or_404(km_id, principal, allow_deleted=True)
    restored = kreg.restore(km_id)
    if restored is None:
        raise HTTPException(status_code=404, detail="Nothing to restore.")
    return {"ok": True, "know_me": restored}


@router.delete("/know-me/{km_id}/purge")
async def purge_know_me_endpoint(km_id: str, principal: Principal = Depends(_write)):
    """Permanently delete a Know-Me (and its revisions + assets)."""
    from app.knowme import registry as kreg

    _km_or_404(km_id, principal, allow_deleted=True)
    kreg.purge(km_id)
    return {"ok": True}


@router.get("/know-me/{km_id}/revisions")
async def list_know_me_revisions_endpoint(km_id: str, principal: Principal = Depends(get_principal)):
    from app.knowme import revisions

    _km_or_404(km_id, principal, allow_deleted=True)
    return {"revisions": revisions.list_revisions(km_id)}


@router.get("/know-me/{km_id}/revisions/{revision_id}")
async def get_know_me_revision_endpoint(
    km_id: str, revision_id: str, principal: Principal = Depends(get_principal)
):
    from app.knowme import revisions
    from app.knowme import sections as km

    km_doc, arch = _km_or_404(km_id, principal, allow_deleted=True)
    rev = revisions.get_revision(km_id, revision_id)
    if rev is None:
        raise HTTPException(status_code=404, detail="Revision not found.")
    clean = {k: v for k, v in rev.items() if k != "sig"}
    markdown = km.render_markdown(clean, arch.get("workload_name", "") or km_doc.get("workload_name", ""))
    return {"revision": clean, "markdown": markdown}


@router.post("/know-me/{km_id}/revisions/{revision_id}/restore")
async def restore_know_me_revision_endpoint(
    km_id: str, revision_id: str, principal: Principal = Depends(_write)
):
    from app.knowme import registry as kreg

    km_doc, arch = _km_or_404(km_id, principal)
    restored = kreg.restore_revision(km_id, revision_id, _actor(principal))
    if restored is None:
        raise HTTPException(status_code=404, detail="Know-Me or revision not found.")
    return _know_me_response(km_doc.get("architecture_id", ""), arch, restored)


@router.get("/know-me/{km_id}/export")
async def export_know_me_endpoint(
    km_id: str, format: str = "md", principal: Principal = Depends(get_principal)
):
    """Export the Know-Me as Markdown (format=md) or a branded PDF (format=pdf)."""
    from fastapi.responses import Response

    from app.knowme import sections as km

    km_doc, arch = _km_or_404(km_id, principal, allow_deleted=True)
    wl_name = arch.get("workload_name", "") or km_doc.get("workload_name", "")
    markdown = km.render_markdown(km_doc, wl_name, cover=True)
    base = re.sub(r"[^a-z0-9]+", "-", (km_doc.get("title") or wl_name or "know-me").lower()).strip("-") or "know-me"
    if format == "pdf":
        from app.connectors import chat_pdf
        from app.knowme import assets as kassets

        # Inline embedded image assets as data-URIs (no live API during PDF render).
        markdown = kassets.inline_asset_data_uris(km_id, markdown)
        title = km_doc.get("title") or (f"Know-Me — {wl_name}" if wl_name else "Workload Know-Me")
        pdf = await asyncio.to_thread(
            chat_pdf.build_chat_pdf, title, [{"role": "assistant", "content": markdown}]
        )
        return Response(
            content=pdf, media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{base}-know-me.pdf"'},
        )
    return Response(
        content=markdown, media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{base}-know-me.md"'},
    )


@router.get("/know-me/{km_id}/mermaid")
async def know_me_mermaid_endpoint(km_id: str, principal: Principal = Depends(get_principal)):
    """Generate a Mermaid flowchart from the source architecture's diagram, for embedding."""
    from app.knowme import assets as kassets

    _km_doc, arch = _km_or_404(km_id, principal, allow_deleted=True)
    mermaid = kassets.architecture_to_mermaid(arch)
    if not mermaid:
        raise HTTPException(status_code=404, detail="This architecture has no diagram to convert.")
    return {"mermaid": mermaid}


@router.get("/know-me/{km_id}/assets")
async def list_know_me_assets_endpoint(km_id: str, principal: Principal = Depends(get_principal)):
    km_doc, _arch = _km_or_404(km_id, principal, allow_deleted=True)
    return {"assets": km_doc.get("assets", [])}


@router.post("/know-me/{km_id}/assets")
async def upload_know_me_asset_endpoint(
    km_id: str, file: UploadFile = File(...), principal: Principal = Depends(_write)
):
    """Upload/paste an image into the Know-Me; returns the asset record + a Markdown snippet."""
    from app.knowme import assets as kassets
    from app.knowme import registry as kreg

    _km_or_404(km_id, principal)
    data = await file.read()
    try:
        asset = kassets.save_asset(km_id, data=data, content_type=file.content_type or "", filename=file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    kreg.add_asset(km_id, {k: asset[k] for k in ("id", "filename", "content_type", "size")})
    return {"asset": asset}


@router.get("/know-me/{km_id}/assets/{asset_id}")
async def get_know_me_asset_endpoint(km_id: str, asset_id: str, principal: Principal = Depends(get_principal)):
    from fastapi.responses import Response

    from app.knowme import assets as kassets

    _km_or_404(km_id, principal, allow_deleted=True)
    got = kassets.read_asset(km_id, asset_id)
    if got is None:
        raise HTTPException(status_code=404, detail="Asset not found.")
    data, ct = got
    return Response(content=data, media_type=ct, headers={"Cache-Control": "private, max-age=86400"})


@router.delete("/know-me/{km_id}/assets/{asset_id}")
async def delete_know_me_asset_endpoint(km_id: str, asset_id: str, principal: Principal = Depends(_write)):
    from app.knowme import assets as kassets
    from app.knowme import registry as kreg

    _km_or_404(km_id, principal)
    kassets.delete_asset(km_id, asset_id)
    kreg.remove_asset(km_id, asset_id)
    return {"ok": True}


@router.post("/know-me/{km_id}/fields/{field_id}/suggest")
async def suggest_know_me_field_endpoint(
    km_id: str, field_id: str, principal: Principal = Depends(_write)
):
    """AI-infer a short list of realistic answer OPTIONS for one human-completion field
    (P3, on-demand). Returns ``{choices: [...]}`` and caches them onto the field so the UI can
    offer a dropdown. Best-effort — returns the field's existing choices on any failure."""
    from app.knowme import context as kctx
    from app.knowme import generator as kgen
    from app.knowme import registry as kreg
    from app.knowme import sections as km

    km_doc, arch = _km_or_404(km_id, principal)
    todos = list(km_doc.get("todos") or [])
    todo = next((t for t in todos if str(t.get("id")) == field_id), None)
    if todo is None:
        raise HTTPException(status_code=404, detail="Field not found.")

    workload_id = arch.get("workload_id") or km_doc.get("workload_id") or ""
    connection_id = arch.get("connection_id") or km_doc.get("connection_id") or ""
    wl = get_workload(workload_id) if workload_id else None
    wl_name = arch.get("workload_name", "") or km_doc.get("workload_name", "")
    section_label = km.section_label(str(todo.get("section_key") or ""))
    facts = km.scope_facts(wl, arch)
    known = await kctx.gather_known_facts(wl, arch, principal.tenant_id, connection_id, facts)
    evidence = await kctx.gather_evidence(km_doc.get("architecture_id", ""), workload_id, principal.tenant_id, connection_id)

    options = await kgen.suggest_field_choices(
        label=str(todo.get("label") or ""),
        field_key=str(todo.get("field_key") or ""),
        section_label=section_label,
        workload_name=wl_name,
        known_block=known.get("block", ""),
        evidence_block=evidence.get("block", ""),
    )
    if not options:
        return {"choices": todo.get("choices") or [], "choice_source": todo.get("choice_source") or ""}

    # Merge AI options after any existing choices (preserve order, dedup) + persist.
    merged: list[str] = []
    for v in [*(todo.get("choices") or []), *options]:
        s = str(v).strip()
        if s and s not in merged:
            merged.append(s)
    merged = merged[:12]
    for t in todos:
        if str(t.get("id")) == field_id:
            t["choices"] = merged
            t["allow_custom"] = True
            if not t.get("choice_source"):
                t["choice_source"] = "ai"
    kreg.update_know_me(
        km_id, todos=todos, source="edited",
        tenant_id=principal.tenant_id, actor=_actor(principal), reason="Suggested field options",
    )
    return {"choices": merged, "choice_source": "ai"}


async def _run_generate_know_me(
    progress: "Callable[[str, str], Awaitable[None]]",
    *, architecture_id: str, arch: dict[str, Any], principal: Principal, extra_context: str,
    target_km_id: str | None,
) -> dict[str, Any]:
    """Background-job runner (KP5/KU4): transform an architecture's Memory into a Know-Me and
    PERSIST it, reporting granular ``progress(phase, message)``. Returns the full know-me
    response (the job's ``done`` payload). Runs detached from any SSE subscriber, so it survives
    the browser navigating away — the document is saved regardless. Raises on hard failure (the
    registry turns that into an ``error`` job)."""
    from app.architectures import memory as mem
    from app.knowme import context as kctx
    from app.knowme import generator as kgen
    from app.knowme import registry as kreg
    from app.knowme import sections as km

    workload_id = arch.get("workload_id") or ""
    connection_id = arch.get("connection_id") or ""
    tenant_id = principal.tenant_id

    memory = mem.get_memory(architecture_id)
    if memory is None or not any(str(s.get("content") or "").strip() for s in memory.get("sections", []) or []):
        raise ValueError("No architecture memory to transform. Generate the Memory first.")

    wl = get_workload(workload_id) if workload_id else None
    wl_name = arch.get("workload_name", "") or (wl or {}).get("name", "")
    await progress("scope", "🔎 Resolving the workload's Azure scope & known values…")
    facts = km.scope_facts(wl, arch)
    known = await kctx.gather_known_facts(wl, arch, tenant_id, connection_id, facts)
    await progress("evidence", "📊 Pulling posture evidence (assessments, coverage, profiler)…")
    evidence = await kctx.gather_evidence(architecture_id, workload_id, tenant_id, connection_id)

    await progress("ai", "Transforming the architecture memory into a Know-Me…")
    queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    async def _gen_progress(phase: str, message: str) -> None:
        await queue.put({"phase": phase, "message": message})

    gen_task = asyncio.create_task(
        kgen.generate_know_me(
            workload_name=wl_name, memory=memory, facts=facts, progress=_gen_progress,
            extra_context=extra_context, known_block=known.get("block", ""),
            evidence_block=evidence.get("block", ""),
        )
    )
    while not gen_task.done() or not queue.empty():
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=0.25)
        except asyncio.TimeoutError:
            continue
        await progress(ev.get("phase", "ai"), ev.get("message", ""))
    result = await gen_task
    if result is None:
        raise RuntimeError("The AI could not draft the Know-Me. Try again.")

    await progress("save", "💾 Validating sections & saving…")
    existing = kreg.get_know_me(target_km_id) if target_km_id else None
    sections = kreg.merge_ai_sections((existing or {}).get("sections"), result["sections"])
    todos, autofilled = _finalize_know_me_todos(sections, existing, known)
    if autofilled:
        await progress("autofill", f"✅ Auto-filled {autofilled} field(s) from platform data.")
    ai_meta = {
        "confidence": result.get("confidence"),
        "passes": result.get("passes"),
        "autofilled": autofilled,
        "evidence_used": _evidence_summary(evidence),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": _actor(principal),
    }
    if target_km_id and existing is not None:
        km_doc = kreg.update_know_me(
            target_km_id, workload_id=workload_id, workload_name=wl_name, connection_id=connection_id,
            sections=sections, todos=todos, source="hybrid", ai=ai_meta,
            tenant_id=tenant_id, actor=_actor(principal), reason="Generated with AI",
        )
    else:
        km_doc = kreg.create_know_me(
            architecture_id=architecture_id, workload_id=workload_id, workload_name=wl_name,
            connection_id=connection_id, tenant_id=tenant_id, actor=_actor(principal),
        )
        km_doc = kreg.update_know_me(
            km_doc["id"], workload_id=workload_id, workload_name=wl_name, connection_id=connection_id,
            sections=sections, todos=todos, source="ai", ai=ai_meta,
            tenant_id=tenant_id, actor=_actor(principal), reason="Generated with AI",
        )
    return _know_me_response(architecture_id, arch, km_doc)


@router.post("/{architecture_id}/know-me/generate/stream")
async def generate_know_me_stream_endpoint(
    architecture_id: str,
    payload: KnowMeGenerateRequest = Body(default_factory=KnowMeGenerateRequest),
    principal: Principal = Depends(_write),
):
    """Create a NEW Know-Me for an architecture by transforming its Memory. Runs as a detached,
    navigation-surviving job; this call starts it (idempotently) and follows it via SSE → done."""
    arch = _tenant_arch_or_404(architecture_id, principal)
    extra_context = payload.extra_context or ""
    key = f"new:{architecture_id}"

    def _runner(progress):
        return _run_generate_know_me(
            progress, architecture_id=architecture_id, arch=arch, principal=principal,
            extra_context=extra_context, target_km_id=None,
        )

    _knowme_jobs.start(key, _runner)
    return EventSourceResponse(_knowme_jobs.stream(key))


@router.post("/know-me/{km_id}/generate/stream")
async def regenerate_know_me_stream_endpoint(
    km_id: str,
    payload: KnowMeGenerateRequest = Body(default_factory=KnowMeGenerateRequest),
    principal: Principal = Depends(_write),
):
    """Regenerate an EXISTING Know-Me document from its architecture's Memory. Runs as a
    detached, navigation-surviving job keyed by km_id; SSE follows it → done."""
    km_doc, arch = _km_or_404(km_id, principal)
    if not arch:
        raise HTTPException(status_code=400, detail="The source architecture no longer exists.")
    extra_context = payload.extra_context or ""
    key = f"km:{km_id}"

    def _runner(progress):
        return _run_generate_know_me(
            progress, architecture_id=km_doc.get("architecture_id", ""), arch=arch, principal=principal,
            extra_context=extra_context, target_km_id=km_id,
        )

    _knowme_jobs.start(key, _runner)
    return EventSourceResponse(_knowme_jobs.stream(key))


@router.get("/know-me/{km_id}/generate/job")
async def know_me_generate_job_endpoint(km_id: str, principal: Principal = Depends(get_principal)):
    """KP5/KU4 — current generation-job status for a Know-Me doc (for reconnect on page visit).
    Returns ``{job: null}`` when nothing is running/recent so the client can resume tailing a
    generation that started before it navigated here."""
    return {"job": _knowme_jobs.public_job(_knowme_jobs.get_job(f"km:{km_id}"))}


def _finalize_know_me_todos(
    sections: list[dict[str, Any]], existing: dict[str, Any] | None, known: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    """Parse typed todos, carry forward prior filled values (by id), then auto-fill from
    platform-known facts. Returns (todos, autofilled_count)."""
    from app.knowme import context as kctx
    from app.knowme import sections as km

    todos = km.parse_todos(sections)
    prior = {t["id"]: t for t in (existing or {}).get("todos", []) if t.get("id")}
    for t in todos:
        p = prior.get(t["id"])
        if p and p.get("value"):
            t["value"] = p["value"]
            t["status"] = p.get("status", "open")
            t["source"] = p.get("source", t.get("source", "human"))
    autofilled = kctx.autofill_todos(todos, known)
    return todos, autofilled


def _evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    """A compact record of what posture evidence fed the generation (for the 'how built' panel)."""
    cov = evidence.get("coverage") or {}
    return {
        "assessment": bool(evidence.get("assessment")),
        "assessment_findings": len((evidence.get("assessment") or {}).get("findings", [])),
        "coverage": sorted(cov.keys()),
        "performance": bool(evidence.get("performance")),
        "idle_resources": len(evidence.get("idle") or []),
    }


@router.post("/know-me/{km_id}/sections/{section_key}/generate/stream")
async def generate_know_me_section_stream_endpoint(
    km_id: str,
    section_key: str,
    payload: KnowMeSectionGenerateRequest = Body(default_factory=KnowMeSectionGenerateRequest),
    principal: Principal = Depends(_write),
):
    """Regenerate ONE Know-Me section with AI (A5), streaming detailed status. SSE:
    status… (phase=scope|evidence|ai|save) → done{know_me_response}."""
    km_doc, arch = _km_or_404(km_id, principal)
    tenant_id = principal.tenant_id
    architecture_id = km_doc.get("architecture_id", "")
    workload_id = arch.get("workload_id") or ""
    connection_id = arch.get("connection_id") or ""

    async def _gen():
        try:
            from app.architectures import memory as mem
            from app.knowme import context as kctx
            from app.knowme import generator as kgen
            from app.knowme import registry as kreg
            from app.knowme import sections as km

            if section_key not in km.SECTION_KEYS:
                yield {"event": "error", "data": json.dumps({"message": "Unknown section."})}
                return
            cur = kreg.get_know_me(km_id)
            if cur is None or cur.get("deleted_at"):
                yield {"event": "error", "data": json.dumps({"message": "No Know-Me to edit."})}
                return
            memory = mem.get_memory(architecture_id)
            if memory is None:
                yield {"event": "error", "data": json.dumps({"message": "No architecture memory to ground the section on."})}
                return

            label = km.section_label(section_key)
            wl = get_workload(workload_id) if workload_id else None
            wl_name = arch.get("workload_name", "") or (wl or {}).get("name", "")
            yield {"event": "status", "data": json.dumps({"phase": "scope", "message": f"🔎 Resolving scope & known values for “{label}”…"})}
            facts = km.scope_facts(wl, arch)
            known = await kctx.gather_known_facts(wl, arch, tenant_id, connection_id, facts)
            yield {"event": "status", "data": json.dumps({"phase": "evidence", "message": "📊 Pulling posture evidence (assessments, coverage, profiler)…"})}
            evidence = await kctx.gather_evidence(architecture_id, workload_id, tenant_id, connection_id)

            queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()

            async def _progress(phase: str, message: str) -> None:
                await queue.put({"phase": phase, "message": message})

            gen_task = asyncio.create_task(
                kgen.generate_section(
                    section_key=section_key, workload_name=wl_name, memory=memory, facts=facts,
                    current_sections=cur.get("sections"), extra_context=payload.extra_context or "",
                    known_block=known.get("block", ""), evidence_block=evidence.get("block", ""),
                    progress=_progress,
                )
            )
            while not gen_task.done() or not queue.empty():
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                yield {"event": "status", "data": json.dumps(ev)}
            content = await gen_task
            if content is None:
                yield {"event": "error", "data": json.dumps({"message": "The AI could not draft this section. Try again."})}
                return

            yield {"event": "status", "data": json.dumps({"phase": "save", "message": "💾 Saving the section…"})}
            sections = [dict(s) for s in (cur.get("sections") or [])]
            matched = False
            for s in sections:
                if str(s.get("key")) == section_key:
                    s["content"] = content
                    matched = True
            if not matched:
                sections.append({"key": section_key, "label": label, "content": content})
            todos, _ = _finalize_know_me_todos(sections, cur, known)
            saved = kreg.update_know_me(
                km_id, workload_id=workload_id, workload_name=wl_name, connection_id=connection_id,
                sections=sections, todos=todos, source="hybrid",
                tenant_id=tenant_id, actor=_actor(principal),
                reason=f"Regenerated section: {label}",
            )
            yield {"event": "done", "data": json.dumps(_know_me_response(architecture_id, arch, saved))}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Know-Me section regeneration failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


class KnowMeFromWorkloadRequest(BaseModel):
    workload_id: str
    connection_id: str | None = None
    # Optionally target a specific existing architecture; otherwise the newest one linked to
    # the workload is used, and if none exists an architecture is reverse-engineered.
    architecture_id: str | None = None
    extra_context: str = ""


async def _run_know_me_from_workload(
    progress: Callable[[str, str], Awaitable[None]],
    *,
    workload_id: str,
    explicit_arch_id: str,
    extra_context: str,
    connection_id: str,
    tenant_id: str,
    principal: Principal,
) -> dict[str, Any]:
    """The from-workload Know-Me pipeline, factored out so it runs as a detached,
    navigation-surviving background job (closing the modal / navigating away doesn't lose it).
    ``progress(phase, message)`` reports each step; raises ``RuntimeError`` with a user-facing
    message on a recoverable failure. Returns the ``_know_me_response`` for the built document."""
    from app.architectures import memory as mem
    from app.architectures.designer import generate_architecture
    from app.architectures.memory_designer import generate_memory
    from app.architectures.reverse import dump_resources
    from app.knowme import context as kctx
    from app.knowme import generator as kgen
    from app.knowme import registry as kreg
    from app.knowme import sections as km

    wl = get_workload(workload_id) if workload_id else None
    if wl is None:
        raise RuntimeError("Workload not found.")
    wl_name = wl.get("name", "") or "workload"
    conn_id = connection_id or wl.get("connection_id") or ""

    # 1) Resolve the architecture: explicit > newest linked to the workload > build new.
    await progress("architecture", f"🏗️ Resolving an architecture for '{wl_name}'…")
    arch: dict[str, Any] | None = None
    if explicit_arch_id:
        arch = arch_registry.get_architecture(explicit_arch_id)
        if arch is not None and (arch.get("tenant_id") or "") not in ("", tenant_id):
            arch = None
    if arch is None:
        linked = [a for a in arch_registry.list_architectures(tenant_id) if (a.get("workload_id") or "") == workload_id]
        arch = linked[0] if linked else None
    if arch is None:
        await progress("architecture", "🔎 No architecture yet — querying Azure Resource Graph for resources…")
        conn = resolve_connection(conn_id or None)
        dump = await dump_resources(wl, conn)
        if dump["error"]:
            raise RuntimeError(dump["error"])
        resources = dump["resources"] or []
        if not resources:
            raise RuntimeError("No resources found in this workload's scope — cannot reverse-engineer an architecture.")
        await progress("architecture", f"🤖 Reverse-engineering an architecture from {len(resources)} resource(s)…")
        ares = await generate_architecture(wl_name, resources)
        if ares is None:
            raise RuntimeError("The AI could not infer an architecture from this workload. Try again.")
        arch = arch_registry.upsert_architecture(
            {
                "name": ares["name"] or f"{wl_name} architecture",
                "description": ares["description"],
                "workload_id": workload_id,
                "workload_name": wl_name,
                "connection_id": conn_id,
                "tenant_id": tenant_id,
                "source": "ai",
                "nodes": ares["nodes"], "edges": ares["edges"], "groups": ares["groups"],
                "ai": {
                    "rationale": ares["rationale"], "confidence": ares["confidence"],
                    "resource_count": len(resources), "generated_by": _actor(principal),
                },
                "created_by": _actor(principal),
            },
            actor=_actor(principal), reason="Generated by AI (Know-Me pipeline)",
        )
    architecture_id = arch["id"]
    workload_name = arch.get("workload_name", "") or wl_name

    # 2) Ensure the architecture has a Memory (draft it with AI if missing/empty).
    memory = mem.get_memory(architecture_id)
    has_memory = memory is not None and any(
        str(s.get("content") or "").strip() for s in memory.get("sections", []) or []
    )
    if not has_memory:
        await progress("memory", "🧠 Drafting the architecture Memory with AI…")
        weakness_signals = await _gather_weakness_signals(architecture_id, workload_id, tenant_id, conn_id)
        conn = resolve_connection(conn_id or None)
        dump = await dump_resources(wl, conn)
        resources = dump.get("resources") or []

        async def _mprog(_phase: str, message: str) -> None:
            await progress("memory", message)

        mres = await generate_memory(
            arch, resources, weakness_signals, workload_name, progress=_mprog, extra_context=extra_context
        )
        if mres is None:
            raise RuntimeError("The AI could not draft the architecture Memory. Try again.")
        existing_mem = mem.get_memory(architecture_id)
        msections = mem.merge_ai_sections((existing_mem or {}).get("sections"), mres["sections"])
        memory = mem.upsert_memory(
            architecture_id, workload_id=workload_id, sections=msections,
            source="ai" if existing_mem is None else "hybrid",
            ai={
                "confidence": mres.get("confidence"),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "generated_by": _actor(principal), "resource_count": len(resources),
            },
            tenant_id=tenant_id, actor=_actor(principal), reason="Generated with AI (Know-Me pipeline)",
        )

    # 3) Transform the Memory into a Know-Me.
    await progress("knowme", "📄 Transforming the Memory into a Know-Me…")
    facts = km.scope_facts(wl, arch)
    await progress("knowme", "🔎 Resolving platform-known values & posture evidence…")
    known = await kctx.gather_known_facts(wl, arch, tenant_id, conn_id, facts)
    evidence = await kctx.gather_evidence(architecture_id, workload_id, tenant_id, conn_id)

    async def _kprog(_phase: str, message: str) -> None:
        await progress("knowme", message)

    kres = await kgen.generate_know_me(
        workload_name=workload_name, memory=memory, facts=facts, progress=_kprog,
        extra_context=extra_context, known_block=known.get("block", ""),
        evidence_block=evidence.get("block", ""),
    )
    if kres is None:
        raise RuntimeError("The AI could not draft the Know-Me. Try again.")

    await progress("save", "💾 Validating sections & saving…")
    sections = kreg.merge_ai_sections(None, kres["sections"])
    todos, autofilled = _finalize_know_me_todos(sections, None, known)
    if autofilled:
        await progress("autofill", f"✅ Auto-filled {autofilled} field(s) from platform data.")
    new_km = kreg.create_know_me(
        architecture_id=architecture_id, workload_id=workload_id, workload_name=workload_name,
        connection_id=conn_id, tenant_id=tenant_id, actor=_actor(principal),
    )
    km_doc = kreg.update_know_me(
        new_km["id"], workload_id=workload_id, workload_name=workload_name, connection_id=conn_id,
        sections=sections, todos=todos, source="ai",
        ai={
            "confidence": kres.get("confidence"),
            "passes": kres.get("passes"),
            "autofilled": autofilled,
            "evidence_used": _evidence_summary(evidence),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": _actor(principal),
        },
        tenant_id=tenant_id, actor=_actor(principal), reason="Generated with AI (from workload)",
    )
    return _know_me_response(architecture_id, arch, km_doc)


def _wlkm_key(tenant_id: str, workload_id: str) -> str:
    """Registry key for a from-workload Know-Me build (tenant-scoped so ``jobs_with_prefix``
    can list only the caller's builds)."""
    return f"wlkm:{tenant_id}:{workload_id}"


@router.post("/know-me/from-workload/stream")
async def know_me_from_workload_stream_endpoint(
    payload: KnowMeFromWorkloadRequest, principal: Principal = Depends(_write)
):
    """One-click pipeline from an Azure workload: ensure the workload has an architecture
    (reverse-engineering one with AI if needed), ensure that architecture has a Memory
    (drafting it with AI if needed), then transform the Memory into a Know-Me.

    Runs as a detached, navigation-surviving job keyed by workload, so closing the modal or
    navigating away keeps it building (the document still persists). This call starts the job
    idempotently and follows it via SSE (replay-then-tail): status… (phase=architecture|memory|
    knowme|save) → done{know_me_response}."""
    workload_id = payload.workload_id or ""
    if not workload_id:
        raise HTTPException(status_code=400, detail="workload_id is required.")
    tenant_id = principal.tenant_id
    key = _wlkm_key(tenant_id, workload_id)

    def _runner(progress: Callable[[str, str], Awaitable[None]]):
        return _run_know_me_from_workload(
            progress,
            workload_id=workload_id,
            explicit_arch_id=payload.architecture_id or "",
            extra_context=payload.extra_context or "",
            connection_id=payload.connection_id or "",
            tenant_id=tenant_id,
            principal=principal,
        )

    _knowme_jobs.start(key, _runner)
    return EventSourceResponse(_knowme_jobs.stream(key))


@router.get("/know-me/from-workload/active")
async def know_me_from_workload_active_endpoint(principal: Principal = Depends(get_principal)):
    """In-flight / recently-finished from-workload Know-Me builds for this tenant, so the
    index can show a background-progress tray and reattach after navigation. Completed jobs
    include the built document id under ``result`` so the tray can offer an Open link."""
    prefix = _wlkm_key(principal.tenant_id, "")
    jobs: list[dict[str, Any]] = []
    for job in _knowme_jobs.jobs_with_prefix(prefix):
        pub = _knowme_jobs.public_job(job)
        if pub is None:
            continue
        wl_id = str(job.get("key", "")).split(":", 2)[-1]
        result = None
        if job.get("status") == "done" and job.get("result"):
            r = job["result"]
            km_id = r.get("id") or (r.get("know_me") or {}).get("id") or ""
            if km_id:
                result = {"id": km_id}
        wl = get_workload(wl_id)
        jobs.append({**pub, "workload_id": wl_id, "workload_name": (wl or {}).get("name", ""), "result": result})
    return {"jobs": jobs}


# ------------------------------------------------------------- clone + revision history
@router.post("/{architecture_id}/clone")
async def clone_architecture_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    """Duplicate an architecture into a fresh Draft copy."""
    _tenant_arch_or_404(architecture_id, principal)
    cloned = arch_registry.clone_architecture(
        architecture_id, actor=_actor(principal), tenant_id=principal.tenant_id
    )
    if cloned is None:
        raise HTTPException(status_code=404, detail="Architecture not found.")
    return {"architecture": cloned}


@router.get("/{architecture_id}/revisions")
async def list_revisions_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    from app.architectures import revisions

    _tenant_arch_or_404(architecture_id, principal)
    return {"revisions": revisions.list_revisions(architecture_id)}


@router.get("/{architecture_id}/revisions/{revision_id}")
async def get_revision_endpoint(
    architecture_id: str, revision_id: str, principal: Principal = Depends(get_principal)
):
    """Full content of one revision, for read-only preview (does not restore)."""
    from app.architectures import revisions

    _tenant_arch_or_404(architecture_id, principal)
    rev = revisions.get_revision(architecture_id, revision_id)
    if rev is None:
        raise HTTPException(status_code=404, detail="Revision not found.")
    # Strip the internal dedup signature; return content + metadata for the preview.
    return {"revision": {k: v for k, v in rev.items() if k != "sig"}}


@router.post("/{architecture_id}/revisions/{revision_id}/restore")
async def restore_revision_endpoint(
    architecture_id: str, revision_id: str, principal: Principal = Depends(get_principal)
):
    _tenant_arch_or_404(architecture_id, principal)
    restored = arch_registry.restore_revision(architecture_id, revision_id, _actor(principal))
    if restored is None:
        raise HTTPException(status_code=404, detail="Architecture or revision not found.")
    return {"architecture": restored}


@router.get("/{architecture_id}/activity")
async def list_activity_endpoint(architecture_id: str, principal: Principal = Depends(get_principal)):
    """The management activity log (status/category changes, edits, clone, restore, AI)."""
    from app.architectures import activity

    _tenant_arch_or_404(architecture_id, principal)
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

    arch = _tenant_arch_or_404(architecture_id, principal)
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
    architecture_id: str, payload: AskRequest, principal: Principal = Depends(get_principal)
):
    """Grounded Q&A about an architecture (SPOFs, blast radius, zone-redundancy, etc.)."""
    from app.architectures.designer import answer_question

    arch = _tenant_arch_or_404(architecture_id, principal)
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

    arch = _tenant_arch_or_404(architecture_id, principal)
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

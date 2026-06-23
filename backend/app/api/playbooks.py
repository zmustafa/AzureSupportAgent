"""Playbook endpoints: CRUD over the JSON registry + run (chained workbooks)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import PlaybookRun
from app.playbooks import registry as pb_registry
from app.playbooks.runner import run_playbook

router = APIRouter(prefix="/playbooks", tags=["playbooks"])

# Viewing/exporting playbooks requires playbooks.read; creating, editing, importing,
# AI-designing, and running them requires playbooks.write. The `get_principal` alias is the
# read tier (so existing call sites stay correct); write endpoints opt into `_write`.
# Admins always pass via require_permission.
get_principal = require_permission("playbooks.read")
_write = require_permission("playbooks.write")
logger = logging.getLogger("app.api.playbooks")


def _tenant_playbook_or_404(playbook_id: str, principal: Principal) -> dict[str, Any]:
    """Load a playbook and verify the caller's tenant owns it.

    Centralizes the per-id IDOR guard. An empty `tenant_id` on a registry row is a
    legacy global record visible to any tenant (matches the list endpoint). Otherwise a
    mismatch raises 404 (not 403) so a cross-tenant probe cannot confirm existence.
    """
    pb = pb_registry.get_playbook(playbook_id)
    if pb is None:
        raise HTTPException(status_code=404, detail="Playbook not found.")
    pb_tenant = pb.get("tenant_id") or ""
    if pb_tenant and pb_tenant != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Playbook not found.")
    return pb


class PlaybookStep(BaseModel):
    id: str = ""
    name: str = ""
    workbook_id: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    param_map: dict[str, str] = Field(default_factory=dict)
    run_if: str = "always"


class AlertCfg(BaseModel):
    enabled: bool = False
    min_severity: str = "warning"


class PlaybookUpsert(BaseModel):
    id: str | None = None
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    connection_id: str = ""
    steps: list[PlaybookStep] = Field(default_factory=list)
    alert: AlertCfg = Field(default_factory=AlertCfg)
    enabled: bool = True


@router.get("")
async def list_playbooks_endpoint(principal: Principal = Depends(get_principal)):
    tid = principal.tenant_id
    rows = [p for p in pb_registry.list_playbooks() if (p.get("tenant_id") or "") in ("", tid)]
    return {"playbooks": rows}


@router.put("")
async def upsert_playbook_endpoint(
    payload: PlaybookUpsert, principal: Principal = Depends(_write)
):
    data = payload.model_dump()
    # When updating an existing playbook, verify the caller owns it before letting
    # the merge proceed (otherwise a user could overwrite any tenant's playbook by
    # supplying its id).
    if payload.id:
        _tenant_playbook_or_404(payload.id, principal)
    if not payload.id:
        data["created_by"] = principal.subject
    if not data.get("tenant_id"):
        data["tenant_id"] = principal.tenant_id
    # Never let a caller forge an arbitrary tenant_id on the bundle; lock it to theirs.
    data["tenant_id"] = principal.tenant_id
    saved = pb_registry.upsert_playbook(data)
    return {"playbook": saved}


@router.delete("/{playbook_id}")
async def delete_playbook_endpoint(
    playbook_id: str, principal: Principal = Depends(_write)
):
    _tenant_playbook_or_404(playbook_id, principal)
    if not pb_registry.delete_playbook(playbook_id):
        raise HTTPException(status_code=404, detail="Playbook not found.")
    return {"ok": True}


# ============================ Import / export ============================
class PlaybookImportRequest(BaseModel):
    bundle: dict[str, Any]


@router.get("/{playbook_id}/export")
async def export_playbook_endpoint(playbook_id: str, principal: Principal = Depends(get_principal)):
    """A portable bundle for a playbook, inlining every workbook its steps reference."""
    from app.automations.portability import export_playbook

    _tenant_playbook_or_404(playbook_id, principal)
    bundle = export_playbook(playbook_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Playbook not found.")
    return bundle


@router.post("/import")
async def import_playbook_endpoint(
    payload: PlaybookImportRequest, principal: Principal = Depends(_write)
):
    """Create a playbook from a bundle: imports referenced workbooks (de-duped by
    content), remaps step references, then creates the playbook.

    The imported playbook is forced into the caller's tenant regardless of what the
    bundle says — a bundle should never elevate into another tenant or land in the
    legacy global scope.
    """
    from app.automations.portability import ImportError_, import_playbook

    try:
        result = import_playbook(
            payload.bundle, actor=principal.subject, tenant_id=principal.tenant_id
        )
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TypeError:
        # Backwards-compat fallback if the portability helper hasn't been updated yet:
        # fall back to the no-tenant signature, then patch the saved playbook's tenant.
        result = import_playbook(payload.bundle, actor=principal.subject)
        saved_pb_id = (result or {}).get("playbook", {}).get("id") if isinstance(result, dict) else None
        if saved_pb_id:
            existing = pb_registry.get_playbook(saved_pb_id) or {}
            existing["id"] = saved_pb_id
            existing["tenant_id"] = principal.tenant_id
            pb_registry.upsert_playbook(existing)
    return result


# ============================ AI designer ============================
class PbInterviewRequest(BaseModel):
    goal: str = Field(default="", max_length=4000)
    answers: list[dict[str, Any]] = Field(default_factory=list)
    step: int = 0


class PbGenerateRequest(BaseModel):
    goal: str = Field(default="", max_length=4000)
    answers: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/draft/interview")
async def playbook_interview_endpoint(payload: PbInterviewRequest, _: Principal = Depends(_write)):
    """Next batch of clarifying questions for designing a new playbook."""
    from app.playbooks.designer import next_questions

    return await next_questions(
        payload.goal, payload.answers[:50], payload.step, pb_registry_workbooks()
    )


@router.post("/draft/generate")
async def playbook_generate_endpoint(payload: PbGenerateRequest, _: Principal = Depends(_write)):
    """Generate a complete playbook draft, grounded on the existing workbook catalog."""
    from app.playbooks.designer import generate_playbook

    draft = await generate_playbook(payload.goal, payload.answers[:50], pb_registry_workbooks())
    if draft is None:
        raise HTTPException(status_code=502, detail="The AI could not draft a playbook. Try again.")
    return {"draft": draft}


def pb_registry_workbooks() -> list[dict[str, Any]]:
    """The workbook catalog used to ground playbook generation (id/name/runtime/desc)."""
    from app.workbooks import registry as wb_registry

    return [
        {"id": w["id"], "name": w.get("name", ""), "runtime": w.get("runtime", ""), "description": w.get("description", "")}
        for w in wb_registry.list_workbooks()
    ]


@router.post("/{playbook_id}/run")
async def run_playbook_endpoint(
    playbook_id: str, principal: Principal = Depends(_write)
):
    _tenant_playbook_or_404(playbook_id, principal)
    try:
        result = await run_playbook(
            playbook_id, tenant_id=principal.tenant_id, actor=principal.subject, trigger="manual"
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"result": result}


@router.get("/runs")
async def list_playbook_runs_endpoint(
    playbook_id: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Run history for a playbook (or all playbooks) — most recent first."""
    q = select(PlaybookRun).where(PlaybookRun.tenant_id == principal.tenant_id)
    if playbook_id:
        q = q.where(PlaybookRun.playbook_id == playbook_id)
    q = q.order_by(desc(PlaybookRun.started_at)).limit(min(limit, 200))
    rows = (await db.execute(q)).scalars().all()
    return {
        "runs": [
            {
                "id": r.id,
                "playbook_id": r.playbook_id,
                "playbook_name": r.playbook_name,
                "trigger": r.trigger,
                "status": r.status,
                "severity": r.severity,
                "steps": r.steps_json or [],
                "step_count": r.step_count,
                "error": r.error,
                "duration_ms": r.duration_ms,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            }
            for r in rows
        ]
    }

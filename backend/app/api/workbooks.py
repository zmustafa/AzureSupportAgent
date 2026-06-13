"""Workbook endpoints: CRUD over the JSON registry + run, run-history, and tiles.

Per the product decision, any authenticated user may author and run workbooks (writes
are still gated by the connection's read-only flag + command-runner approval)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, get_principal
from app.models import WorkbookRun
from app.workbooks import registry as wb_registry
from app.workbooks.executor import preview_workbook, run_workbook

router = APIRouter(prefix="/workbooks", tags=["workbooks"])
logger = logging.getLogger("app.api.workbooks")


class AifyCfg(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    enabled: bool = True
    modes: list[str] = Field(default_factory=lambda: ["summary", "severity"])
    schema_hint: str = Field(default="", alias="schema")


class AlertCfg(BaseModel):
    enabled: bool = False
    min_severity: str = "warning"


class TileCfg(BaseModel):
    enabled: bool = False
    label: str = ""
    format: str = "severity"
    metric_key: str = ""


class WorkbookParam(BaseModel):
    key: str
    label: str = ""
    type: str = "text"
    default: Any = ""
    required: bool = False
    help: str = ""


class WorkbookUpsert(BaseModel):
    id: str | None = None
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    runtime: str = "az"
    body: str = Field(default="", max_length=8000)
    params: list[WorkbookParam] = Field(default_factory=list)
    kind: str = "read"
    tags: list[str] = Field(default_factory=list)
    connection_id: str = ""
    aify: AifyCfg = Field(default_factory=AifyCfg)
    alert: AlertCfg = Field(default_factory=AlertCfg)
    tile: TileCfg = Field(default_factory=TileCfg)
    enabled: bool = True


class WorkbookRunRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None
    confirm: bool = False


class WorkbookPreviewRequest(BaseModel):
    """A full (possibly unsaved) workbook draft plus runtime params, for a test run."""
    workbook: WorkbookUpsert
    params: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None
    confirm: bool = False


@router.get("")
async def list_workbooks_endpoint(principal: Principal = Depends(get_principal)):
    tid = principal.tenant_id
    rows = [w for w in wb_registry.list_workbooks() if (w.get("tenant_id") or "") in ("", tid)]
    return {"workbooks": rows}


@router.put("")
async def upsert_workbook_endpoint(
    payload: WorkbookUpsert, principal: Principal = Depends(get_principal)
):
    if payload.runtime not in wb_registry.RUNTIMES:
        raise HTTPException(status_code=400, detail=f"Invalid runtime '{payload.runtime}'.")
    data = payload.model_dump(by_alias=True)
    if not payload.id:
        data["created_by"] = principal.subject
    if not data.get("tenant_id"):
        data["tenant_id"] = principal.tenant_id
    saved = wb_registry.upsert_workbook(data)
    return {"workbook": saved}


@router.delete("/{workbook_id}")
async def delete_workbook_endpoint(
    workbook_id: str, _: Principal = Depends(get_principal)
):
    if not wb_registry.delete_workbook(workbook_id):
        raise HTTPException(status_code=404, detail="Workbook not found.")
    return {"ok": True}


# ============================ Import / export ============================
class WorkbookImportRequest(BaseModel):
    bundle: dict[str, Any]


@router.get("/{workbook_id}/export")
async def export_workbook_endpoint(workbook_id: str, _: Principal = Depends(get_principal)):
    """A portable JSON bundle for a single workbook (no ids/secrets)."""
    from app.automations.portability import export_workbook

    bundle = export_workbook(workbook_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Workbook not found.")
    return bundle


@router.post("/import")
async def import_workbook_endpoint(
    payload: WorkbookImportRequest, principal: Principal = Depends(get_principal)
):
    """Create a workbook from an exported bundle (name de-duplicated on collision)."""
    from app.automations.portability import ImportError_, import_workbook

    try:
        saved = import_workbook(payload.bundle, actor=principal.subject)
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"workbook": saved}


# ============================ AI designer ============================
class WbInterviewRequest(BaseModel):
    goal: str = Field(default="", max_length=4000)
    answers: list[dict[str, Any]] = Field(default_factory=list)
    step: int = 0


class WbGenerateRequest(BaseModel):
    goal: str = Field(default="", max_length=4000)
    answers: list[dict[str, Any]] = Field(default_factory=list)


class WbEnhanceRequest(BaseModel):
    answers: list[dict[str, Any]] = Field(default_factory=list)
    step: int = 0


@router.post("/draft/interview")
async def workbook_interview_endpoint(payload: WbInterviewRequest, _: Principal = Depends(get_principal)):
    """Next batch of clarifying questions for designing a new workbook."""
    from app.workbooks.designer import next_questions

    return await next_questions(payload.goal, payload.answers[:50], payload.step)


@router.post("/draft/generate")
async def workbook_generate_endpoint(payload: WbGenerateRequest, _: Principal = Depends(get_principal)):
    """Generate a complete workbook draft from the interview, grounded on connections."""
    from app.core.azure_connections import list_connections
    from app.workbooks.designer import generate_workbook

    draft = await generate_workbook(payload.goal, payload.answers[:50], list_connections())
    if draft is None:
        raise HTTPException(status_code=502, detail="The AI could not draft a workbook. Try again.")
    return {"draft": draft}


@router.post("/{workbook_id}/enhance/interview")
async def workbook_enhance_interview_endpoint(
    workbook_id: str, payload: WbEnhanceRequest, _: Principal = Depends(get_principal)
):
    from app.workbooks.designer import enhance_questions

    wb = wb_registry.get_workbook(workbook_id)
    if wb is None:
        raise HTTPException(status_code=404, detail="Workbook not found.")
    return await enhance_questions(wb, payload.answers[:50], payload.step)


@router.post("/{workbook_id}/enhance/generate")
async def workbook_enhance_generate_endpoint(
    workbook_id: str, payload: WbEnhanceRequest, _: Principal = Depends(get_principal)
):
    from app.core.azure_connections import list_connections
    from app.workbooks.designer import enhance_workbook

    wb = wb_registry.get_workbook(workbook_id)
    if wb is None:
        raise HTTPException(status_code=404, detail="Workbook not found.")
    draft = await enhance_workbook(wb, payload.answers[:50], list_connections())
    if draft is None:
        raise HTTPException(status_code=502, detail="The AI could not enhance this workbook. Try again.")
    return {
        "draft": draft,
        "current": {
            "name": wb.get("name"),
            "runtime": wb.get("runtime"),
            "body": wb.get("body"),
            "params": wb.get("params", []),
        },
    }


@router.post("/{workbook_id}/run")
async def run_workbook_endpoint(
    workbook_id: str,
    payload: WorkbookRunRequest,
    principal: Principal = Depends(get_principal),
):
    try:
        run = await run_workbook(
            workbook_id,
            tenant_id=principal.tenant_id,
            actor=principal.subject,
            params=payload.params,
            connection_id=payload.connection_id,
            trigger="manual",
            confirm=payload.confirm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"run": run}


@router.post("/preview")
async def preview_workbook_endpoint(
    payload: WorkbookPreviewRequest,
    _: Principal = Depends(get_principal),
):
    """Execute an unsaved workbook draft and return its result without persisting it."""
    if payload.workbook.runtime not in wb_registry.RUNTIMES:
        raise HTTPException(
            status_code=400, detail=f"Invalid runtime '{payload.workbook.runtime}'."
        )
    draft = payload.workbook.model_dump(by_alias=True)
    run = await preview_workbook(
        draft,
        params=payload.params,
        connection_id=payload.connection_id,
        confirm=payload.confirm,
    )
    return {"run": run}


@router.get("/runs")
async def list_runs_endpoint(
    workbook_id: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    q = select(WorkbookRun).where(WorkbookRun.tenant_id == principal.tenant_id)
    if workbook_id:
        q = q.where(WorkbookRun.workbook_id == workbook_id)
    q = q.order_by(desc(WorkbookRun.started_at)).limit(min(limit, 200))
    rows = (await db.execute(q)).scalars().all()
    return {"runs": [_run_row(r) for r in rows]}


@router.get("/tiles")
async def tiles_endpoint(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Latest run per tile-enabled workbook, shaped for Monitor dashboard tiles."""
    tiles: list[dict[str, Any]] = []
    for wb in wb_registry.list_workbooks():
        tile = wb.get("tile", {}) or {}
        if not tile.get("enabled"):
            continue
        latest = (
            await db.execute(
                select(WorkbookRun)
                .where(
                    WorkbookRun.workbook_id == wb["id"],
                    WorkbookRun.tenant_id == principal.tenant_id,
                )
                .order_by(desc(WorkbookRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        value: Any = None
        if latest and tile.get("format") == "number":
            metric_key = tile.get("metric_key", "")
            structured = latest.structured_json or {}
            if isinstance(structured, dict):
                value = structured.get(metric_key)
        tiles.append(
            {
                "workbook_id": wb["id"],
                "label": tile.get("label") or wb["name"],
                "format": tile.get("format", "severity"),
                "metric_key": tile.get("metric_key", ""),
                "value": value,
                "severity": latest.severity if latest else None,
                "narrative": latest.narrative if latest else None,
                "ran_at": latest.started_at.isoformat() if latest and latest.started_at else None,
                "status": latest.status if latest else "never",
            }
        )
    return {"tiles": tiles}


def _run_row(r: WorkbookRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "workbook_id": r.workbook_id,
        "workbook_name": r.workbook_name,
        "runtime": r.runtime,
        "command": r.command,
        "status": r.status,
        "exit_code": r.exit_code,
        "output": r.output,
        "structured": r.structured_json,
        "narrative": r.narrative,
        "severity": r.severity,
        "diff": r.diff_json,
        "error": r.error,
        "duration_ms": r.duration_ms,
        "trigger": r.trigger,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
    }

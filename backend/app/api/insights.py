"""AI Insight Packs endpoints.

A pack is a reusable, scope-agnostic definition; scheduling a pack against a scope creates
a ScheduledTask (target_type="insight_pack") via the existing automations API. This router
owns the library (CRUD + templates + AI generator), on-demand / test runs, and the run
history that the digest UI and dashboard tiles read.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.schedule import compute_next_run, human_schedule
from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.insights import designer, packfile, registry, runs as runs_store, sources, starters
from app.models import ScheduledTask

router = APIRouter(prefix="/insights", tags=["insights"])
log = logging.getLogger("app.api.insights")

_read = require_permission("insights.read")
_write = require_permission("insights.write")
_run = require_permission("insights.run")


def _actor(p: Principal) -> str:
    return p.display_name or p.email or p.subject


# --------------------------------------------------------------------------- library
@router.get("/packs")
async def list_packs_endpoint(_: Principal = Depends(_read)) -> dict[str, Any]:
    return {
        "packs": registry.list_packs(),
        "categories": registry.CATEGORIES,
        "sources": sources.SOURCE_CATALOG,
        "flag_codes": designer.FLAG_CODES,
        "verdicts": list(packfile.VERDICTS),
    }


@router.get("/templates")
async def list_templates_endpoint(_: Principal = Depends(_read)) -> dict[str, Any]:
    return {"templates": [packfile.normalize(s) for s in starters.STARTERS]}


@router.get("/packs/{pack_id}")
async def get_pack_endpoint(pack_id: str, _: Principal = Depends(_read)) -> dict[str, Any]:
    pack = registry.get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="Insight pack not found.")
    return {"pack": pack, "markdown": packfile.to_markdown(pack)}


class PackUpsert(BaseModel):
    id: str = ""
    name: str = ""
    icon: str = "🧠"
    category: str = "general"
    description: str = ""
    sources: list[str] = Field(default_factory=lambda: ["change_explorer"])
    supported_scopes: list[str] = Field(default_factory=lambda: ["workload", "subscription", "tenant"])
    lookback_hours: int = 24
    filters: dict[str, Any] = Field(default_factory=dict)
    materiality: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    instructions: str = ""
    enabled: bool = True


@router.put("/packs")
async def upsert_pack_endpoint(payload: PackUpsert, principal: Principal = Depends(_write)) -> dict[str, Any]:
    pack = registry.upsert_pack(payload.model_dump(), actor=_actor(principal))
    return {"pack": pack}


class MarkdownBody(BaseModel):
    markdown: str = ""


@router.post("/packs/parse-markdown")
async def parse_markdown_endpoint(body: MarkdownBody, _: Principal = Depends(_write)) -> dict[str, Any]:
    """Parse a raw ``.pack.md`` document into a normalized pack draft (for the raw-MD editor)."""
    return {"pack": packfile.parse(body.markdown)}


@router.delete("/packs/{pack_id}")
async def delete_pack_endpoint(pack_id: str, _: Principal = Depends(_write)) -> dict[str, Any]:
    if not registry.delete_pack(pack_id):
        raise HTTPException(status_code=404, detail="Insight pack not found.")
    return {"ok": True}


class EnableBody(BaseModel):
    enabled: bool = True


@router.post("/packs/{pack_id}/enable")
async def enable_pack_endpoint(pack_id: str, body: EnableBody, _: Principal = Depends(_write)) -> dict[str, Any]:
    pack = registry.set_enabled(pack_id, body.enabled)
    if pack is None:
        raise HTTPException(status_code=404, detail="Insight pack not found.")
    return {"pack": pack}


@router.post("/packs/{pack_id}/clone")
async def clone_pack_endpoint(pack_id: str, principal: Principal = Depends(_write)) -> dict[str, Any]:
    pack = registry.clone_pack(pack_id, actor=_actor(principal))
    if pack is None:
        raise HTTPException(status_code=404, detail="Insight pack or template not found.")
    return {"pack": pack}


# --------------------------------------------------------------------------- AI generator
class InterviewRequest(BaseModel):
    goal: str = ""
    answers: list[dict[str, Any]] = Field(default_factory=list)
    step: int = 0


@router.post("/draft/interview")
async def interview_endpoint(payload: InterviewRequest, _: Principal = Depends(_write)) -> dict[str, Any]:
    return await designer.next_questions(payload.goal, payload.answers, payload.step)


class GenerateRequest(BaseModel):
    goal: str = ""
    answers: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/draft/generate")
async def generate_endpoint(payload: GenerateRequest, _: Principal = Depends(_write)) -> dict[str, Any]:
    result = await designer.generate_pack(payload.goal, payload.answers)
    if result is None:
        raise HTTPException(status_code=502, detail="The AI could not generate a pack. Try again.")
    return result


# --------------------------------------------------------------------------- runs
class RunRequest(BaseModel):
    pack_id: str = ""
    pack: dict[str, Any] | None = None  # inline (unsaved) pack for a test run
    scope: dict[str, Any] = Field(default_factory=dict)
    overrides: dict[str, Any] = Field(default_factory=dict)
    notify: bool = False  # test runs default to NOT notifying


@router.post("/run")
async def run_endpoint(payload: RunRequest, principal: Principal = Depends(_run)) -> dict[str, Any]:
    """Run a pack on demand (or test an inline draft). Test runs default to notify=False."""
    from app.insights.runner import run_pack

    pack = payload.pack or registry.get_pack(payload.pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Insight pack not found.")
    digest = await run_pack(
        pack, payload.scope, tenant_id=principal.tenant_id,
        overrides=payload.overrides, trigger="manual", notify=payload.notify,
    )
    return {"run": digest}


@router.get("/runs")
async def list_runs_endpoint(pack_id: str = "", limit: int = 100,
                             principal: Principal = Depends(_read)) -> dict[str, Any]:
    return {"runs": runs_store.list_runs(principal.tenant_id, pack_id=pack_id or None, limit=min(500, max(1, limit)))}


@router.get("/latest")
async def latest_runs_endpoint(principal: Principal = Depends(_read)) -> dict[str, Any]:
    """The latest run per pack — powers the Dashboard 'Daily Intelligence' tiles."""
    by_pack: dict[str, dict[str, Any]] = {}
    for r in runs_store.list_runs(principal.tenant_id, limit=500):
        pid = r.get("pack_id", "")
        if pid and pid not in by_pack:
            by_pack[pid] = r
    return {"latest": list(by_pack.values())}


@router.get("/runs/{run_id}")
async def get_run_endpoint(run_id: str, principal: Principal = Depends(_read)) -> dict[str, Any]:
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"run": run}


# --------------------------------------------------------------------------- upcoming schedule
@router.get("/schedule/upcoming")
async def upcoming_endpoint(days: int = 7, principal: Principal = Depends(_read),
                            db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Project the next occurrences of every enabled insight-pack schedule over a window —
    powers the 'Next N days' timeline."""
    days = min(31, max(1, days))
    horizon = datetime.now(timezone.utc) + timedelta(days=days)
    rows = (await db.execute(
        select(ScheduledTask).where(
            ScheduledTask.tenant_id == principal.tenant_id,
            ScheduledTask.target_type == "insight_pack",
            ScheduledTask.status == "on",
            ScheduledTask.deleted_at.is_(None),
        )
    )).scalars().all()
    occurrences: list[dict[str, Any]] = []
    for t in rows:
        cfg = t.target_config or {}
        pack = registry.get_pack(cfg.get("pack_id") or "") or {}
        task_dict = {
            "schedule_kind": t.schedule_kind, "cron_expr": t.cron_expr, "time_of_day": t.time_of_day,
            "weekday": t.weekday, "timezone": t.timezone, "start_date": t.start_date, "end_date": t.end_date,
        }
        after: datetime | None = None
        for _ in range(50):  # bound the projection per task
            nxt = compute_next_run(task_dict, after=after)
            if nxt is None or nxt > horizon:
                break
            occurrences.append({
                "task_id": t.id, "task_name": t.name,
                "pack_id": pack.get("id", ""), "pack_name": pack.get("name", ""),
                "pack_icon": pack.get("icon", "🧠"),
                "at": nxt.isoformat(), "schedule_label": human_schedule(task_dict),
            })
            after = nxt
    occurrences.sort(key=lambda o: o["at"])
    return {"days": days, "occurrences": occurrences}

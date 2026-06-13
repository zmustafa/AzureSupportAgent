"""Admin endpoints for automations: custom agents and scheduled tasks.

- Custom agents: CRUD over the JSON registry + a tool catalog (connector + Azure MCP).
- Scheduled tasks: CRUD over the DB, with run-now and per-task run history. Editing a
  task preserves its run history (TaskRun rows are never overwritten).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations import agents as agents_registry
from app.automations.schedule import compute_next_run, human_schedule
from app.connectors.registry import all_tool_names
from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.models import AuditLog, ScheduledTask, TaskRun

router = APIRouter(prefix="/admin/automations", tags=["automations"])
logger = logging.getLogger("app.api.automations")

# Strong refs to fire-and-forget manual-trigger tasks so they aren't GC'd mid-run
# ("Task was destroyed but it is pending"); discarded on completion.
_manual_runs: set[asyncio.Task] = set()


def _spawn_manual_run(task_id: str, target_type: str = "agent") -> None:
    """Run a task in the background, keeping a ref and logging any failure."""
    from app.automations.runner import run_target_task, run_task

    async def _runner() -> None:
        try:
            if target_type == "agent":
                await run_task(task_id, trigger="manual")
            else:
                await run_target_task(task_id, trigger="manual")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Manual task run %s failed: %s", task_id, exc)

    t = asyncio.create_task(_runner())
    _manual_runs.add(t)
    t.add_done_callback(_manual_runs.discard)


# --------------------------------------------------------------------------- agents
class AgentUpsert(BaseModel):
    id: str | None = None
    name: str = Field(max_length=200)
    # IMPORTANT: instructions defaults to None (not "") so a partial update — e.g. the
    # bulk model change that sends only {id, name, provider, model} — is excluded by
    # model_dump(exclude_none=True) and the registry PRESERVES the existing instructions.
    # A default of "" would be kept by exclude_none and WIPE every agent's instructions.
    instructions: str | None = Field(default=None, max_length=20000)
    provider: str | None = None
    model: str | None = None
    connection_id: str | None = None
    category: str | None = None
    allow_all_azure: bool | None = None
    allow_all_entra: bool | None = None
    connector_tools: list[str] | None = None
    run_mode: str | None = None
    enabled: bool | None = None


@router.get("/agents")
async def list_agents_endpoint(_: Principal = Depends(require_admin)):
    from app.automations.agents import CATEGORIES

    return {"agents": agents_registry.list_agents(), "tools": all_tool_names(), "categories": CATEGORIES}


@router.put("/agents")
async def upsert_agent_endpoint(
    payload: AgentUpsert,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    data = payload.model_dump(exclude_none=True)
    if not payload.id:
        data["created_by"] = principal.subject
    saved = agents_registry.upsert_agent(data)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="agent.upsert",
            target=saved["id"],
            metadata_json={"name": saved.get("name")},
        )
    )
    await db.commit()
    return {"agent": saved}


@router.delete("/agents/{agent_id}")
async def delete_agent_endpoint(
    agent_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not agents_registry.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found.")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="agent.delete",
            target=agent_id,
        )
    )
    await db.commit()
    return {"ok": True}


# ----------------------------------------------------------- agent export (config)
# The portable fields that define an agent (excludes server-managed id/timestamps).
_EXPORT_FIELDS = (
    "name",
    "instructions",
    "provider",
    "model",
    "connection_id",
    "allow_all_azure",
    "allow_all_entra",
    "connector_tools",
    "run_mode",
    "enabled",
)


def _agent_export_dict(agent: dict[str, Any]) -> dict[str, Any]:
    return {k: agent.get(k) for k in _EXPORT_FIELDS}


@router.get("/agents/{agent_id}/export")
async def export_agent_endpoint(
    agent_id: str,
    _: Principal = Depends(require_admin),
):
    """Export a single custom agent's config as a portable JSON object."""
    agent = agents_registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return {
        "version": 1,
        "kind": "azure-support-agent",
        "agent": _agent_export_dict(agent),
    }


class BulkExportRequest(BaseModel):
    # When omitted/empty, export ALL agents; otherwise only these ids.
    ids: list[str] = Field(default_factory=list)


@router.post("/agents/export")
async def export_agents_endpoint(
    payload: BulkExportRequest,
    _: Principal = Depends(require_admin),
):
    """Export multiple (or all) custom agents' configs as a portable JSON bundle."""
    all_agents = agents_registry.list_agents()
    if payload.ids:
        wanted = set(payload.ids)
        selected = [a for a in all_agents if a["id"] in wanted]
    else:
        selected = all_agents
    return {
        "version": 1,
        "kind": "azure-support-agents",
        "count": len(selected),
        "agents": [_agent_export_dict(a) for a in selected],
    }


# ----------------------------------------------------------- agent import (config)
class ImportRequest(BaseModel):
    # The parsed JSON of an export file. Accepts any of the export shapes:
    #   {"agent": {...}}            (single export)
    #   {"agents": [{...}, ...]}    (bulk export)
    #   {...}                       (a bare agent object)
    #   [{...}, ...]                (a bare list of agents)
    data: Any
    # When True (default), an imported agent whose name matches an existing one updates
    # that agent in place; when False, a name clash creates a new agent with a suffix.
    overwrite_existing: bool = True


def _coerce_imported_agents(data: Any) -> list[dict[str, Any]]:
    """Pull a list of agent dicts out of any supported export/import shape."""
    if isinstance(data, dict):
        if isinstance(data.get("agents"), list):
            items = data["agents"]
        elif isinstance(data.get("agent"), dict):
            items = [data["agent"]]
        elif data.get("name"):
            items = [data]
        else:
            items = []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    return [a for a in items if isinstance(a, dict) and str(a.get("name", "")).strip()]


@router.post("/agents/import")
async def import_agents_endpoint(
    payload: ImportRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Import custom agents from a previously exported JSON config (single or bulk).

    Only the portable fields are honoured; ids/timestamps are server-managed. By
    default an imported agent updates an existing agent of the same name; otherwise a
    uniquely-suffixed copy is created. Unknown connector tools are dropped so an import
    can never reference a tool this deployment doesn't have."""
    items = _coerce_imported_agents(payload.data)
    if not items:
        raise HTTPException(status_code=400, detail="No valid agents found in the import.")

    valid_tools = {t["name"] for t in all_tool_names()}
    by_name = {a["name"]: a for a in agents_registry.list_agents()}
    existing_names = set(by_name)

    imported: list[dict[str, Any]] = []
    created = updated = 0
    for raw in items:
        name = str(raw.get("name", "")).strip()[:200]
        instructions = str(raw.get("instructions", "") or "")[:20000]
        if not instructions.strip():
            # Refuse to import a blank agent (would be useless / could clobber).
            continue
        record: dict[str, Any] = {
            "name": name,
            "instructions": instructions,
            "provider": (raw.get("provider") or "") if raw.get("provider") is not None else "",
            "model": (raw.get("model") or "") if raw.get("model") is not None else "",
            "connection_id": raw.get("connection_id") or "",
            "allow_all_azure": bool(raw.get("allow_all_azure", True)),
            "allow_all_entra": bool(raw.get("allow_all_entra", False)),
            "connector_tools": [
                t for t in (raw.get("connector_tools") or []) if t in valid_tools
            ],
            "run_mode": raw.get("run_mode") if raw.get("run_mode") in ("review", "autonomous") else "review",
            "enabled": bool(raw.get("enabled", True)),
        }
        match = by_name.get(name)
        if match and payload.overwrite_existing:
            record["id"] = match["id"]
            updated += 1
        else:
            if match:
                # Name clash but not overwriting — make the name unique.
                base = name
                i = 2
                while f"{base} ({i})" in existing_names:
                    i += 1
                record["name"] = f"{base} ({i})"
                existing_names.add(record["name"])
            record["created_by"] = principal.subject
            created += 1
        saved = agents_registry.upsert_agent(record)
        existing_names.add(saved["name"])
        by_name[saved["name"]] = saved
        imported.append({"id": saved["id"], "name": saved["name"]})

    if not imported:
        raise HTTPException(
            status_code=400,
            detail="Nothing imported — agents were missing a name or instructions.",
        )

    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="agent.import",
            target=None,
            metadata_json={"created": created, "updated": updated, "count": len(imported)},
        )
    )
    await db.commit()
    return {"created": created, "updated": updated, "agents": imported}


# ----------------------------------------------------- AI agent designer (wizard)
class InterviewRequest(BaseModel):
    goal: str = Field(default="", max_length=4000)
    answers: list[dict[str, Any]] = Field(default_factory=list)
    step: int = 0


@router.post("/agents/draft/interview")
async def agent_interview_endpoint(
    payload: InterviewRequest,
    _: Principal = Depends(require_admin),
):
    """Dynamic interview step: the model asks the next batch of clarifying questions
    (or signals done) based on the goal and answers gathered so far."""
    from app.automations.agent_designer import next_questions

    result = await next_questions(payload.goal, payload.answers[:50], payload.step)
    return result


class GenerateRequest(BaseModel):
    goal: str = Field(default="", max_length=4000)
    answers: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/agents/draft/generate")
async def agent_generate_endpoint(
    payload: GenerateRequest,
    _: Principal = Depends(require_admin),
):
    """Generate a complete agent draft (name, instructions, tools, run mode, model)
    grounded in the real connector-tool catalog and Azure connections."""
    from app.automations.agent_designer import generate_agent
    from app.core.azure_connections import list_connections
    from app.core.llm_config import load_config

    try:
        cfg = load_config()
        providers = [pid for pid, p in cfg.get("providers", {}).items() if not p.get("disabled")]
    except Exception:  # noqa: BLE001 - provider list is best-effort grounding only
        providers = []

    draft = await generate_agent(
        goal=payload.goal,
        answers=payload.answers[:50],
        tool_catalog=all_tool_names(),
        connections=list_connections(),
        providers=providers,
    )
    if draft is None:
        raise HTTPException(status_code=502, detail="The AI could not draft an agent. Try again.")
    return {"draft": draft}


# --------------------------------------------------- AI agent enhancer (existing agent)
class EnhanceInterviewRequest(BaseModel):
    agent_id: str
    answers: list[dict[str, Any]] = Field(default_factory=list)
    step: int = 0


@router.post("/agents/{agent_id}/enhance/interview")
async def agent_enhance_interview_endpoint(
    agent_id: str,
    payload: EnhanceInterviewRequest,
    _: Principal = Depends(require_admin),
):
    """Assess an EXISTING agent and ask the next batch of clarifying questions whose
    answers will be used to substantially enhance it."""
    agent = agents_registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    from app.automations.agent_designer import enhance_questions

    return await enhance_questions(agent, payload.answers[:50], payload.step)


class EnhanceGenerateRequest(BaseModel):
    agent_id: str
    answers: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/agents/{agent_id}/enhance/generate")
async def agent_enhance_generate_endpoint(
    agent_id: str,
    payload: EnhanceGenerateRequest,
    _: Principal = Depends(require_admin),
):
    """Produce an enhanced draft of an existing agent. Returns the draft plus the
    CURRENT values so the client can show a before/after review before saving."""
    agent = agents_registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    from app.automations.agent_designer import enhance_agent
    from app.core.azure_connections import list_connections

    catalog = all_tool_names()
    conns = list_connections()
    draft = None
    # The LLM occasionally returns malformed JSON; retry a couple of times before
    # surfacing an error (the client also offers a manual Retry).
    for _ in range(3):
        draft = await enhance_agent(
            agent=agent, answers=payload.answers[:50], tool_catalog=catalog, connections=conns
        )
        if draft:
            break
    if draft is None:
        raise HTTPException(status_code=502, detail="The AI could not enhance this agent. Try again.")
    return {
        "draft": draft,
        "current": {
            "name": agent.get("name"),
            "instructions": agent.get("instructions"),
            "connector_tools": agent.get("connector_tools", []),
            "run_mode": agent.get("run_mode", "review"),
            "allow_all_azure": agent.get("allow_all_azure", True),
            "allow_all_entra": agent.get("allow_all_entra", False),
        },
    }


# ---------------------------------------------------------------------------- tasks
class TaskUpsert(BaseModel):
    id: str | None = None
    name: str = Field(max_length=256)
    instructions: str = Field(default="", max_length=20000)
    agent_id: str | None = None
    connection_id: str | None = None
    # Unified scheduling: what this schedule invokes + its type-specific payload.
    target_type: str = "agent"  # agent | assessment | workbook | playbook
    target_config: dict[str, Any] | None = None
    schedule_kind: str = "daily"  # daily | weekly | cron
    cron_expr: str | None = None
    time_of_day: str | None = "08:00"
    weekday: int | None = None
    timezone: str = "UTC"
    start_date: datetime | None = None
    end_date: datetime | None = None
    max_runs: int | None = None
    run_mode: str = "review"
    message_grouping: str = "new_thread"
    notify_connector_ids: list[str] | None = None
    status: str | None = None  # on | off


def _task_dict(t: ScheduledTask) -> dict[str, Any]:
    from app.automations.targets import TARGET_META, target_label

    target_type = getattr(t, "target_type", None) or "agent"
    target_config = getattr(t, "target_config", None) or {}
    d = {
        "id": t.id,
        "name": t.name,
        "instructions": t.instructions,
        "agent_id": t.agent_id,
        "connection_id": t.connection_id,
        "target_type": target_type,
        "target_config": target_config,
        "schedule_kind": t.schedule_kind,
        "cron_expr": t.cron_expr,
        "time_of_day": t.time_of_day,
        "weekday": t.weekday,
        "timezone": t.timezone,
        "start_date": t.start_date,
        "end_date": t.end_date,
        "max_runs": t.max_runs,
        "run_mode": t.run_mode,
        "message_grouping": t.message_grouping,
        "notify_connector_ids": list(t.notify_connector_ids or []),
        "thread_id": t.thread_id,
        "status": t.status,
        "completed_runs": t.completed_runs,
        "last_run_at": t.last_run_at,
        "next_run_at": t.next_run_at,
        "deleted_at": t.deleted_at,
        "created_by": t.created_by,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }
    d["schedule_label"] = human_schedule(d)
    d["target_label"] = target_label(target_type, target_config)
    d["target_meta"] = TARGET_META.get(target_type, {"label": target_type, "icon": "📋"})
    return d


@router.get("/tasks")
async def list_tasks_endpoint(
    principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.execute(
            select(ScheduledTask)
            .where(
                ScheduledTask.tenant_id == principal.tenant_id,
                ScheduledTask.deleted_at.is_(None),
            )
            .order_by(ScheduledTask.created_at.desc())
        )
    ).scalars().all()
    tasks = [_task_dict(t) for t in rows]
    active = sum(1 for t in rows if t.status == "on")
    total_runs = (
        await db.execute(
            select(func.count(TaskRun.id)).where(TaskRun.tenant_id == principal.tenant_id)
        )
    ).scalar() or 0
    return {
        "tasks": tasks,
        "metrics": {"active": active, "total": len(rows), "total_runs": int(total_runs)},
    }


@router.put("/tasks")
async def upsert_task_endpoint(
    payload: TaskUpsert,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    now_fields = payload.model_dump(exclude_none=True)
    # Validate the target configuration for the chosen schedule type.
    from app.automations.targets import TARGET_TYPES, validate_config

    target_type = now_fields.get("target_type", "agent")
    if target_type not in TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown schedule type '{target_type}'.")
    if target_type != "agent":
        err = validate_config(target_type, now_fields.get("target_config") or {})
        if err:
            raise HTTPException(status_code=400, detail=err)
    if payload.id:
        task = await db.get(ScheduledTask, payload.id)
        if task is None or task.tenant_id != principal.tenant_id or task.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Task not found.")
        for k, v in now_fields.items():
            if k != "id":
                setattr(task, k, v)
        task.updated_at = datetime.now().astimezone()
    else:
        task = ScheduledTask(
            tenant_id=principal.tenant_id,
            created_by=principal.subject,
            **{k: v for k, v in now_fields.items() if k != "id"},
        )
        db.add(task)
    # Recompute next run from the (new) schedule.
    await db.flush()
    if task.status == "on":
        task.next_run_at = compute_next_run(_task_dict(task))
        if task.next_run_at is None:
            task.status = "ended"
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="task.upsert",
            target=task.id,
            metadata_json={"name": task.name},
        )
    )
    await db.commit()
    await db.refresh(task)
    return {"task": _task_dict(task)}


@router.post("/tasks/{task_id}/toggle")
async def toggle_task_endpoint(
    task_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(ScheduledTask, task_id)
    if task is None or task.tenant_id != principal.tenant_id or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.status in ("on",):
        task.status = "off"
        task.next_run_at = None
    else:
        task.status = "on"
        task.next_run_at = compute_next_run(_task_dict(task))
        if task.next_run_at is None:
            task.status = "ended"
    await db.commit()
    await db.refresh(task)
    return {"task": _task_dict(task)}


@router.delete("/tasks/{task_id}")
async def delete_task_endpoint(
    task_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete: archive the schedule but preserve its run history."""
    task = await db.get(ScheduledTask, task_id)
    if task is None or task.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Task not found.")
    task.status = "deleted"
    task.next_run_at = None
    task.deleted_at = datetime.now().astimezone()
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="task.delete",
            target=task_id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.get("/tasks/archived")
async def list_archived_tasks_endpoint(
    principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    """Deleted schedules whose run history is preserved."""
    rows = (
        await db.execute(
            select(ScheduledTask)
            .where(
                ScheduledTask.tenant_id == principal.tenant_id,
                ScheduledTask.deleted_at.is_not(None),
            )
            .order_by(ScheduledTask.deleted_at.desc())
        )
    ).scalars().all()
    # Run counts per archived task so the UI can show "N runs preserved".
    counts: dict[str, int] = {}
    if rows:
        count_rows = (
            await db.execute(
                select(TaskRun.task_id, func.count(TaskRun.id))
                .where(TaskRun.task_id.in_([t.id for t in rows]))
                .group_by(TaskRun.task_id)
            )
        ).all()
        counts = {tid: int(n) for tid, n in count_rows}
    tasks = []
    for t in rows:
        d = _task_dict(t)
        d["run_count"] = counts.get(t.id, 0)
        tasks.append(d)
    return {"tasks": tasks}


@router.post("/tasks/{task_id}/restore")
async def restore_task_endpoint(
    task_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Un-delete an archived schedule (resumes paused)."""
    task = await db.get(ScheduledTask, task_id)
    if task is None or task.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Task not found.")
    task.deleted_at = None
    task.status = "off"  # restore paused; the user re-enables explicitly
    task.next_run_at = None
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="task.restore",
            target=task_id,
        )
    )
    await db.commit()
    await db.refresh(task)
    return {"task": _task_dict(task)}


@router.delete("/tasks/{task_id}/purge")
async def purge_task_endpoint(
    task_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Permanently remove an archived schedule AND its run history."""
    task = await db.get(ScheduledTask, task_id)
    if task is None or task.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Task not found.")
    runs = (
        await db.execute(
            select(TaskRun).where(
                TaskRun.task_id == task_id, TaskRun.tenant_id == principal.tenant_id
            )
        )
    ).scalars().all()
    for r in runs:
        await db.delete(r)
    await db.delete(task)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="task.purge",
            target=task_id,
            metadata_json={"runs_deleted": len(runs)},
        )
    )
    await db.commit()
    return {"ok": True}


@router.post("/tasks/{task_id}/run")
async def run_task_now_endpoint(
    task_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(ScheduledTask, task_id)
    if task is None or task.tenant_id != principal.tenant_id or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Task not found.")
    # Run in the background so the request returns immediately.
    _spawn_manual_run(task_id, task.target_type or "agent")
    return {"ok": True, "message": "Task started. Open its run history to watch progress."}


@router.get("/tasks/{task_id}/runs")
async def task_runs_endpoint(
    task_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(TaskRun)
            .where(TaskRun.task_id == task_id, TaskRun.tenant_id == principal.tenant_id)
            .order_by(TaskRun.started_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return {
        "runs": [
            {
                "id": r.id,
                "thread_id": r.thread_id,
                "trigger": r.trigger,
                "status": r.status,
                "summary": r.summary,
                "error": r.error,
                "target_type": getattr(r, "target_type", None) or "agent",
                "result_ref": getattr(r, "result_ref", None),
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "duration_ms": r.duration_ms,
            }
            for r in rows
        ]
    }

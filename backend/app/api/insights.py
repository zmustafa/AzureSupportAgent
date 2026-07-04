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

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.schedule import compute_next_run, human_schedule
from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.insights import designer, packfile, registry, runs as runs_store, snapshots, sources, starters
from app.models import ScheduledTask
from app.workloads import registry as workloads_registry

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
        "collections": registry.list_collections(),
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


class SnoozeBody(BaseModel):
    days: float = 7.0


@router.post("/packs/{pack_id}/snooze")
async def snooze_pack_endpoint(pack_id: str, body: SnoozeBody, _: Principal = Depends(_write)) -> dict[str, Any]:
    """Mute a pack's notifications for ``days`` (a value <= 0 clears the snooze). Snoozed packs
    still run on schedule and record digests — the runner just suppresses the notification."""
    until = ""
    if body.days and body.days > 0:
        until = (datetime.now(timezone.utc) + timedelta(days=float(body.days))).isoformat()
    pack = registry.set_snooze(pack_id, until)
    if pack is None:
        raise HTTPException(status_code=404, detail="Insight pack not found.")
    return {"pack": pack}


class PinBody(BaseModel):
    pinned: bool = True


@router.post("/packs/{pack_id}/pin")
async def pin_pack_endpoint(pack_id: str, body: PinBody, _: Principal = Depends(_write)) -> dict[str, Any]:
    """Pin/unpin a pack so it surfaces in the Library's top section."""
    pack = registry.set_pinned(pack_id, body.pinned)
    if pack is None:
        raise HTTPException(status_code=404, detail="Insight pack not found.")
    return {"pack": pack}


class PackCollectionsBody(BaseModel):
    collection_ids: list[str] = Field(default_factory=list)


@router.post("/packs/{pack_id}/collections")
async def set_pack_collections_endpoint(pack_id: str, body: PackCollectionsBody,
                                        _: Principal = Depends(_write)) -> dict[str, Any]:
    """Replace a pack's collection membership (unknown collection ids are dropped)."""
    pack = registry.set_pack_collections(pack_id, body.collection_ids)
    if pack is None:
        raise HTTPException(status_code=404, detail="Insight pack not found.")
    return {"pack": pack}


# --------------------------------------------------------------------------- collections
class CollectionBody(BaseModel):
    name: str = ""
    icon: str = "\U0001f4c1"


@router.get("/collections")
async def list_collections_endpoint(_: Principal = Depends(_read)) -> dict[str, Any]:
    return {"collections": registry.list_collections()}


@router.post("/collections")
async def create_collection_endpoint(body: CollectionBody, principal: Principal = Depends(_write)) -> dict[str, Any]:
    col = registry.create_collection(body.name, icon=body.icon, actor=_actor(principal))
    if col is None:
        raise HTTPException(status_code=400, detail="A collection name is required.")
    return {"collection": col}


@router.post("/collections/{collection_id}")
async def update_collection_endpoint(collection_id: str, body: CollectionBody,
                                     _: Principal = Depends(_write)) -> dict[str, Any]:
    col = registry.update_collection(collection_id, name=body.name, icon=body.icon)
    if col is None:
        raise HTTPException(status_code=404, detail="Collection not found.")
    return {"collection": col}


@router.delete("/collections/{collection_id}")
async def delete_collection_endpoint(collection_id: str, _: Principal = Depends(_write)) -> dict[str, Any]:
    if not registry.delete_collection(collection_id):
        raise HTTPException(status_code=404, detail="Collection not found.")
    return {"ok": True}


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


class PreviewRequest(BaseModel):
    goal: str = ""
    answers: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/draft/preview")
async def preview_endpoint(payload: PreviewRequest, _: Principal = Depends(_write)) -> dict[str, Any]:
    """Fast, deterministic (no-LLM) best-guess of the pack from goal + answers so far.

    Powers the wizard's live 'pack so far' pane without incurring reasoning-model latency.
    """
    return designer.preview_pack(payload.goal, payload.answers)


class RefineRequest(BaseModel):
    pack: dict[str, Any] = Field(default_factory=dict)
    instruction: str = ""
    mode: str = "command"


@router.post("/draft/refine")
async def refine_endpoint(payload: RefineRequest, _: Principal = Depends(_write)) -> dict[str, Any]:
    """AI copilot for the pack editor: apply a natural-language edit, improve instructions,
    suggest sources/flags, explain, critique, or synthesize an example finding. All output is
    grounded to the real source/flag catalogs and treats the pack + instruction as untrusted data.
    """
    result = await designer.refine_pack(payload.pack, payload.instruction, payload.mode)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
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


@router.post("/run/async")
async def run_async_endpoint(payload: RunRequest, principal: Principal = Depends(_run)) -> dict[str, Any]:
    """Start a pack run in the background and return a ``job_id`` to poll for detailed progress.

    The run executes as a detached asyncio task so it continues even if the caller navigates
    away; the durable result is still the persisted digest in the run history."""
    import asyncio

    from app.insights import jobs
    from app.insights.runner import run_pack
    from app.insights import sources as sources_mod

    pack = payload.pack or registry.get_pack(payload.pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Insight pack not found.")

    tenant_id = principal.tenant_id
    scope_label = sources_mod.scope_label(sources_mod.resolve_scope_names(dict(payload.scope)))
    job = jobs.create(tenant_id, pack_name=str(pack.get("name", "")), scope_label=scope_label)

    async def _worker() -> None:
        try:
            digest = await run_pack(
                pack, payload.scope, tenant_id=tenant_id,
                overrides=payload.overrides, trigger="manual", notify=payload.notify,
                progress=lambda **ev: jobs.progress(job, **ev),
            )
            jobs.finish(job, digest)
        except Exception as exc:  # noqa: BLE001 — surface failure to the poller, never crash the loop
            log.exception("Background insight run failed")
            jobs.fail(job, str(exc))

    asyncio.create_task(_worker())
    return {"job_id": job["id"]}


@router.get("/run/jobs/{job_id}")
async def run_job_endpoint(job_id: str, principal: Principal = Depends(_read)) -> dict[str, Any]:
    """Poll a background run job for progress and (when finished) the resulting digest."""
    from app.insights import jobs

    job = jobs.get(principal.tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Run job not found.")
    return {"job": jobs.snapshot(job)}


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


@router.get("/health")
async def pack_health_endpoint(principal: Principal = Depends(_read)) -> dict[str, Any]:
    """Per-pack health rollup (verdict mix, notify/false-positive rates, verdict sparkline)
    that powers the Trends view, the Library 'noisy' hints, and threshold-tuning suggestions."""
    runs = runs_store.list_runs(principal.tenant_id, limit=500)
    return {"health": runs_store.aggregate_health(runs)}


@router.get("/runs/{run_id}")
async def get_run_endpoint(run_id: str, principal: Principal = Depends(_read)) -> dict[str, Any]:
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"run": run}


@router.get("/runs/{run_id}/pdf")
async def run_pdf_endpoint(run_id: str, principal: Principal = Depends(_read)) -> Response:
    """Render one run's digest as a board-ready PDF via the shared xhtml2pdf engine."""
    run = runs_store.get_run(principal.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    from app.insights.pdf_report import build_insight_pdf
    pdf = build_insight_pdf(run)
    fname = f"insight-{(run.get('pack_id') or 'pack')}-{run_id[:8]}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


class RunStateBody(BaseModel):
    read: bool | None = None
    acknowledged: bool | None = None
    false_positive: bool | None = None


@router.post("/runs/read-all")
async def mark_all_read_endpoint(principal: Principal = Depends(_write)) -> dict[str, Any]:
    """Stamp every notified-but-unread run as read (clears the inbox badge)."""
    return {"updated": runs_store.mark_all_read(principal.tenant_id)}


@router.post("/runs/{run_id}/state")
async def set_run_state_endpoint(run_id: str, body: RunStateBody,
                                 principal: Principal = Depends(_write)) -> dict[str, Any]:
    """Update a run's review state: read (opened), acknowledged, or flagged false-positive."""
    now = datetime.now(timezone.utc).isoformat()
    patch: dict[str, Any] = {}
    if body.read is not None:
        patch["read_at"] = now if body.read else None
    if body.acknowledged is not None:
        patch["acknowledged_at"] = now if body.acknowledged else None
        patch["acknowledged_by"] = _actor(principal) if body.acknowledged else None
    if body.false_positive is not None:
        patch["false_positive"] = bool(body.false_positive)
    run = runs_store.update_run(principal.tenant_id, run_id, patch)
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
        scope_lbl = sources.scope_label(sources.resolve_scope_names(cfg.get("scope") or {}))
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
                "scope_label": scope_lbl,
                "at": nxt.isoformat(), "schedule_label": human_schedule(task_dict),
            })
            after = nxt
    occurrences.sort(key=lambda o: o["at"])
    return {"days": days, "occurrences": occurrences}


# --------------------------------------------------------------------------- coverage (watchers)
_STALE_GRACE_DAYS = 1.0
_STATUS_RANK = {"covered": 3, "stale": 2, "paused": 1}


def _task_schedule_dict(t: ScheduledTask) -> dict[str, Any]:
    return {
        "schedule_kind": t.schedule_kind, "cron_expr": t.cron_expr, "time_of_day": t.time_of_day,
        "weekday": t.weekday, "timezone": t.timezone, "start_date": t.start_date, "end_date": t.end_date,
    }


def _interval_days(kind: str | None) -> float:
    return {"weekly": 7.0, "cron": 1.0}.get(kind or "daily", 1.0)


def _age_seconds(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt.astimezone(timezone.utc)).total_seconds()


def _sub_guid(value: str) -> str:
    """Bare subscription GUID from an ARM id or plain guid (for subscription-scope matching)."""
    s = str(value or "").strip("/")
    if "subscriptions/" in s:
        s = s.split("subscriptions/", 1)[1]
    return s.split("/", 1)[0].lower()


def _workload_sub_guids(wl: dict[str, Any] | None) -> set[str]:
    out: set[str] = set()
    for n in (wl or {}).get("nodes") or []:
        if not isinstance(n, dict):
            continue
        if n.get("subscription_id"):
            out.add(_sub_guid(n["subscription_id"]))
        if n.get("kind") == "subscription" and n.get("id"):
            out.add(_sub_guid(n["id"]))
    return {g for g in out if g}


def _scope_covers_workload(scope: dict[str, Any], workload_id: str, sub_guids: set[str]) -> bool:
    """Does a pack scheduled against ``scope`` watch ``workload_id``?"""
    mode = (scope or {}).get("mode", "workload")
    if mode == "tenant":
        return True
    if mode == "subscription":
        return _sub_guid(scope.get("subscription_id", "")) in sub_guids
    wids = scope.get("workload_ids") or ([scope["workload_id"]] if scope.get("workload_id") else [])
    return workload_id in [str(w) for w in wids]


@router.get("/coverage")
async def coverage_endpoint(workload_id: str = "", days: int = 7,
                            principal: Principal = Depends(_read),
                            db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Watcher coverage: which insight packs watch a scope, on what cadence, how healthy, and
    where the blind spots are. With ``?workload_id=`` it pivots to one workload (per-workload
    coverage view); without it, returns the flat watcher join (feeds the coverage matrix)."""
    now = datetime.now(timezone.utc)
    days = min(31, max(1, days))
    rows = (await db.execute(
        select(ScheduledTask).where(
            ScheduledTask.tenant_id == principal.tenant_id,
            ScheduledTask.target_type == "insight_pack",
            ScheduledTask.status.in_(("on", "off")),
            ScheduledTask.deleted_at.is_(None),
        )
    )).scalars().all()

    packs_by_id = {p["id"]: p for p in registry.list_packs()}
    all_runs = runs_store.list_runs(principal.tenant_id, limit=500)
    # Latest run per (pack_id, scope_key) — runs are newest-first, so first wins.
    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in all_runs:
        key = (r.get("pack_id", ""), snapshots.scope_key(r.get("scope") or {}))
        latest_by_key.setdefault(key, r)

    wl = workloads_registry.get_workload(workload_id, include_deleted=True) if workload_id else None
    sub_guids = _workload_sub_guids(wl)

    watchers: list[dict[str, Any]] = []
    for t in rows:
        cfg = t.target_config or {}
        scope = sources.resolve_scope_names(cfg.get("scope") or {})
        if workload_id and not _scope_covers_workload(scope, workload_id, sub_guids):
            continue
        pack = packs_by_id.get(cfg.get("pack_id") or "") or {}
        skey = snapshots.scope_key(scope)
        enabled = t.status == "on"
        sched = _task_schedule_dict(t)
        nxt = compute_next_run(sched) if enabled else None
        last = latest_by_key.get((pack.get("id", ""), skey))
        if not enabled:
            status = "paused"
        elif last is None:
            status = "stale"
        else:
            age = _age_seconds(last.get("created_at"), now)
            fresh_for = (_interval_days(t.schedule_kind) + _STALE_GRACE_DAYS) * 86400
            status = "covered" if (age is not None and age <= fresh_for) else "stale"
        watchers.append({
            "task_id": t.id, "task_name": t.name, "enabled": enabled,
            "pack_id": pack.get("id", cfg.get("pack_id", "")), "pack_name": pack.get("name", cfg.get("pack_id", "")),
            "pack_icon": pack.get("icon", "\U0001f9e0"), "category": pack.get("category", "general"),
            "sources": pack.get("sources", []), "lookback_hours": pack.get("lookback_hours", 24),
            "scope": scope, "scope_key": skey, "scope_label": sources.scope_label(scope),
            "schedule_label": human_schedule(sched), "next_run_at": nxt.isoformat() if nxt else None,
            "status": status,
            "last_run_id": (last or {}).get("id"), "last_verdict": (last or {}).get("verdict"),
            "last_run_at": (last or {}).get("created_at"), "last_headline": (last or {}).get("headline"),
            "last_notified": bool((last or {}).get("notified")),
        })

    result: dict[str, Any] = {"watchers": watchers, "categories": registry.CATEGORIES}
    if not workload_id:
        return result

    # Per-workload pivot: area (category) rollup, gaps, summary.
    areas: list[dict[str, Any]] = []
    gaps: list[str] = []
    summary = {"covered": 0, "stale": 0, "paused": 0, "gaps": 0}
    for cat in registry.CATEGORIES:
        cat_watchers = [w for w in watchers if w["category"] == cat["id"]]
        if not cat_watchers:
            gaps.append(cat["id"])
            summary["gaps"] += 1
            areas.append({"area": cat["id"], "label": cat["label"], "icon": cat["icon"],
                          "status": "gap", "packs": []})
            continue
        best = max((w["status"] for w in cat_watchers), key=lambda s: _STATUS_RANK.get(s, 0))
        summary[best] = summary.get(best, 0) + 1
        areas.append({
            "area": cat["id"], "label": cat["label"], "icon": cat["icon"], "status": best,
            "packs": sorted(cat_watchers, key=lambda w: (-_STATUS_RANK.get(w["status"], 0), w["pack_name"].lower())),
        })

    # Scoped upcoming occurrences over the window (enabled watchers only).
    horizon = now + timedelta(days=days)
    upcoming: list[dict[str, Any]] = []
    for t in rows:
        cfg = t.target_config or {}
        scope = sources.resolve_scope_names(cfg.get("scope") or {})
        if t.status != "on" or not _scope_covers_workload(scope, workload_id, sub_guids):
            continue
        pack = packs_by_id.get(cfg.get("pack_id") or "") or {}
        sched = _task_schedule_dict(t)
        after: datetime | None = None
        for _ in range(20):
            occ = compute_next_run(sched, after=after)
            if occ is None or occ > horizon:
                break
            upcoming.append({
                "task_id": t.id, "task_name": t.name, "pack_id": pack.get("id", ""), "pack_name": pack.get("name", ""),
                "pack_icon": pack.get("icon", "\U0001f9e0"), "at": occ.isoformat(),
                "scope_label": sources.scope_label(scope), "schedule_label": human_schedule(sched),
            })
            after = occ
    upcoming.sort(key=lambda o: o["at"])

    # Scoped recent runs (most recent first).
    recent = [r for r in all_runs
              if _scope_covers_workload(r.get("scope") or {}, workload_id, sub_guids)][:25]

    result.update({
        "workload_id": workload_id, "workload_name": (wl or {}).get("name", ""),
        "areas": areas, "gaps": gaps, "summary": summary,
        "upcoming": upcoming, "recent_runs": recent,
    })
    return result

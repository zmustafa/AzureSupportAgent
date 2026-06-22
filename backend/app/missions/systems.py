"""Mission Control system registry.

Each *system* is a thin adapter over an existing per-workload collector/runner. It exposes:

- ``run(ctx, force, progress)`` — execute the analysis for the workload, returning a
  :class:`SystemResult` (status + headline + deep-link ref). This re-uses the very same
  service functions the standalone screens call, so a mission populates the same caches /
  run-history the deep-linked screens read.
- ``last_state(ctx)`` — a cached-only read (never scans Azure) used to render the board
  before launch and to decide freshness-skip (don't re-run a system whose last run is
  recent unless ``force``).

Adding a system = appending one :class:`SystemDef` to :data:`SYSTEMS`.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("app.missions.systems")

# A system whose last successful run is younger than this is "fresh" and skipped on a
# normal launch (override with force=True).
FRESH_SECONDS = 24 * 60 * 60


@dataclass
class MissionContext:
    """Everything a system needs to run against one workload."""

    tenant_id: str
    actor: str
    workload_id: str
    workload: dict[str, Any]
    connection: dict[str, Any] | None
    connection_id: str


@dataclass
class SystemResult:
    """Outcome of running one system."""

    status: str  # done | fail | skipped | error
    headline: str = ""
    detail: str = ""
    score: int | None = None
    attention: bool = False  # below par -> contributes to the readiness "needs attention" count
    link: str = ""
    result_ref: dict[str, Any] | None = None
    error: str = ""


# A system's run/last_state signatures.
RunFn = Callable[..., Awaitable[SystemResult]]
StateFn = Callable[..., Awaitable[dict[str, Any] | None]]


@dataclass
class SystemDef:
    key: str
    label: str
    icon: str
    run: RunFn
    last_state: StateFn
    # When True the system is informational (architecture/memory) and never marks the
    # mission "needs attention" on its own.
    informational: bool = False


# --------------------------------------------------------------------------- helpers
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _admin_principal(tenant_id: str, actor: str):
    """A minimal admin Principal for calling the screens' service functions in a
    background (non-request) context. The coverage ``_get_snapshot`` helpers only read
    ``principal.tenant_id``; admin role keeps any gating happy."""
    from app.core.security import Principal

    return Principal(
        subject=actor or "mission",
        email="",
        tenant_id=tenant_id or "default",
        role="admin",
    )


def _age_seconds(snap: dict[str, Any] | None) -> float | None:
    if not snap:
        return None
    a = snap.get("age_seconds")
    if isinstance(a, (int, float)):
        return float(a)
    ts = snap.get("generated_at") or snap.get("run_at") or ""
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (_now() - dt).total_seconds()
        except (ValueError, TypeError):
            return None
    return None


def _latest_arch_for_workload(tenant_id: str, workload_id: str) -> dict[str, Any] | None:
    from app.architectures.registry import list_architectures

    arts = [a for a in list_architectures(tenant_id) if a.get("workload_id") == workload_id]
    if not arts:
        return None
    arts.sort(key=lambda a: a.get("updated_at", "") or a.get("created_at", ""), reverse=True)
    return arts[0]


# --------------------------------------------------------------- coverage system factory
def _coverage_system(
    *,
    key: str,
    label: str,
    icon: str,
    module: str,
    cache_module: str,
    headline: Callable[[dict[str, Any]], tuple[str, int | None, bool]],
    link_path: str,
    recorder: Callable[[dict[str, Any], str, str, str, str], None] | None = None,
) -> SystemDef:
    """Build a SystemDef for one of the coverage features (amba/telemetry/backupdr/radar)
    which all share the ``_get_snapshot(principal, scope_kind, scope_id, *, force)`` +
    ``cache.read_snapshot`` shape.

    ``recorder`` (when set) persists a trend point + coverage run after a successful scan —
    exactly like the feature's ``/refresh`` endpoint — so a Mission Control scan shows up on
    the dashboard coverage lens and the coverage screen history, not just the mission board."""

    async def run(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
        from importlib import import_module

        if progress:
            await progress(f"Scanning {label.lower()}…")
        get_snapshot = getattr(import_module(module), "_get_snapshot")
        snap = await get_snapshot(_admin_principal(ctx.tenant_id, ctx.actor), "workload", ctx.workload_id, force=True)
        link = link_path.format(wid=ctx.workload_id)
        err = snap.get("error")
        if err:
            return SystemResult(status="fail", headline=str(err)[:140], error=str(err), attention=True, link=link)
        if recorder is not None:
            try:
                recorder(snap, ctx.tenant_id or "default", "workload", ctx.workload_id, ctx.actor)
            except Exception:  # noqa: BLE001 - trend/run recording must never break a mission
                logger.warning("%s trend recording failed", key, exc_info=True)
        head, score, attention = headline(snap)
        return SystemResult(
            status="done",
            headline=head,
            score=score,
            attention=attention,
            link=link,
            result_ref={"kind": key, "workload_id": ctx.workload_id},
        )

    async def last_state(ctx: MissionContext) -> dict[str, Any] | None:
        from importlib import import_module

        cache = import_module(cache_module)
        snap = cache.read_snapshot(ctx.tenant_id or "default", "workload", ctx.workload_id)
        if not snap:
            return None
        head, score, attention = headline(snap)
        return {
            "status": "done",
            "headline": head,
            "score": score,
            "attention": attention,
            "age_seconds": _age_seconds(snap),
            "link": link_path.format(wid=ctx.workload_id),
        }

    return SystemDef(key=key, label=label, icon=icon, run=run, last_state=last_state)


# headline extractors (snapshot -> (headline, score, attention))
def _h_amba(s: dict[str, Any]) -> tuple[str, int | None, bool]:
    pct = int(round(float(s.get("coverage_pct") or 0)))
    missing = int((s.get("kpis") or {}).get("alerts_missing") or 0)
    return f"{pct}% coverage · {missing} missing", pct, pct < 80


def _h_telemetry(s: dict[str, Any]) -> tuple[str, int | None, bool]:
    pct = int(round(float(s.get("coverage_pct") or 0)))
    return f"{pct}% with diagnostics", pct, pct < 80


def _h_backupdr(s: dict[str, Any]) -> tuple[str, int | None, bool]:
    sc = s.get("scorecard") or {}
    pct = int(round(float(sc.get("pct_protected") or 0)))
    protected = int(sc.get("protected") or 0)
    total = int(sc.get("total") or 0)
    return f"{pct}% protected ({protected}/{total})", pct, pct < 80


def _h_radar(s: dict[str, Any]) -> tuple[str, int | None, bool]:
    c = s.get("counts") or {}
    total = int(c.get("total") or 0)
    red = int(c.get("red") or 0)
    retire = int(c.get("retirement") or 0)
    if total == 0:
        return "clear · 0 retirements", 0, False
    return f"{total} item(s) · {red} critical", total, (red > 0 or retire > 0)


# trend/run recorders (mirror each feature's /refresh endpoint so a Mission Control scan is
# charted on the dashboard coverage lens + listed in the coverage screen run history)
def _record_coverage_scan(
    feature: str, snap: dict[str, Any], tenant_id: str, scope_kind: str, scope_id: str, actor: str,
    *, pct: Any, extra: dict[str, Any], counts: dict[str, Any],
) -> None:
    from app.core import coverage_runs, coverage_trends

    coverage_trends.record(
        feature, tenant_id, scope_kind, scope_id,
        pct=pct, extra=extra, demo=bool(snap.get("demo")),
    )
    coverage_runs.save_run(
        feature, tenant_id, scope_kind, scope_id, snap,
        headline=pct, counts=counts,
        resource_count=len(snap.get("all_resources") or []), actor=actor,
    )


def _rec_amba(snap: dict[str, Any], tenant_id: str, scope_kind: str, scope_id: str, actor: str) -> None:
    kpis = snap.get("kpis") or {}
    _record_coverage_scan("amba", snap, tenant_id, scope_kind, scope_id, actor,
                          pct=snap.get("coverage_pct"), extra=kpis, counts=kpis)


def _rec_telemetry(snap: dict[str, Any], tenant_id: str, scope_kind: str, scope_id: str, actor: str) -> None:
    kpis = snap.get("kpis") or {}
    _record_coverage_scan("telemetry", snap, tenant_id, scope_kind, scope_id, actor,
                          pct=snap.get("coverage_pct"), extra=kpis, counts=kpis)


def _rec_backupdr(snap: dict[str, Any], tenant_id: str, scope_kind: str, scope_id: str, actor: str) -> None:
    sc = snap.get("scorecard") or {}
    _record_coverage_scan("backupdr", snap, tenant_id, scope_kind, scope_id, actor,
                          pct=sc.get("pct_protected"),
                          extra={k: sc.get(k) for k in ("pct_offsite", "pct_recent_job", "dr_pairs")},
                          counts=sc)


# --------------------------------------------------------------- architecture / memory
async def _run_architecture(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.architectures import registry as areg
    from app.architectures.designer import generate_architecture
    from app.architectures.reverse import dump_resources

    if progress:
        await progress("Querying Azure Resource Graph…")
    dump = await dump_resources(ctx.workload, ctx.connection)
    if dump.get("error"):
        return SystemResult(status="fail", headline=str(dump["error"])[:140], error=str(dump["error"]), attention=True)
    resources = dump.get("resources") or []
    if not resources:
        return SystemResult(status="fail", headline="No resources in scope", attention=True)
    if progress:
        await progress(f"Reverse-engineering architecture from {len(resources)} resource(s)…")
    result = await generate_architecture(ctx.workload.get("name", ""), resources)
    if result is None:
        return SystemResult(status="fail", headline="AI could not infer an architecture", attention=True)

    existing = _latest_arch_for_workload(ctx.tenant_id, ctx.workload_id)
    payload: dict[str, Any] = {
        "description": result["description"],
        "workload_id": ctx.workload_id,
        "workload_name": ctx.workload.get("name", ""),
        "connection_id": ctx.connection_id,
        "tenant_id": ctx.tenant_id,
        "source": "ai",
        "nodes": result["nodes"],
        "edges": result["edges"],
        "groups": result["groups"],
        "created_by": ctx.actor,
        "ai": {
            "rationale": result["rationale"],
            "confidence": result["confidence"],
            "resource_count": len(resources),
            "generated_by": ctx.actor,
        },
    }
    if existing:
        payload["id"] = existing["id"]
        saved = areg.upsert_architecture(payload, actor=ctx.actor, reason="Mission Control rebuild")
    else:
        payload["name"] = result["name"] or f"{ctx.workload.get('name', 'Workload')} architecture"
        saved = areg.upsert_architecture(payload, actor=ctx.actor, reason="Mission Control")
    n = len(result["nodes"])
    return SystemResult(
        status="done",
        headline=f"{n} nodes · {result.get('confidence', 'medium')} confidence",
        score=n,
        link=f"/architectures/{saved['id']}",
        result_ref={"kind": "architecture", "id": saved["id"]},
    )


async def _state_architecture(ctx: MissionContext) -> dict[str, Any] | None:
    arch = _latest_arch_for_workload(ctx.tenant_id, ctx.workload_id)
    if not arch:
        return None
    n = len(arch.get("nodes") or [])
    return {
        "status": "done",
        "headline": f"{n} nodes",
        "score": n,
        "attention": False,
        "age_seconds": _age_seconds({"generated_at": arch.get("updated_at") or arch.get("created_at")}),
        "link": f"/architectures/{arch['id']}",
    }


async def _run_memory(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.architectures import memory as mem
    from app.architectures.memory_designer import generate_memory
    from app.architectures.reverse import dump_resources

    arch = _latest_arch_for_workload(ctx.tenant_id, ctx.workload_id)
    if arch is None:
        return SystemResult(status="skipped", headline="No architecture yet", detail="Run Architecture first")
    if progress:
        await progress("Querying resources for memory context…")
    dump = await dump_resources(ctx.workload, ctx.connection)
    resources = dump.get("resources") or []
    try:
        from app.api.architectures import _gather_weakness_signals

        signals = await _gather_weakness_signals(arch["id"], ctx.workload_id, ctx.tenant_id, ctx.connection_id)
    except Exception:  # noqa: BLE001
        signals = []
    if progress:
        await progress("Drafting memory sections…")
    result = await generate_memory(arch, resources, signals, ctx.workload.get("name", ""))
    if result is None:
        return SystemResult(status="fail", headline="AI could not draft memory", attention=True, link=f"/architectures/{arch['id']}/memory")
    existing = mem.get_memory(arch["id"])
    sections = mem.merge_ai_sections((existing or {}).get("sections"), result["sections"])
    mem.upsert_memory(
        arch["id"],
        workload_id=ctx.workload_id,
        sections=sections,
        source="ai" if existing is None else "hybrid",
        ai={
            "confidence": result.get("confidence"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": ctx.actor,
            "resource_count": len(resources),
        },
        tenant_id=ctx.tenant_id,
        actor=ctx.actor,
        reason="Mission Control",
    )
    n = len([k for k, v in (result["sections"] or {}).items() if v])
    return SystemResult(
        status="done",
        headline=f"{n} sections documented",
        score=n,
        link=f"/architectures/{arch['id']}/memory",
        result_ref={"kind": "architecture_memory", "id": arch["id"]},
    )


async def _state_memory(ctx: MissionContext) -> dict[str, Any] | None:
    from app.architectures import memory as mem

    arch = _latest_arch_for_workload(ctx.tenant_id, ctx.workload_id)
    if arch is None:
        return None
    m = mem.get_memory(arch["id"])
    if not m:
        return None
    n = len([k for k, v in (m.get("sections") or {}).items() if v])
    return {
        "status": "done",
        "headline": f"{n} sections documented",
        "score": n,
        "attention": False,
        "age_seconds": _age_seconds({"generated_at": m.get("updated_at") or m.get("created_at")}),
        "link": f"/architectures/{arch['id']}/memory",
    }


# --------------------------------------------------------------------------- assessment
def _assessment_pillars() -> list[str]:
    from app.assessments import catalog

    return list(catalog.PILLARS)


async def _run_assessment(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.assessments.runner import run_assessment_to_completion
    from app.core.db import SessionLocal
    from app.models import AssessmentRun

    pillars = _assessment_pillars()
    run_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(
            AssessmentRun(
                id=run_id,
                workload_id=ctx.workload_id,
                workload_name=ctx.workload.get("name", "workload"),
                tenant_id=ctx.tenant_id,
                connection_id=ctx.connection_id or None,
                pillars=pillars,
                status="queued",
                triggered_by=ctx.actor,
                trigger="mission",
            )
        )
        await db.commit()
    if progress:
        await progress("Running Well-Architected assessment…")
    await run_assessment_to_completion(
        run_id=run_id,
        workload_id=ctx.workload_id,
        pillars=pillars,
        tenant_id=ctx.tenant_id,
        connection_id=ctx.connection_id,
        actor=ctx.actor,
        trigger="mission",
        use_ai=True,
    )
    link = f"/assessments/{run_id}"
    async with SessionLocal() as db:
        row = await db.get(AssessmentRun, run_id)
    if row is None or row.status == "failed":
        err = (row.error if row else "") or "Assessment failed"
        return SystemResult(status="fail", headline=str(err)[:140], error=str(err), attention=True, link=link, result_ref={"kind": "assessment", "id": run_id})
    score = row.overall_score
    failed = int((row.totals_json or {}).get("failed") or 0)
    attention = (score is not None and score < 70) or failed > 0
    head = f"{score}/100 · {failed} fail" if score is not None else f"{failed} fail"
    return SystemResult(status="done", headline=head, score=score, attention=attention, link=link, result_ref={"kind": "assessment", "id": run_id})


async def _state_assessment(ctx: MissionContext) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models import AssessmentRun

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(AssessmentRun)
                .where(
                    AssessmentRun.tenant_id == ctx.tenant_id,
                    AssessmentRun.workload_id == ctx.workload_id,
                    AssessmentRun.status == "succeeded",
                    AssessmentRun.deleted_at.is_(None),
                )
                .order_by(AssessmentRun.started_at.desc())
                .limit(1)
            )
        ).scalars().first()
    if row is None:
        return None
    score = row.overall_score
    failed = int((row.totals_json or {}).get("failed") or 0)
    ran = row.ended_at or row.started_at
    return {
        "status": "done",
        "headline": f"{score}/100 · {failed} fail" if score is not None else f"{failed} fail",
        "score": score,
        "attention": (score is not None and score < 70) or failed > 0,
        "age_seconds": (_now() - ran.replace(tzinfo=ran.tzinfo or timezone.utc)).total_seconds() if ran else None,
        "link": f"/assessments/{row.id}",
    }


# --------------------------------------------------------------------------- performance
async def _run_performance(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.api.perfprofile import _get_snapshot

    if progress:
        await progress("Profiling performance metrics…")
    snap = await _get_snapshot(_admin_principal(ctx.tenant_id, ctx.actor), "workload", ctx.workload_id, force=True)
    link = f"/performance?workload_id={ctx.workload_id}"
    if snap.get("error"):
        return SystemResult(status="fail", headline=str(snap["error"])[:140], error=str(snap["error"]), attention=True, link=link)
    # Persist a run (which also records the performance trend point) so this scan shows up on the
    # dashboard performance lens + the Performance screen history, like a screen-launched refresh.
    try:
        from app.perfprofile import runs as perf_runs

        perf_runs.save_run(ctx.tenant_id or "default", "workload", ctx.workload_id, snap, actor=ctx.actor)
    except Exception:  # noqa: BLE001 - trend/run recording must never break a mission
        logger.warning("performance trend recording failed", exc_info=True)
    sc = snap.get("scorecard") or {}
    score = sc.get("workload_score")
    breaching = int(sc.get("breaching") or 0)
    attention = breaching > 0 or (isinstance(score, (int, float)) and score < 70)
    head = f"score {score} · {breaching} breaching" if score is not None else f"{breaching} breaching"
    return SystemResult(
        status="done",
        headline=head,
        score=int(score) if isinstance(score, (int, float)) else None,
        attention=attention,
        link=link,
        result_ref={"kind": "performance", "workload_id": ctx.workload_id},
    )


async def _state_performance(ctx: MissionContext) -> dict[str, Any] | None:
    from app.perfprofile import cache

    snap = cache.read_snapshot(ctx.tenant_id or "default", "workload", ctx.workload_id)
    if not snap:
        return None
    sc = snap.get("scorecard") or {}
    score = sc.get("workload_score")
    breaching = int(sc.get("breaching") or 0)
    return {
        "status": "done",
        "headline": f"score {score} · {breaching} breaching" if score is not None else f"{breaching} breaching",
        "score": int(score) if isinstance(score, (int, float)) else None,
        "attention": breaching > 0 or (isinstance(score, (int, float)) and score < 70),
        "age_seconds": _age_seconds(snap),
        "link": f"/performance?workload_id={ctx.workload_id}",
    }


# --------------------------------------------------------------------------- tag intelligence
def _normalize_dump(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map ``reverse.dump_resources`` rows (camelCase, tags may be None) into the snake_case
    shape the tagintel analysis layer expects."""
    out = []
    for r in resources:
        tags = r.get("tags")
        out.append({
            "id": r.get("id", ""), "name": r.get("name", ""), "type": (r.get("type", "") or "").lower(),
            "resource_group": r.get("resourceGroup") or "", "subscription_id": r.get("subscriptionId") or "",
            "tags": tags if isinstance(tags, dict) else {}, "workloads": [],
        })
    return out


def _tagintel_headline(resources: list[dict[str, Any]], tenant_id: str) -> tuple[str, int | None, bool]:
    from app.tagintel import analysis, catalog
    from app.tagintel import coverage as coverage_mod

    cen = analysis.census(resources)
    required = catalog.required_keys(tenant_id)
    if required:
        cov = coverage_mod.coverage(resources, required)
        pct = int(round(cov["coverage_pct"]))
        return f"{pct}% required tags · {cov['missing_one_total']} one-off gaps", pct, pct < 80
    pct = int(round(cen["tag_coverage_pct"]))
    return f"{pct}% tagged · {cen['distinct_keys']} keys", pct, pct < 50


async def _run_tagintel(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.architectures.reverse import dump_resources

    if progress:
        await progress("Analyzing tags…")
    link = "/tagintel/coverage"
    dump = await dump_resources(ctx.workload, ctx.connection)
    if dump.get("error"):
        return SystemResult(status="fail", headline=str(dump["error"])[:140], error=str(dump["error"]), attention=True, link=link)
    resources = _normalize_dump(dump.get("resources", []))
    head, score, attention = _tagintel_headline(resources, ctx.tenant_id or "default")
    return SystemResult(status="done", headline=head, score=score, attention=attention, link=link,
                        result_ref={"kind": "tagintel", "workload_id": ctx.workload_id})


async def _state_tagintel(ctx: MissionContext) -> dict[str, Any] | None:
    # No dedicated per-workload tagintel cache — the board shows "not run yet" until launched.
    return None


# --------------------------------------------------------------------------- change explorer
def _changeexplorer_window(days: int = 1) -> tuple[str, str]:
    """Default change-analysis window — the trailing ``days`` (defaults to 1 day)."""
    end = _now()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _changeexplorer_headline(run: dict[str, Any]) -> tuple[str, int, bool]:
    total = int(run.get("totalChanges") or 0)
    crit = int(run.get("criticalCount") or 0)
    high = int(run.get("highCount") or 0)
    attention = (crit + high) > 0
    if total == 0:
        return "no changes detected", 0, False
    parts = [f"{total} change(s)"]
    if crit:
        parts.append(f"{crit} critical")
    if high:
        parts.append(f"{high} high")
    return " · ".join(parts), total, attention


async def _run_changeexplorer(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.changeexplorer import demo as ce_demo, runs as runs_store, service

    link = f"/change-explorer?workload_id={ctx.workload_id}"
    if progress:
        await progress("Analyzing changes in the last 24h…")
    start_iso, end_iso = _changeexplorer_window(1)
    run = await service.analyze(
        tenant_id=ctx.tenant_id, workload=ctx.workload, connection=ctx.connection,
        start_iso=start_iso, end_iso=end_iso, scope_mode="workload",
        requested_by=ctx.actor, force_demo=ce_demo.is_demo(ctx.workload_id),
    )
    if not run:
        return SystemResult(status="fail", headline="Change analysis produced no run", attention=True, link=link)
    runs_store.save_run(ctx.tenant_id, ctx.workload_id, run)
    head, total, attention = _changeexplorer_headline(run)
    return SystemResult(status="done", headline=head, score=total, attention=attention, link=link,
                        result_ref={"kind": "changeexplorer", "workload_id": ctx.workload_id, "run_id": run.get("runId", "")})


async def _state_changeexplorer(ctx: MissionContext) -> dict[str, Any] | None:
    from app.changeexplorer import runs as runs_store

    runs = runs_store.list_runs(ctx.tenant_id, ctx.workload_id)
    if not runs:
        return None
    latest = runs[0]
    head, total, attention = _changeexplorer_headline(latest)
    return {
        "status": "done",
        "headline": head,
        "score": total,
        "attention": attention,
        "age_seconds": _age_seconds({"generated_at": latest.get("completedAt") or latest.get("createdAt")}),
        "link": f"/change-explorer?workload_id={ctx.workload_id}",
    }


# --------------------------------------------------------------------------- inventory
async def _run_inventory(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.architectures.reverse import dump_resources

    if progress:
        await progress("Enumerating resources in scope…")
    link = "/inventory"
    dump = await dump_resources(ctx.workload, ctx.connection)
    if dump.get("error"):
        return SystemResult(status="fail", headline=str(dump["error"])[:140], error=str(dump["error"]), attention=True, link=link)
    resources = dump.get("resources") or []
    if not resources:
        return SystemResult(status="done", headline="0 resources in scope", score=0, link=link,
                            result_ref={"kind": "inventory", "workload_id": ctx.workload_id})
    types = len({(r.get("type", "") or "").lower() for r in resources})
    rgs = len({(r.get("resourceGroup") or "").lower() for r in resources if r.get("resourceGroup")})
    head = f"{len(resources)} resources · {types} types · {rgs} RGs"
    return SystemResult(status="done", headline=head, score=len(resources), link=link,
                        result_ref={"kind": "inventory", "workload_id": ctx.workload_id})


async def _state_inventory(ctx: MissionContext) -> dict[str, Any] | None:
    # No dedicated per-workload inventory cache — the board shows "not run yet" until launched.
    return None


# --------------------------------------------------------------------------- rbac
def _scope_subscriptions(scope: dict[str, Any]) -> set[str]:
    """Every subscription GUID a workload touches — whole-sub nodes plus the subscription
    side of any resource-group / individual-resource node, so RG/resource-scoped workloads
    still resolve their owning subscription(s) for an access scan."""
    subs: set[str] = set(scope.get("subs") or set())
    for pair in (scope.get("rg_pairs") or set()) | (scope.get("resource_rgs") or set()):
        if pair and pair[0]:
            subs.add(pair[0])
    return {s for s in subs if s}


def _rbac_headline(ov: dict[str, Any]) -> tuple[str, int, bool]:
    k = ov.get("kpis") or {}
    total = int(k.get("total_assignments") or 0)
    priv = int(k.get("privileged") or 0)
    owners = int(k.get("owners") or 0)
    head = f"{total} assignments · {priv} privileged · {owners} owners"
    return head, priv, (priv > 0 or owners > 0)


async def _run_rbac(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.architectures.reverse import resolve_scope
    from app.rbac import compose, orchestrator

    link = "/rbac"
    if ctx.connection is None:
        return SystemResult(status="skipped", headline="No Azure connection", link=link)

    async def _p(level: str, msg: str) -> None:
        if progress:
            await progress(msg)

    scope = await resolve_scope(ctx.workload, ctx.connection)
    subs = sorted(_scope_subscriptions(scope))
    if not subs:
        return SystemResult(status="skipped", headline="No subscriptions in scope", detail=scope.get("error", ""), link=link)
    if progress:
        await progress(f"Scanning access across {len(subs)} subscription(s)…")
    for sid in subs:
        await orchestrator.refresh_scope(ctx.tenant_id, ctx.connection, f"/subscriptions/{sid}", display_name=sid, progress=_p)
    if progress:
        await progress("Resolving directory roles, groups & owners…")
    await orchestrator.refresh_directory(ctx.tenant_id, ctx.connection, progress=_p)

    ov = compose.compute_overview(ctx.tenant_id)
    head, priv, attention = _rbac_headline(ov)
    return SystemResult(status="done", headline=head, score=priv, attention=attention, link=link,
                        result_ref={"kind": "rbac", "tenant_id": ctx.tenant_id})


async def _state_rbac(ctx: MissionContext) -> dict[str, Any] | None:
    from app.rbac import cache, compose

    if not cache.has_any(ctx.tenant_id):
        return None
    ov = compose.compute_overview(ctx.tenant_id)
    head, priv, attention = _rbac_headline(ov)
    return {
        "status": "done",
        "headline": head,
        "score": priv,
        "attention": attention,
        "age_seconds": _age_seconds({"generated_at": ov.get("generated_at")}),
        "link": "/rbac",
    }


# --------------------------------------------------------------------------- identity
def _identity_headline(snap: dict[str, Any]) -> tuple[str, int, bool]:
    k = snap.get("kpis") or {}
    secrets = int(k.get("expiring_secrets") or 0) + int(k.get("expiring_certs") or 0) + int(k.get("keyvault_expiring") or 0)
    ownerless = int(k.get("ownerless_apps") or 0)
    no_mfa = int(k.get("users_without_mfa") or 0)
    ca = int(k.get("ca_gaps") or 0)
    total = secrets + ownerless + no_mfa + ca
    sev = snap.get("group_severity") or {}
    attention = total > 0 or any(s in ("warning", "error") for s in sev.values())
    if total == 0:
        return "clear · no expiries or gaps", 0, attention
    return f"{secrets} expiring · {ownerless} ownerless · {no_mfa} no-MFA", total, attention


def _identity_tenant(ctx: MissionContext) -> str:
    return (ctx.connection or {}).get("tenant_id") or ctx.tenant_id or "default"


async def _run_identity(ctx: MissionContext, *, force: bool, progress=None) -> SystemResult:
    from app.api.identity import _get_snapshot

    link = "/identity"
    if ctx.connection is None:
        return SystemResult(status="skipped", headline="No Azure connection", link=link)
    if progress:
        await progress("Auditing identities, secrets & MFA…")
    snap = await _get_snapshot(_admin_principal(ctx.tenant_id, ctx.actor), 90, force=True, connection_id=ctx.connection_id)
    head, total, attention = _identity_headline(snap)
    errs = snap.get("errors") or {}
    if errs and total == 0:
        first = next(iter(errs.values()), "Microsoft Graph error")
        return SystemResult(status="fail", headline=str(first)[:140], error=str(first), attention=True, link=link)
    return SystemResult(status="done", headline=head, score=total, attention=attention, link=link,
                        result_ref={"kind": "identity", "tenant_id": _identity_tenant(ctx)})


async def _state_identity(ctx: MissionContext) -> dict[str, Any] | None:
    from app.identity import cache as idcache

    snap = idcache.read_snapshot(_identity_tenant(ctx), 90)
    if not snap or snap.get("never_loaded"):
        return None
    head, total, attention = _identity_headline(snap)
    errs = snap.get("errors") or {}
    if errs and total == 0:
        first = next(iter(errs.values()), "Microsoft Graph error")
        return {
            "status": "fail",
            "headline": str(first)[:140],
            "attention": True,
            "age_seconds": _age_seconds(snap),
            "link": "/identity",
        }
    return {
        "status": "done",
        "headline": head,
        "score": total,
        "attention": attention,
        "age_seconds": _age_seconds(snap),
        "link": "/identity",
    }


# --------------------------------------------------------------------------- registry
SYSTEMS: list[SystemDef] = [
    SystemDef(key="architecture", label="Architecture", icon="🗺️", run=_run_architecture, last_state=_state_architecture, informational=True),
    SystemDef(key="memory", label="Memory", icon="🧠", run=_run_memory, last_state=_state_memory, informational=True),
    SystemDef(key="assessment", label="Assessment", icon="✓", run=_run_assessment, last_state=_state_assessment),
    _coverage_system(key="monitoring", label="Monitoring", icon="📈", module="app.api.amba", cache_module="app.amba.cache", headline=_h_amba, link_path="/coverage?workload_id={wid}", recorder=_rec_amba),
    _coverage_system(key="telemetry", label="Telemetry", icon="📡", module="app.api.telemetry", cache_module="app.telemetry.cache", headline=_h_telemetry, link_path="/telemetry?workload_id={wid}", recorder=_rec_telemetry),
    _coverage_system(key="backupdr", label="Backup & DR", icon="💾", module="app.api.backupdr", cache_module="app.backupdr.cache", headline=_h_backupdr, link_path="/backupdr?workload_id={wid}", recorder=_rec_backupdr),
    SystemDef(key="performance", label="Performance", icon="⚡", run=_run_performance, last_state=_state_performance),
    _coverage_system(key="radar", label="Retirement Radar", icon="📡", module="app.api.radar", cache_module="app.radar.cache", headline=_h_radar, link_path="/radar?workload_id={wid}"),
    SystemDef(key="tagintel", label="Tag Intelligence", icon="🏷️", run=_run_tagintel, last_state=_state_tagintel),
    SystemDef(key="changeexplorer", label="Change Explorer", icon="🕵️", run=_run_changeexplorer, last_state=_state_changeexplorer),
    SystemDef(key="inventory", label="Inventory", icon="🗂️", run=_run_inventory, last_state=_state_inventory, informational=True),
    SystemDef(key="rbac", label="RBAC", icon="🔐", run=_run_rbac, last_state=_state_rbac, informational=True),
    SystemDef(key="identity", label="Identity", icon="🪪", run=_run_identity, last_state=_state_identity),
]

_BY_KEY: dict[str, SystemDef] = {s.key: s for s in SYSTEMS}


def all_system_keys() -> list[str]:
    return [s.key for s in SYSTEMS]


def default_system_keys() -> list[str]:
    return all_system_keys()


def get_system(key: str) -> SystemDef | None:
    return _BY_KEY.get(key)


def resolve_keys(keys: list[str] | None) -> list[str]:
    """Validate + order a requested subset against the canonical system order."""
    if not keys:
        return default_system_keys()
    wanted = {k for k in keys if k in _BY_KEY}
    return [s.key for s in SYSTEMS if s.key in wanted] or default_system_keys()

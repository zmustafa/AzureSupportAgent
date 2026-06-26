"""Mission Control orchestrator.

In-process manager that drives a *mission* (one sweep over a workload): it runs the
selected systems, streams live progress to SSE subscribers, and persists a ``MissionRun``
row incrementally so partial progress + history survive a crash. Mirrors the
``architectures.jobs`` manager pattern (in-memory live state; DB is the durable record).

Concurrency: every system is scheduled at once and runs under a small semaphore (so ≥3 are
in flight from the start). Systems declare ``depends_on`` (Memory → Architecture) and a
dependent simply waits for its prerequisites to finish first. Heavy-LLM systems share a
smaller AI semaphore and retry on provider rate-limits (HTTP 429) with exponential backoff,
so a fan-out of AI work overlaps without tripping the model's per-minute quota.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.missions import systems as sysreg

logger = logging.getLogger("app.missions.orchestrator")

_MAX_SYSTEM_CONCURRENCY = 1   # systems run ONE AT A TIME — each system issues its own Azure
                              # Resource Graph / ARM queries, and fanning several out in parallel
                              # trips Azure's per-tenant request throttling (HTTP 429). Serializing
                              # the systems keeps the whole sweep under Azure's rate limits.
_AI_CONCURRENCY = 1           # at most this many heavy-LLM systems at once (moot while system
                              # concurrency is 1, but kept as an explicit guard).
_AI_MAX_RETRIES = 3           # retry a rate-limited (429) AI system this many times
_AI_BACKOFF_BASE = 4.0        # seconds; exponential backoff base for 429 retries
_RETAIN_SECONDS = 1800  # keep finished missions in memory briefly for live SSE replay
_TERMINAL = {"succeeded", "partial", "failed", "cancelled"}

# Substrings that mark an exception as a provider rate-limit (HTTP 429) we should back off on.
_THROTTLE_MARKERS = ("429", "rate limit", "rate_limit", "too many requests", "toomanyrequests", "throttl")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Mission:
    id: str
    tenant_id: str
    workload_id: str
    workload_name: str
    connection_id: str
    actor: str
    force: bool
    trigger: str
    system_keys: list[str]
    status: str = "queued"
    readiness: str = "unknown"
    systems: dict[str, dict[str, Any]] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    task: asyncio.Task | None = field(default=None, repr=False)
    cancel_requested: bool = field(default=False, repr=False)
    subscribers: set[asyncio.Queue] = field(default_factory=set, repr=False)

    def public(self) -> dict[str, Any]:
        systems = [self.systems[k] for k in self.system_keys if k in self.systems]
        done = sum(1 for s in systems if s.get("status") in ("done", "skipped"))
        attention = sum(1 for s in systems if s.get("attention") or s.get("status") in ("fail", "error"))
        return {
            "id": self.id,
            "workload_id": self.workload_id,
            "workload_name": self.workload_name,
            "connection_id": self.connection_id,
            "status": self.status,
            "readiness": self.readiness,
            "force": self.force,
            "trigger": self.trigger,
            "systems_total": len(self.system_keys),
            "systems_done": done,
            "systems_attention": attention,
            "systems": systems,
            "log": self.log[-50:],
            "error": self.error,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
        }


class _Manager:
    def __init__(self) -> None:
        self._missions: dict[str, _Mission] = {}
        self._bg: set[asyncio.Task] = set()

    # ----------------------------------------------------------------- public API
    def create(
        self,
        *,
        tenant_id: str,
        workload_id: str,
        workload_name: str,
        connection_id: str,
        actor: str,
        force: bool,
        trigger: str,
        system_keys: list[str],
    ) -> dict[str, Any]:
        self._prune()
        mission = _Mission(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            workload_id=workload_id,
            workload_name=workload_name,
            connection_id=connection_id,
            actor=actor,
            force=force,
            trigger=trigger,
            system_keys=sysreg.resolve_keys(system_keys),
        )
        # Seed each system as "queued" so the board renders immediately.
        for key in mission.system_keys:
            sd = sysreg.get_system(key)
            mission.systems[key] = {
                "key": key,
                "label": sd.label if sd else key,
                "icon": sd.icon if sd else "•",
                "status": "queued",
                "headline": "",
                "detail": "",
                "score": None,
                "attention": False,
                "link": "",
                "result_ref": None,
                "error": "",
                "started_at": "",
                "ended_at": "",
            }
        self._missions[mission.id] = mission
        task = asyncio.create_task(self._run(mission))
        mission.task = task
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return mission.public()

    def get_live(self, mission_id: str, tenant_id: str) -> _Mission | None:
        m = self._missions.get(mission_id)
        if m is None or m.tenant_id != tenant_id:
            return None
        return m

    def cancel(self, mission_id: str, tenant_id: str) -> bool:
        m = self.get_live(mission_id, tenant_id)
        if m is None or m.status in _TERMINAL:
            return False
        m.cancel_requested = True
        if m.task is not None:
            m.task.cancel()
        return True

    async def stream(self, mission_id: str, tenant_id: str):
        """SSE generator: emit a snapshot then live deltas until the mission ends."""
        m = self.get_live(mission_id, tenant_id)
        if m is None:
            # Not live in this process. It either finished and aged out of the in-memory
            # retain window, or it was orphaned by a server restart (the driving task died
            # but the DB row may still read running/queued). Fall back to the durable DB
            # record and emit a final snapshot instead of a bare "Mission not found." — a
            # reconnect should show the mission's last persisted state, not a scary error.
            snap = await get_mission(mission_id, tenant_id)
            if snap is None:
                yield {"event": "error", "data": _json({"message": "Mission not found."})}
                return
            yield {"event": "snapshot", "data": _json(snap)}
            yield {"event": "done", "data": _json(snap)}
            return
        q: asyncio.Queue = asyncio.Queue()
        m.subscribers.add(q)
        try:
            yield {"event": "snapshot", "data": _json(m.public())}
            if m.status in _TERMINAL:
                yield {"event": "done", "data": _json(m.public())}
                return
            while True:
                ev = await q.get()
                yield ev
                if ev.get("event") == "done":
                    return
        finally:
            m.subscribers.discard(q)

    async def state(self, *, tenant_id: str, workload_id: str, actor: str) -> dict[str, Any]:
        """Build the board from each system's cached last_state — never scans Azure."""
        from app.core.azure_connections import connection_for_workload
        from app.workloads.registry import get_workload

        wl = get_workload(workload_id)
        if wl is None:
            return {"workload_id": workload_id, "systems": [], "error": "Workload not found."}
        conn = connection_for_workload(wl)
        ctx = sysreg.MissionContext(
            tenant_id=tenant_id,
            actor=actor,
            workload_id=workload_id,
            workload=wl,
            connection=conn,
            connection_id=(conn or {}).get("id", ""),
        )
        systems: list[dict[str, Any]] = []
        for sd in sysreg.SYSTEMS:
            try:
                st = await sd.last_state(ctx)
            except Exception:  # noqa: BLE001
                logger.warning("last_state failed for %s", sd.key, exc_info=True)
                st = None
            entry = {"key": sd.key, "label": sd.label, "icon": sd.icon, "informational": sd.informational}
            if st:
                age = st.get("age_seconds")
                entry.update(
                    {
                        "status": st.get("status", "done"),
                        "headline": st.get("headline", ""),
                        "score": st.get("score"),
                        "attention": bool(st.get("attention")),
                        "link": st.get("link", ""),
                        "age_seconds": age,
                        "fresh": age is not None and age < sysreg.FRESH_SECONDS,
                    }
                )
            else:
                entry.update({"status": "idle", "headline": "", "score": None, "attention": False, "link": "", "age_seconds": None, "fresh": False})
            systems.append(entry)
        return {
            "workload_id": workload_id,
            "workload_name": wl.get("name", ""),
            "connection_id": (conn or {}).get("id", ""),
            "systems": systems,
        }

    # ----------------------------------------------------------------- internals
    def _emit(self, mission: _Mission, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": _json(data)}
        for q in list(mission.subscribers):
            try:
                q.put_nowait(payload)
            except Exception:  # noqa: BLE001
                pass

    def _log(self, mission: _Mission, message: str, key: str = "") -> None:
        mission.log.append({"ts": _now().isoformat(), "key": key, "message": message})
        self._emit(mission, "log", {"message": message, "key": key, "ts": _now().isoformat()})

    async def _persist(self, mission: _Mission) -> None:
        from app.core.db import SessionLocal
        from app.models import MissionRun

        pub = mission.public()
        try:
            async with SessionLocal() as db:
                row = await db.get(MissionRun, mission.id)
                if row is None:
                    return
                row.status = mission.status
                row.readiness = mission.readiness
                row.systems_total = pub["systems_total"]
                row.systems_done = pub["systems_done"]
                row.systems_attention = pub["systems_attention"]
                row.systems_json = pub["systems"]
                row.log_json = mission.log  # persist the full activity log so it reloads on reopen
                row.error = mission.error or None
                if mission.status in _TERMINAL and row.ended_at is None:
                    row.ended_at = _now()
                    if mission.started_at:
                        row.duration_ms = int((time.time() - mission.started_at) * 1000)
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("Mission persist failed for %s", mission.id, exc_info=True)

    async def _create_row(self, mission: _Mission) -> None:
        from app.core.db import SessionLocal
        from app.models import MissionRun

        try:
            async with SessionLocal() as db:
                db.add(
                    MissionRun(
                        id=mission.id,
                        tenant_id=mission.tenant_id,
                        workload_id=mission.workload_id,
                        workload_name=mission.workload_name,
                        connection_id=mission.connection_id or None,
                        status="running",
                        readiness="unknown",
                        systems_total=len(mission.system_keys),
                        systems_json=[mission.systems[k] for k in mission.system_keys],
                        log_json=list(mission.log),
                        force=mission.force,
                        triggered_by=mission.actor,
                        trigger=mission.trigger,
                    )
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("Mission row create failed for %s", mission.id, exc_info=True)

    async def _exec_system(self, mission: _Mission, ctx: sysreg.MissionContext, key: str, sem: asyncio.Semaphore, ai_sem: asyncio.Semaphore) -> None:
        sd = sysreg.get_system(key)
        if sd is None:
            return
        entry = mission.systems[key]
        ai_heavy = bool(getattr(sd, "ai_heavy", False))
        # Heavy-LLM systems take the AI throttle FIRST (before a general slot) so a system
        # that's waiting its turn for the LLM doesn't sit on one of the few general slots —
        # that keeps non-AI systems flowing and ≥_MAX_SYSTEM_CONCURRENCY work in flight.
        if ai_heavy:
            await ai_sem.acquire()
        try:
            async with sem:
                if mission.cancel_requested:
                    entry.update({"status": "skipped", "headline": "Cancelled", "ended_at": _now().isoformat()})
                    self._emit(mission, "system", entry)
                    return
                entry["status"] = "running"
                entry["started_at"] = _now().isoformat()
                self._emit(mission, "system", entry)
                self._log(mission, f"{sd.label}: started", key)

                # Freshness skip (unless forced).
                if not mission.force:
                    try:
                        st = await sd.last_state(ctx)
                    except Exception:  # noqa: BLE001
                        st = None
                    age = (st or {}).get("age_seconds")
                    if st and age is not None and age < sysreg.FRESH_SECONDS:
                        entry.update(
                            {
                                "status": "skipped",
                                "headline": st.get("headline", "fresh"),
                                "detail": "Fresh — skipped (force to re-run)",
                                "score": st.get("score"),
                                "attention": bool(st.get("attention")),
                                "link": st.get("link", ""),
                                "ended_at": _now().isoformat(),
                            }
                        )
                        self._emit(mission, "system", entry)
                        self._log(mission, f"{sd.label}: skipped (fresh)", key)
                        await self._persist(mission)
                        return

                async def _progress(msg: str) -> None:
                    self._log(mission, f"{sd.label}: {msg}", key)

                # Run with a bounded retry on provider rate-limits (429). A heavy fan-out of
                # AI systems can trip the LLM's per-minute quota; we back off and retry rather
                # than fail the whole system on a transient throttle.
                res: sysreg.SystemResult | None = None
                for attempt in range(_AI_MAX_RETRIES + 1):
                    try:
                        res = await sd.run(ctx, force=mission.force, progress=_progress)
                        break
                    except asyncio.CancelledError:
                        entry.update({"status": "skipped", "headline": "Cancelled", "ended_at": _now().isoformat()})
                        self._emit(mission, "system", entry)
                        raise
                    except Exception as exc:  # noqa: BLE001
                        msg = str(exc).lower()
                        throttled = any(m in msg for m in _THROTTLE_MARKERS)
                        if throttled and attempt < _AI_MAX_RETRIES:
                            delay = _AI_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1.5)
                            self._log(mission, f"{sd.label}: rate-limited (429) — backing off {delay:.0f}s (retry {attempt + 1}/{_AI_MAX_RETRIES})", key)
                            await asyncio.sleep(delay)
                            continue
                        logger.exception("Mission system %s failed", key)
                        head = "Rate-limited (429) — try again later" if throttled else str(exc)[:140]
                        res = sysreg.SystemResult(status="error", headline=head, error=str(exc)[:300], attention=True)
                        break

                entry.update(
                    {
                        "status": res.status,
                        "headline": res.headline,
                        "detail": res.detail,
                        "score": res.score,
                        "attention": res.attention,
                        "link": res.link,
                        "result_ref": res.result_ref,
                        "error": res.error,
                        "ended_at": _now().isoformat(),
                    }
                )
                self._emit(mission, "system", entry)
                self._log(mission, f"{sd.label}: {res.status} — {res.headline}", key)
                await self._persist(mission)
        finally:
            if ai_heavy:
                ai_sem.release()

    def _rollup(self, mission: _Mission) -> None:
        systems = [mission.systems[k] for k in mission.system_keys]
        hard_fail = any(s["status"] in ("fail", "error") for s in systems)
        attention = any(s.get("attention") or s["status"] in ("fail", "error") for s in systems)
        ran = [s for s in systems if s["status"] in ("done", "fail", "error")]
        if hard_fail:
            mission.readiness = "nogo"
        elif attention:
            mission.readiness = "warn"
        else:
            mission.readiness = "go"
        if mission.cancel_requested:
            mission.status = "cancelled"
        elif ran and all(s["status"] in ("fail", "error") for s in ran):
            mission.status = "failed"
        elif any(s["status"] in ("fail", "error") for s in systems):
            mission.status = "partial"
        else:
            mission.status = "succeeded"

    async def _run(self, mission: _Mission) -> None:
        from app.core.azure_connections import connection_for_workload, resolve_connection
        from app.workloads.registry import get_workload

        mission.status = "running"
        mission.started_at = time.time()
        await self._create_row(mission)
        self._log(mission, f"Mission launched for '{mission.workload_name}' ({len(mission.system_keys)} systems)")

        try:
            wl = get_workload(mission.workload_id)
            if wl is None:
                mission.error = "Workload not found."
                mission.status = "failed"
                mission.ended_at = time.time()
                self._emit(mission, "done", {})
                await self._persist(mission)
                return
            conn = resolve_connection(mission.connection_id) if mission.connection_id else connection_for_workload(wl)
            ctx = sysreg.MissionContext(
                tenant_id=mission.tenant_id,
                actor=mission.actor,
                workload_id=mission.workload_id,
                workload=wl,
                connection=conn,
                connection_id=mission.connection_id or (conn or {}).get("id", ""),
            )

            sem = asyncio.Semaphore(_MAX_SYSTEM_CONCURRENCY)
            ai_sem = asyncio.Semaphore(_AI_CONCURRENCY)
            keys = list(mission.system_keys)

            # Dependency-aware SERIAL scheduling. Every system coroutine is created up front, but
            # the shared `sem` (concurrency 1) lets only ONE run at a time, so the systems execute
            # sequentially — no parallel Azure calls that would trip 429 throttling. A system that
            # declares `depends_on` (e.g. Memory → Architecture) still waits for those systems to
            # finish first — but only for dependencies that are part of THIS mission, so a subset
            # that excludes the dependency still runs.
            done_events: dict[str, asyncio.Event] = {k: asyncio.Event() for k in keys}

            async def _scheduled(key: str) -> None:
                sd = sysreg.get_system(key)
                for dep in (sd.depends_on if sd else ()):
                    ev = done_events.get(dep)
                    if ev is not None:
                        await ev.wait()
                try:
                    await self._exec_system(mission, ctx, key, sem, ai_sem)
                finally:
                    done_events[key].set()

            await asyncio.gather(*(_scheduled(k) for k in keys))

            self._rollup(mission)
            mission.ended_at = time.time()
            self._log(mission, f"Mission complete — {mission.readiness.upper()} ({mission.status})")
        except asyncio.CancelledError:
            mission.cancel_requested = True
            self._rollup(mission)
            mission.ended_at = time.time()
            self._log(mission, "Mission cancelled")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Mission run failed")
            mission.error = str(exc)[:300]
            mission.status = "failed"
            mission.ended_at = time.time()
        finally:
            await self._persist(mission)
            self._emit(mission, "done", mission.public())

    def _prune(self) -> None:
        now = time.time()
        stale = [mid for mid, m in self._missions.items() if m.status in _TERMINAL and m.ended_at and (now - m.ended_at) > _RETAIN_SECONDS]
        for mid in stale:
            self._missions.pop(mid, None)


def _json(data: Any) -> str:
    import json

    return json.dumps(data, default=str)


manager = _Manager()


async def run_to_completion(
    *,
    tenant_id: str,
    workload_id: str,
    workload_name: str,
    connection_id: str,
    actor: str,
    force: bool,
    trigger: str,
    system_keys: list[str] | None,
) -> dict[str, Any]:
    """Launch a mission and await its completion (used by the scheduler)."""
    pub = manager.create(
        tenant_id=tenant_id,
        workload_id=workload_id,
        workload_name=workload_name,
        connection_id=connection_id,
        actor=actor,
        force=force,
        trigger=trigger,
        system_keys=system_keys or [],
    )
    mission = manager.get_live(pub["id"], tenant_id)
    if mission is not None and mission.task is not None:
        try:
            await mission.task
        except Exception:  # noqa: BLE001
            logger.warning("Awaited mission %s ended with error", pub["id"], exc_info=True)
    return (await get_mission(pub["id"], tenant_id)) or pub

async def list_missions(tenant_id: str, workload_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models import MissionRun

    async with SessionLocal() as db:
        stmt = select(MissionRun).where(MissionRun.tenant_id == tenant_id, MissionRun.deleted_at.is_(None))
        if workload_id:
            stmt = stmt.where(MissionRun.workload_id == workload_id)
        stmt = stmt.order_by(MissionRun.started_at.desc()).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
    return [_row_public(r, include_log=False) for r in rows]


async def reap_orphaned_missions() -> int:
    """Fail any missions left ``queued``/``running`` by a previous process.

    A mission only advances inside a live in-process task (the in-memory ``_Manager``);
    once the process exits, an in-flight mission can never resume, so a row stuck at
    ``running``/``queued`` is an orphan. If left as-is the board's reconnect-on-mount sees
    that stale "running" row, tries to follow its (now non-existent) live stream, and the
    user gets a spurious "Mission not found." Called once at startup so history never shows
    a mission as perpetually in progress and the board never tries to resume a dead run.
    Returns the number of missions reaped."""
    from sqlalchemy import update

    from app.core.db import SessionLocal
    from app.models import MissionRun

    async with SessionLocal() as db:
        result = await db.execute(
            update(MissionRun)
            .where(MissionRun.status.in_(("queued", "running")))
            .values(
                status="failed",
                error="Interrupted by a server restart before completion.",
                ended_at=_now(),
            )
        )
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def get_mission(mission_id: str, tenant_id: str) -> dict[str, Any] | None:
    # Prefer the live in-memory mission (has the log + latest deltas); fall back to DB.
    live = manager.get_live(mission_id, tenant_id)
    if live is not None:
        return live.public()
    from app.core.db import SessionLocal
    from app.models import MissionRun

    async with SessionLocal() as db:
        row = await db.get(MissionRun, mission_id)
    if row is None or row.tenant_id != tenant_id or row.deleted_at is not None:
        return None
    return _row_public(row)


async def delete_mission(mission_id: str, tenant_id: str) -> bool:
    from app.core.db import SessionLocal
    from app.models import MissionRun

    async with SessionLocal() as db:
        row = await db.get(MissionRun, mission_id)
        if row is None or row.tenant_id != tenant_id or row.deleted_at is not None:
            return False
        row.deleted_at = _now()
        await db.commit()
    return True


async def delete_missions_for_workload(tenant_id: str, workload_id: str) -> int:
    """Permanently remove a workload's entire Mission Control (every mission run for it).

    There is no trash for missions, so this hard-deletes the rows. Any live in-memory mission
    for the workload is cancelled and dropped first so a streaming board can't resurrect it.
    Returns the number of mission rows deleted."""
    from sqlalchemy import delete

    from app.core.db import SessionLocal
    from app.models import MissionRun

    # Drop/cancel any live missions for this workload from the in-memory manager.
    for mid, m in list(manager._missions.items()):
        if m.tenant_id == tenant_id and m.workload_id == workload_id:
            if m.status not in _TERMINAL and m.task is not None:
                m.cancel_requested = True
                m.task.cancel()
            manager._missions.pop(mid, None)

    async with SessionLocal() as db:
        result = await db.execute(
            delete(MissionRun).where(
                MissionRun.tenant_id == tenant_id, MissionRun.workload_id == workload_id
            )
        )
        await db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


def _row_public(row: Any, *, include_log: bool = True) -> dict[str, Any]:
    return {
        "id": row.id,
        "workload_id": row.workload_id,
        "workload_name": row.workload_name,
        "connection_id": row.connection_id,
        "status": row.status,
        "readiness": row.readiness,
        "force": row.force,
        "trigger": row.trigger,
        "systems_total": row.systems_total,
        "systems_done": row.systems_done,
        "systems_attention": row.systems_attention,
        "systems": row.systems_json or [],
        # The log can be long; the history LIST doesn't render it, so omit it there and only
        # return it from get_mission (which the board uses to reload a reopened mission's log).
        "log": (row.log_json or []) if include_log else [],
        "error": row.error or "",
        "created_at": row.started_at.isoformat() if row.started_at else "",
        "started_at": row.started_at.isoformat() if row.started_at else "",
        "ended_at": row.ended_at.isoformat() if row.ended_at else "",
        "duration_ms": row.duration_ms,
    }

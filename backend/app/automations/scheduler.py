"""In-process scheduler: ticks periodically and dispatches due scheduled tasks.

A single async loop wakes every ``TICK_SECONDS``, finds enabled tasks whose
``next_run_at`` is due, and runs them as background tasks (so a slow run never blocks
the loop). Task definitions live in the DB (durable, queryable), so promoting to a
Redis-backed multi-instance worker later is a drop-in (add a leader lock around the
dispatch). Concurrency is bounded; failures are logged and never crash the loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.automations.runner import run_target_task, run_task
from app.automations.schedule import compute_next_run
from app.core.db import SessionLocal
from app.models import ScheduledTask

logger = logging.getLogger("app.automations.scheduler")

TICK_SECONDS = 30
MAX_CONCURRENT_RUNS = 4
# Run housekeeping (purge stale auth sessions) roughly once a day.
_HOUSEKEEPING_EVERY_TICKS = max(1, (24 * 60 * 60) // TICK_SECONDS)


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running_ids: set[str] = set()
        self._inflight: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self._stop = asyncio.Event()
        self._tick_count = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            logger.info("Scheduler started (tick=%ss)", TICK_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # On startup, backfill any missing next_run_at so freshly-loaded tasks schedule.
        await self._import_assessment_schedules()  # one-time migration into unified store
        await self._backfill_next_runs()
        await self._housekeeping()  # purge stale sessions once on boot
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                logger.warning("Scheduler tick error: %s", exc)
            try:
                await self._siem_flush()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                logger.warning("SIEM flush error: %s", exc)
            try:
                await self._reservations_digest()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                logger.warning("Reservations digest error: %s", exc)
            self._tick_count += 1
            if self._tick_count % _HOUSEKEEPING_EVERY_TICKS == 0:
                try:
                    await self._housekeeping()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Scheduler housekeeping error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _housekeeping(self) -> None:
        """Periodic maintenance: purge expired/revoked auth sessions."""
        from app.auth.service import purge_stale_sessions

        async with SessionLocal() as db:
            removed = await purge_stale_sessions(db)
        if removed:
            logger.info("Housekeeping: purged %d stale sessions", removed)

        # Optional nightly fleet profile refresh (off by default). Warms every workload's
        # per-feature caches so the command center is fully populated each morning.
        try:
            from app.core.app_settings import load_settings

            if load_settings().get("workload_nightly_refresh"):
                from app.workloads.nightly import refresh_all

                await refresh_all()
        except Exception as exc:  # noqa: BLE001 - best-effort; never break housekeeping
            logger.warning("Nightly workload refresh error: %s", exc)

    async def _backfill_next_runs(self) -> None:
        async with SessionLocal() as db:
            rows = (await db.execute(select(ScheduledTask).where(ScheduledTask.status == "on"))).scalars().all()
            changed = False
            for task in rows:
                if task.next_run_at is None:
                    task.next_run_at = compute_next_run(_to_dict(task))
                    if task.next_run_at is None:
                        task.status = "ended"
                    changed = True
            if changed:
                await db.commit()

    async def _siem_flush(self) -> None:
        """Stream new audit-log rows to every configured SIEM destination. Each
        destination drains a bounded number of batches internally so bursts catch up
        quickly without blocking task dispatch. No-op when none are enabled."""
        from app.core.siem_export import flush_once

        await flush_once()

    async def _reservations_digest(self) -> None:
        """Send the weekly Azure Reservations digest when enabled and due. No-op (cheap
        settings read) unless ``reservations_digest_enabled`` is set, so it stays dormant
        until an operator opts in after reviewing the preview."""
        from app.reservations.digest import maybe_send_weekly_digest

        await maybe_send_weekly_digest()

    async def _import_assessment_schedules(self) -> None:
        """One-time migration: fold legacy assessment_schedules.json into ScheduledTask
        rows (target_type='assessment'). The JSON file is renamed to .imported as a
        backup so this runs at most once. Idempotent and best-effort."""
        from pathlib import Path

        json_path = Path(__file__).resolve().parents[2] / ".data" / "assessment_schedules.json"
        if not json_path.exists():
            return
        try:
            from app.assessments import schedules as sched_registry

            legacy = sched_registry.list_schedules()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Assessment schedule import: could not read legacy file: %s", exc)
            return
        if not legacy:
            try:
                json_path.rename(json_path.with_suffix(".json.imported"))
            except OSError:
                pass
            return
        from app.automations.schedule import compute_next_run

        imported = 0
        async with SessionLocal() as db:
            for s in legacy:
                cfg = {
                    "workload_ids": [s["workload_id"]] if s.get("workload_id") else [],
                    "pillars": s.get("pillars") or ["security", "reliability"],
                    "use_ai": bool(s.get("use_ai", True)),
                    "connection_id": s.get("connection_id") or "",
                    "alert_on_new_findings": bool(s.get("alert_on_new_findings", True)),
                    "alert_min_severity": s.get("alert_min_severity", "warning"),
                }
                task = ScheduledTask(
                    tenant_id=s.get("tenant_id") or "",
                    name=s.get("name") or "Imported assessment schedule",
                    target_type="assessment",
                    target_config=cfg,
                    schedule_kind=s.get("schedule_kind", "weekly"),
                    cron_expr=s.get("cron_expr") or None,
                    time_of_day=s.get("time_of_day", "08:00"),
                    weekday=s.get("weekday", 0),
                    timezone=s.get("timezone", "UTC"),
                    status="on" if s.get("enabled") else "off",
                    created_by=s.get("created_by") or "import",
                )
                db.add(task)
                await db.flush()
                if task.status == "on":
                    task.next_run_at = compute_next_run(_to_dict(task))
                    if task.next_run_at is None:
                        task.status = "ended"
                imported += 1
            await db.commit()
        try:
            json_path.rename(json_path.with_suffix(".json.imported"))
        except OSError:
            pass
        logger.info("Imported %d legacy assessment schedule(s) into unified scheduler", imported)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(ScheduledTask).where(ScheduledTask.status == "on")
                )
            ).scalars().all()
            due = [
                t
                for t in rows
                if t.next_run_at is not None
                and t.next_run_at.astimezone(timezone.utc) <= now
                and t.id not in self._running_ids
            ]
        for task in due:
            self._dispatch(task.id, task.target_type or "agent")

    def _dispatch(self, task_id: str, target_type: str) -> None:
        self._running_ids.add(task_id)

        async def _run() -> None:
            async with self._sem:
                try:
                    if target_type == "agent":
                        # The agent runner owns its own TaskRun bookkeeping + lifecycle.
                        await run_task(task_id, trigger="schedule")
                    else:
                        await run_target_task(task_id, trigger="schedule")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Scheduled run for %s failed: %s", task_id, exc)
                finally:
                    self._running_ids.discard(task_id)

        t = asyncio.create_task(_run())
        self._inflight.add(t)
        t.add_done_callback(self._inflight.discard)


def _to_dict(task: ScheduledTask) -> dict:
    return {
        "schedule_kind": task.schedule_kind,
        "cron_expr": task.cron_expr,
        "time_of_day": task.time_of_day,
        "weekday": task.weekday,
        "timezone": task.timezone,
        "start_date": task.start_date,
        "end_date": task.end_date,
    }


# Process-wide singleton.
scheduler = Scheduler()

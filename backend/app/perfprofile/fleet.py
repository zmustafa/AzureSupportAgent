"""Durable SQL-backed Performance Profiler fleet worker.

The browser submits one batch and polls it.  Workload items are claimed from SQL and persisted
at every transition, so closing the browser cannot drop the tail of a large selection.  On a
process restart, interrupted items are re-queued; an item-specific trigger in run history makes
the final persistence window idempotent.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PerfProfileFleetBatch, PerfProfileFleetItem

log = logging.getLogger("app.perfprofile.fleet")

_TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
_ITEM_TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _item_public(item: PerfProfileFleetItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "workload_id": item.workload_id,
        "workload_name": item.workload_name,
        "connection_id": item.connection_id or "",
        "status": item.status,
        "run_id": item.run_id or "",
        "resources_completed": item.resources_completed,
        "resources_total": item.resources_total,
        "collection": item.collection_json or {},
        "error": item.error or "",
        "started_at": _iso(item.started_at),
        "ended_at": _iso(item.ended_at),
        "duration_ms": item.duration_ms,
    }


def _batch_public(batch: PerfProfileFleetBatch, items: list[PerfProfileFleetItem]) -> dict[str, Any]:
    return {
        "id": batch.id,
        "status": batch.status,
        "window": batch.window,
        "start_time": batch.start_time,
        "end_time": batch.end_time,
        "total": batch.total,
        "completed": batch.completed,
        "succeeded": batch.succeeded,
        "partial": batch.partial,
        "failed": batch.failed,
        "cancelled": batch.cancelled,
        "cancel_requested": batch.cancel_requested,
        "error": batch.error or "",
        "triggered_by": batch.triggered_by,
        "created_at": _iso(batch.created_at),
        "started_at": _iso(batch.started_at),
        "ended_at": _iso(batch.ended_at),
        "items": [_item_public(item) for item in items],
    }


async def _refresh_batch(db: AsyncSession, batch_id: str) -> PerfProfileFleetBatch | None:
    batch = await db.get(PerfProfileFleetBatch, batch_id)
    if batch is None:
        return None
    items = (
        await db.execute(
            select(PerfProfileFleetItem).where(PerfProfileFleetItem.batch_id == batch_id)
        )
    ).scalars().all()
    counts = {status: 0 for status in _ITEM_TERMINAL}
    for item in items:
        if item.status in counts:
            counts[item.status] += 1
    completed = sum(counts.values())
    batch.total = len(items)
    batch.completed = completed
    batch.succeeded = counts["succeeded"]
    batch.partial = counts["partial"]
    batch.failed = counts["failed"]
    batch.cancelled = counts["cancelled"]
    if completed == len(items) and items:
        if counts["cancelled"] == len(items):
            batch.status = "cancelled"
        elif counts["failed"] == len(items):
            batch.status = "failed"
        elif counts["partial"] or counts["failed"] or counts["cancelled"]:
            batch.status = "partial"
        else:
            batch.status = "succeeded"
        batch.ended_at = batch.ended_at or _now()
        representative = next((item.error for item in items if item.error), None)
        batch.error = representative
    elif any(item.status == "running" for item in items):
        batch.status = "running"
        batch.started_at = batch.started_at or _now()
    elif batch.cancel_requested and completed == len(items):
        batch.status = "cancelled"
        batch.ended_at = batch.ended_at or _now()
    else:
        batch.status = "queued"
    return batch


async def create_batch(
    db: AsyncSession,
    *,
    tenant_id: str,
    actor: str,
    idempotency_key: str,
    workloads: list[dict[str, Any]],
    window: str,
    start_time: str,
    end_time: str,
) -> tuple[PerfProfileFleetBatch, bool]:
    """Create a batch, or return the existing row for the tenant/idempotency key."""
    existing = (
        await db.execute(
            select(PerfProfileFleetBatch).where(
                PerfProfileFleetBatch.tenant_id == tenant_id,
                PerfProfileFleetBatch.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    batch = PerfProfileFleetBatch(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        status="queued",
        window=window,
        start_time=start_time,
        end_time=end_time,
        total=len(workloads),
        triggered_by=actor,
    )
    db.add(batch)
    for workload in workloads:
        db.add(
            PerfProfileFleetItem(
                id=str(uuid.uuid4()),
                batch_id=batch.id,
                tenant_id=tenant_id,
                workload_id=str(workload.get("id", "")),
                workload_name=str(workload.get("name", "")),
                connection_id=str(workload.get("connection_id") or "") or None,
                status="queued",
            )
        )
    await db.commit()
    return batch, True


async def get_batch(batch_id: str, tenant_id: str) -> dict[str, Any] | None:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        batch = await db.get(PerfProfileFleetBatch, batch_id)
        if batch is None or batch.tenant_id != tenant_id:
            return None
        items = (
            await db.execute(
                select(PerfProfileFleetItem)
                .where(PerfProfileFleetItem.batch_id == batch_id)
                .order_by(PerfProfileFleetItem.started_at.asc(), PerfProfileFleetItem.workload_name.asc())
            )
        ).scalars().all()
        return _batch_public(batch, list(items))


async def latest_batch(tenant_id: str, *, active_only: bool = False) -> dict[str, Any] | None:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        stmt = select(PerfProfileFleetBatch).where(PerfProfileFleetBatch.tenant_id == tenant_id)
        if active_only:
            stmt = stmt.where(PerfProfileFleetBatch.status.in_(("queued", "running")))
        batch = (
            await db.execute(stmt.order_by(PerfProfileFleetBatch.created_at.desc()).limit(1))
        ).scalar_one_or_none()
        if batch is None:
            return None
        items = (
            await db.execute(
                select(PerfProfileFleetItem)
                .where(PerfProfileFleetItem.batch_id == batch.id)
                .order_by(PerfProfileFleetItem.started_at.asc(), PerfProfileFleetItem.workload_name.asc())
            )
        ).scalars().all()
        return _batch_public(batch, list(items))


async def cancel_batch(batch_id: str, tenant_id: str) -> bool:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        batch = await db.get(PerfProfileFleetBatch, batch_id)
        if batch is None or batch.tenant_id != tenant_id or batch.status in _TERMINAL:
            return False
        batch.cancel_requested = True
        await db.execute(
            update(PerfProfileFleetItem)
            .where(
                PerfProfileFleetItem.batch_id == batch_id,
                PerfProfileFleetItem.status == "queued",
            )
            .values(status="cancelled", ended_at=_now(), error="Cancelled before start.")
        )
        await _refresh_batch(db, batch_id)
        await db.commit()
    worker.wake()
    return True


async def delete_batch(batch_id: str, tenant_id: str) -> bool:
    """Delete one terminal batch control record; profile run history remains intact."""
    from sqlalchemy import delete

    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        batch = await db.get(PerfProfileFleetBatch, batch_id)
        if batch is None or batch.tenant_id != tenant_id or batch.status not in _TERMINAL:
            return False
        await db.execute(
            delete(PerfProfileFleetItem).where(PerfProfileFleetItem.batch_id == batch_id)
        )
        await db.delete(batch)
        await db.commit()
        return True


async def retryable_workloads(batch_id: str, tenant_id: str) -> list[dict[str, Any]] | None:
    """Return failed/partial/cancelled workload descriptors, or None when batch is unknown."""
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        batch = await db.get(PerfProfileFleetBatch, batch_id)
        if batch is None or batch.tenant_id != tenant_id:
            return None
        items = (
            await db.execute(
                select(PerfProfileFleetItem).where(
                    PerfProfileFleetItem.batch_id == batch_id,
                    PerfProfileFleetItem.status.in_(("failed", "partial", "cancelled")),
                )
            )
        ).scalars().all()
        return [
            {
                "id": item.workload_id,
                "name": item.workload_name,
                "connection_id": item.connection_id or "",
            }
            for item in items
        ]


async def recover_interrupted() -> int:
    """Requeue interrupted items and preserve already-terminal work after a restart."""
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        running_ids = list(
            (
                await db.execute(
                    select(PerfProfileFleetItem.id).where(
                        PerfProfileFleetItem.status == "running"
                    )
                )
            ).scalars().all()
        )
        if running_ids:
            await db.execute(
                update(PerfProfileFleetItem)
                .where(PerfProfileFleetItem.id.in_(running_ids))
                .values(
                    status="queued",
                    started_at=None,
                    error="Requeued after server restart.",
                )
            )
        batch_ids = list(
            (
                await db.execute(
                    select(PerfProfileFleetBatch.id).where(
                        PerfProfileFleetBatch.status.in_(("queued", "running"))
                    )
                )
            ).scalars().all()
        )
        for batch_id in batch_ids:
            batch = await db.get(PerfProfileFleetBatch, batch_id)
            if batch and batch.cancel_requested:
                await db.execute(
                    update(PerfProfileFleetItem)
                    .where(
                        PerfProfileFleetItem.batch_id == batch_id,
                        PerfProfileFleetItem.status == "queued",
                    )
                    .values(status="cancelled", ended_at=_now(), error="Cancelled before restart.")
                )
            await _refresh_batch(db, batch_id)
        await db.commit()
        return len(running_ids)


class FleetWorker:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._wake: asyncio.Event | None = None
        self._claim_lock: asyncio.Lock | None = None
        self._start_lock: asyncio.Lock | None = None
        self._last_start = 0.0

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    async def start(self) -> None:
        if self.running:
            return
        from app.core.app_settings import load_settings

        await recover_interrupted()
        self._wake = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        concurrency = max(
            1, int(load_settings().get("perfprofile_fleet_concurrency", 1) or 1)
        )
        self._tasks = [
            asyncio.create_task(self._loop(index), name=f"perf-fleet-{index}")
            for index in range(concurrency)
        ]
        self.wake()

    async def ensure_running(self) -> None:
        """Start/restart the worker if its task exited, then signal newly queued work."""
        if not self.running:
            await self.start()
        self.wake()

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Any item interrupted by graceful shutdown is durable queued work for next startup.
        with contextlib.suppress(Exception):
            await recover_interrupted()
        self._wake = None
        self._claim_lock = None
        self._start_lock = None

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _loop(self, _index: int) -> None:
        while True:
            try:
                item_id = await self._claim_next()
                if item_id:
                    await self._delay_start()
                    await self._run_item(item_id)
                    continue
                assert self._wake is not None
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one transient DB error must not kill Fleet
                log.exception("Performance Fleet worker loop failed; retrying")
                await asyncio.sleep(1.0)

    async def _delay_start(self) -> None:
        from app.core.app_settings import load_settings

        delay = max(
            0.0,
            float(load_settings().get("perfprofile_fleet_start_delay_ms", 1000) or 0)
            / 1000.0,
        )
        assert self._start_lock is not None
        async with self._start_lock:
            wait = self._last_start + delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_start = time.monotonic()

    async def _claim_next(self) -> str | None:
        from app.core.db import SessionLocal

        assert self._claim_lock is not None
        async with self._claim_lock:
            async with SessionLocal() as db:
                item = (
                    await db.execute(
                        select(PerfProfileFleetItem)
                        .join(
                            PerfProfileFleetBatch,
                            PerfProfileFleetBatch.id == PerfProfileFleetItem.batch_id,
                        )
                        .where(
                            PerfProfileFleetItem.status == "queued",
                            PerfProfileFleetBatch.status.in_(("queued", "running")),
                            PerfProfileFleetBatch.cancel_requested.is_(False),
                        )
                        .order_by(
                            PerfProfileFleetBatch.created_at.asc(),
                            PerfProfileFleetItem.workload_name.asc(),
                        )
                        .with_for_update(skip_locked=True, of=PerfProfileFleetItem)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if item is None:
                    return None
                batch = await db.get(PerfProfileFleetBatch, item.batch_id)
                item.status = "running"
                item.started_at = _now()
                item.error = None
                if batch is not None:
                    batch.status = "running"
                    batch.started_at = batch.started_at or _now()
                await db.commit()
                return item.id

    async def _run_item(self, item_id: str) -> None:
        from app.core.app_settings import load_settings
        from app.core.azure_connections import connection_for_scope
        from app.core.db import SessionLocal
        from app.perfprofile import runs
        from app.perfprofile.service import execute_profile
        from app.workloads.registry import get_workload

        async with SessionLocal() as db:
            item = await db.get(PerfProfileFleetItem, item_id)
            if item is None:
                return
            batch = await db.get(PerfProfileFleetBatch, item.batch_id)
            if batch is None:
                return
            item_data = {
                "id": item.id,
                "batch_id": item.batch_id,
                "tenant_id": item.tenant_id,
                "workload_id": item.workload_id,
                "connection_id": item.connection_id or "",
                "started_at": item.started_at,
            }
            batch_data = {
                "window": batch.window,
                "start_time": batch.start_time,
                "end_time": batch.end_time,
                "actor": batch.triggered_by,
            }

        trigger = f"fleet:{item_data['batch_id']}:{item_data['id']}"
        stored = runs.find_run_by_trigger(
            item_data["tenant_id"], "workload", item_data["workload_id"], trigger
        )
        try:
            if stored is None:
                workload = get_workload(item_data["workload_id"])
                if workload is None:
                    raise RuntimeError("Workload no longer exists.")
                connection = connection_for_scope(
                    "workload",
                    connection_id=item_data["connection_id"] or None,
                    workload=workload,
                )
                settings = load_settings()
                progress_count = 0

                async def progress(_name: str, _rtype: str) -> None:
                    nonlocal progress_count
                    progress_count += 1
                    # Persist every fifth resource (and the first) to bound DB writes while still
                    # giving the polling UI useful movement on large profiles.
                    if progress_count != 1 and progress_count % 5:
                        return
                    async with SessionLocal() as progress_db:
                        progress_item = await progress_db.get(PerfProfileFleetItem, item_id)
                        if progress_item is not None and progress_item.status == "running":
                            progress_item.resources_completed = progress_count
                            await progress_db.commit()

                stored = await execute_profile(
                    tenant_id=item_data["tenant_id"],
                    actor=batch_data["actor"],
                    scope_kind="workload",
                    scope_id=item_data["workload_id"],
                    connection=connection,
                    workload=workload,
                    window=batch_data["window"],
                    interval=str(settings.get("perfprofile_interval", "PT15M") or "PT15M"),
                    scan_cap=int(settings.get("perfprofile_scan_cap", 200) or 200),
                    start_time=batch_data["start_time"],
                    end_time=batch_data["end_time"],
                    progress=progress,
                    trigger=trigger,
                )
            await self._finish_item(item_id, stored)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Profiler fleet item failed: %s", item_id)
            await self._fail_item(item_id, str(exc)[:1000])

    async def _finish_item(self, item_id: str, stored: dict[str, Any]) -> None:
        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            item = await db.get(PerfProfileFleetItem, item_id)
            if item is None:
                return
            status = str(stored.get("status") or "succeeded")
            if status not in {"succeeded", "partial", "failed"}:
                status = "failed"
            collection = stored.get("collection") or {}
            item.status = status
            item.run_id = stored.get("id") or None
            item.collection_json = collection
            item.resources_completed = int(collection.get("resources_completed") or 0)
            item.resources_total = int(collection.get("resources_selected") or 0)
            item.error = str(stored.get("error") or stored.get("warning") or "") or None
            item.ended_at = _now()
            if item.started_at:
                start = item.started_at
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                item.duration_ms = max(0, int((_now() - start).total_seconds() * 1000))
            await _refresh_batch(db, item.batch_id)
            await db.commit()
        self.wake()

    async def _fail_item(self, item_id: str, message: str) -> None:
        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            item = await db.get(PerfProfileFleetItem, item_id)
            if item is None:
                return
            item.status = "failed"
            item.error = message
            item.ended_at = _now()
            await _refresh_batch(db, item.batch_id)
            await db.commit()
        self.wake()


worker = FleetWorker()

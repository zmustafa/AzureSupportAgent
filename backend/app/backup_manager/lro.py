"""Long-running-operation poller for Backup Manager changes.

Azure Backup control-plane writes are asynchronous: ARM answers ``202 Accepted`` and the work
(enrol a VM, create a policy, run a test failover) completes minutes later.  Blocking an HTTP
request or an apply worker on that is unacceptable, so an applied change is parked in
``applying`` with its tracking URL and this dedicated background task drives it to a terminal
state.

Deliberately standalone rather than a job on the automations scheduler: the ledger must
converge even when scheduling is paused, and its cadence (seconds) is unrelated to the
scheduler's (minutes).  It follows the same start/stop lifecycle contract as
``app.monitor.sampler`` so ``main`` treats all background workers identically.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from app.backup_manager import changes as change_ops
from app.backup_manager import service

log = logging.getLogger("app.backup_manager.lro")

# How often the poller wakes. Individual rows carry their own `poll_after`, honouring any
# Retry-After Azure supplied, so this only bounds the shortest possible turnaround.
TICK_SECONDS = 10.0
# Rows advanced per tick. Bounded so a large backlog can never monopolise the connection pool.
BATCH_SIZE = 40
# Concurrent ARM polls.
CONCURRENCY = 6


class OperationPoller:
    """Singleton background worker; safe to start/stop repeatedly."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.ticks = 0
        self.last_error = ""

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="backup-manager-lro-poller")
        log.info("Backup Manager LRO poller started (tick=%ss).", TICK_SECONDS)

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
            pass
        log.info("Backup Manager LRO poller stopped.")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- loop -----------------------------------------------------------------
    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the worker
                self.last_error = service.safe_error(str(exc))
                log.warning("Backup Manager LRO poll tick failed: %s", self.last_error, exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> int:
        """Advance every due ``applying`` change once. Returns how many rows were polled."""
        from app.core.db import background_session
        from app.models import BackupManagerChange

        self.ticks += 1
        now = service.now()
        async with background_session() as db:
            rows = list(
                (
                    await db.execute(
                        select(BackupManagerChange)
                        .where(BackupManagerChange.status == "applying")
                        .order_by(BackupManagerChange.poll_after.is_(None).desc(), BackupManagerChange.poll_after)
                        .limit(BATCH_SIZE)
                    )
                ).scalars()
            )
            due = [
                row for row in rows
                if row.poll_after is None or _aware(row.poll_after) <= now
            ]
            if not due:
                return 0

            timed_out = [
                row for row in due
                if (row.poll_deadline is not None and _aware(row.poll_deadline) <= now)
                or int(row.poll_attempts or 0) >= change_ops.MAX_POLL_ATTEMPTS
            ]
            for row in timed_out:
                change_ops.mark_timed_out(row)
            pollable = [row for row in due if row not in timed_out]

            await service.bounded_gather(
                [lambda r=row: self._poll_one(r) for row in pollable], limit=CONCURRENCY,
            )
            await db.commit()
            return len(due)

    async def _poll_one(self, row: Any) -> None:
        """Poll one row's tracking URL and record the outcome on the ORM instance."""
        from app.core.azure_connections import resolve_connection

        url = str(row.operation_url or "")
        if not url:
            change_ops.mark_polled(row, "succeeded", {}, "", 0.0)
            return
        connection = resolve_connection(row.connection_id)
        if not connection:
            row.status = "failed"
            row.error_code = "ConnectionMissing"
            row.error_message = "The Azure connection for this change no longer exists."
            row.poll_after = None
            return
        try:
            token = await service.token_for(connection)
        except (ValueError, KeyError) as exc:  # noqa: BLE001 - retry on the next tick
            change_ops.mark_polled(row, "running", {}, service.safe_error(str(exc)), 30.0)
            return
        state, body, error, retry_after = await service.arm_poll(token, url)
        change_ops.mark_polled(row, state, body, error, retry_after)
        if state in ("succeeded", "failed"):
            # A terminal Azure operation invalidates every cached view of this connection.
            from app.backup_manager import cache as inventory_cache

            await inventory_cache.invalidate(tenant_id=row.tenant_id, connection_id=row.connection_id)


def _aware(value: Any):
    """SQLite round-trips naive datetimes; treat those as UTC rather than crashing."""
    from datetime import timezone

    return value if getattr(value, "tzinfo", None) else value.replace(tzinfo=timezone.utc)


poller = OperationPoller()

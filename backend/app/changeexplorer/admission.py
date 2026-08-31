"""Cross-replica admission control for expensive Change Explorer analyses.

PostgreSQL session advisory locks provide one active run per tenant/principal lane and two
active Change Explorer runs globally across all Container App replicas. Local SQLite uses the
same policy with process-local asyncio primitives. No durable counter can leak when a process
crashes: PostgreSQL releases session locks with the connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, text, update

from app.azure.arg_throttle import principal_key
from app.core.structured_events import emit, opaque_id

ANALYSIS_CONCURRENCY = 2
_POLL_MIN_SECONDS = 0.05
_POLL_MAX_SECONDS = 0.15
_CLAIM_LOCK_KEY = 7_143_290_119_999
_LEASE_SECONDS = 90
_HEARTBEAT_SECONDS = 25

_local_loop: asyncio.AbstractEventLoop | None = None
_local_slots: asyncio.Semaphore | None = None
_local_lanes: dict[str, asyncio.Lock] = {}


def _local_state() -> tuple[asyncio.Semaphore, dict[str, asyncio.Lock]]:
    global _local_loop, _local_slots, _local_lanes
    loop = asyncio.get_running_loop()
    if _local_loop is not loop or _local_slots is None:
        _local_loop = loop
        _local_slots = asyncio.Semaphore(ANALYSIS_CONCURRENCY)
        _local_lanes = {}
    return _local_slots, _local_lanes


@asynccontextmanager
async def _local_admission(lane: str) -> AsyncIterator[float]:
    slots, lanes = _local_state()
    lane_lock = lanes.setdefault(lane, asyncio.Lock())
    started = time.monotonic()
    async with lane_lock:
        async with slots:
            yield time.monotonic() - started


async def _postgres_acquire(lane: str) -> tuple[str, str, float]:
    from app.core.db import SessionLocal
    from app.models import ChangeExplorerAnalysisLease

    started = time.monotonic()
    lane_hash = hashlib.sha256(lane.encode("utf-8")).hexdigest()
    owner = uuid.uuid4().hex
    while True:
        lease_id = ""
        async with SessionLocal.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CLAIM_LOCK_KEY})
            now = datetime.now(timezone.utc)
            await session.execute(delete(ChangeExplorerAnalysisLease).where(
                ChangeExplorerAnalysisLease.expires_at <= now
            ))
            lane_busy = (await session.execute(select(ChangeExplorerAnalysisLease.id).where(
                ChangeExplorerAnalysisLease.lane_hash == lane_hash
            ))).scalar_one_or_none()
            running = int((await session.execute(
                select(func.count(ChangeExplorerAnalysisLease.id))
            )).scalar_one())
            if lane_busy is None and running < ANALYSIS_CONCURRENCY:
                lease_id = uuid.uuid4().hex
                session.add(ChangeExplorerAnalysisLease(
                    id=lease_id, lane_hash=lane_hash, owner=owner,
                    expires_at=now + timedelta(seconds=_LEASE_SECONDS), created_at=now,
                ))
        if lease_id:
            return lease_id, owner, time.monotonic() - started
        await asyncio.sleep(random.uniform(_POLL_MIN_SECONDS, _POLL_MAX_SECONDS))


async def _heartbeat(lease_id: str, owner: str) -> None:
    from app.core.db import SessionLocal
    from app.models import ChangeExplorerAnalysisLease

    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        async with SessionLocal.begin() as session:
            result = await session.execute(
                update(ChangeExplorerAnalysisLease)
                .where(ChangeExplorerAnalysisLease.id == lease_id,
                       ChangeExplorerAnalysisLease.owner == owner)
                .values(expires_at=datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS))
            )
            if getattr(result, "rowcount", 0) != 1:
                return


async def _postgres_release(lease_id: str, owner: str) -> None:
    from app.core.db import SessionLocal
    from app.models import ChangeExplorerAnalysisLease

    async with SessionLocal.begin() as session:
        await session.execute(delete(ChangeExplorerAnalysisLease).where(
            ChangeExplorerAnalysisLease.id == lease_id,
            ChangeExplorerAnalysisLease.owner == owner,
        ))


@asynccontextmanager
async def analysis_slot(tenant_id: str, connection: dict[str, Any] | None) -> AsyncIterator[float]:
    """Admit one analysis under the shared global and tenant/principal lane limits."""
    lane = f"{tenant_id or 'default'}|{principal_key(connection)}"
    lane_hash = opaque_id(lane)
    emit("analysis_admission", feature="changeexplorer", action="queued", lane=lane_hash)
    try:
        from app.core.db import engine

        postgres = engine.dialect.name == "postgresql"
    except Exception:  # noqa: BLE001 - local admission is safer than no admission
        postgres = False
    if not postgres:
        manager = _local_admission(lane)
        async with manager as waited:
            emit("analysis_admission", feature="changeexplorer", action="acquired",
                 lane=lane_hash, wait_seconds=round(waited, 3))
            try:
                yield waited
            finally:
                emit("analysis_admission", feature="changeexplorer", action="released", lane=lane_hash)
        return

    try:
        lease_id, owner, waited = await _postgres_acquire(lane)
    except Exception as exc:  # noqa: BLE001 - retain bounded local admission if SQL is unavailable
        emit("analysis_admission", feature="changeexplorer", action="distributed_unavailable",
             lane=lane_hash, error=type(exc).__name__)
        async with _local_admission(lane) as waited:
            emit("analysis_admission", feature="changeexplorer", action="acquired",
                 lane=lane_hash, wait_seconds=round(waited, 3), distributed=False)
            try:
                yield waited
            finally:
                emit("analysis_admission", feature="changeexplorer", action="released",
                     lane=lane_hash, distributed=False)
        return
    heartbeat = asyncio.create_task(_heartbeat(lease_id, owner), name=f"changeexplorer-lease-{lease_id}")
    try:
        emit("analysis_admission", feature="changeexplorer", action="acquired",
             lane=lane_hash, wait_seconds=round(waited, 3))
        yield waited
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat
        with contextlib.suppress(Exception):
            await _postgres_release(lease_id, owner)
        emit("analysis_admission", feature="changeexplorer", action="released", lane=lane_hash)


def reset_for_tests() -> None:
    global _local_loop, _local_slots, _local_lanes
    _local_loop = None
    _local_slots = None
    _local_lanes = {}
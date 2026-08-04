"""Aggregated recording of allowlist block events.

Writing one database row per refused request is not viable: a single internet scanner produces
tens of thousands of hits in minutes. Instead, hits are counted in memory per source IP and
flushed as ONE upserted row per IP per window.

The in-memory map is capped, because a distributed scan would otherwise let a remote party grow
process memory without bound — the exact class of caller this feature exists to keep out.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import IpBlockEvent

#: Buffer at most this many distinct source IPs between flushes.
MAX_TRACKED_IPS = 2000
#: Seconds between flushes to the database.
FLUSH_INTERVAL_S = 60.0


@dataclass
class _Pending:
    hits: int = 0
    first_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_path: str = ""


# (ip, mode) -> pending counters
_PENDING: dict[tuple[str, str], _Pending] = {}
_LAST_FLUSH = 0.0
_LOCK = asyncio.Lock()
_DROPPED = 0


def record(ip: str, mode: str, path: str) -> None:
    """Count one refused (or would-be refused) request. Cheap and synchronous."""
    global _DROPPED
    key = (ip, mode)
    entry = _PENDING.get(key)
    if entry is None:
        if len(_PENDING) >= MAX_TRACKED_IPS:
            # Cap reached: stop tracking NEW addresses until the next flush rather than growing
            # without bound. Existing counters keep incrementing, so the heaviest sources —
            # the ones actually worth showing the operator — are still measured accurately.
            _DROPPED += 1
            return
        entry = _Pending()
        _PENDING[key] = entry
    entry.hits += 1
    entry.last_seen = datetime.now(UTC)
    entry.last_path = path[:512]


def due_for_flush(now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    return bool(_PENDING) and (now - _LAST_FLUSH) >= FLUSH_INTERVAL_S


async def flush(db) -> int:
    """Upsert the buffered counters. Returns the number of source IPs written."""
    global _LAST_FLUSH, _DROPPED
    async with _LOCK:
        if not _PENDING:
            return 0
        batch, _PENDING_LOCAL = dict(_PENDING), None  # noqa: F841 - clarity over the swap below
        _PENDING.clear()
        _LAST_FLUSH = time.monotonic()
        _DROPPED = 0

    written = 0
    for (ip, mode), pending in batch.items():
        existing = (
            await db.execute(
                select(IpBlockEvent).where(IpBlockEvent.ip == ip, IpBlockEvent.mode == mode)
            )
        ).scalars().first()
        if existing is None:
            db.add(
                IpBlockEvent(
                    ip=ip,
                    mode=mode,
                    hits=pending.hits,
                    last_path=pending.last_path,
                    first_seen=pending.first_seen,
                    last_seen=pending.last_seen,
                )
            )
        else:
            existing.hits += pending.hits
            existing.last_path = pending.last_path
            existing.last_seen = pending.last_seen
        written += 1
    await db.commit()
    return written


def pending_snapshot() -> dict[tuple[str, str], _Pending]:
    """Un-flushed counters, so the UI can show activity from the last few seconds."""
    return dict(_PENDING)


#: Block records older than this are pruned. The in-memory buffer is capped, but the TABLE is
#: not: one row per distinct source IP means a sustained distributed scan grows it without
#: bound. Aggregated counters are an operational signal with a short shelf life, not an audit
#: record — the audit log keeps the durable trail of configuration changes.
RETENTION_DAYS = 30
#: Hard ceiling regardless of age, so a burst inside the retention window still cannot grow the
#: table indefinitely. Keeps the newest rows, which are the ones an operator acts on.
MAX_ROWS = 5000


async def purge(db) -> int:
    """Drop block records that are older than the retention window or beyond the row cap."""
    from datetime import timedelta

    from sqlalchemy import delete, select

    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    result = await db.execute(delete(IpBlockEvent).where(IpBlockEvent.last_seen < cutoff))
    removed = result.rowcount or 0

    # Age alone is not enough: trim to the newest MAX_ROWS by last activity.
    keep = (
        await db.execute(
            select(IpBlockEvent.id).order_by(IpBlockEvent.last_seen.desc()).limit(MAX_ROWS)
        )
    ).scalars().all()
    if len(keep) >= MAX_ROWS:
        result = await db.execute(delete(IpBlockEvent).where(IpBlockEvent.id.not_in(keep)))
        removed += result.rowcount or 0

    await db.commit()
    return removed


def reset() -> None:
    """Clear all buffered state (tests)."""
    global _LAST_FLUSH, _DROPPED
    _PENDING.clear()
    _LAST_FLUSH = 0.0
    _DROPPED = 0

"""Server-side admission control for Azure Resource Graph (ARG) queries.

ARG enforces a quota of roughly **15 queries per 5-second window per security principal**,
shared tenant-wide across every caller using that identity. Exceeding it returns
``429 RateLimiting``. Because the quota is per *principal* — not per request, per workload or
per browser tab — client-side pacing can never be authoritative: a second tab, the automation
scheduler, Mission Control and a fleet launch all draw from the same budget without seeing
each other.

This module is the one place that does see them all. Every ARG query in the app passes through
:func:`acquire`. PostgreSQL deployments coordinate query starts with per-principal advisory
locks across replicas; SQLite/local runs retain the process-local sliding window.

Two mechanisms combine:

- **Distributed pacing** — PostgreSQL replicas serialize and evenly space starts for each
    principal. The local fallback uses a per-principal sliding window.
- **Quota-header feedback** — ARG reports ``x-ms-user-quota-remaining`` and
  ``x-ms-user-quota-resets-after`` on every response. When remaining hits zero (or a genuine
  429 arrives) the bucket hard-blocks until the reported reset, so we back off using Azure's
  own accounting rather than guessing.

The limiter paces query *starts* only: the lock is held across the admission decision, never
across the HTTP request itself, so in-flight queries still overlap.

Principal scoping is deliberately **coarse**. When identity fields are missing we fall back to
a shared bucket rather than minting a private one — sharing over-paces (safe), splitting
under-paces (the failure we are fixing).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterator

from app.core.structured_events import emit, opaque_id

log = logging.getLogger("app.azure.arg_throttle")

# ARG's documented allowance is 15 queries / 5 s per principal. Default to 12 so other tooling
# in the tenant (the Portal, a colleague's script, Terraform) retains headroom.
DEFAULT_MAX_QUERIES = 12
DEFAULT_WINDOW_SECONDS = 5.0

# Never sleep longer than this in one step, so a bogus header can't wedge a scan forever.
_MAX_SLEEP = 60.0

_principal_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "arg_principal", default="default"
)


def principal_key(connection: dict[str, Any] | None) -> str:
    """Bucket key for the security principal a connection authenticates as.

    Connections that resolve to the same Azure identity MUST share a bucket, because Azure
    meters them together. When identity fields are absent we deliberately collapse to a shared
    key: over-pacing costs a little latency, under-pacing costs a 429.
    """
    if not isinstance(connection, dict):
        return "default"
    tenant = str(connection.get("tenant_id") or "").strip().lower()
    method = str(connection.get("auth_method") or "").strip().lower()
    client = str(connection.get("client_id") or "").strip().lower()
    if not (tenant or method or client):
        return "default"
    return f"{tenant}|{method}|{client}"


def _limits() -> tuple[bool, int, float]:
    """(enabled, max_queries, window_seconds) from admin settings, clamped to sane bounds."""
    try:
        from app.core.app_settings import load_settings

        s = load_settings()
        enabled = bool(s.get("arg_rate_limit_enabled", True))
        max_q = int(s.get("arg_max_queries_per_window", DEFAULT_MAX_QUERIES))
        window = float(s.get("arg_rate_window_seconds", DEFAULT_WINDOW_SECONDS))
    except Exception:  # noqa: BLE001 - settings unavailable (tests, early boot): use defaults
        return True, DEFAULT_MAX_QUERIES, DEFAULT_WINDOW_SECONDS
    return enabled, max(1, min(100, max_q)), max(0.5, min(60.0, window))


class _Bucket:
    """Sliding-window limiter for one security principal."""

    __slots__ = ("_starts", "_lock", "_blocked_until", "waits", "wait_seconds", "throttled")

    def __init__(self) -> None:
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        # Observability — surfaced by `stats()` and asserted by the throttle tests.
        self.waits = 0
        self.wait_seconds = 0.0
        self.throttled = 0

    async def acquire(self, max_queries: int, window_s: float) -> float:
        """Reserve one query slot, sleeping as needed. Returns seconds waited."""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()

                # A quota header or an observed 429 put us in a hard back-off.
                if now < self._blocked_until:
                    delay = min(self._blocked_until - now, _MAX_SLEEP)
                    await asyncio.sleep(delay)
                    waited += delay
                    continue

                cutoff = now - window_s
                while self._starts and self._starts[0] <= cutoff:
                    self._starts.popleft()

                if len(self._starts) >= max_queries:
                    # Window is full — wait for the oldest start to age out.
                    delay = self._starts[0] + window_s - now
                    if delay <= 0:
                        continue
                    await asyncio.sleep(min(delay, _MAX_SLEEP))
                    waited += delay
                    continue

                # Under contention, SMOOTH the remaining budget rather than releasing it in one
                # instant. Measured against live Azure: pacing 12 queries per 5s still drew 429s
                # when all 12 left at once, because ARG's sliding window doesn't align with ours
                # and the previous window hadn't fully aged out. Spacing them evenly removed it.
                # Below the halfway mark we stay out of the way, so a lone scan pays no latency
                # tax — the smoothing only engages once a burst is actually forming.
                if self._starts and len(self._starts) * 2 >= max_queries:
                    gap = (now - self._starts[-1])
                    min_gap = window_s / max_queries
                    if gap < min_gap:
                        await asyncio.sleep(min_gap - gap)
                        waited += min_gap - gap
                        continue

                self._starts.append(now)
                if waited:
                    self.waits += 1
                    self.wait_seconds += waited
                return waited

    def block_for(self, seconds: float) -> None:
        """Hard-block admission for ``seconds`` (quota exhausted / 429 observed)."""
        if seconds and seconds > 0:
            self._blocked_until = max(self._blocked_until, time.monotonic() + min(seconds, _MAX_SLEEP))


_buckets: dict[str, _Bucket] = {}


def _advisory_key(principal: str) -> int:
    """Stable signed bigint key for PostgreSQL's session advisory-lock namespace."""
    raw = hashlib.blake2b(f"arg-rate:{principal}".encode("utf-8"), digest_size=8).digest()
    unsigned = int.from_bytes(raw, "big")
    return unsigned if unsigned < 2**63 else unsigned - 2**64


def _state_key(principal: str) -> str:
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


async def _distributed_acquire(principal: str, max_queries: int, window_s: float) -> float | None:
    """Reserve a query start in PostgreSQL, or return ``None`` outside PostgreSQL.

    Each decision is a short transaction under a principal-specific transaction advisory lock.
    Waits happen only after that transaction releases its database connection. The row carries
    both recent starts and hard-block state, so quota-reset feedback is visible to every replica.
    """
    try:
        from sqlalchemy import select, text

        from app.core.db import SessionLocal, engine
        from app.models import DistributedRateLimit

        if engine.dialect.name != "postgresql":
            return None
    except Exception:  # noqa: BLE001 - retain local pacing during early boot
        return None

    lock_key = _advisory_key(principal)
    state_key = _state_key(principal)
    started = time.monotonic()
    while True:
        try:
            delay = 0.0
            async with SessionLocal.begin() as session:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
                row = (await session.execute(
                    select(DistributedRateLimit).where(DistributedRateLimit.key == state_key).with_for_update()
                )).scalar_one_or_none()
                now = time.time()
                if row is None:
                    row = DistributedRateLimit(key=state_key, starts_json=[], blocked_until_epoch=0.0)
                    session.add(row)
                starts = [float(value) for value in (row.starts_json or []) if float(value) > now - window_s]
                if float(row.blocked_until_epoch or 0.0) > now:
                    delay = float(row.blocked_until_epoch) - now
                elif len(starts) >= max_queries:
                    delay = starts[0] + window_s - now
                elif starts and len(starts) * 2 >= max_queries:
                    min_gap = window_s / max_queries
                    delay = max(0.0, starts[-1] + min_gap - now)
                if delay <= 0:
                    starts.append(now)
                    row.starts_json = starts
                    row.updated_at = datetime.now(timezone.utc)
            if delay <= 0:
                waited = time.monotonic() - started
                if waited >= 0.05:
                    emit("rate_limit_admission", source="ResourceGraph", principal=opaque_id(principal),
                         wait_seconds=round(waited, 3), distributed=True)
                return waited
            await asyncio.sleep(min(max(0.001, delay), _MAX_SLEEP))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to the existing safe local limiter
            log.warning("Distributed ARG pacing unavailable; using local limiter: %s", exc)
            return None


async def _distributed_block(principal: str, seconds: float) -> None:
    """Publish a quota-reset block to the PostgreSQL state row for all replicas."""
    if seconds <= 0:
        return
    try:
        from sqlalchemy import select, text

        from app.core.db import SessionLocal, engine
        from app.models import DistributedRateLimit

        if engine.dialect.name != "postgresql":
            return
        lock_key = _advisory_key(principal)
        state_key = _state_key(principal)
        async with SessionLocal.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            row = (await session.execute(
                select(DistributedRateLimit).where(DistributedRateLimit.key == state_key).with_for_update()
            )).scalar_one_or_none()
            if row is None:
                row = DistributedRateLimit(key=state_key, starts_json=[])
                session.add(row)
            row.blocked_until_epoch = max(
                float(row.blocked_until_epoch or 0.0), time.time() + min(seconds, _MAX_SLEEP)
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - local block still protects the observing replica
        log.warning("Could not publish distributed ARG backoff: %s", exc)


def _bucket(key: str) -> _Bucket:
    b = _buckets.get(key)
    if b is None:
        b = _Bucket()
        _buckets[key] = b
    return b


@contextlib.contextmanager
def use_principal(connection: dict[str, Any] | None) -> Iterator[str]:
    """Bind the ARG principal for everything executed inside the block.

    Callers that hold a connection (``command_runner``) set this once; the low-level ARM
    helpers then pace against the right bucket without having to thread the connection
    through every signature — including across the internal paging loop, where each page is
    a separate metered query.
    """
    key = principal_key(connection)
    token = _principal_var.set(key)
    try:
        yield key
    finally:
        _principal_var.reset(token)


async def acquire() -> float:
    """Gate one ARG query against the current principal's budget. Returns seconds waited."""
    enabled, max_q, window = _limits()
    if not enabled:
        return 0.0
    principal = _principal_var.get()
    distributed = await _distributed_acquire(principal, max_q, window)
    if distributed is not None:
        # Quota-header feedback remains process-local, but every replica now obeys the aggregate
        # base rate. A replica that actually observed exhaustion waits the longer server window.
        bucket = _bucket(principal)
        now = time.monotonic()
        if now < bucket._blocked_until:
            delay = min(bucket._blocked_until - now, _MAX_SLEEP)
            await asyncio.sleep(delay)
            distributed += delay
        return distributed
    return await _bucket(principal).acquire(max_q, window)


def parse_reset_after(value: str | None) -> float | None:
    """Parse ``x-ms-user-quota-resets-after`` (``hh:mm:ss[.fff]``) into seconds."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if ":" in text:
            parts = [float(p) for p in text.split(":")]
            if len(parts) != 3:
                return None
            hours, minutes, seconds = parts
            return max(0.0, hours * 3600 + minutes * 60 + seconds)
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return None


def note_quota_headers(headers: Any) -> None:
    """Feed ARG's own quota accounting back into the limiter (proactive pacing).

    When ARG says zero queries remain, block until it says the window resets — rather than
    firing the next query into a guaranteed 429.
    """
    if headers is None:
        return
    try:
        remaining_raw = headers.get("x-ms-user-quota-remaining")
        reset_raw = headers.get("x-ms-user-quota-resets-after")
    except (AttributeError, TypeError):
        return
    if remaining_raw is None:
        return
    try:
        remaining = int(str(remaining_raw).strip())
    except (TypeError, ValueError):
        return
    if remaining > 0:
        return
    reset_s = parse_reset_after(reset_raw)
    seconds = reset_s if reset_s is not None else DEFAULT_WINDOW_SECONDS
    _bucket(_principal_var.get()).block_for(seconds)


async def note_quota_headers_async(headers: Any) -> None:
    """Async variant that also shares an exhausted-quota block with every replica."""
    try:
        remaining_raw = headers.get("x-ms-user-quota-remaining") if headers is not None else None
        reset_raw = headers.get("x-ms-user-quota-resets-after") if headers is not None else None
    except (AttributeError, TypeError):
        remaining_raw = reset_raw = None
    note_quota_headers(headers)
    try:
        exhausted = remaining_raw is not None and int(str(remaining_raw).strip()) <= 0
    except (TypeError, ValueError):
        exhausted = False
    if exhausted:
        reset_s = parse_reset_after(reset_raw)
        seconds = reset_s if reset_s is not None else DEFAULT_WINDOW_SECONDS
        if reset_s is None:
            emit("quota_header_invalid", source="ResourceGraph",
                 principal=opaque_id(_principal_var.get()), header="x-ms-user-quota-resets-after")
        await _distributed_block(_principal_var.get(), seconds)


def note_throttled(retry_after_s: float | None = None) -> None:
    """Record an observed 429 and back the bucket off for the advertised interval."""
    b = _bucket(_principal_var.get())
    b.throttled += 1
    b.block_for(retry_after_s if retry_after_s is not None else DEFAULT_WINDOW_SECONDS)
    emit("throttle_observed", source="ResourceGraph", principal=opaque_id(_principal_var.get()),
         status=429, retry_after_seconds=retry_after_s)


async def note_throttled_async(retry_after_s: float | None = None) -> None:
    """Record a 429 locally and publish its hard backoff to every PostgreSQL replica."""
    note_throttled(retry_after_s)
    await _distributed_block(
        _principal_var.get(), retry_after_s if retry_after_s is not None else DEFAULT_WINDOW_SECONDS,
    )


def stats() -> dict[str, dict[str, float]]:
    """Per-principal counters (diagnostics + tests)."""
    return {
        key: {"waits": b.waits, "wait_seconds": round(b.wait_seconds, 3), "throttled": b.throttled}
        for key, b in _buckets.items()
    }


def reset() -> None:
    """Drop all buckets. Tests only — never call this from request handling."""
    _buckets.clear()

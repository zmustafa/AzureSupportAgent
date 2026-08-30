"""Shared PostgreSQL/SQLite coordination for background jobs and chat turns.

The database is the source of truth. Process-local conditions and task references only reduce
same-replica latency; every wait path polls SQL on a bounded interval so another replica can
start, update, cancel, finish, or recover a job safely.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import db as dbmod
from app.core.leases import LEASE_SECONDS, worker_id
from app.models import DurableJob, DurableJobEvent, DurableJobSlot

log = logging.getLogger("app.core.durable_jobs")

TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})
DEFAULT_POLL_SECONDS = 0.25
DEFAULT_RETENTION_SECONDS = 30 * 60
DEFAULT_EVENT_LIMIT = 1000
MAX_EVENT_BYTES = 512 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    normalized = as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def _bounded_json(value: Any, max_bytes: int) -> Any | None:
    """Return a detached JSON value, or ``None`` when it exceeds the storage bound."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > max_bytes:
        return None
    return json.loads(encoded)


def _event_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = _bounded_json(dict(value), MAX_EVENT_BYTES)
    if isinstance(payload, dict):
        return payload
    return {"message": "Event payload exceeded the durable replay limit."}


@dataclass(frozen=True)
class Claim:
    job: dict[str, Any]
    acquired: bool
    lease_token: str | None


@dataclass(frozen=True)
class JobOutcome:
    """Terminal result returned by a feature runner."""

    status: str = "done"
    result: Mapping[str, Any] | None = None
    error: str = ""


class DurableJobStore:
    """Atomic durable operations shared by the generic and chat registries."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        owner_id: str | None = None,
        lease_seconds: float = LEASE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.session_factory = session_factory
        self.owner_id = owner_id or worker_id("durable-job")
        self.lease_seconds = max(0.05, float(lease_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        return self.session_factory or dbmod.SessionLocal

    @staticmethod
    def _scope(
        tenant_id: str, feature: str, key: str
    ) -> tuple[str, str, str]:
        return (tenant_id or "default", feature, key)

    async def _ensure_slot(self, tenant_id: str, feature: str, key: str) -> None:
        tenant_id, feature, key = self._scope(tenant_id, feature, key)
        values = {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "feature": feature,
            "job_key": key,
            "updated_at": utcnow(),
        }
        async with self._sessions()() as db:
            dialect = db.bind.dialect.name if db.bind is not None else ""
            if dialect == "postgresql":
                statement = pg_insert(DurableJobSlot).values(**values).on_conflict_do_nothing(
                    index_elements=["tenant_id", "feature", "job_key"]
                )
            elif dialect == "sqlite":
                statement = sqlite_insert(DurableJobSlot).values(**values).on_conflict_do_nothing(
                    index_elements=["tenant_id", "feature", "job_key"]
                )
            else:  # pragma: no cover - supported deployments are PostgreSQL and SQLite
                raise RuntimeError(f"Unsupported durable-job database dialect: {dialect}")
            await db.execute(statement)
            await db.commit()

    async def claim(
        self,
        *,
        tenant_id: str,
        feature: str,
        key: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Claim:
        """Acquire a scope or attach to its current unexpired owner.

        An expired running row is recovered in place with a new fencing token, preserving its
        event history. A terminal row starts a new execution and remains available as history
        until retention cleanup removes it.
        """
        tenant_id, feature, key = self._scope(tenant_id, feature, key)
        await self._ensure_slot(tenant_id, feature, key)
        now = utcnow()
        token = str(uuid.uuid4())
        lease_until = now + timedelta(seconds=self.lease_seconds)
        async with self._sessions()() as db:
            claimed = (
                await db.execute(
                    update(DurableJobSlot)
                    .where(
                        DurableJobSlot.tenant_id == tenant_id,
                        DurableJobSlot.feature == feature,
                        DurableJobSlot.job_key == key,
                        or_(
                            DurableJobSlot.lease_expires_at.is_(None),
                            DurableJobSlot.lease_expires_at <= now,
                        ),
                    )
                    .values(
                        lease_owner=self.owner_id,
                        lease_token=token,
                        lease_expires_at=lease_until,
                        updated_at=now,
                    )
                    .returning(DurableJobSlot.current_job_id)
                )
            ).first()
            if claimed is None:
                await db.rollback()
                attached = await self.load_current(
                    tenant_id=tenant_id, feature=feature, key=key
                )
                if attached is None:
                    # A concurrent creator can be between slot creation and publishing its job.
                    # Bounded polling avoids turning that tiny window into a duplicate execution.
                    for _ in range(3):
                        await asyncio.sleep(self.poll_seconds)
                        attached = await self.load_current(
                            tenant_id=tenant_id, feature=feature, key=key
                        )
                        if attached is not None:
                            break
                if attached is None:
                    raise RuntimeError("Durable job scope was claimed without a current job.")
                return Claim(attached, False, None)

            current_id = claimed[0]
            current = await db.get(DurableJob, current_id) if current_id else None
            clean_metadata = _bounded_json(metadata or {}, MAX_METADATA_BYTES)
            if not isinstance(clean_metadata, dict):
                clean_metadata = {}
            if current is not None and current.status == "running":
                current.owner_id = self.owner_id
                current.lease_token = token
                current.lease_expires_at = lease_until
                current.lease_heartbeat_at = now
                current.cancel_requested = False
                current.attempt = int(current.attempt or 0) + 1
                current.metadata_json = clean_metadata or dict(current.metadata_json or {})
                current.updated_at = now
                job = current
            else:
                job = DurableJob(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    feature=feature,
                    job_key=key,
                    status="running",
                    owner_id=self.owner_id,
                    lease_token=token,
                    lease_expires_at=lease_until,
                    lease_heartbeat_at=now,
                    cancel_requested=False,
                    attempt=1,
                    next_event_seq=0,
                    metadata_json=clean_metadata,
                    started_at=now,
                    updated_at=now,
                )
                db.add(job)
                await db.flush()
                slot = (
                    await db.execute(
                        select(DurableJobSlot).where(
                            DurableJobSlot.tenant_id == tenant_id,
                            DurableJobSlot.feature == feature,
                            DurableJobSlot.job_key == key,
                        )
                    )
                ).scalar_one()
                slot.current_job_id = job.id
            await db.commit()
            return Claim(self._job_dict(job, []), True, token)

    @staticmethod
    def _job_dict(job: DurableJob, events: Sequence[DurableJobEvent]) -> dict[str, Any]:
        return {
            "id": job.id,
            "tenant_id": job.tenant_id,
            "feature": job.feature,
            "key": job.job_key,
            "status": job.status,
            "owner_id": job.owner_id,
            "lease_token": job.lease_token,
            "lease_expires_at": iso(job.lease_expires_at),
            "cancel_requested": bool(job.cancel_requested),
            "attempt": int(job.attempt or 0),
            "started_at": iso(job.started_at),
            "finished_at": iso(job.finished_at),
            "expires_at": iso(job.expires_at),
            "metadata": dict(job.metadata_json or {}),
            "result": job.result_json,
            "error": job.error or "",
            "events": [
                {
                    "seq": event.seq,
                    "event": event.event_type,
                    "data": dict(event.data_json or {}),
                    "created_at": iso(event.created_at),
                }
                for event in events
            ],
        }

    async def load_current(
        self, *, tenant_id: str, feature: str, key: str, include_events: bool = True
    ) -> dict[str, Any] | None:
        tenant_id, feature, key = self._scope(tenant_id, feature, key)
        async with self._sessions()() as db:
            job = (
                await db.execute(
                    select(DurableJob)
                    .join(DurableJobSlot, DurableJobSlot.current_job_id == DurableJob.id)
                    .where(
                        DurableJobSlot.tenant_id == tenant_id,
                        DurableJobSlot.feature == feature,
                        DurableJobSlot.job_key == key,
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            expires_at = as_utc(job.expires_at)
            if job.status in TERMINAL_STATUSES and expires_at is not None and expires_at <= utcnow():
                return None
            events: list[DurableJobEvent] = []
            if include_events:
                events = list(
                    (
                        await db.execute(
                            select(DurableJobEvent)
                            .where(DurableJobEvent.job_id == job.id)
                            .order_by(DurableJobEvent.seq)
                        )
                    ).scalars()
                )
            return self._job_dict(job, events)

    async def load_by_id(
        self, *, tenant_id: str, feature: str, job_id: str, include_events: bool = True
    ) -> dict[str, Any] | None:
        """Load a current job by its public execution id, scoped to its tenant and feature."""
        async with self._sessions()() as db:
            job = (
                await db.execute(
                    select(DurableJob)
                    .join(DurableJobSlot, DurableJobSlot.current_job_id == DurableJob.id)
                    .where(
                        DurableJob.id == job_id,
                        DurableJob.tenant_id == (tenant_id or "default"),
                        DurableJob.feature == feature,
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            expires_at = as_utc(job.expires_at)
            if job.status in TERMINAL_STATUSES and expires_at is not None and expires_at <= utcnow():
                return None
            events: list[DurableJobEvent] = []
            if include_events:
                events = list(
                    (
                        await db.execute(
                            select(DurableJobEvent)
                            .where(DurableJobEvent.job_id == job.id)
                            .order_by(DurableJobEvent.seq)
                        )
                    ).scalars()
                )
            return self._job_dict(job, events)

    async def events_after(
        self, job_id: str, after_seq: int, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        async with self._sessions()() as db:
            events = list(
                (
                    await db.execute(
                        select(DurableJobEvent)
                        .where(
                            DurableJobEvent.job_id == job_id,
                            DurableJobEvent.seq > after_seq,
                        )
                        .order_by(DurableJobEvent.seq)
                        .limit(limit)
                    )
                ).scalars()
            )
            return self._job_dict_events(events)

    @staticmethod
    def _job_dict_events(events: Iterable[DurableJobEvent]) -> list[dict[str, Any]]:
        return [
            {
                "seq": event.seq,
                "event": event.event_type,
                "data": dict(event.data_json or {}),
                "created_at": iso(event.created_at),
            }
            for event in events
        ]

    async def append_events(
        self,
        *,
        job_id: str,
        lease_token: str,
        events: Sequence[tuple[str, Mapping[str, Any]]],
        event_limit: int,
        metadata: Mapping[str, Any] | None = None,
        include_seq: bool = False,
    ) -> list[dict[str, Any]]:
        if not events:
            return []
        now = utcnow()
        clean_metadata = _bounded_json(metadata, MAX_METADATA_BYTES) if metadata is not None else None
        async with self._sessions()() as db:
            values: dict[str, Any] = {
                "next_event_seq": DurableJob.next_event_seq + len(events),
                "updated_at": now,
            }
            if isinstance(clean_metadata, dict):
                values["metadata_json"] = clean_metadata
            reserved = (
                await db.execute(
                    update(DurableJob)
                    .where(
                        DurableJob.id == job_id,
                        DurableJob.status == "running",
                        DurableJob.lease_token == lease_token,
                    )
                    .values(**values)
                    .returning(DurableJob.next_event_seq)
                )
            ).scalar_one_or_none()
            if reserved is None:
                await db.rollback()
                return []
            first_seq = int(reserved) - len(events)
            stored: list[dict[str, Any]] = []
            for offset, (event_type, raw_data) in enumerate(events):
                seq = first_seq + offset
                data = _event_payload(raw_data)
                if include_seq:
                    data["seq"] = seq
                row = DurableJobEvent(
                    id=str(uuid.uuid4()),
                    job_id=job_id,
                    seq=seq,
                    event_type=str(event_type)[:64],
                    data_json=data,
                    created_at=now,
                )
                db.add(row)
                stored.append(
                    {"seq": seq, "event": row.event_type, "data": data, "created_at": iso(now)}
                )
            keep = max(1, int(event_limit))
            stale_ids = (
                select(DurableJobEvent.id)
                .where(DurableJobEvent.job_id == job_id)
                .order_by(DurableJobEvent.seq.desc())
                .offset(keep)
            )
            await db.execute(delete(DurableJobEvent).where(DurableJobEvent.id.in_(stale_ids)))
            await db.commit()
            return stored

    async def heartbeat(self, *, job_id: str, lease_token: str) -> tuple[bool, bool]:
        now = utcnow()
        lease_until = now + timedelta(seconds=self.lease_seconds)
        async with self._sessions()() as db:
            job = (
                await db.execute(
                    update(DurableJob)
                    .where(
                        DurableJob.id == job_id,
                        DurableJob.status == "running",
                        DurableJob.lease_token == lease_token,
                    )
                    .values(
                        lease_expires_at=lease_until,
                        lease_heartbeat_at=now,
                        updated_at=now,
                    )
                    .returning(DurableJob.cancel_requested)
                )
            ).first()
            if job is None:
                await db.rollback()
                return False, False
            slot_updated = await db.execute(
                update(DurableJobSlot)
                .where(
                    DurableJobSlot.current_job_id == job_id,
                    DurableJobSlot.lease_token == lease_token,
                )
                .values(lease_expires_at=lease_until, updated_at=now)
            )
            if slot_updated.rowcount != 1:
                await db.rollback()
                return False, False
            await db.commit()
            return True, bool(job[0])

    async def lease_state(self, *, job_id: str, lease_token: str) -> tuple[bool, bool]:
        """Read ownership and cancellation without extending the lease."""
        async with self._sessions()() as db:
            row = (
                await db.execute(
                    select(
                        DurableJob.status,
                        DurableJob.lease_token,
                        DurableJob.cancel_requested,
                    ).where(DurableJob.id == job_id)
                )
            ).one_or_none()
            if row is None or row[0] != "running" or row[1] != lease_token:
                return False, False
            return True, bool(row[2])

    async def request_cancel(
        self, *, tenant_id: str, feature: str, key: str
    ) -> bool:
        tenant_id, feature, key = self._scope(tenant_id, feature, key)
        async with self._sessions()() as db:
            result = await db.execute(
                update(DurableJob)
                .where(
                    DurableJob.id
                    == select(DurableJobSlot.current_job_id)
                    .where(
                        DurableJobSlot.tenant_id == tenant_id,
                        DurableJobSlot.feature == feature,
                        DurableJobSlot.job_key == key,
                    )
                    .scalar_subquery(),
                    DurableJob.status == "running",
                )
                .values(cancel_requested=True, updated_at=utcnow())
            )
            await db.commit()
            return result.rowcount == 1

    async def interrupt_expired(
        self,
        *,
        tenant_id: str,
        feature: str,
        key: str,
        error: str,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ) -> bool:
        """Fence and fail expired work that cannot be resumed safely.

        This is deliberately separate from :meth:`claim`: resumable managers claim the same
        attempt with fresh executor input, while non-resumable polling managers call this on
        status reads rather than silently running a side-effecting operation twice.
        """
        tenant_id, feature, key = self._scope(tenant_id, feature, key)
        now = utcnow()
        async with self._sessions()() as db:
            current_id = (
                await db.execute(
                    select(DurableJobSlot.current_job_id).where(
                        DurableJobSlot.tenant_id == tenant_id,
                        DurableJobSlot.feature == feature,
                        DurableJobSlot.job_key == key,
                        DurableJobSlot.lease_expires_at.is_not(None),
                        DurableJobSlot.lease_expires_at <= now,
                    )
                )
            ).scalar_one_or_none()
            if current_id is None:
                return False
            interrupted = await db.execute(
                update(DurableJob)
                .where(
                    DurableJob.id == current_id,
                    DurableJob.status == "running",
                    DurableJob.lease_expires_at.is_not(None),
                    DurableJob.lease_expires_at <= now,
                )
                .values(
                    status="error",
                    owner_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    finished_at=now,
                    expires_at=now + timedelta(seconds=max(0.0, retention_seconds)),
                    error=(error or "Background work was interrupted.")[:1500],
                    updated_at=now,
                )
            )
            if interrupted.rowcount != 1:
                await db.rollback()
                return False
            released = await db.execute(
                update(DurableJobSlot)
                .where(
                    DurableJobSlot.current_job_id == current_id,
                    DurableJobSlot.lease_expires_at.is_not(None),
                    DurableJobSlot.lease_expires_at <= now,
                )
                .values(
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if released.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def finalize(
        self,
        *,
        job_id: str,
        lease_token: str,
        status: str,
        result: Mapping[str, Any] | None,
        error: str = "",
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal durable-job status: {status}")
        bounded_result = _bounded_json(result, MAX_RESULT_BYTES) if result is not None else None
        for attempt in range(5):
            now = utcnow()
            try:
                async with self._sessions()() as db:
                    finished = await db.execute(
                        update(DurableJob)
                        .where(
                            DurableJob.id == job_id,
                            DurableJob.status == "running",
                            DurableJob.lease_token == lease_token,
                        )
                        .values(
                            status=status,
                            result_json=bounded_result,
                            error=(error or "")[:1500] or None,
                            owner_id=None,
                            lease_expires_at=None,
                            finished_at=now,
                            expires_at=now
                            + timedelta(seconds=max(0.0, retention_seconds)),
                            updated_at=now,
                        )
                    )
                    if finished.rowcount != 1:
                        await db.rollback()
                        return False
                    released = await db.execute(
                        update(DurableJobSlot)
                        .where(
                            DurableJobSlot.current_job_id == job_id,
                            DurableJobSlot.lease_token == lease_token,
                        )
                        .values(
                            lease_owner=None,
                            lease_token=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                    if released.rowcount != 1:
                        await db.rollback()
                        return False
                    await db.commit()
                    return True
            except OperationalError:
                if attempt == 4:
                    raise
                # SQLite has one writer; PostgreSQL can also abort a transaction during
                # failover/deadlock handling. A terminal transition is idempotent under its
                # fencing token, so a short bounded retry is safe.
                await asyncio.sleep(0.05 * (2**attempt))
        return False

    async def list_current(
        self,
        *,
        tenant_id: str,
        feature: str,
        key_prefix: str = "",
        active_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        now = utcnow()
        async with self._sessions()() as db:
            statement = (
                select(DurableJob)
                .join(DurableJobSlot, DurableJobSlot.current_job_id == DurableJob.id)
                .where(
                    DurableJobSlot.tenant_id == (tenant_id or "default"),
                    DurableJobSlot.feature == feature,
                )
                .order_by(DurableJob.started_at.desc())
            )
            if key_prefix:
                statement = statement.where(DurableJobSlot.job_key.startswith(key_prefix))
            if active_only:
                statement = statement.where(
                    DurableJob.status == "running",
                    DurableJob.lease_expires_at.is_not(None),
                    DurableJob.lease_expires_at > now,
                )
            else:
                statement = statement.where(
                    or_(
                        DurableJob.status == "running",
                        DurableJob.expires_at.is_(None),
                        DurableJob.expires_at > now,
                    )
                )
            if limit is not None:
                statement = statement.limit(max(1, int(limit)))
            jobs = list((await db.execute(statement)).scalars())
            if not jobs:
                return []
            ids = [job.id for job in jobs]
            event_rows = list(
                (
                    await db.execute(
                        select(DurableJobEvent)
                        .where(DurableJobEvent.job_id.in_(ids))
                        .order_by(DurableJobEvent.job_id, DurableJobEvent.seq)
                    )
                ).scalars()
            )
            by_job: dict[str, list[DurableJobEvent]] = {job_id: [] for job_id in ids}
            for event in event_rows:
                by_job[event.job_id].append(event)
            return [self._job_dict(job, by_job[job.id]) for job in jobs]

    async def dismiss(self, *, tenant_id: str, feature: str, job_id: str) -> bool:
        """Delete one terminal current job and its replay events."""
        async with self._sessions()() as db:
            job = (
                await db.execute(
                    select(DurableJob)
                    .join(DurableJobSlot, DurableJobSlot.current_job_id == DurableJob.id)
                    .where(
                        DurableJob.id == job_id,
                        DurableJob.tenant_id == (tenant_id or "default"),
                        DurableJob.feature == feature,
                        DurableJob.status.in_(tuple(TERMINAL_STATUSES)),
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                return False
            await db.execute(
                update(DurableJobSlot)
                .where(DurableJobSlot.current_job_id == job.id)
                .values(current_job_id=None, updated_at=utcnow())
            )
            await db.delete(job)
            await db.commit()
            return True

    async def cleanup(self, *, feature: str | None = None, now: datetime | None = None) -> int:
        cutoff = now or utcnow()
        async with self._sessions()() as db:
            expired = select(DurableJob.id).where(
                DurableJob.status.in_(tuple(TERMINAL_STATUSES)),
                DurableJob.expires_at.is_not(None),
                DurableJob.expires_at <= cutoff,
            )
            if feature is not None:
                expired = expired.where(DurableJob.feature == feature)
            ids = list((await db.execute(expired)).scalars())
            if not ids:
                return 0
            await db.execute(
                update(DurableJobSlot)
                .where(DurableJobSlot.current_job_id.in_(ids))
                .values(current_job_id=None, updated_at=cutoff)
            )
            await db.execute(delete(DurableJobEvent).where(DurableJobEvent.job_id.in_(ids)))
            await db.execute(delete(DurableJob).where(DurableJob.id.in_(ids)))
            await db.commit()
            return len(ids)

    async def wait_for_terminal(self, job_id: str) -> dict[str, Any] | None:
        while True:
            async with self._sessions()() as db:
                job = await db.get(DurableJob, job_id)
                if job is None:
                    return None
                if job.status != "running":
                    return self._job_dict(job, [])
            await asyncio.sleep(self.poll_seconds)


class DurableJobContext:
    """Fenced mutation surface handed to one feature runner."""

    def __init__(
        self,
        *,
        store: DurableJobStore,
        job: Mapping[str, Any],
        lease_token: str,
        event_limit: int,
    ) -> None:
        self.store = store
        self.job_id = str(job["id"])
        self.key = str(job["key"])
        self.started_at = str(job.get("started_at") or "")
        self.attempt = int(job.get("attempt") or 1)
        self.lease_token = lease_token
        self.event_limit = event_limit
        self.metadata = dict(job.get("metadata") or {})
        self.cancel_requested = False

    async def emit(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
        include_seq: bool = False,
    ) -> dict[str, Any] | None:
        if metadata:
            self.metadata.update(metadata)
        stored = await self.store.append_events(
            job_id=self.job_id,
            lease_token=self.lease_token,
            events=[(event_type, data)],
            event_limit=self.event_limit,
            metadata=self.metadata,
            include_seq=include_seq,
        )
        return stored[0] if stored else None

    async def checkpoint(self) -> None:
        """Raise cancellation when this runner no longer owns the durable lease."""
        owned, cancel_requested = await self.store.lease_state(
            job_id=self.job_id, lease_token=self.lease_token
        )
        self.cancel_requested = cancel_requested
        if not owned or cancel_requested:
            raise asyncio.CancelledError()


FeatureRunner = Callable[[DurableJobContext], Awaitable[JobOutcome | Mapping[str, Any] | None]]


class DurableJobExecutor:
    """Shared detached-runner lifecycle used by feature-specific job adapters.

    The local task maps are only cancellation/latency aids. SQL remains the source of truth,
    and the heartbeat polls cancellation so another replica can control the owner.
    """

    def __init__(
        self,
        feature: str,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        owner_id: str | None = None,
        lease_seconds: float = LEASE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> None:
        self.feature = feature
        self.store = DurableJobStore(
            session_factory=session_factory,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
        )
        self.retention_seconds = retention_seconds
        self.event_limit = max(1, int(event_limit))
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._heartbeats: dict[str, asyncio.Task[None]] = {}

    async def start(
        self,
        *,
        tenant_id: str,
        key: str,
        metadata: Mapping[str, Any],
        runner: FeatureRunner,
    ) -> Claim:
        claim = await self.store.claim(
            tenant_id=tenant_id, feature=self.feature, key=key, metadata=metadata
        )
        if not claim.acquired or claim.lease_token is None:
            current = await self.store.load_current(
                tenant_id=tenant_id, feature=self.feature, key=key
            )
            return Claim(current or claim.job, False, None)

        context = DurableJobContext(
            store=self.store,
            job=claim.job,
            lease_token=claim.lease_token,
            event_limit=self.event_limit,
        )

        async def _heartbeat() -> None:
            interval = max(self.store.poll_seconds, self.store.lease_seconds / 3)
            loop = asyncio.get_running_loop()
            next_renewal = loop.time() + interval
            try:
                while True:
                    await asyncio.sleep(self.store.poll_seconds)
                    if loop.time() >= next_renewal:
                        owned, cancel_requested = await self.store.heartbeat(
                            job_id=context.job_id, lease_token=context.lease_token
                        )
                        next_renewal = loop.time() + interval
                    else:
                        owned, cancel_requested = await self.store.lease_state(
                            job_id=context.job_id, lease_token=context.lease_token
                        )
                    context.cancel_requested = cancel_requested
                    if not owned or cancel_requested:
                        task = self.tasks.get(key)
                        if task is not None and not task.done():
                            task.cancel()
                        return
            except asyncio.CancelledError:
                return

        async def _run() -> None:
            try:
                raw_outcome = await runner(context)
                if isinstance(raw_outcome, JobOutcome):
                    outcome = raw_outcome
                elif isinstance(raw_outcome, Mapping):
                    outcome = JobOutcome(result=raw_outcome)
                else:
                    outcome = JobOutcome()
                await self.store.finalize(
                    job_id=context.job_id,
                    lease_token=context.lease_token,
                    status=outcome.status,
                    result=outcome.result,
                    error=outcome.error,
                    retention_seconds=self.retention_seconds,
                )
            except asyncio.CancelledError:
                owned, cancel_requested = await asyncio.shield(
                    self.store.lease_state(
                        job_id=context.job_id, lease_token=context.lease_token
                    )
                )
                if owned:
                    await asyncio.shield(
                        self.store.finalize(
                            job_id=context.job_id,
                            lease_token=context.lease_token,
                            status="cancelled" if cancel_requested else "error",
                            result=None,
                            error=(
                                "Background work was cancelled."
                                if cancel_requested
                                else "Background work was interrupted."
                            ),
                            retention_seconds=self.retention_seconds,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - isolate detached feature work
                log.exception("%s durable job failed (key=%s)", self.feature, key)
                await self.store.finalize(
                    job_id=context.job_id,
                    lease_token=context.lease_token,
                    status="error",
                    result=None,
                    error=str(exc)[:1500],
                    retention_seconds=self.retention_seconds,
                )
            finally:
                heartbeat = self._heartbeats.pop(key, None)
                if heartbeat is not None:
                    heartbeat.cancel()

        task = asyncio.create_task(_run(), name=f"durable-{self.feature}-{key[:32]}")
        self.tasks[key] = task
        self._heartbeats[key] = asyncio.create_task(_heartbeat())

        def _forget(completed: asyncio.Task[None]) -> None:
            if self.tasks.get(key) is completed:
                self.tasks.pop(key, None)

        task.add_done_callback(_forget)
        # Give the detached runner one scheduling opportunity before returning. Besides making
        # progress immediately visible to polling clients, this avoids an attached replica
        # observing a claimed-but-not-yet-started execution as idle.
        await asyncio.sleep(0)
        return claim

    async def cancel(self, *, tenant_id: str, key: str) -> bool:
        requested = await self.store.request_cancel(
            tenant_id=tenant_id, feature=self.feature, key=key
        )
        task = self.tasks.get(key)
        if requested and task is not None and not task.done():
            task.cancel()
        return requested


class DurableJobJanitor:
    """Periodically remove terminal rows whose feature-specific replay window elapsed."""

    def __init__(self, *, interval_seconds: float = 60.0) -> None:
        self.interval_seconds = max(1.0, interval_seconds)
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        assert self._stop is not None
        self._stop.set()
        await self._task
        self._task = None
        self._stop = None

    async def _loop(self) -> None:
        assert self._stop is not None
        store = DurableJobStore(owner_id="durable-job-janitor")
        while not self._stop.is_set():
            try:
                removed = await store.cleanup()
                if removed:
                    log.info("Removed %d expired durable background job(s)", removed)
            except Exception:  # noqa: BLE001 - cleanup must not take down the application
                log.warning("Durable background-job cleanup failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue


janitor = DurableJobJanitor()
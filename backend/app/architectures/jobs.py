"""Background AI architecture-generation jobs.

The reverse-engineering pipeline (resolve scope → query Azure Resource Graph → ask the
LLM → save) can take a while, so the dashboard launches it as a background job instead of
blocking. Several jobs (e.g. one per workload) can run at once; each reports its phase and
percentage and can be cancelled mid-flight. Jobs live in memory only — the resulting
architecture is persisted to the registry, and bounded job telemetry is stored in the shared
durable-job tables so polling and cancellation work from any replica.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.durable_jobs import (
    DurableJobContext,
    DurableJobExecutor,
    JobOutcome,
    as_utc,
    utcnow,
)

logger = logging.getLogger("app.architectures.jobs")

# Cap concurrent reverse-engineering pipelines so launching "dozens at once" queues
# gracefully instead of hammering Azure Resource Graph + the LLM all at the same time.
_MAX_CONCURRENCY = 3
# Keep finished jobs around briefly so the UI can show their outcome, then prune them.
_RETAIN_SECONDS = 1800
_MAX_JOBS = 200

_TERMINAL = {"done", "error", "canceled"}


class _JobContext(Protocol):
    async def emit(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
        include_seq: bool = False,
    ) -> dict[str, Any] | None: ...

    async def checkpoint(self) -> None: ...


class _DirectContext:
    """Compatibility context for focused tests that invoke the runner directly."""

    def __init__(self, job: "_Job") -> None:
        self.job = job

    async def emit(
        self,
        _event_type: str,
        _data: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
        include_seq: bool = False,
    ) -> None:
        del metadata, include_seq

    async def checkpoint(self) -> None:
        if self.job.cancel_requested:
            raise asyncio.CancelledError()


def _iso(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class _Job:
    id: str
    tenant_id: str
    workload_id: str
    workload_name: str
    connection_id: str
    created_by: str
    status: str = "queued"  # queued | running | done | error | canceled
    phase: str = "queued"  # queued | scope | query | ai | save | done
    progress: int = 0  # 0..100
    message: str = "Queued…"
    architecture_id: str = ""
    architecture_name: str = ""
    resource_count: int = 0
    # When set, regenerate INTO this existing architecture (preserve id/name/category/
    # state) instead of creating a new one — powers "Rebuild from workload".
    target_architecture_id: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    task: asyncio.Task | None = field(default=None, repr=False)
    cancel_requested: bool = field(default=False, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workload_id": self.workload_id,
            "workload_name": self.workload_name,
            "status": self.status,
            "phase": self.phase,
            "progress": self.progress,
            "message": self.message,
            "architecture_id": self.architecture_id,
            "architecture_name": self.architecture_name,
            "resource_count": self.resource_count,
            "target_architecture_id": self.target_architecture_id,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
        }


class _Manager:
    """Durable registry + runner for background generation jobs."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        owner_id: str | None = None,
        lease_seconds: float = 60.0,
        poll_seconds: float = 0.25,
    ) -> None:
        self._executor = DurableJobExecutor(
            "architecture.generate",
            session_factory=session_factory,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            retention_seconds=_RETAIN_SECONDS,
            event_limit=100,
        )
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        # Created lazily so it binds to the running event loop.
        if self._sem is None:
            self._sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        return self._sem

    # ----------------------------------------------------------------- public API
    @staticmethod
    def _from_durable(durable: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(durable.get("metadata") or {})
        status = str(metadata.get("status") or "queued")
        if durable["status"] == "cancelled":
            status = "canceled"
        elif durable["status"] == "error":
            status = "error"
        elif durable["status"] == "done":
            status = "done"
        return {
            "id": metadata.get("id") or durable["key"],
            "workload_id": metadata.get("workload_id", ""),
            "workload_name": metadata.get("workload_name", ""),
            "status": status,
            "phase": metadata.get("phase", "queued"),
            "progress": int(metadata.get("progress") or 0),
            "message": metadata.get("message", "Queued…"),
            "architecture_id": metadata.get("architecture_id", ""),
            "architecture_name": metadata.get("architecture_name", ""),
            "resource_count": int(metadata.get("resource_count") or 0),
            "target_architecture_id": metadata.get("target_architecture_id", ""),
            "error": durable.get("error") or metadata.get("error", ""),
            "created_at": metadata.get("created_at") or durable["started_at"],
            "started_at": metadata.get("started_at", ""),
            "ended_at": metadata.get("ended_at") or durable.get("finished_at") or "",
        }

    async def _load(self, job_id: str, tenant_id: str) -> dict[str, Any] | None:
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id,
            feature=self._executor.feature,
            key=job_id,
        )
        if durable is None:
            return None
        lease_expires = durable.get("lease_expires_at")
        if durable["status"] == "running" and lease_expires:
            expires = datetime.fromisoformat(lease_expires)
            if as_utc(expires) <= utcnow():
                # Architecture generation has no safe checkpoint. Re-running automatically
                # could save a second architecture after the first owner already wrote one.
                await self._executor.store.interrupt_expired(
                    tenant_id=tenant_id,
                    feature=self._executor.feature,
                    key=job_id,
                    error="Architecture generation was interrupted before completion.",
                    retention_seconds=_RETAIN_SECONDS,
                )
                durable = await self._executor.store.load_current(
                    tenant_id=tenant_id,
                    feature=self._executor.feature,
                    key=job_id,
                )
                if durable is None:
                    return None
        return self._from_durable(durable)

    async def create(
        self,
        *,
        tenant_id: str,
        workload_id: str,
        workload_name: str,
        connection_id: str,
        created_by: str,
        target_architecture_id: str = "",
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        job = _Job(
            id=job_id,
            tenant_id=tenant_id,
            workload_id=workload_id,
            workload_name=workload_name,
            connection_id=connection_id,
            created_by=created_by,
            target_architecture_id=target_architecture_id,
        )
        claim = await self._executor.start(
            tenant_id=tenant_id,
            key=job_id,
            metadata=job.public(),
            runner=lambda context: self._run(job, context),
        )
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=self._executor.feature, key=claim.job["key"]
        )
        return self._from_durable(durable or claim.job)

    async def list(self, tenant_id: str) -> list[dict[str, Any]]:
        durable = await self._executor.store.list_current(
            tenant_id=tenant_id, feature=self._executor.feature, limit=_MAX_JOBS
        )
        result: list[dict[str, Any]] = []
        for item in durable:
            loaded = await self._load(str(item["key"]), tenant_id)
            if loaded is not None:
                result.append(loaded)
        return result

    async def get(self, job_id: str, tenant_id: str) -> dict[str, Any] | None:
        return await self._load(job_id, tenant_id)

    async def cancel(self, job_id: str, tenant_id: str) -> bool:
        return await self._executor.cancel(tenant_id=tenant_id, key=job_id)

    async def dismiss(self, job_id: str, tenant_id: str) -> bool:
        current = await self._load(job_id, tenant_id)
        if current is None or current["status"] not in _TERMINAL:
            return False
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=self._executor.feature, key=job_id,
            include_events=False,
        )
        if durable is None:
            return False
        return await self._executor.store.dismiss(
            tenant_id=tenant_id, feature=self._executor.feature, job_id=durable["id"]
        )

    # ----------------------------------------------------------------- internals
    async def _set(
        self, job: _Job, context: _JobContext, phase: str, progress: int, message: str
    ) -> None:
        job.phase = phase
        job.progress = progress
        job.message = message
        await context.emit(
            "progress",
            {"phase": phase, "progress": progress, "message": message},
            metadata=job.public(),
        )

    async def _fail(self, job: _Job, context: _JobContext, message: str) -> JobOutcome:
        job.status = "error"
        job.phase = "done"
        job.error = message
        job.message = message
        job.ended_at = time.time()
        await context.emit("progress", {"phase": "done", "progress": job.progress, "message": message}, metadata=job.public())
        return JobOutcome(status="error", error=message)

    async def _checkpoint(self, context: _JobContext) -> None:
        await context.checkpoint()

    async def _run(
        self, job: _Job, context: DurableJobContext | None = None
    ) -> JobOutcome:
        from app.architectures import registry as arch_registry
        from app.architectures.designer import generate_architecture
        from app.architectures.reverse import dump_resources
        from app.core.azure_connections import resolve_connection
        from app.workloads.registry import get_workload
        from app.azure.credentials import get_arm_token

        active_context: _JobContext = context or _DirectContext(job)
        try:
            async with self._semaphore():
                if job.cancel_requested:
                    return JobOutcome(status="cancelled", error="Canceled.")
                job.status = "running"
                job.started_at = time.time()
                await self._set(job, active_context, "scope", 10, f"Resolving scope for '{job.workload_name}'…")

                wl = get_workload(job.workload_id)
                if wl is None:
                    return await self._fail(job, active_context, "Workload not found.")
                conn = resolve_connection(job.connection_id or wl.get("connection_id") or None)

                # Pre-flight auth probe (mirrors the assessment runner). open_sp_session is a
                # NO-OP for pasted-token connections, so an expired/invalid token would otherwise
                # only surface deep in the Resource Graph phase as a lower-level error. Probe the
                # connection's ARM token here and fail fast with ONE clear, actionable message so
                # "Rebuild from workload" tells the user exactly what to fix. A None connection
                # (pure local ambient `az`) is left to the query path.
                if conn is not None:
                    _tok, _terr = await get_arm_token(conn)
                    if not _tok:
                        cname = conn.get("name") or "the selected connection"
                        return await self._fail(
                            job,
                            active_context,
                            f"Can't authenticate to Azure with {cname}: {_terr} "
                            "Refresh its token in Settings → Azure Tenants, then rebuild again.",
                        )

                await self._checkpoint(active_context)
                await self._set(job, active_context, "query", 35, "Querying Azure Resource Graph for resources + properties…")
                dump = await dump_resources(wl, conn)
                if dump.get("error"):
                    return await self._fail(job, active_context, str(dump["error"]))
                resources = dump.get("resources") or []
                resource_context = dump.get("context") or {}
                job.resource_count = int(resource_context.get("total_resource_count") or len(resources))
                if not resources:
                    return await self._fail(job, active_context, "No resources found in this workload's scope.")

                await self._checkpoint(active_context)
                represented = int(resource_context.get("represented_resource_count") or len(resources))
                mode = str(resource_context.get("mode") or "detailed")
                await self._set(job, active_context, "ai", 70, f"Reverse-engineering a {mode} architecture representing {represented} resource(s)…")
                result = await generate_architecture(job.workload_name, resources, context=resource_context)
                if result is None:
                    return await self._fail(job, active_context, "The AI could not infer an architecture. Try again.")

                await self._checkpoint(active_context)
                await self._set(job, active_context, "save", 90, "Saving architecture…")
                rebuild = bool(job.target_architecture_id)
                arch_payload = {
                    "description": result["description"],
                    "workload_id": job.workload_id,
                    "workload_name": job.workload_name,
                    "connection_id": job.connection_id,
                    "tenant_id": job.tenant_id,
                    "source": "ai",
                    "nodes": result["nodes"],
                    "edges": result["edges"],
                    "groups": result["groups"],
                    "created_by": job.created_by,
                    "ai": {
                        "rationale": result["rationale"],
                        "confidence": result["confidence"],
                        "resource_count": job.resource_count,
                        "context": resource_context,
                        "generated_by": job.created_by,
                    },
                }
                if rebuild:
                    # Regenerate in place: keep the existing id (and its name, which we
                    # don't overwrite, so the link/title the user chose is preserved).
                    arch_payload["id"] = job.target_architecture_id
                    saved = arch_registry.upsert_architecture(
                        arch_payload, actor=job.created_by, reason="Rebuilt from workload"
                    )
                else:
                    arch_payload["name"] = result["name"] or f"{job.workload_name} architecture"
                    saved = arch_registry.upsert_architecture(
                        arch_payload, actor=job.created_by, reason="Generated by AI"
                    )
                job.architecture_id = saved["id"]
                job.architecture_name = saved.get("name", "")
                job.status = "done"
                job.phase = "done"
                job.progress = 100
                job.message = "Done."
                job.ended_at = time.time()
                await active_context.emit(
                    "progress",
                    {"phase": "done", "progress": 100, "message": "Done."},
                    metadata=job.public(),
                )
                return JobOutcome(
                    result={
                        "architecture_id": job.architecture_id,
                        "architecture_name": job.architecture_name,
                        "resource_count": job.resource_count,
                    }
                )
        except asyncio.CancelledError:
            job.status = "canceled"
            job.phase = "done"
            job.message = "Canceled."
            job.ended_at = time.time()
            await asyncio.shield(
                active_context.emit(
                    "progress",
                    {"phase": "done", "progress": job.progress, "message": "Canceled."},
                    metadata=job.public(),
                )
            )
            return JobOutcome(status="cancelled", error="Canceled.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Architecture generation job failed")
            return await self._fail(job, active_context, str(exc)[:300])


manager = _Manager()

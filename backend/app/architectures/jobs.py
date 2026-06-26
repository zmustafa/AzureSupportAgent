"""Background AI architecture-generation jobs.

The reverse-engineering pipeline (resolve scope → query Azure Resource Graph → ask the
LLM → save) can take a while, so the dashboard launches it as a background job instead of
blocking. Several jobs (e.g. one per workload) can run at once; each reports its phase and
percentage and can be cancelled mid-flight. Jobs live in memory only — the resulting
architecture is persisted to the registry, but the job records themselves are ephemeral
(cleared on restart and auto-pruned once finished).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("app.architectures.jobs")

# Cap concurrent reverse-engineering pipelines so launching "dozens at once" queues
# gracefully instead of hammering Azure Resource Graph + the LLM all at the same time.
_MAX_CONCURRENCY = 3
# Keep finished jobs around briefly so the UI can show their outcome, then prune them.
_RETAIN_SECONDS = 1800
_MAX_JOBS = 200

_TERMINAL = {"done", "error", "canceled"}


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
    """In-process registry + runner for background generation jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._sem: asyncio.Semaphore | None = None
        self._bg: set[asyncio.Task] = set()

    def _semaphore(self) -> asyncio.Semaphore:
        # Created lazily so it binds to the running event loop.
        if self._sem is None:
            self._sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        return self._sem

    # ----------------------------------------------------------------- public API
    def create(
        self,
        *,
        tenant_id: str,
        workload_id: str,
        workload_name: str,
        connection_id: str,
        created_by: str,
        target_architecture_id: str = "",
    ) -> dict[str, Any]:
        self._prune()
        job = _Job(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            workload_id=workload_id,
            workload_name=workload_name,
            connection_id=connection_id,
            created_by=created_by,
            target_architecture_id=target_architecture_id,
        )
        self._jobs[job.id] = job
        task = asyncio.create_task(self._run(job))
        job.task = task
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return job.public()

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        self._prune()
        jobs = [j for j in self._jobs.values() if j.tenant_id == tenant_id]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [j.public() for j in jobs]

    def get(self, job_id: str, tenant_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id:
            return None
        return job.public()

    def cancel(self, job_id: str, tenant_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id or job.status in _TERMINAL:
            return False
        job.cancel_requested = True
        if job.status == "queued":
            # Not yet started — mark terminal right away for a snappy UI.
            job.status = "canceled"
            job.phase = "done"
            job.message = "Canceled."
            job.ended_at = time.time()
        if job.task is not None:
            job.task.cancel()
        return True

    def dismiss(self, job_id: str, tenant_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id or job.status not in _TERMINAL:
            return False
        self._jobs.pop(job_id, None)
        return True

    # ----------------------------------------------------------------- internals
    def _set(self, job: _Job, phase: str, progress: int, message: str) -> None:
        job.phase = phase
        job.progress = progress
        job.message = message

    def _fail(self, job: _Job, message: str) -> None:
        job.status = "error"
        job.phase = "done"
        job.error = message
        job.message = message
        job.ended_at = time.time()

    def _checkpoint(self, job: _Job) -> None:
        if job.cancel_requested:
            raise asyncio.CancelledError()

    async def _run(self, job: _Job) -> None:
        from app.architectures import registry as arch_registry
        from app.architectures.designer import generate_architecture
        from app.architectures.reverse import dump_resources
        from app.core.azure_connections import resolve_connection
        from app.workloads.registry import get_workload
        from app.azure.credentials import get_arm_token

        try:
            async with self._semaphore():
                if job.cancel_requested:
                    return  # cancelled while queued
                job.status = "running"
                job.started_at = time.time()
                self._set(job, "scope", 10, f"Resolving scope for '{job.workload_name}'…")

                wl = get_workload(job.workload_id)
                if wl is None:
                    self._fail(job, "Workload not found.")
                    return
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
                        self._fail(
                            job,
                            f"Can't authenticate to Azure with {cname}: {_terr} "
                            "Refresh its token in Settings → Azure Tenants, then rebuild again.",
                        )
                        return

                self._checkpoint(job)
                self._set(job, "query", 35, "Querying Azure Resource Graph for resources + properties…")
                dump = await dump_resources(wl, conn)
                if dump.get("error"):
                    self._fail(job, str(dump["error"]))
                    return
                resources = dump.get("resources") or []
                job.resource_count = len(resources)
                if not resources:
                    self._fail(job, "No resources found in this workload's scope.")
                    return

                self._checkpoint(job)
                self._set(job, "ai", 70, f"Reverse-engineering architecture from {len(resources)} resource(s)…")
                result = await generate_architecture(job.workload_name, resources)
                if result is None:
                    self._fail(job, "The AI could not infer an architecture. Try again.")
                    return

                self._checkpoint(job)
                self._set(job, "save", 90, "Saving architecture…")
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
                        "resource_count": len(resources),
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
        except asyncio.CancelledError:
            job.status = "canceled"
            job.phase = "done"
            job.message = "Canceled."
            job.ended_at = time.time()
            # Swallow: this is the task's top-level coroutine, so ending cleanly is correct.
        except Exception as exc:  # noqa: BLE001
            logger.exception("Architecture generation job failed")
            self._fail(job, str(exc)[:300])

    def _prune(self) -> None:
        now = time.time()
        stale = [
            jid
            for jid, job in self._jobs.items()
            if job.status in _TERMINAL and job.ended_at and (now - job.ended_at) > _RETAIN_SECONDS
        ]
        for jid in stale:
            self._jobs.pop(jid, None)
        if len(self._jobs) > _MAX_JOBS:
            terminal = sorted(
                (j for j in self._jobs.values() if j.status in _TERMINAL),
                key=lambda j: j.ended_at or j.created_at,
            )
            for job in terminal[: len(self._jobs) - _MAX_JOBS]:
                self._jobs.pop(job.id, None)


manager = _Manager()

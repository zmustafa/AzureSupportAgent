"""Durable polling jobs for on-demand Insight Pack runs."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.durable_jobs import DurableJobContext, DurableJobExecutor, JobOutcome, as_utc, utcnow

_FEATURE = "insights.pack"
_MAX_STEPS = 40
_INTERRUPTED = "Insight Pack execution was interrupted before completion."

ProgressCallback = Callable[..., None]
Run = Callable[[ProgressCallback], Awaitable[dict[str, Any]]]


class InsightsJobManager:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        owner_id: str | None = None,
        lease_seconds: float = 60.0,
        poll_seconds: float = 0.25,
    ) -> None:
        self._executor = DurableJobExecutor(
            _FEATURE,
            session_factory=session_factory,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            event_limit=_MAX_STEPS,
        )

    @staticmethod
    def _from_durable(durable: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(durable.get("metadata") or {})
        steps = [
            dict(event.get("data") or {})
            for event in durable.get("events") or []
            if event.get("event") == "step"
        ]
        latest = steps[-1] if steps else {}
        status = {
            "running": "running",
            "done": "succeeded",
            "error": "failed",
            "cancelled": "failed",
        }[durable["status"]]
        return {
            "id": durable["key"],
            "tenant_id": durable["tenant_id"],
            "pack_name": metadata.get("pack_name", ""),
            "scope_label": metadata.get("scope_label", ""),
            "status": status,
            "stage": "done" if status == "succeeded" else ("error" if status == "failed" else latest.get("stage", "queued")),
            "label": "Digest ready" if status == "succeeded" else ("Run failed" if status == "failed" else latest.get("label", "Queued…")),
            "pct": 100 if status == "succeeded" else int(latest.get("pct") or 0),
            "steps": steps,
            "run": durable.get("result"),
            "error": durable.get("error") or None,
            "started_at": durable["started_at"],
            "updated_at": (steps[-1].get("ts") if steps else durable["started_at"]),
            "finished_at": durable.get("finished_at"),
        }

    async def start(
        self,
        tenant_id: str,
        runner: Run,
        *,
        pack_name: str = "",
        scope_label: str = "",
    ) -> dict[str, Any]:
        key = uuid.uuid4().hex

        async def _run(context: DurableJobContext) -> JobOutcome:
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=100)

            def progress(
                *,
                stage: str,
                label: str,
                detail: str = "",
                pct: int | None = None,
                state: str = "done",
            ) -> None:
                event = {
                    "ts": time.time(),
                    "stage": stage,
                    "label": label,
                    "detail": detail,
                    "state": state,
                    "pct": max(0, min(100, int(pct))) if pct is not None else 0,
                }
                if queue.full():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(event)

            async def write_progress() -> None:
                while True:
                    event = await queue.get()
                    try:
                        if event is None:
                            return
                        await context.emit("step", event)
                    finally:
                        queue.task_done()

            writer = asyncio.create_task(write_progress())
            try:
                digest = await runner(progress)
                await queue.join()
                queue.put_nowait(None)
                await writer
                done = {
                    "ts": time.time(),
                    "stage": "done",
                    "label": "Digest ready",
                    "detail": "",
                    "state": "done",
                    "pct": 100,
                }
                await context.emit("step", done)
                return JobOutcome(result=digest)
            except Exception as exc:  # noqa: BLE001 - expose bounded polling failure
                if not writer.done():
                    writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
                message = str(exc)[:500]
                await context.emit(
                    "step",
                    {
                        "ts": time.time(),
                        "stage": "error",
                        "label": "Run failed",
                        "detail": message[:300],
                        "state": "error",
                        "pct": 0,
                    },
                )
                return JobOutcome(status="error", error=message)

        claim = await self._executor.start(
            tenant_id=tenant_id,
            key=key,
            metadata={"pack_name": pack_name, "scope_label": scope_label},
            runner=_run,
        )
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=_FEATURE, key=key
        )
        return self._from_durable(durable or claim.job)

    async def get(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=_FEATURE, key=job_id
        )
        if durable is None:
            return None
        if durable["status"] == "running" and durable.get("lease_expires_at"):
            expires = as_utc(datetime.fromisoformat(durable["lease_expires_at"]))
            if expires is not None and expires <= utcnow():
                await self._executor.store.interrupt_expired(
                    tenant_id=tenant_id,
                    feature=_FEATURE,
                    key=job_id,
                    error=_INTERRUPTED,
                )
                durable = await self._executor.store.load_current(
                    tenant_id=tenant_id, feature=_FEATURE, key=job_id
                )
                if durable is None:
                    return None
        return self._from_durable(durable)


manager = InsightsJobManager()


async def start(
    tenant_id: str,
    runner: Run,
    *,
    pack_name: str = "",
    scope_label: str = "",
) -> dict[str, Any]:
    return await manager.start(
        tenant_id, runner, pack_name=pack_name, scope_label=scope_label
    )


async def get(tenant_id: str, job_id: str) -> dict[str, Any] | None:
    return await manager.get(tenant_id, job_id)


def snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "label": job["label"],
        "pct": job["pct"],
        "steps": list(job["steps"]),
        "run": job["run"],
        "error": job["error"],
        "pack_name": job.get("pack_name", ""),
        "scope_label": job.get("scope_label", ""),
    }

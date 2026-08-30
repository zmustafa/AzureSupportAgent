"""Durable per-scope IAM refresh jobs with cross-replica SSE progress."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.durable_jobs import DurableJobContext, DurableJobExecutor, JobOutcome
from app.iam import orchestrator, progress as progress_mod

log = logging.getLogger("app.iam.job")

SCOPE_ALL = "__all__"
SCOPE_DIRECTORY = "directory"
_TICK_SECONDS = 3
_FEATURE = "iam.refresh"


def job_key(tenant_id: str, scope: str) -> str:
    return f"{tenant_id or 'default'}|{scope}"


def _tenant(key: str) -> str:
    return key.split("|", 1)[0] or "default"


def _elapsed(job: dict[str, Any]) -> float:
    try:
        started = datetime.fromisoformat(str(job["started_at"]))
        finished = datetime.fromisoformat(str(job["finished_at"])) if job.get("finished_at") else datetime.now(timezone.utc)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        return max(0.0, (finished - started).total_seconds())
    except (TypeError, ValueError):
        return 0.0


async def _warm_derived(tenant_id: str, progress) -> None:
    """Rebuild derived caches inside the visible refresh job, but never fail collection."""
    from app.iam import cache, compose, cpu, effective, escalation

    await progress("info", "Rebuilding the escalation graph…")
    started = time.monotonic()
    try:
        def _build() -> dict[str, Any]:
            rows = compose.build_master_rows(tenant_id)
            directory = cache.read_directory(tenant_id)
            return escalation.graph_for_tenant(
                tenant_id,
                rows,
                effective.build_role_index(directory.get("role_defs", [])),
                identities=directory.get("identities", {}),
                federated=directory.get("federated", []),
            )

        graph = await cpu.run(_build, label="escalation graph (refresh warm-up)")
    except Exception:  # noqa: BLE001 - collection succeeded; warm-up is optional
        log.warning("iam job: could not warm the escalation graph", exc_info=True)
        await progress(
            "warning",
            "The escalation graph could not be pre-built; the next screen that needs it will build it instead.",
        )
        return
    await progress(
        "ok",
        f"Escalation graph ready in {time.monotonic() - started:.0f}s "
        f"({len(graph.get('nodes') or [])} nodes, {len(graph.get('edges') or [])} edges).",
    )


class IamJobManager:
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
            event_limit=1000,
        )

    @staticmethod
    def _from_durable(durable: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(durable.get("metadata") or {})
        progress = []
        for event in durable.get("events") or []:
            if event.get("event") != "progress":
                continue
            row = dict(event.get("data") or {})
            row.setdefault("seq", event.get("seq"))
            row.setdefault("ts", event.get("created_at"))
            progress.append(row)
        return {
            "id": durable["id"],
            "key": durable["key"],
            "tenant_id": durable["tenant_id"],
            "scope": metadata.get("scope", ""),
            "mode": metadata.get("mode", "all"),
            "status": durable["status"],
            "started_at": durable["started_at"],
            "finished_at": durable.get("finished_at"),
            "progress": progress,
            "error": durable.get("error") or "",
        }

    async def get_job(self, key: str) -> dict[str, Any] | None:
        durable = await self._executor.store.load_current(
            tenant_id=_tenant(key), feature=_FEATURE, key=key
        )
        return self._from_durable(durable) if durable else None

    async def is_running(self, key: str) -> bool:
        current = await self.get_job(key)
        return bool(current and current["status"] == "running")

    async def start_job(
        self,
        *,
        tenant_id: str,
        connection: dict[str, Any] | None,
        scope: str,
        mode: str,
        display_name: str = "",
        connection_id: str | None = None,
        triggered_by: str = "",
        record_run: bool = True,
    ) -> dict[str, Any]:
        key = job_key(tenant_id, scope)

        async def _run(context: DurableJobContext) -> JobOutcome:
            async def _progress(level: str, message: str) -> None:
                elapsed = _elapsed(
                    {
                        "started_at": context.metadata.get("started_at"),
                        "finished_at": None,
                    }
                )
                timing = progress_mod.public(tenant_id, mode, elapsed)
                await context.emit(
                    "progress",
                    {"level": level, "message": message, **timing},
                    include_seq=True,
                )

            try:
                if mode in ("all", "delta"):
                    await orchestrator.refresh_all(
                        tenant_id,
                        connection,
                        progress=_progress,
                        mode="delta" if mode == "delta" else "full",
                    )
                elif mode == "directory":
                    await orchestrator.refresh_directory(tenant_id, connection, progress=_progress)
                else:
                    await orchestrator.refresh_scope(
                        tenant_id,
                        connection,
                        scope,
                        display_name=display_name,
                        progress=_progress,
                    )
                await _warm_derived(tenant_id, _progress)
                await context.checkpoint()
                if record_run:
                    try:
                        from app.iam import store

                        await store.save_run(
                            tenant_id,
                            connection_id=connection_id,
                            scope=scope,
                            trigger="manual",
                            triggered_by=triggered_by,
                        )
                    except Exception:  # noqa: BLE001 - history is best effort
                        log.warning("rbac run history record failed", exc_info=True)
                elapsed = _elapsed(
                    {
                        "started_at": context.metadata.get("started_at"),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                try:
                    progress_mod.record(tenant_id, mode, elapsed)
                except Exception:  # noqa: BLE001 - estimate recording cannot fail refresh
                    log.warning("iam job: could not record run duration", exc_info=True)
                return JobOutcome(result={"key": key, "scope": scope, "mode": mode})
            except Exception as exc:  # noqa: BLE001 - record bounded job failure
                log.exception("rbac refresh job failed")
                message = str(exc)[:300]
                await _progress("error", f"Refresh failed: {message}")
                return JobOutcome(status="error", error=message)

        started_at = datetime.now(timezone.utc).isoformat()
        claim = await self._executor.start(
            tenant_id=tenant_id,
            key=key,
            metadata={
                "scope": scope,
                "mode": mode,
                "connection_id": connection_id or "",
                "started_at": started_at,
            },
            runner=_run,
        )
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=_FEATURE, key=key
        )
        return self._from_durable(durable or claim.job)

    async def stream(self, key: str):
        current = await self.get_job(key)
        if current is None:
            yield {
                "event": "error",
                "data": json.dumps({"message": "No refresh job for this scope."}),
            }
            return
        yield {"event": "start", "data": json.dumps(public_job(current) or {})}
        sent_seq = -1
        last_tick = asyncio.get_running_loop().time()
        while True:
            durable = await self._executor.store.load_current(
                tenant_id=_tenant(key), feature=_FEATURE, key=key, include_events=False
            )
            if durable is None:
                return
            for event in await self._executor.store.events_after(durable["id"], sent_seq):
                sent_seq = max(sent_seq, int(event["seq"]))
                if event["event"] != "progress":
                    continue
                row = dict(event["data"])
                row.setdefault("seq", event["seq"])
                row.setdefault("ts", event["created_at"])
                yield {"event": "progress", "data": json.dumps(row)}
            if durable["status"] != "running":
                current = self._from_durable(
                    await self._executor.store.load_current(
                        tenant_id=_tenant(key), feature=_FEATURE, key=key
                    )
                    or durable
                )
                break
            await asyncio.sleep(self._executor.store.poll_seconds)
            now = asyncio.get_running_loop().time()
            if now - last_tick >= _TICK_SECONDS:
                timing = progress_mod.public(current["tenant_id"], current["mode"], _elapsed(current))
                yield {
                    "event": "tick",
                    "data": json.dumps(
                        {
                            "status": "running",
                            "last_message": current["progress"][-1]["message"] if current["progress"] else "",
                            **timing,
                        }
                    ),
                }
                last_tick = now
        if current["status"] == "done":
            timing = progress_mod.public(current["tenant_id"], current["mode"], _elapsed(current))
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "key": key,
                        "scope": current["scope"],
                        "mode": current["mode"],
                        "elapsed_seconds": timing["elapsed_seconds"],
                        "elapsed_label": timing["elapsed_label"],
                        "eta_seconds": None,
                        "eta_label": "—",
                        "eta_basis": f"completed in {timing['elapsed_label']}",
                        "typical_seconds": timing["typical_seconds"],
                    }
                ),
            }
        else:
            yield {
                "event": "error",
                "data": json.dumps({"message": current["error"] or "Refresh failed."}),
            }


manager = IamJobManager()
_tasks = manager._executor.tasks


async def get_job(key: str) -> dict[str, Any] | None:
    return await manager.get_job(key)


async def is_running(key: str) -> bool:
    return await manager.is_running(key)


def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    return {
        "id": job["id"],
        "key": job["key"],
        "scope": job["scope"],
        "mode": job["mode"],
        "status": job["status"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "progress_count": len(job["progress"]),
        "last_message": job["progress"][-1]["message"] if job["progress"] else "",
        "error": job["error"],
        **progress_mod.public(job["tenant_id"], job["mode"], _elapsed(job)),
    }


async def start_job(**kwargs: Any) -> dict[str, Any]:
    return await manager.start_job(**kwargs)


async def stream(key: str):
    async for frame in manager.stream(key):
        yield frame

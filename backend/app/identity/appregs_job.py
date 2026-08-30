"""Durable background manager for the slow Application Registrations refresh.

Execution ownership, status, and bounded replay events live in SQL. The existing page
checkpoint remains the source for exact enumeration resume; connection dictionaries are kept
only in the owner task and are never written to durable job metadata.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.durable_jobs import DurableJobContext, DurableJobExecutor, JobOutcome

log = logging.getLogger("app.identity.appregs_job")

_FEATURE = "identity.appregs"
_EVENT_LIMIT = 1000


def _tenant_from_key(key: str) -> str:
    return key.split("|", 1)[0] or "default"


class AppRegistrationsJobManager:
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
            event_limit=_EVENT_LIMIT,
        )

    @staticmethod
    def _from_durable(durable: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(durable.get("metadata") or {})
        progress: list[dict[str, Any]] = []
        for event in durable.get("events") or []:
            if event.get("event") != "progress":
                continue
            row = dict(event.get("data") or {})
            row.setdefault("seq", event.get("seq"))
            row.setdefault("ts", event.get("created_at"))
            progress.append(row)
        status = durable["status"]
        if status == "running" and durable.get("cancel_requested"):
            status = "cancelling"
        return {
            "id": durable["id"],
            "key": durable["key"],
            "status": status,
            "started_at": durable["started_at"],
            "finished_at": durable.get("finished_at"),
            "progress": progress,
            "result": durable.get("result"),
            "error": durable.get("error") or "",
            "mode": metadata.get("mode", "capped"),
            "configured_limit": int(metadata.get("configured_limit") or 500),
            "page_size": int(metadata.get("page_size") or 250),
            "current": int(metadata.get("current") or 0),
            "total": metadata.get("total"),
            "percent": metadata.get("percent"),
            "page": int(metadata.get("page") or 0),
            "retries": int(metadata.get("retries") or 0),
            "throttles": int(metadata.get("throttles") or 0),
            "resumed": bool(metadata.get("resumed", False)),
            "resume_available": bool(metadata.get("resume_available", False)),
            "cancel_requested": bool(durable.get("cancel_requested")),
            "connection_id": str(metadata.get("connection_id") or ""),
            "tenant_id": str(metadata.get("tenant_id") or _tenant_from_key(durable["key"])),
        }

    async def get_job(self, key: str) -> dict[str, Any] | None:
        durable = await self._executor.store.load_current(
            tenant_id=_tenant_from_key(key), feature=_FEATURE, key=key
        )
        return self._from_durable(durable) if durable else None

    async def is_running(self, key: str) -> bool:
        job = await self.get_job(key)
        return bool(job and job["status"] in {"running", "cancelling"})

    async def start_job(
        self,
        *,
        key: str,
        tenant_id: str,
        connection: dict[str, Any] | None,
        connection_id: str,
        limit: int = 500,
        mode: str = "capped",
        page_size: int = 250,
    ) -> dict[str, Any]:
        from app.identity import appregs, appregs_cache

        requested_mode = "full" if mode == "full" else "capped"
        saved_checkpoint = appregs_cache.get_checkpoint(tenant_id, connection_id)
        checkpoint_reset_reason = ""
        if (
            saved_checkpoint
            and saved_checkpoint.get("schema") == appregs.APPREGS_CHECKPOINT_SCHEMA
            and saved_checkpoint.get("mode") == requested_mode
        ):
            limit = max(
                50,
                min(
                    5000,
                    int(
                        saved_checkpoint.get("configured_limit")
                        or (
                            saved_checkpoint.get("target_limit")
                            if requested_mode == "capped"
                            else limit
                        )
                        or limit
                    ),
                ),
            )
            page_size = max(50, min(999, int(saved_checkpoint.get("page_size") or page_size)))
        elif saved_checkpoint:
            if saved_checkpoint.get("schema") != appregs.APPREGS_CHECKPOINT_SCHEMA:
                checkpoint_reset_reason = (
                    "A saved refresh checkpoint uses an older data schema; restarting from page 1."
                )
            else:
                checkpoint_reset_reason = (
                    f"A saved {saved_checkpoint.get('mode', 'previous')} refresh checkpoint cannot be "
                    f"used for {requested_mode} mode; restarting from page 1."
                )

        metadata = {
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "mode": requested_mode,
            "configured_limit": limit,
            "page_size": page_size,
            "current": 0,
            "total": None,
            "percent": None,
            "page": 0,
            "retries": 0,
            "throttles": 0,
            "resumed": False,
            "resume_available": bool(saved_checkpoint),
        }

        async def _run(context: DurableJobContext) -> JobOutcome:
            checkpoint = appregs_cache.get_checkpoint(tenant_id, connection_id)
            expected_target = (
                appregs.APPREGS_FULL_SAFETY_LIMIT if requested_mode == "full" else limit
            )
            if checkpoint and (
                checkpoint.get("schema") != appregs.APPREGS_CHECKPOINT_SCHEMA
                or checkpoint.get("mode") != requested_mode
                or int(checkpoint.get("target_limit") or 0) != expected_target
                or int(checkpoint.get("page_size") or 0) != page_size
            ):
                appregs_cache.delete_checkpoint(tenant_id, connection_id)
                checkpoint = None
            resumed = bool(checkpoint)
            context.metadata["resumed"] = resumed

            async def _progress(
                level: str, message: str, progress_metadata: dict[str, Any] | None = None
            ) -> None:
                patch = {
                    field: value
                    for field, value in (progress_metadata or {}).items()
                    if field in {
                        "current", "total", "percent", "page", "retries", "throttles",
                        "resumed", "phase", "status", "delay_seconds", "retry",
                    }
                }
                row = {"level": level, "message": message, **patch}
                await context.emit("progress", row, metadata=patch, include_seq=True)

            async def _checkpoint(state: dict[str, Any]) -> None:
                appregs_cache.set_checkpoint(
                    tenant_id,
                    connection_id,
                    {
                        **state,
                        "job_id": context.job_id,
                        "started_at": context.started_at,
                    },
                )
                context.metadata["resume_available"] = True

            await _progress("info", "Starting Application Registrations refresh…")
            if checkpoint_reset_reason:
                await _progress("warn", checkpoint_reset_reason, {"phase": "restart"})
            try:
                snap = await appregs.collect_app_registrations(
                    connection,
                    tenant_id=tenant_id,
                    limit=limit,
                    full=requested_mode == "full",
                    page_size=page_size,
                    checkpoint=checkpoint,
                    on_checkpoint=_checkpoint,
                    progress=_progress,
                    should_cancel=lambda: context.cancel_requested,
                )
                if snap.get("source") == "unavailable":
                    raise RuntimeError("provider_unavailable")
                fetched_at = appregs_cache.set_(tenant_id, connection_id, snap)
                appregs_cache.delete_checkpoint(tenant_id, connection_id)
                context.metadata["resume_available"] = False
                result = {
                    **snap,
                    "cached": True,
                    "never_loaded": False,
                    "fetched_at": fetched_at,
                    "age_seconds": 0,
                    "configured_limit": limit,
                    "max_configurable_limit": 5000,
                    "full_safety_limit": appregs.APPREGS_FULL_SAFETY_LIMIT,
                    "page_size": page_size,
                }
                await _progress(
                    "ok",
                    f"Cached snapshot — {snap.get('summary', {}).get('total', 0)} app registration(s).",
                )
                return JobOutcome(result=result)
            except asyncio.CancelledError:
                available = appregs_cache.get_checkpoint(tenant_id, connection_id) is not None
                context.metadata["resume_available"] = available
                await asyncio.shield(
                    _progress(
                        "warn",
                        "Refresh cancelled. Completed pages were checkpointed; the previous snapshot is unchanged.",
                    )
                )
                return JobOutcome(status="cancelled", error="Refresh cancelled.")
            except Exception:  # noqa: BLE001 - never expose provider or credential details
                log.warning("app-registrations refresh job failed", exc_info=True)
                available = appregs_cache.get_checkpoint(tenant_id, connection_id) is not None
                context.metadata["resume_available"] = available
                message = "Refresh failed. The previous completed snapshot was preserved."
                await _progress("error", message)
                return JobOutcome(status="error", error=message)

        claim = await self._executor.start(
            tenant_id=tenant_id, key=key, metadata=metadata, runner=_run
        )
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=_FEATURE, key=key
        )
        return self._from_durable(durable or claim.job)

    async def cancel_job(self, key: str) -> bool:
        return await self._executor.cancel(tenant_id=_tenant_from_key(key), key=key)

    @staticmethod
    def _cached_result(job: dict[str, Any]) -> dict[str, Any]:
        if isinstance(job.get("result"), dict):
            return job["result"]
        from app.identity import appregs, appregs_cache

        cached = appregs_cache.get(job["tenant_id"], job["connection_id"])
        if not cached:
            return {}
        return {
            **(cached.get("payload") or {}),
            "cached": True,
            "never_loaded": False,
            "fetched_at": cached.get("fetched_at", ""),
            "age_seconds": cached.get("age_seconds", 0),
            "configured_limit": job["configured_limit"],
            "max_configurable_limit": 5000,
            "full_safety_limit": appregs.APPREGS_FULL_SAFETY_LIMIT,
            "page_size": job["page_size"],
        }

    async def stream(self, key: str):
        job = await self.get_job(key)
        if job is None:
            yield {
                "event": "error",
                "data": json.dumps({"message": "No refresh job for this scope."}),
            }
            return
        yield {
            "event": "start",
            "data": json.dumps(
                {
                    "id": job["id"],
                    "status": job["status"],
                    "started_at": job["started_at"],
                    "mode": job["mode"],
                    "configured_limit": job["configured_limit"],
                    "current": job["current"],
                    "total": job["total"],
                    "page": job["page"],
                    "resumed": job["resumed"],
                }
            ),
        }
        sent_seq = -1
        last_ping = asyncio.get_running_loop().time()
        while True:
            durable = await self._executor.store.load_current(
                tenant_id=_tenant_from_key(key), feature=_FEATURE, key=key,
                include_events=False,
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
                job = self._from_durable(
                    await self._executor.store.load_current(
                        tenant_id=_tenant_from_key(key), feature=_FEATURE, key=key
                    )
                    or durable
                )
                break
            await asyncio.sleep(self._executor.store.poll_seconds)
            now = asyncio.get_running_loop().time()
            if now - last_ping >= 20:
                yield {"event": "ping", "data": "{}"}
                last_ping = now
        if job["status"] == "done":
            yield {"event": "done", "data": json.dumps(self._cached_result(job))}
        elif job["status"] == "cancelled":
            yield {
                "event": "cancelled",
                "data": json.dumps(
                    {"message": job["error"], "resume_available": job["resume_available"]}
                ),
            }
        else:
            yield {
                "event": "error",
                "data": json.dumps({"message": job["error"] or "Refresh failed."}),
            }


manager = AppRegistrationsJobManager()
_tasks = manager._executor.tasks  # local handles only; durable SQL is authoritative


async def get_job(key: str) -> dict[str, Any] | None:
    return await manager.get_job(key)


def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    return {
        "id": job["id"],
        "status": job["status"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "progress_count": len(job["progress"]),
        "last_message": job["progress"][-1]["message"] if job["progress"] else "",
        "error": job["error"],
        "mode": job.get("mode", "capped"),
        "configured_limit": job.get("configured_limit", 500),
        "page_size": job.get("page_size", 250),
        "current": job.get("current", 0),
        "total": job.get("total"),
        "percent": job.get("percent"),
        "page": job.get("page", 0),
        "retries": job.get("retries", 0),
        "throttles": job.get("throttles", 0),
        "resumed": bool(job.get("resumed", False)),
        "resume_available": bool(job.get("resume_available", False)),
    }


async def is_running(key: str) -> bool:
    return await manager.is_running(key)


async def start_job(**kwargs: Any) -> dict[str, Any]:
    return await manager.start_job(**kwargs)


async def cancel_job(key: str) -> bool:
    return await manager.cancel_job(key)


def recoverable_job(tenant_id: str, connection_id: str) -> dict[str, Any] | None:
    """Public paused-job shape reconstructed from the existing page checkpoint."""
    from app.identity import appregs, appregs_cache

    checkpoint = appregs_cache.get_checkpoint(tenant_id, connection_id)
    if not checkpoint:
        return None
    if checkpoint.get("schema") != appregs.APPREGS_CHECKPOINT_SCHEMA:
        appregs_cache.delete_checkpoint(tenant_id, connection_id)
        return None
    current = len(checkpoint.get("apps_raw") or [])
    total = checkpoint.get("graph_total")
    percent = round((current / total) * 100, 1) if total else None
    return {
        "id": checkpoint.get("job_id") or "checkpoint",
        "status": "paused",
        "started_at": checkpoint.get("started_at") or "",
        "finished_at": None,
        "progress": [],
        "error": "",
        "mode": checkpoint.get("mode") or "capped",
        "configured_limit": checkpoint.get("configured_limit")
        or (checkpoint.get("target_limit") if checkpoint.get("mode") == "capped" else 500),
        "page_size": checkpoint.get("page_size") or 250,
        "current": current,
        "total": total,
        "percent": percent,
        "page": checkpoint.get("pages") or 0,
        "retries": 0,
        "throttles": 0,
        "resumed": True,
        "resume_available": True,
    }


async def stream(key: str):
    async for frame in manager.stream(key):
        yield frame

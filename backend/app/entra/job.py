"""Durable Entra refresh jobs with cross-replica SSE replay."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.durable_jobs import DurableJobContext, DurableJobExecutor, JobOutcome

log = logging.getLogger("app.entra.job")

SCOPE_ALL = "__all__"
_FEATURE = "entra.refresh"


def job_key(tenant_id: str, scope: str = SCOPE_ALL) -> str:
    return f"{tenant_id or 'default'}|{scope}"


def _tenant(key: str) -> str:
    return key.split("|", 1)[0] or "default"


async def _backfill_signin_outcomes(
    tenant_id: str,
    connection: dict[str, Any] | None,
    domains: list[str] | None,
    progress,
) -> None:
    if domains and "apps" not in domains:
        return
    try:
        from app.entra import signin_outcomes

        await signin_outcomes.run_backfill(tenant_id, connection, progress=progress)
    except Exception:  # noqa: BLE001 - enrichment cannot fail a completed refresh
        log.warning("sign-in outcome backfill failed", exc_info=True)


class EntraJobManager:
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
            "scope": metadata.get("scope", SCOPE_ALL),
            "domains": list(metadata.get("domains") or []),
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
        job = await self.get_job(key)
        return bool(job and job["status"] == "running")

    async def start_job(
        self,
        *,
        tenant_id: str,
        connection: dict[str, Any] | None,
        domains: list[str] | None = None,
        connection_id: str = "",
    ) -> dict[str, Any]:
        key = job_key(tenant_id)
        safe_domains = list(domains or [])

        async def _run(context: DurableJobContext) -> JobOutcome:
            from app.entra import snapshot as snapshot_mod

            async def _progress(level: str, message: str) -> None:
                await context.emit(
                    "progress", {"level": level, "message": message}, include_seq=True
                )

            try:
                result = await snapshot_mod.refresh(
                    tenant_id,
                    connection,
                    domains=domains,
                    connection_id=connection_id,
                    progress=_progress,
                )
                if not result.get("ok"):
                    return JobOutcome(
                        status="error", error=str(result.get("error") or "Refresh failed.")[:300]
                    )
                await _backfill_signin_outcomes(tenant_id, connection, domains, _progress)
                return JobOutcome(result={"key": key, "domains": safe_domains})
            except Exception as exc:  # noqa: BLE001 - record bounded job failure
                log.exception("entra refresh job failed")
                message = str(exc)[:300]
                await _progress("error", f"Refresh failed: {message}")
                return JobOutcome(status="error", error=message)

        claim = await self._executor.start(
            tenant_id=tenant_id,
            key=key,
            metadata={
                "scope": SCOPE_ALL,
                "domains": safe_domains,
                "connection_id": connection_id,
            },
            runner=_run,
        )
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=_FEATURE, key=key
        )
        return self._from_durable(durable or claim.job)

    async def stream(self, key: str):
        job = await self.get_job(key)
        if job is None:
            yield {
                "event": "error",
                "data": json.dumps({"message": "No refresh job for this tenant."}),
            }
            return
        yield {"event": "start", "data": json.dumps(public_job(job) or {})}
        sent_seq = -1
        last_ping = asyncio.get_running_loop().time()
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
                job = self._from_durable(
                    await self._executor.store.load_current(
                        tenant_id=_tenant(key), feature=_FEATURE, key=key
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
            yield {"event": "done", "data": json.dumps({"key": key, "domains": job["domains"]})}
        else:
            yield {
                "event": "error",
                "data": json.dumps({"message": job["error"] or "Refresh failed."}),
            }


manager = EntraJobManager()
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
        "domains": job["domains"],
        "status": job["status"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "progress_count": len(job["progress"]),
        "last_message": job["progress"][-1]["message"] if job["progress"] else "",
        "error": job["error"],
    }


async def start_job(**kwargs: Any) -> dict[str, Any]:
    return await manager.start_job(**kwargs)


async def stream(key: str):
    async for frame in manager.stream(key):
        yield frame

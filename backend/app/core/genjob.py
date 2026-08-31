"""Generic background-job registry — run a slow coroutine detached from the request so it
survives a client disconnect (browser navigates away, SSE stream drops), with a granular
progress log that SSE subscribers replay-then-tail until the job finishes.

This generalizes the proven Application-Registrations refresh manager so other slow,
"survive navigation" flows (Know-Me / FMEA AI generation) get the same behavior without
duplicating the plumbing. Each feature creates one ``JobRegistry`` instance; jobs are keyed
within it (e.g. by document id). Starting a job whose key is already running just returns the
in-flight job. The job RESULT is held in memory until the next start for that key; the durable
artifact (saved document, cached snapshot) is written by the runner itself, so a completed job
that nobody is listening to still persists its work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.durable_jobs import (
    DEFAULT_EVENT_LIMIT,
    DEFAULT_POLL_SECONDS,
    DEFAULT_RETENTION_SECONDS,
    DurableJobStore,
)

log = logging.getLogger("app.core.genjob")

# A runner receives a ``progress(level, message)`` async callback and returns the result dict
# that the final SSE ``done`` event carries (and that the runner has already persisted).
ProgressFn = Callable[[str, str], Awaitable[None]]
Runner = Callable[[ProgressFn], Awaitable[dict[str, Any]]]

_REGISTRIES: weakref.WeakSet[Any] = weakref.WeakSet()


class JobRegistry:
    """A durable registry of background jobs for one feature (e.g. ``"knowme"``)."""

    def __init__(
        self,
        name: str,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        owner_id: str | None = None,
        lease_seconds: float = 60.0,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> None:
        self.name = name
        self._store = DurableJobStore(
            session_factory=session_factory,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
        )
        self._retention_seconds = retention_seconds
        self._event_limit = event_limit
        self._jobs: dict[str, dict[str, Any]] = {}
        self._conds: dict[str, asyncio.Condition] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        _REGISTRIES.add(self)

    # ---- internals --------------------------------------------------------------
    def _cond(self, key: str) -> asyncio.Condition:
        c = self._conds.get(key)
        if c is None:
            c = asyncio.Condition()
            self._conds[key] = c
        return c

    async def _append(
        self, key: str, lease_token: str, level: str, message: str
    ) -> None:
        # ``phase`` mirrors ``level`` so SSE clients that key off a ``phase`` field (the legacy
        # inline-stream shape) work unchanged against a background-job stream.
        job = self._jobs.get(key)
        if job is None:
            return
        stored = await self._store.append_events(
            job_id=job["id"],
            lease_token=lease_token,
            events=[
                (
                    "status",
                    {"ts": None, "level": level, "phase": level, "message": message},
                )
            ],
            event_limit=self._event_limit,
            include_seq=True,
        )
        if stored:
            line = dict(stored[0]["data"])
            line["ts"] = stored[0]["created_at"]
            job["progress"].append(line)
        cond = self._cond(key)
        async with cond:
            cond.notify_all()

    async def _heartbeat(self, key: str, job_id: str, lease_token: str) -> None:
        try:
            owned, cancel_requested = await self._store.monitor_lease(
                job_id=job_id,
                lease_token=lease_token,
                should_stop=lambda: (
                    self._tasks.get(key) is None or self._tasks[key].done()
                ),
            )
            if not owned or cancel_requested:
                task = self._tasks.get(key)
                if task is not None and not task.done():
                    task.cancel()
        except asyncio.CancelledError:
            return

    # ---- public API -------------------------------------------------------------
    @staticmethod
    def _from_durable(job: dict[str, Any]) -> dict[str, Any]:
        progress = []
        for event in job.get("events") or []:
            if event.get("event") != "status":
                continue
            line = dict(event.get("data") or {})
            if not line.get("ts"):
                line["ts"] = event.get("created_at")
            progress.append(line)
        return {
            "id": job["id"],
            "key": job["key"],
            "status": job["status"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "progress": progress,
            "result": job.get("result"),
            "error": job.get("error") or "",
        }

    async def get_job(
        self, key: str, *, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        durable = await self._store.load_current(
            tenant_id=tenant_id, feature=self.name, key=key
        )
        if durable is None:
            return None
        job = self._from_durable(durable)
        self._jobs[key] = job
        return job

    async def jobs_with_prefix(
        self, prefix: str, *, tenant_id: str = "default"
    ) -> list[dict[str, Any]]:
        """All jobs whose key starts with ``prefix`` (raw job dicts). Lets a caller surface
        every in-flight/recent job for a scope (e.g. one tenant) in a progress tray."""
        jobs = await self._store.list_current(
            tenant_id=tenant_id, feature=self.name, key_prefix=prefix
        )
        return [self._from_durable(job) for job in jobs]

    async def is_running(self, key: str, *, tenant_id: str = "default") -> bool:
        job = await self.get_job(key, tenant_id=tenant_id)
        return bool(job and job["status"] == "running")

    def public_job(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        """A client-safe view (omits the heavy result payload)."""
        if not job:
            return None
        return {
            "id": job["id"],
            "key": job["key"],
            "status": job["status"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "progress_count": len(job["progress"]),
            "last_message": job["progress"][-1]["message"] if job["progress"] else "",
            "error": job["error"],
        }

    async def start(
        self, key: str, runner: Runner, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        """Start ``runner`` for ``key`` unless a job is already running for it (idempotency —
        this is the KP6 'don't double-generate' guard). Returns the (possibly in-flight) job."""
        claim = await self._store.claim(
            tenant_id=tenant_id, feature=self.name, key=key
        )
        durable = await self._store.load_current(
            tenant_id=tenant_id, feature=self.name, key=key
        )
        if durable is None:
            raise RuntimeError("Claimed durable job could not be loaded.")
        job = self._from_durable(durable)
        self._jobs[key] = job
        if not claim.acquired or claim.lease_token is None:
            return job
        lease_token = claim.lease_token

        async def _run() -> None:
            async def _progress(level: str, message: str) -> None:
                await self._append(key, lease_token, level, message)

            try:
                result = await runner(_progress)
                await self._store.finalize(
                    job_id=job["id"],
                    lease_token=lease_token,
                    status="done",
                    result=result,
                    retention_seconds=self._retention_seconds,
                )
            except asyncio.CancelledError:  # task cancelled (e.g. shutdown) — mark and re-raise
                await asyncio.shield(
                    self._store.finalize(
                        job_id=job["id"],
                        lease_token=lease_token,
                        status="error",
                        result=None,
                        error="Generation was cancelled.",
                        retention_seconds=self._retention_seconds,
                    )
                )
                raise
            except Exception as exc:  # noqa: BLE001 - isolate the job
                # Feature names and keys can contain request-derived values. The durable row
                # below retains the bounded error for authorized readers; process logs remain
                # static to prevent log-record injection.
                log.error("Background generation job failed")
                await self._store.finalize(
                    job_id=job["id"],
                    lease_token=lease_token,
                    status="error",
                    result=None,
                    error=str(exc)[:300],
                    retention_seconds=self._retention_seconds,
                )
            finally:
                heartbeat = self._heartbeat_tasks.pop(key, None)
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                latest = await self.get_job(key, tenant_id=tenant_id)
                if latest is not None:
                    self._jobs[key] = latest
                cond = self._cond(key)
                async with cond:
                    cond.notify_all()

        task = asyncio.create_task(_run())
        self._tasks[key] = task
        self._heartbeat_tasks[key] = asyncio.create_task(
            self._heartbeat(key, job["id"], lease_token)
        )

        def _forget(completed: asyncio.Task) -> None:
            if self._tasks.get(key) is completed:
                self._tasks.pop(key, None)

        task.add_done_callback(_forget)
        return job

    async def cancel(self, key: str, *, tenant_id: str = "default") -> bool:
        requested = await self._store.request_cancel(
            tenant_id=tenant_id, feature=self.name, key=key
        )
        task = self._tasks.get(key)
        if requested and task is not None and not task.done():
            task.cancel()
        return requested

    async def cleanup(self) -> int:
        return await self._store.cleanup(feature=self.name)

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Cancel and terminalize this process's runners before replica shutdown."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=max(0.1, timeout_seconds))
            if pending:
                log.warning("Background generation jobs exceeded the graceful shutdown window")
        heartbeats = list(self._heartbeat_tasks.values())
        for heartbeat in heartbeats:
            heartbeat.cancel()
        if heartbeats:
            await asyncio.gather(*heartbeats, return_exceptions=True)

    async def stream(self, key: str, *, tenant_id: str = "default"):
        """Async generator of SSE-ready dicts: replay the progress log so far, then tail new
        lines until the job finishes. Safe to (re)attach at any time; the job runs regardless
        of subscribers, so a dropped connection never loses the result."""
        job = await self._store.load_current(
            tenant_id=tenant_id, feature=self.name, key=key, include_events=False
        )
        if job is None:
            yield {"event": "error", "data": json.dumps({"message": "No job for this key."})}
            return

        yield {"event": "start", "data": json.dumps({"id": job["id"], "status": job["status"], "started_at": job["started_at"]})}

        sent_seq = -1
        last_ping = asyncio.get_running_loop().time()
        cond = self._cond(key)
        while True:
            events = await self._store.events_after(job["id"], sent_seq)
            for event in events:
                sent_seq = max(sent_seq, int(event["seq"]))
                if event["event"] == "status":
                    data = dict(event["data"])
                    if not data.get("ts"):
                        data["ts"] = event["created_at"]
                    yield {"event": "status", "data": json.dumps(data)}
            current = await self._store.load_current(
                tenant_id=tenant_id, feature=self.name, key=key, include_events=False
            )
            if current is None:
                return
            if current["status"] != "running":
                job = current
                break
            async with cond:
                try:
                    await asyncio.wait_for(cond.wait(), timeout=self._store.poll_seconds)
                except asyncio.TimeoutError:
                    now = asyncio.get_running_loop().time()
                    if now - last_ping >= 20:
                        yield {"event": "ping", "data": "{}"}
                        last_ping = now

        for event in await self._store.events_after(job["id"], sent_seq):
            if event["event"] == "status":
                data = dict(event["data"])
                if not data.get("ts"):
                    data["ts"] = event["created_at"]
                yield {"event": "status", "data": json.dumps(data)}

        if job["status"] == "done":
            yield {"event": "done", "data": json.dumps(job["result"] or {})}
        else:
            yield {"event": "error", "data": json.dumps({"message": job["error"] or "Generation failed."})}


async def shutdown_registries() -> None:
    """Gracefully stop every generic registry created in this process."""
    registries = list(_REGISTRIES)
    if registries:
        await asyncio.gather(
            *(registry.stop() for registry in registries),
            return_exceptions=True,
        )

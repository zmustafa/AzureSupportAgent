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
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger("app.core.genjob")

# A runner receives a ``progress(level, message)`` async callback and returns the result dict
# that the final SSE ``done`` event carries (and that the runner has already persisted).
ProgressFn = Callable[[str, str], Awaitable[None]]
Runner = Callable[[ProgressFn], Awaitable[dict[str, Any]]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRegistry:
    """An in-memory registry of background jobs for one feature (e.g. ``"knowme"``)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._jobs: dict[str, dict[str, Any]] = {}
        self._conds: dict[str, asyncio.Condition] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ---- internals --------------------------------------------------------------
    def _cond(self, key: str) -> asyncio.Condition:
        c = self._conds.get(key)
        if c is None:
            c = asyncio.Condition()
            self._conds[key] = c
        return c

    async def _append(self, key: str, level: str, message: str) -> None:
        job = self._jobs.get(key)
        if job is None:
            return
        seq = len(job["progress"])
        # ``phase`` mirrors ``level`` so SSE clients that key off a ``phase`` field (the legacy
        # inline-stream shape) work unchanged against a background-job stream.
        job["progress"].append({"seq": seq, "ts": _now(), "level": level, "phase": level, "message": message})
        cond = self._cond(key)
        async with cond:
            cond.notify_all()

    async def _finish(self, key: str, *, status: str, result: dict[str, Any] | None, error: str) -> None:
        job = self._jobs.get(key)
        if job is None:
            return
        job["status"] = status
        job["finished_at"] = _now()
        job["result"] = result
        job["error"] = error
        cond = self._cond(key)
        async with cond:
            cond.notify_all()

    # ---- public API -------------------------------------------------------------
    def get_job(self, key: str) -> dict[str, Any] | None:
        return self._jobs.get(key)

    def jobs_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """All jobs whose key starts with ``prefix`` (raw job dicts). Lets a caller surface
        every in-flight/recent job for a scope (e.g. one tenant) in a progress tray."""
        return [job for key, job in self._jobs.items() if key.startswith(prefix)]

    def is_running(self, key: str) -> bool:
        job = self._jobs.get(key)
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

    def start(self, key: str, runner: Runner) -> dict[str, Any]:
        """Start ``runner`` for ``key`` unless a job is already running for it (idempotency —
        this is the KP6 'don't double-generate' guard). Returns the (possibly in-flight) job."""
        existing = self._jobs.get(key)
        if existing and existing["status"] == "running":
            return existing

        job: dict[str, Any] = {
            "id": uuid.uuid4().hex[:16],
            "key": key,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "progress": [],
            "result": None,
            "error": "",
        }
        self._jobs[key] = job

        async def _run() -> None:
            async def _progress(level: str, message: str) -> None:
                await self._append(key, level, message)

            try:
                result = await runner(_progress)
                await self._finish(key, status="done", result=result, error="")
            except asyncio.CancelledError:  # task cancelled (e.g. shutdown) — mark and re-raise
                await self._finish(key, status="error", result=None, error="Generation was cancelled.")
                raise
            except Exception as exc:  # noqa: BLE001 - isolate the job
                log.exception("%s job failed (key=%s)", self.name, key)
                await self._finish(key, status="error", result=None, error=str(exc)[:300])

        task = asyncio.create_task(_run())
        self._tasks[key] = task
        task.add_done_callback(lambda _t: self._tasks.pop(key, None))
        return job

    async def stream(self, key: str):
        """Async generator of SSE-ready dicts: replay the progress log so far, then tail new
        lines until the job finishes. Safe to (re)attach at any time; the job runs regardless
        of subscribers, so a dropped connection never loses the result."""
        import json

        job = self._jobs.get(key)
        if job is None:
            yield {"event": "error", "data": json.dumps({"message": "No job for this key."})}
            return

        yield {"event": "start", "data": json.dumps({"id": job["id"], "status": job["status"], "started_at": job["started_at"]})}

        sent = 0
        cond = self._cond(key)
        while True:
            progress = job["progress"]
            while sent < len(progress):
                yield {"event": "status", "data": json.dumps(progress[sent])}
                sent += 1
            if job["status"] != "running":
                break
            async with cond:
                try:
                    await asyncio.wait_for(cond.wait(), timeout=20)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}

        progress = job["progress"]
        while sent < len(progress):
            yield {"event": "status", "data": json.dumps(progress[sent])}
            sent += 1

        if job["status"] == "done":
            yield {"event": "done", "data": json.dumps(job["result"] or {})}
        else:
            yield {"event": "error", "data": json.dumps({"message": job["error"] or "Generation failed."})}

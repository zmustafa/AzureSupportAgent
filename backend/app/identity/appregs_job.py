"""Background job manager for the (slow) Application Registrations refresh.

A live Entra enumeration can take 10–30 minutes on a large tenant, so the refresh runs as a
detached background ``asyncio`` task that keeps going even if the browser navigates away or
the SSE stream disconnects. The job records a granular progress log; SSE subscribers replay
the log so far and then tail new lines until the job finishes. When it completes the snapshot
is written to the permanent server cache.

One job per (tenant, connection) key — starting a refresh while one is already running just
returns the in-flight job.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("app.identity.appregs_job")

# key -> job dict. In-memory (a refresh is ephemeral; the RESULT persists in the cache).
_jobs: dict[str, dict[str, Any]] = {}
_conds: dict[str, asyncio.Condition] = {}
_tasks: dict[str, asyncio.Task] = {}  # hold task refs so they aren't garbage-collected


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cond(key: str) -> asyncio.Condition:
    c = _conds.get(key)
    if c is None:
        c = asyncio.Condition()
        _conds[key] = c
    return c


def get_job(key: str) -> dict[str, Any] | None:
    """Return the current job for a key (running or last-finished), or None."""
    return _jobs.get(key)


def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """A client-safe view of a job (omits the heavy result snapshot)."""
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
    }


def is_running(key: str) -> bool:
    job = _jobs.get(key)
    return bool(job and job["status"] == "running")


async def _append(key: str, level: str, message: str) -> None:
    job = _jobs[key]
    seq = len(job["progress"])
    job["progress"].append({"seq": seq, "ts": _now(), "level": level, "message": message})
    cond = _cond(key)
    async with cond:
        cond.notify_all()


async def _finish(key: str, *, status: str, result: dict[str, Any] | None, error: str) -> None:
    job = _jobs[key]
    job["status"] = status
    job["finished_at"] = _now()
    job["result"] = result
    job["error"] = error
    cond = _cond(key)
    async with cond:
        cond.notify_all()


def start_job(
    *, key: str, tenant_id: str, connection: dict[str, Any] | None, connection_id: str, limit: int = 200
) -> dict[str, Any]:
    """Start a background refresh for ``key`` if one isn't already running. Returns the job."""
    existing = _jobs.get(key)
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
    _jobs[key] = job

    async def _run() -> None:
        from app.identity import appregs, appregs_cache

        async def _progress(level: str, message: str) -> None:
            await _append(key, level, message)

        await _append(key, "info", "Starting Application Registrations refresh…")
        try:
            snap = await appregs.collect_app_registrations(
                connection, tenant_id=tenant_id, limit=limit, progress=_progress
            )
            fetched_at = appregs_cache.set_(tenant_id, connection_id, snap)
            # Shape the done payload like the GET response so the client can use it directly.
            result = {**snap, "cached": True, "never_loaded": False, "fetched_at": fetched_at, "age_seconds": 0}
            await _append(key, "ok", f"Cached snapshot — {snap.get('summary', {}).get('total', 0)} app registration(s).")
            await _finish(key, status="done", result=result, error="")
        except Exception as exc:  # noqa: BLE001 - record on the job, never crash the loop
            log.exception("app-registrations refresh job failed")
            await _append(key, "error", f"Refresh failed: {str(exc)[:300]}")
            await _finish(key, status="error", result=None, error=str(exc)[:300])

    task = asyncio.create_task(_run())
    _tasks[key] = task
    task.add_done_callback(lambda _t: _tasks.pop(key, None))
    return job


async def stream(key: str):
    """Async generator of SSE-ready dicts for a job: replays the progress log so far, then
    tails new lines until the job finishes (done/error). Safe to (re)attach at any time; the
    underlying job keeps running regardless of subscribers."""
    import json

    job = _jobs.get(key)
    if job is None:
        yield {"event": "error", "data": json.dumps({"message": "No refresh job for this scope."})}
        return

    yield {"event": "start", "data": json.dumps({"id": job["id"], "status": job["status"], "started_at": job["started_at"]})}

    sent = 0
    cond = _cond(key)
    while True:
        # Drain any progress lines we haven't sent yet.
        progress = job["progress"]
        while sent < len(progress):
            yield {"event": "progress", "data": json.dumps(progress[sent])}
            sent += 1

        if job["status"] != "running":
            break

        # Wait for the next notification (new progress or completion).
        async with cond:
            try:
                await asyncio.wait_for(cond.wait(), timeout=20)
            except asyncio.TimeoutError:
                # Heartbeat keeps the SSE connection alive during long quiet stretches.
                yield {"event": "ping", "data": "{}"}

    # Flush any final lines appended alongside completion.
    progress = job["progress"]
    while sent < len(progress):
        yield {"event": "progress", "data": json.dumps(progress[sent])}
        sent += 1

    if job["status"] == "done":
        yield {"event": "done", "data": json.dumps(job["result"] or {})}
    else:
        yield {"event": "error", "data": json.dumps({"message": job["error"] or "Refresh failed."})}

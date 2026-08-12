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

# Active execution is in-memory; page checkpoints and completed results are durable.
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


def is_running(key: str) -> bool:
    job = _jobs.get(key)
    return bool(job and job["status"] in ("running", "cancelling"))


async def _append(key: str, level: str, message: str, metadata: dict[str, Any] | None = None) -> None:
    job = _jobs[key]
    seq = len(job["progress"])
    meta = metadata or {}
    entry = {"seq": seq, "ts": _now(), "level": level, "message": message, **meta}
    job["progress"].append(entry)
    for field in ("current", "total", "percent", "page", "retries", "throttles", "resumed"):
        if field in meta:
            job[field] = meta[field]
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
    *,
    key: str,
    tenant_id: str,
    connection: dict[str, Any] | None,
    connection_id: str,
    limit: int = 500,
    mode: str = "capped",
    page_size: int = 250,
) -> dict[str, Any]:
    """Start a background refresh for ``key`` if one isn't already running. Returns the job."""
    from app.identity import appregs, appregs_cache

    existing = _jobs.get(key)
    if existing and existing["status"] in ("running", "cancelling"):
        return existing

    requested_mode = "full" if mode == "full" else "capped"
    saved_checkpoint = appregs_cache.get_checkpoint(tenant_id, connection_id)
    checkpoint_reset_reason = ""
    if (
        saved_checkpoint
        and saved_checkpoint.get("schema") == appregs.APPREGS_CHECKPOINT_SCHEMA
        and saved_checkpoint.get("mode") == requested_mode
    ):
        # Resume the job exactly as it began even if an admin changed the normal cap while
        # the process was down. A continuation belongs to its original query boundary.
        limit = max(50, min(5000, int(
            saved_checkpoint.get("configured_limit")
            or (saved_checkpoint.get("target_limit") if requested_mode == "capped" else limit)
            or limit
        )))
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

    job: dict[str, Any] = {
        "id": uuid.uuid4().hex[:16],
        "key": key,
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "progress": [],
        "result": None,
        "error": "",
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
        "resume_available": False,
        "cancel_requested": False,
    }
    _jobs[key] = job

    async def _run() -> None:
        from app.identity import appregs, appregs_cache

        checkpoint = appregs_cache.get_checkpoint(tenant_id, connection_id)
        expected_target = appregs.APPREGS_FULL_SAFETY_LIMIT if job["mode"] == "full" else limit
        if checkpoint and (
            checkpoint.get("schema") != appregs.APPREGS_CHECKPOINT_SCHEMA
            or checkpoint.get("mode") != job["mode"]
            or int(checkpoint.get("target_limit") or 0) != expected_target
            or int(checkpoint.get("page_size") or 0) != page_size
        ):
            appregs_cache.delete_checkpoint(tenant_id, connection_id)
            checkpoint = None
        job["resumed"] = bool(checkpoint)

        async def _progress(level: str, message: str, metadata: dict[str, Any] | None = None) -> None:
            await _append(key, level, message, metadata)

        async def _checkpoint(state: dict[str, Any]) -> None:
            appregs_cache.set_checkpoint(
                tenant_id,
                connection_id,
                {**state, "job_id": job["id"], "started_at": job["started_at"]},
            )
            job["resume_available"] = True

        await _append(key, "info", "Starting Application Registrations refresh…")
        if checkpoint_reset_reason:
            await _append(key, "warn", checkpoint_reset_reason, {"phase": "restart"})
        try:
            snap = await appregs.collect_app_registrations(
                connection,
                tenant_id=tenant_id,
                limit=limit,
                full=job["mode"] == "full",
                page_size=page_size,
                checkpoint=checkpoint,
                on_checkpoint=_checkpoint,
                progress=_progress,
                should_cancel=lambda: bool(job.get("cancel_requested")),
            )
            if snap.get("source") == "unavailable":
                raise RuntimeError("provider_unavailable")
            fetched_at = appregs_cache.set_(tenant_id, connection_id, snap)
            appregs_cache.delete_checkpoint(tenant_id, connection_id)
            job["resume_available"] = False
            # Shape the done payload like the GET response so the client can use it directly.
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
            await _append(key, "ok", f"Cached snapshot — {snap.get('summary', {}).get('total', 0)} app registration(s).")
            await _finish(key, status="done", result=result, error="")
        except asyncio.CancelledError:
            job["resume_available"] = appregs_cache.get_checkpoint(tenant_id, connection_id) is not None
            await _append(key, "warn", "Refresh cancelled. Completed pages were checkpointed; the previous snapshot is unchanged.")
            await _finish(key, status="cancelled", result=None, error="Refresh cancelled.")
        except Exception as exc:  # noqa: BLE001 - record bounded status, never provider details
            log.warning("app-registrations refresh job failed: %s", type(exc).__name__)
            job["resume_available"] = appregs_cache.get_checkpoint(tenant_id, connection_id) is not None
            message = "Refresh failed. The previous completed snapshot was preserved."
            await _append(key, "error", message)
            await _finish(key, status="error", result=None, error=message)

    task = asyncio.create_task(_run())
    _tasks[key] = task
    task.add_done_callback(lambda _t: _tasks.pop(key, None))
    return job


def cancel_job(key: str) -> bool:
    """Request cancellation of an active job. Its last completed page remains resumable."""
    job = _jobs.get(key)
    if not job or job.get("status") not in ("running", "cancelling"):
        return False
    job["cancel_requested"] = True
    job["status"] = "cancelling"
    task = _tasks.get(key)
    if task and not task.done():
        task.cancel()
    return True


def recoverable_job(tenant_id: str, connection_id: str) -> dict[str, Any] | None:
    """Public paused-job shape reconstructed from a durable process-restart checkpoint."""
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
        "configured_limit": checkpoint.get("configured_limit") or (
            checkpoint.get("target_limit") if checkpoint.get("mode") == "capped" else 500
        ),
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
    """Async generator of SSE-ready dicts for a job: replays the progress log so far, then
    tails new lines until the job finishes (done/error). Safe to (re)attach at any time; the
    underlying job keeps running regardless of subscribers."""
    import json

    job = _jobs.get(key)
    if job is None:
        yield {"event": "error", "data": json.dumps({"message": "No refresh job for this scope."})}
        return

    yield {"event": "start", "data": json.dumps({
        "id": job["id"], "status": job["status"], "started_at": job["started_at"],
        "mode": job.get("mode", "capped"), "configured_limit": job.get("configured_limit", 500),
        "current": job.get("current", 0), "total": job.get("total"),
        "page": job.get("page", 0), "resumed": bool(job.get("resumed", False)),
    })}

    sent = 0
    cond = _cond(key)
    while True:
        # Drain any progress lines we haven't sent yet.
        progress = job["progress"]
        while sent < len(progress):
            yield {"event": "progress", "data": json.dumps(progress[sent])}
            sent += 1

        if job["status"] not in ("running", "cancelling"):
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
    elif job["status"] == "cancelled":
        yield {"event": "cancelled", "data": json.dumps({"message": job["error"], "resume_available": job.get("resume_available", False)})}
    else:
        yield {"event": "error", "data": json.dumps({"message": job["error"] or "Refresh failed."})}

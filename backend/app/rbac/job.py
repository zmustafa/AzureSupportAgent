"""Background per-scope refresh jobs + SSE progress for the RBAC dashboard.

A full access refresh can take minutes (the scanner sample spent ~8 minutes across 21 subs), so
a refresh runs as a detached ``asyncio`` task keyed by ``(tenant, scope)``: starting a refresh
for a scope that's already running just returns the in-flight job, and disconnecting the SSE
stream never stops it. Different scopes refresh concurrently. Clones the proven
``identity/appregs_job`` pattern (progress log + ``asyncio.Condition`` fan-out + replay-then-tail
stream)."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.rbac import orchestrator

log = logging.getLogger("app.rbac.job")

# Sentinel scope keys for the non-single-scope refresh modes.
SCOPE_ALL = "__all__"
SCOPE_DIRECTORY = "directory"

_jobs: dict[str, dict[str, Any]] = {}
_conds: dict[str, asyncio.Condition] = {}
_tasks: dict[str, asyncio.Task] = {}


def job_key(tenant_id: str, scope: str) -> str:
    return f"{tenant_id or 'default'}|{scope}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cond(key: str) -> asyncio.Condition:
    c = _conds.get(key)
    if c is None:
        c = asyncio.Condition()
        _conds[key] = c
    return c


def get_job(key: str) -> dict[str, Any] | None:
    return _jobs.get(key)


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


async def _finish(key: str, *, status: str, error: str = "") -> None:
    job = _jobs[key]
    job["status"] = status
    job["finished_at"] = _now()
    job["error"] = error
    cond = _cond(key)
    async with cond:
        cond.notify_all()


def start_job(
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
    """Start (or return the in-flight) background refresh for ``(tenant, scope)``.

    ``mode`` is one of ``scope`` (single subscription/MG), ``directory`` (Entra layer), or
    ``all`` (every subscription + directory). On success a compact :class:`RbacScanRun` history
    point is recorded (drift) unless ``record_run`` is False."""
    key = job_key(tenant_id, scope)
    existing = _jobs.get(key)
    if existing and existing["status"] == "running":
        return existing

    job: dict[str, Any] = {
        "id": uuid.uuid4().hex[:16],
        "key": key,
        "scope": scope,
        "mode": mode,
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "progress": [],
        "error": "",
    }
    _jobs[key] = job

    async def _progress(level: str, message: str) -> None:
        await _append(key, level, message)

    async def _run() -> None:
        try:
            if mode == "all":
                await orchestrator.refresh_all(tenant_id, connection, progress=_progress)
            elif mode == "directory":
                await orchestrator.refresh_directory(tenant_id, connection, progress=_progress)
            else:
                await orchestrator.refresh_scope(
                    tenant_id, connection, scope, display_name=display_name, progress=_progress
                )
            if record_run:
                try:
                    from app.rbac import store

                    await store.save_run(
                        tenant_id, connection_id=connection_id, scope=scope, trigger="manual", triggered_by=triggered_by
                    )
                except Exception:  # noqa: BLE001 - history is best-effort, never fail the refresh
                    log.warning("rbac run history record failed", exc_info=True)
            await _finish(key, status="done")
        except Exception as exc:  # noqa: BLE001 - record on the job, never crash the loop
            log.exception("rbac refresh job failed")
            await _append(key, "error", f"Refresh failed: {str(exc)[:300]}")
            await _finish(key, status="error", error=str(exc)[:300])

    task = asyncio.create_task(_run())
    _tasks[key] = task
    task.add_done_callback(lambda _t: _tasks.pop(key, None))
    return job


async def stream(key: str):
    """SSE generator: replay the progress log so far, then tail until the job completes."""
    job = _jobs.get(key)
    if job is None:
        yield {"event": "error", "data": json.dumps({"message": "No refresh job for this scope."})}
        return

    yield {"event": "start", "data": json.dumps(public_job(job) or {})}

    sent = 0
    cond = _cond(key)
    while True:
        progress = job["progress"]
        while sent < len(progress):
            yield {"event": "progress", "data": json.dumps(progress[sent])}
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
        yield {"event": "progress", "data": json.dumps(progress[sent])}
        sent += 1

    if job["status"] == "done":
        yield {"event": "done", "data": json.dumps({"key": key, "scope": job["scope"], "mode": job["mode"]})}
    else:
        yield {"event": "error", "data": json.dumps({"message": job["error"] or "Refresh failed."})}

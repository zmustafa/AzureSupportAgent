"""Background Entra refresh jobs with SSE progress.

Clones the proven ``rbac/job.py`` / ``identity/appregs_job.py`` pattern: a detached
``asyncio`` task keyed by ``(tenant, scope)``, a progress log fanned out through an
``asyncio.Condition``, and a replay-then-tail stream so a browser reload re-attaches
instead of losing the run. Disconnecting the SSE stream never stops the job.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("app.entra.job")

SCOPE_ALL = "__all__"

_jobs: dict[str, dict[str, Any]] = {}
_conds: dict[str, asyncio.Condition] = {}
_tasks: dict[str, asyncio.Task] = {}


def job_key(tenant_id: str, scope: str = SCOPE_ALL) -> str:
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


def is_running(key: str) -> bool:
    job = _jobs.get(key)
    return bool(job and job["status"] == "running")


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


async def _append(key: str, level: str, message: str) -> None:
    job = _jobs.get(key)
    if job is None:
        return
    job["progress"].append({"seq": len(job["progress"]), "ts": _now(), "level": level, "message": message})
    cond = _cond(key)
    async with cond:
        cond.notify_all()


async def _finish(key: str, *, status: str, error: str = "") -> None:
    job = _jobs.get(key)
    if job is None:
        return
    job["status"] = status
    job["finished_at"] = _now()
    job["error"] = error
    cond = _cond(key)
    async with cond:
        cond.notify_all()


async def _backfill_signin_outcomes(
    tenant_id: str,
    connection: dict[str, Any] | None,
    domains: list[str] | None,
    progress,
) -> None:
    """Read per-application sign-in outcomes after the refresh, not inside it.

    Detached because the reads are slow and overwhelmingly empty; a failure here leaves the
    snapshot exactly as the refresh built it.
    """
    if domains and "apps" not in domains:
        return
    try:
        from app.entra import signin_outcomes

        await signin_outcomes.run_backfill(tenant_id, connection, progress=progress)
    except Exception:  # noqa: BLE001 - the refresh already succeeded; this is enrichment
        log.warning("sign-in outcome backfill failed", exc_info=True)


def start_job(
    *,
    tenant_id: str,
    connection: dict[str, Any] | None,
    domains: list[str] | None = None,
    connection_id: str = "",
) -> dict[str, Any]:
    """Start (or return the in-flight) refresh for this tenant."""
    key = job_key(tenant_id)
    existing = _jobs.get(key)
    if existing and existing["status"] == "running":
        return existing

    job: dict[str, Any] = {
        "id": uuid.uuid4().hex[:16],
        "key": key,
        "scope": SCOPE_ALL,
        "domains": list(domains or []),
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
        from app.entra import snapshot as snapshot_mod

        try:
            result = await snapshot_mod.refresh(
                tenant_id, connection, domains=domains,
                connection_id=connection_id, progress=_progress,
            )
            if result.get("ok"):
                await _finish(key, status="done")
                await _backfill_signin_outcomes(tenant_id, connection, domains, _progress)
            else:
                await _finish(key, status="error", error=str(result.get("error") or "Refresh failed."))
        except Exception as exc:  # noqa: BLE001 - record on the job, never crash the loop
            log.exception("entra refresh job failed")
            await _append(key, "error", f"Refresh failed: {str(exc)[:300]}")
            await _finish(key, status="error", error=str(exc)[:300])

    task = asyncio.create_task(_run())
    _tasks[key] = task
    task.add_done_callback(lambda _t: _tasks.pop(key, None))
    return job


async def stream(key: str):
    """SSE generator: replay the log so far, then tail until the job completes."""
    job = _jobs.get(key)
    if job is None:
        yield {"event": "error", "data": json.dumps({"message": "No refresh job for this tenant."})}
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
        yield {"event": "done", "data": json.dumps({"key": key, "domains": job["domains"]})}
    else:
        yield {"event": "error", "data": json.dumps({"message": job["error"] or "Refresh failed."})}

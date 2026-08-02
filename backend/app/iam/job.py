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
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.iam import orchestrator, progress as progress_mod

log = logging.getLogger("app.iam.job")

# Sentinel scope keys for the non-single-scope refresh modes.
SCOPE_ALL = "__all__"
SCOPE_DIRECTORY = "directory"

# How often the stream emits a clock tick when the job is producing no messages. Short enough
# that a progress bar moves visibly; long enough not to be chatty over a multi-minute refresh.
_TICK_SECONDS = 3

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


def _elapsed(job: dict[str, Any]) -> float:
    """Seconds since the job started, from a MONOTONIC clock.

    Not wall-clock arithmetic on `started_at`: a refresh can outlive an NTP correction or a DST
    change, and an elapsed counter that jumps backwards during a four-minute scan is worse than
    no counter."""
    end = job.get("finished_monotonic")
    return float((end if end is not None else time.monotonic()) - job["started_monotonic"])


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


def is_running(key: str) -> bool:
    job = _jobs.get(key)
    return bool(job and job["status"] == "running")


async def _append(key: str, level: str, message: str) -> None:
    job = _jobs[key]
    seq = len(job["progress"])
    # Every line carries the clock. A progress log without elapsed time reads identically at
    # five seconds and five minutes, which is when people start reloading the page and firing
    # off a second refresh.
    job["progress"].append({
        "seq": seq, "ts": _now(), "level": level, "message": message,
        **progress_mod.public(job["tenant_id"], job["mode"], _elapsed(job)),
    })
    cond = _cond(key)
    async with cond:
        cond.notify_all()


async def _finish(key: str, *, status: str, error: str = "") -> None:
    job = _jobs[key]
    job["status"] = status
    job["finished_at"] = _now()
    job["finished_monotonic"] = time.monotonic()
    job["error"] = error
    if status == "done":
        # Only successful runs feed the estimate. A refresh that died after four seconds would
        # otherwise teach the estimator that this tenant takes four seconds.
        try:
            progress_mod.record(job["tenant_id"], job["mode"], _elapsed(job))
        except Exception:  # noqa: BLE001 - an estimate is never worth failing a refresh for
            log.warning("iam job: could not record run duration", exc_info=True)
    cond = _cond(key)
    async with cond:
        cond.notify_all()


async def _warm_derived(tenant_id: str, progress) -> None:
    """Rebuild the derived caches the collection just invalidated, INSIDE the job.

    A refresh changes the rows, which correctly invalidates the escalation graph. Left alone,
    the next person to open Findings pays the 30-to-60-second rebuild as an unexplained spinner
    — the work simply moves from a screen that reports progress to one that does not.

    Doing it here puts the cost inside the job that already has a progress log, an elapsed
    counter and an estimate. It also means the estimate covers the *whole* real cost of a
    refresh rather than only the collection half, which is what makes the number trustworthy.

    Off the event loop, and never fatal: a warm cache is an optimisation, and failing a
    successful collection because an optimisation failed would be a poor trade."""
    from app.iam import cache, compose, effective, escalation

    await progress("info", "Rebuilding the escalation graph…")
    started = time.monotonic()
    try:
        rows = compose.build_master_rows(tenant_id)
        directory = cache.read_directory(tenant_id)
        graph = await asyncio.to_thread(
            escalation.graph_for_tenant,
            tenant_id, rows,
            effective.build_role_index(directory.get("role_defs", [])),
            identities=directory.get("identities", {}),
            federated=directory.get("federated", []),
        )
    except Exception:  # noqa: BLE001 - the collection succeeded; this is a cache warm-up
        log.warning("iam job: could not warm the escalation graph", exc_info=True)
        await progress("warning", "The escalation graph could not be pre-built; the next screen "
                                  "that needs it will build it instead.")
        return
    await progress(
        "ok",
        f"Escalation graph ready in {time.monotonic() - started:.0f}s "
        f"({len(graph.get('nodes') or [])} nodes, {len(graph.get('edges') or [])} edges).",
    )


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

    ``mode`` is one of ``scope`` (single subscription/MG), ``directory`` (Entra layer),
    ``all`` (every subscription + directory), or ``delta`` (only subscriptions with
    authorization activity since their own last collection; falls back to a full refresh when
    the change feed cannot answer). On success a compact :class:`IamScanRun` history point is
    recorded (drift) unless ``record_run`` is False."""
    key = job_key(tenant_id, scope)
    existing = _jobs.get(key)
    if existing and existing["status"] == "running":
        return existing

    job: dict[str, Any] = {
        "id": uuid.uuid4().hex[:16],
        "key": key,
        "tenant_id": tenant_id,
        "scope": scope,
        "mode": mode,
        "status": "running",
        "started_at": _now(),
        "started_monotonic": time.monotonic(),
        "finished_at": None,
        "finished_monotonic": None,
        "progress": [],
        "error": "",
    }
    _jobs[key] = job

    async def _progress(level: str, message: str) -> None:
        await _append(key, level, message)

    async def _run() -> None:
        try:
            if mode in ("all", "delta"):
                await orchestrator.refresh_all(
                    tenant_id, connection, progress=_progress, mode="delta" if mode == "delta" else "full"
                )
            elif mode == "directory":
                await orchestrator.refresh_directory(tenant_id, connection, progress=_progress)
            else:
                await orchestrator.refresh_scope(
                    tenant_id, connection, scope, display_name=display_name, progress=_progress
                )
            await _warm_derived(tenant_id, _progress)
            if record_run:
                try:
                    from app.iam import store

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
                await asyncio.wait_for(cond.wait(), timeout=_TICK_SECONDS)
            except asyncio.TimeoutError:
                # A heartbeat that carries the CLOCK. Collecting one large subscription can run
                # for minutes without emitting a message, and during that gap a bare `ping` and
                # a hung job look exactly the same to the person watching. Sending the elapsed
                # time and the estimate keeps the indicator alive between messages.
                yield {
                    "event": "tick",
                    "data": json.dumps({
                        "status": job["status"],
                        "last_message": job["progress"][-1]["message"] if job["progress"] else "",
                        **progress_mod.public(job["tenant_id"], job["mode"], _elapsed(job)),
                    }),
                }

    progress = job["progress"]
    while sent < len(progress):
        yield {"event": "progress", "data": json.dumps(progress[sent])}
        sent += 1

    if job["status"] == "done":
        # No ETA on the final event. The job is over, so "0s remaining" is at best noise and at
        # worst reads as though something is still pending; the total elapsed is the fact that
        # matters once it has finished.
        timing = progress_mod.public(job["tenant_id"], job["mode"], _elapsed(job))
        yield {
            "event": "done",
            "data": json.dumps({
                "key": key, "scope": job["scope"], "mode": job["mode"],
                "elapsed_seconds": timing["elapsed_seconds"],
                "elapsed_label": timing["elapsed_label"],
                "eta_seconds": None,
                "eta_label": "—",
                "eta_basis": f"completed in {timing['elapsed_label']}",
                "typical_seconds": timing["typical_seconds"],
            }),
        }
    else:
        yield {"event": "error", "data": json.dumps({"message": job["error"] or "Refresh failed."})}

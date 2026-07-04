"""In-memory job registry for background Insight Pack runs.

An on-demand run (or re-run) is executed as a background asyncio task so the HTTP request
returns immediately and the UI can poll for *detailed progress* while the four-stage loop
(gather → reason → gate → deliver) executes server-side. Jobs are process-local and best
kept short-lived; the durable result is the persisted digest in ``runs`` — a job only tracks
progress and the final run payload for the polling UI.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

# Keep the registry bounded so a long-lived process doesn't leak completed jobs.
_MAX_JOBS = 200
_MAX_STEPS = 40
_JOBS: dict[str, dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _prune() -> None:
    if len(_JOBS) <= _MAX_JOBS:
        return
    # Drop the oldest finished jobs first, then oldest overall.
    ordered = sorted(_JOBS.values(), key=lambda j: j.get("updated_at", 0.0))
    for job in ordered:
        if len(_JOBS) <= _MAX_JOBS:
            break
        _JOBS.pop(job["id"], None)


def create(tenant_id: str, *, pack_name: str = "", scope_label: str = "") -> dict[str, Any]:
    """Create a queued job and return it."""
    job = {
        "id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "pack_name": pack_name,
        "scope_label": scope_label,
        "status": "queued",  # queued | running | succeeded | failed
        "stage": "queued",
        "label": "Queued…",
        "pct": 0,
        "steps": [],  # [{ts, stage, label, detail, state}]
        "run": None,
        "error": None,
        "started_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
    }
    _JOBS[job["id"]] = job
    _prune()
    return job


def get(tenant_id: str, job_id: str) -> dict[str, Any] | None:
    job = _JOBS.get(job_id)
    if job is None or job.get("tenant_id") != tenant_id:
        return None
    return job


def snapshot(job: dict[str, Any]) -> dict[str, Any]:
    """A JSON-serializable view of the job for the polling API."""
    return {
        "id": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "label": job["label"],
        "pct": job["pct"],
        "steps": list(job["steps"]),
        "run": job["run"],
        "error": job["error"],
        "pack_name": job.get("pack_name", ""),
        "scope_label": job.get("scope_label", ""),
    }


def progress(job: dict[str, Any], *, stage: str, label: str, detail: str = "",
             pct: int | None = None, state: str = "done") -> None:
    """Record a progress milestone on the job."""
    if pct is not None:
        job["pct"] = max(0, min(100, int(pct)))
    job["stage"] = stage
    job["label"] = label
    job["status"] = "running"
    job["updated_at"] = _now()
    steps = job["steps"]
    steps.append({"ts": _now(), "stage": stage, "label": label, "detail": detail, "state": state})
    if len(steps) > _MAX_STEPS:
        del steps[0 : len(steps) - _MAX_STEPS]


def finish(job: dict[str, Any], run: dict[str, Any]) -> None:
    job["run"] = run
    job["status"] = "succeeded"
    job["stage"] = "done"
    job["label"] = "Digest ready"
    job["pct"] = 100
    job["updated_at"] = job["finished_at"] = _now()
    job["steps"].append({"ts": _now(), "stage": "done", "label": "Digest ready", "detail": "", "state": "done"})


def fail(job: dict[str, Any], error: str) -> None:
    job["error"] = str(error)[:500]
    job["status"] = "failed"
    job["stage"] = "error"
    job["label"] = "Run failed"
    job["updated_at"] = job["finished_at"] = _now()
    job["steps"].append({"ts": _now(), "stage": "error", "label": "Run failed", "detail": str(error)[:300], "state": "error"})

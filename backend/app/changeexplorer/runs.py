"""Persistent history of Change Explorer analysis runs, per (tenant, workload).

Stored as JSON on the data volume (``backend/.data/changeexplorer_runs.json``), newest-first,
bounded per workload, with soft-delete (trash) — mirrors perfprofile.runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "changeexplorer_runs.json"
_MAX_PER_WORKLOAD = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    return {k: run.get(k) for k in (
        "runId", "tenantId", "workloadId", "workloadName", "startTime", "endTime", "scopeMode",
        "requestedBy", "createdAt", "completedAt", "status", "totalChanges", "criticalCount",
        "highCount", "mediumCount", "lowCount", "informationalCount", "demo", "deleted_at",
    )}


def save_run(tenant_id: str, workload_id: str, run: dict[str, Any]) -> dict[str, Any]:
    run_id = str(run.get("runId") or "")

    def _mutate(data: dict[str, Any]) -> None:
        bucket = data.setdefault(tenant_id or "default", {})
        runs = bucket.setdefault(workload_id or "default", [])
        replaced = False
        if run_id:
            for index, existing in enumerate(runs):
                if existing.get("runId") == run_id:
                    runs[index] = run
                    replaced = True
                    break
        if not replaced:
            runs.insert(0, run)
        active = [i for i, stored in enumerate(runs) if not stored.get("deleted_at")]
        if len(active) > _MAX_PER_WORKLOAD:
            for i in sorted(active[_MAX_PER_WORKLOAD:], reverse=True):
                del runs[i]

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return run


def list_runs(tenant_id: str, workload_id: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    bucket = _read().get(tenant_id or "default", {})
    runs = bucket.get(workload_id or "default", [])
    return [_summary(r) for r in runs if include_deleted or not r.get("deleted_at")]


def list_trashed(tenant_id: str, workload_id: str) -> list[dict[str, Any]]:
    bucket = _read().get(tenant_id or "default", {})
    runs = bucket.get(workload_id or "default", [])
    out = [_summary(r) for r in runs if r.get("deleted_at")]
    out.sort(key=lambda r: r.get("deleted_at", ""), reverse=True)
    return out


def latest_runs_for_workloads(
    tenant_id: str, workload_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Latest non-trashed run SUMMARY per workload, reading the store ONCE (no N+1 reads).

    Returns a map of ``workload_id`` → run summary for the workloads that have at least one
    active run; workloads with no runs are simply absent from the result."""
    bucket = _read().get(tenant_id or "default", {})
    out: dict[str, dict[str, Any]] = {}
    for wid in workload_ids:
        for r in bucket.get(wid or "default", []):  # newest-first; first active wins
            if not r.get("deleted_at"):
                out[wid] = _summary(r)
                break
    return out


def get_run(tenant_id: str, run_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    bucket = _read().get(tenant_id or "default", {})
    for runs in bucket.values():
        for r in runs:
            if r.get("runId") == run_id:
                if r.get("deleted_at") and not include_deleted:
                    return None
                return r
    return None


def update_run(tenant_id: str, run: dict[str, Any]) -> bool:
    """Replace an existing run (matched by ``runId``) in place, preserving its list position and
    any soft-delete marker. Used to persist an AI re-enrichment of an already-stored run. Returns
    True when the run was found and updated."""
    rid = run.get("runId", "")
    if not rid:
        return False
    updated = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal updated
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for index, existing in enumerate(runs):
                if existing.get("runId") == rid:
                    if existing.get("deleted_at") and "deleted_at" not in run:
                        run["deleted_at"] = existing["deleted_at"]
                    runs[index] = run
                    updated = True
                    return

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return updated


def set_case(tenant_id: str, run_id: str, case: dict[str, Any]) -> dict[str, Any] | None:
    """Persist the investigator 'case file' for a run (D1): pinned change ids + per-change notes +
    a free-text case summary. ``case`` = {pinned: [changeId], notes: {changeId: text},
    caseSummary: str}. Returns the saved case, or None if the run wasn't found."""
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("runId") != run_id:
                    continue
                existing = run.get("caseFile") or {}
                merged = {
                    "pinned": list(case.get("pinned", existing.get("pinned", []))),
                    "notes": {**(existing.get("notes") or {}), **(case.get("notes") or {})},
                    "caseSummary": case.get("caseSummary", existing.get("caseSummary", "")),
                    "updatedAt": _now(),
                }
                # Drop empty notes so they don't accumulate.
                merged["notes"] = {k: v for k, v in merged["notes"].items() if (v or "").strip()}
                run["caseFile"] = merged
                result.update(merged)
                return

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return result or None


def soft_delete(tenant_id: str, run_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("runId") == run_id and not run.get("deleted_at"):
                    run["deleted_at"] = _now()
                    deleted = True
                    return

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return deleted


def restore(tenant_id: str, run_id: str) -> bool:
    restored = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal restored
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("runId") == run_id and run.get("deleted_at"):
                    run["deleted_at"] = ""
                    restored = True
                    return

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return restored


def purge(tenant_id: str, run_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for index, run in enumerate(runs):
                if run.get("runId") == run_id:
                    del runs[index]
                    deleted = True
                    return

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return deleted


# --------------------------------------------------------------------------- cleanup
def _run_size(run: dict[str, Any]) -> int:
    try:
        return len(json.dumps(run, default=str))
    except (TypeError, ValueError):
        return 0


def list_all_runs(tenant_id: str) -> list[dict[str, Any]]:
    """Every change-analysis run across EVERY workload (active + trashed), NORMALIZED to the
    shared cleanup shape (scope_kind/scope_id/scope_name/run_at + size_bytes). Newest-first."""
    bucket = _read().get(tenant_id or "default", {})
    out: list[dict[str, Any]] = []
    for runs in bucket.values():
        for r in runs:
            out.append({
                "id": r.get("runId", ""),
                "scope_kind": "workload",
                "scope_id": r.get("workloadId", ""),
                "scope_name": r.get("workloadName") or r.get("workloadId", ""),
                "run_at": r.get("completedAt") or r.get("createdAt") or "",
                "total_changes": r.get("totalChanges", 0),
                "critical_count": r.get("criticalCount", 0),
                "status": r.get("status", ""),
                "demo": bool(r.get("demo", False)),
                "size_bytes": _run_size(r),
                "deleted_at": r.get("deleted_at", ""),
            })
    out.sort(key=lambda r: r.get("run_at", ""), reverse=True)
    return out


def cleanup_stats(tenant_id: str) -> dict[str, Any]:
    runs = list_all_runs(tenant_id)
    active = [r for r in runs if not r.get("deleted_at")]
    trashed = [r for r in runs if r.get("deleted_at")]
    scopes = {r.get("scope_id") for r in runs}
    oldest = min((r.get("run_at", "") for r in active if r.get("run_at")), default="")
    return {
        "total_runs": len(runs),
        "active_runs": len(active),
        "trashed_runs": len(trashed),
        "total_bytes": sum(r.get("size_bytes", 0) for r in runs),
        "trashed_bytes": sum(r.get("size_bytes", 0) for r in trashed),
        "scopes": len(scopes),
        "oldest_run_at": oldest,
    }


def trash_runs(tenant_id: str, ids: list[str]) -> dict[str, int]:
    idset = {i for i in ids if i}
    count = 0
    freed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal count, freed
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("runId") in idset and not run.get("deleted_at"):
                    run["deleted_at"] = _now()
                    count += 1
                    freed += _run_size(run)

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return {"count": count, "freed_bytes": freed}


def restore_runs(tenant_id: str, ids: list[str]) -> dict[str, int]:
    idset = {i for i in ids if i}
    count = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal count
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("runId") in idset and run.get("deleted_at"):
                    run["deleted_at"] = ""
                    count += 1

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return {"count": count}


def purge_runs(tenant_id: str, ids: list[str]) -> dict[str, int]:
    idset = {i for i in ids if i}
    count = 0
    freed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal count, freed
        bucket = data.get(tenant_id or "default", {})
        for key in list(bucket):
            keep: list[dict[str, Any]] = []
            for run in bucket[key]:
                if run.get("runId") in idset:
                    count += 1
                    freed += _run_size(run)
                else:
                    keep.append(run)
            bucket[key] = keep

    jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    return {"count": count, "freed_bytes": freed}

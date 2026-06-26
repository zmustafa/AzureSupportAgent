"""Persistent history of Change Explorer analysis runs, per (tenant, workload).

Stored as JSON on the data volume (``backend/.data/changeexplorer_runs.json``), newest-first,
bounded per workload, with soft-delete (trash) — mirrors perfprofile.runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "changeexplorer_runs.json"
_MAX_PER_WORKLOAD = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data), encoding="utf-8")


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    return {k: run.get(k) for k in (
        "runId", "tenantId", "workloadId", "workloadName", "startTime", "endTime", "scopeMode",
        "requestedBy", "createdAt", "completedAt", "status", "totalChanges", "criticalCount",
        "highCount", "mediumCount", "lowCount", "informationalCount", "demo", "deleted_at",
    )}


def save_run(tenant_id: str, workload_id: str, run: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    bucket = data.setdefault(tenant_id or "default", {})
    runs = bucket.setdefault(workload_id or "default", [])
    runs.insert(0, run)
    active = [i for i, r in enumerate(runs) if not r.get("deleted_at")]
    if len(active) > _MAX_PER_WORKLOAD:
        for i in sorted(active[_MAX_PER_WORKLOAD:], reverse=True):
            del runs[i]
    _write(data)
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
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for runs in bucket.values():
        for i, r in enumerate(runs):
            if r.get("runId") == rid:
                if r.get("deleted_at") and "deleted_at" not in run:
                    run["deleted_at"] = r["deleted_at"]
                runs[i] = run
                _write(data)
                return True
    return False


def set_case(tenant_id: str, run_id: str, case: dict[str, Any]) -> dict[str, Any] | None:
    """Persist the investigator 'case file' for a run (D1): pinned change ids + per-change notes +
    a free-text case summary. ``case`` = {pinned: [changeId], notes: {changeId: text},
    caseSummary: str}. Returns the saved case, or None if the run wasn't found."""
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for runs in bucket.values():
        for r in runs:
            if r.get("runId") == run_id:
                existing = r.get("caseFile") or {}
                merged = {
                    "pinned": list(case.get("pinned", existing.get("pinned", []))),
                    "notes": {**(existing.get("notes") or {}), **(case.get("notes") or {})},
                    "caseSummary": case.get("caseSummary", existing.get("caseSummary", "")),
                    "updatedAt": _now(),
                }
                # Drop empty notes so they don't accumulate.
                merged["notes"] = {k: v for k, v in merged["notes"].items() if (v or "").strip()}
                r["caseFile"] = merged
                _write(data)
                return merged
    return None


def soft_delete(tenant_id: str, run_id: str) -> bool:
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for runs in bucket.values():
        for r in runs:
            if r.get("runId") == run_id and not r.get("deleted_at"):
                r["deleted_at"] = _now()
                _write(data)
                return True
    return False


def restore(tenant_id: str, run_id: str) -> bool:
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for runs in bucket.values():
        for r in runs:
            if r.get("runId") == run_id and r.get("deleted_at"):
                r["deleted_at"] = ""
                _write(data)
                return True
    return False


def purge(tenant_id: str, run_id: str) -> bool:
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for key, runs in bucket.items():
        for i, r in enumerate(runs):
            if r.get("runId") == run_id:
                del runs[i]
                _write(data)
                return True
    return False

"""Persistent history of coverage scans, per (feature, tenant, scope).

A shared run-history store for the three coverage dashboards — Monitoring (``amba``),
Telemetry (``telemetry``) and Backup & DR (``backupdr``) — mirroring
``app/perfprofile/runs.py`` for the Performance Profiler. Each "Refresh now" persists the
full snapshot here so operators can review past scans, re-open them, and delete them.

Stored on the Azure Files volume (``backend/.data/coverage_runs.json``), newest-first,
bounded per scope. Distinct from each feature's ``cache.py`` (which holds only the single
latest snapshot for freshness) and from ``coverage_trends.py`` (compact %-over-time points);
this is the full-snapshot audit trail with soft-delete/trash."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "coverage_runs.json"
_MAX_PER_SCOPE = 30

# Features that record run history (used for validation + demo purge enumeration).
FEATURES = ("amba", "telemetry", "backupdr")


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
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bucket_key(feature: str, tenant_id: str) -> str:
    return f"{feature}|{tenant_id or 'default'}"


def _scope_key(scope_kind: str, scope_id: str) -> str:
    return f"{scope_kind}:{scope_id}"


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    """Compact, feature-agnostic row for the history grid. ``headline`` is the 0-100 metric
    each dashboard charts (coverage % / % protected); ``counts`` is the small KPI dict the
    feature stored at save time."""
    return {
        "id": run.get("id", ""),
        "run_at": run.get("run_at", run.get("generated_at", "")),
        "scope_kind": run.get("scope_kind", ""),
        "scope_id": run.get("scope_id", ""),
        "scope_name": run.get("scope_name", run.get("scope_id", "")),
        "headline": run.get("_headline"),
        "counts": run.get("_counts", {}) or {},
        "resource_count": run.get("_resource_count", 0),
        "demo": bool(run.get("demo", False)),
        "triggered_by": run.get("triggered_by", ""),
        "deleted_at": run.get("deleted_at", ""),
    }


def save_run(
    feature: str, tenant_id: str, scope_kind: str, scope_id: str, snapshot: dict[str, Any],
    *, headline: float | int | None = None, counts: dict[str, Any] | None = None,
    resource_count: int = 0, actor: str = "",
) -> dict[str, Any]:
    """Persist a snapshot as a new run for ``feature``; returns the stored run (with id +
    run_at). ``headline``/``counts``/``resource_count`` are the compact grid fields the
    dashboard supplies (since each feature's snapshot shape differs)."""
    data = _read()
    bucket = data.setdefault(_bucket_key(feature, tenant_id), {})
    runs = bucket.setdefault(_scope_key(scope_kind, scope_id), [])
    run = dict(snapshot)
    run["id"] = uuid.uuid4().hex[:16]
    run["run_at"] = _now()
    run["triggered_by"] = actor
    run["_headline"] = headline
    run["_counts"] = counts or {}
    run["_resource_count"] = resource_count
    runs.insert(0, run)
    # Cap ACTIVE (non-trashed) runs only; evict the oldest active ones beyond the cap.
    # Trashed runs are preserved until restored or purged.
    active_positions = [i for i, r in enumerate(runs) if not r.get("deleted_at")]
    if len(active_positions) > _MAX_PER_SCOPE:
        for i in sorted(active_positions[_MAX_PER_SCOPE:], reverse=True):
            del runs[i]
    _write(data)
    return run


def list_runs(feature: str, tenant_id: str, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Active run summaries (newest first) for a scope — trashed runs are excluded."""
    bucket = _read().get(_bucket_key(feature, tenant_id), {})
    runs = bucket.get(_scope_key(scope_kind, scope_id), [])
    return [_summary(r) for r in runs if not r.get("deleted_at")]


def list_trashed_runs(feature: str, tenant_id: str, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Trashed (soft-deleted) run summaries for a scope, most-recently-deleted first."""
    bucket = _read().get(_bucket_key(feature, tenant_id), {})
    runs = bucket.get(_scope_key(scope_kind, scope_id), [])
    trashed = [_summary(r) for r in runs if r.get("deleted_at")]
    trashed.sort(key=lambda r: r.get("deleted_at", ""), reverse=True)
    return trashed


def get_run(feature: str, tenant_id: str, run_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    """Full run snapshot by id (searches all scopes within the feature+tenant). Trashed runs
    are excluded unless ``include_deleted`` is set."""
    bucket = _read().get(_bucket_key(feature, tenant_id), {})
    for runs in bucket.values():
        for r in runs:
            if r.get("id") == run_id:
                if r.get("deleted_at") and not include_deleted:
                    return None
                return r
    return None


def delete_run(feature: str, tenant_id: str, run_id: str) -> bool:
    """Soft-delete: move a run to the Trash (set ``deleted_at``). Returns False if not found
    or already trashed."""
    data = _read()
    bucket = data.get(_bucket_key(feature, tenant_id), {})
    for runs in bucket.values():
        for r in runs:
            if r.get("id") == run_id:
                if r.get("deleted_at"):
                    return False
                r["deleted_at"] = _now()
                _write(data)
                return True
    return False


def restore_run(feature: str, tenant_id: str, run_id: str) -> bool:
    """Restore a trashed run back into active history. Returns False if not found or not
    currently trashed."""
    data = _read()
    bucket = data.get(_bucket_key(feature, tenant_id), {})
    for runs in bucket.values():
        for r in runs:
            if r.get("id") == run_id:
                if not r.get("deleted_at"):
                    return False
                r["deleted_at"] = ""
                _write(data)
                return True
    return False


def purge_run(feature: str, tenant_id: str, run_id: str) -> bool:
    """Permanently delete a single run (hard delete), regardless of trash state."""
    data = _read()
    bucket = data.get(_bucket_key(feature, tenant_id), {})
    for runs in bucket.values():
        for i, r in enumerate(runs):
            if r.get("id") == run_id:
                del runs[i]
                _write(data)
                return True
    return False


def empty_trash(feature: str, tenant_id: str, scope_kind: str, scope_id: str) -> int:
    """Permanently delete every trashed run for a scope. Returns the count removed."""
    data = _read()
    bucket = data.get(_bucket_key(feature, tenant_id), {})
    k = _scope_key(scope_kind, scope_id)
    runs = bucket.get(k) or []
    keep = [r for r in runs if not r.get("deleted_at")]
    removed = len(runs) - len(keep)
    if removed:
        bucket[k] = keep
        _write(data)
    return removed


def delete_scope(feature: str, tenant_id: str, scope_kind: str, scope_id: str) -> bool:
    """Hard-delete an entire scope's run history (used by demo-data purge)."""
    data = _read()
    bucket = data.get(_bucket_key(feature, tenant_id), {})
    k = _scope_key(scope_kind, scope_id)
    if k in bucket:
        del bucket[k]
        _write(data)
        return True
    return False


# --------------------------------------------------------------------------- cleanup
def _run_size(run: dict[str, Any]) -> int:
    try:
        return len(json.dumps(run, default=str))
    except (TypeError, ValueError):
        return 0


def list_all_runs(feature: str, tenant_id: str) -> list[dict[str, Any]]:
    """Every run across EVERY scope for one feature+tenant (active + trashed), each summary
    annotated with ``size_bytes`` — drives the Cleanup tab. Newest-first."""
    bucket = _read().get(_bucket_key(feature, tenant_id), {})
    out: list[dict[str, Any]] = []
    for runs in bucket.values():
        for r in runs:
            s = _summary(r)
            s["size_bytes"] = _run_size(r)
            out.append(s)
    out.sort(key=lambda r: r.get("run_at", ""), reverse=True)
    return out


def cleanup_stats(feature: str, tenant_id: str) -> dict[str, Any]:
    runs = list_all_runs(feature, tenant_id)
    active = [r for r in runs if not r.get("deleted_at")]
    trashed = [r for r in runs if r.get("deleted_at")]
    scopes = {f"{r.get('scope_kind')}:{r.get('scope_id')}" for r in runs}
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


def trash_runs(feature: str, tenant_id: str, ids: list[str]) -> dict[str, int]:
    idset = {i for i in ids if i}
    data = _read()
    bucket = data.get(_bucket_key(feature, tenant_id), {})
    count = 0
    freed = 0
    for runs in bucket.values():
        for r in runs:
            if r.get("id") in idset and not r.get("deleted_at"):
                r["deleted_at"] = _now()
                count += 1
                freed += _run_size(r)
    if count:
        _write(data)
    return {"count": count, "freed_bytes": freed}


def restore_runs(feature: str, tenant_id: str, ids: list[str]) -> dict[str, int]:
    idset = {i for i in ids if i}
    data = _read()
    bucket = data.get(_bucket_key(feature, tenant_id), {})
    count = 0
    for runs in bucket.values():
        for r in runs:
            if r.get("id") in idset and r.get("deleted_at"):
                r["deleted_at"] = ""
                count += 1
    if count:
        _write(data)
    return {"count": count}


def purge_runs(feature: str, tenant_id: str, ids: list[str]) -> dict[str, int]:
    idset = {i for i in ids if i}
    data = _read()
    bucket = data.get(_bucket_key(feature, tenant_id), {})
    count = 0
    freed = 0
    for k in list(bucket.keys()):
        keep: list[dict[str, Any]] = []
        for r in bucket[k]:
            if r.get("id") in idset:
                count += 1
                freed += _run_size(r)
            else:
                keep.append(r)
        bucket[k] = keep
    if count:
        _write(data)
    return {"count": count, "freed_bytes": freed}

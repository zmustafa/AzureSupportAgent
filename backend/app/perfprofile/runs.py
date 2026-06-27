"""Persistent history of performance-profile runs, per (tenant, scope).

Each "Run profile" persists the full snapshot here so operators can review past runs,
compare, and delete them. Stored on the Azure Files volume
(``backend/.data/perfprofile_runs.json``), newest-first, bounded per scope. Distinct from
cache.py (which holds the single latest snapshot for freshness); this is the audit trail."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "perfprofile_runs.json"
_MAX_PER_SCOPE = 30


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


def _key(scope_kind: str, scope_id: str) -> str:
    return f"{scope_kind}:{scope_id}"


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    sc = run.get("scorecard", {}) or {}
    top = run.get("top_bottleneck") or {}
    return {
        "id": run.get("id", ""),
        "run_at": run.get("run_at", run.get("generated_at", "")),
        "scope_kind": run.get("scope_kind", ""),
        "scope_id": run.get("scope_id", ""),
        "scope_name": run.get("scope_name", ""),
        "window": run.get("window", ""),
        "requested_window": run.get("requested_window", ""),
        "requested_start": run.get("requested_start", ""),
        "requested_end": run.get("requested_end", ""),
        "workload_score": sc.get("workload_score"),
        "resources_profiled": sc.get("resources_profiled", 0),
        "breaching": sc.get("breaching", 0),
        "approaching": sc.get("approaching", 0),
        "healthy": sc.get("healthy", 0),
        "top_bottleneck": (
            {
                "resource_name": top.get("resource_name", ""),
                "metric_name": top.get("metric_name", ""),
                "pct_of_threshold": top.get("pct_of_threshold"),
                "state": top.get("state", ""),
            }
            if top
            else None
        ),
        "demo": run.get("demo", False),
        "triggered_by": run.get("triggered_by", ""),
        "deleted_at": run.get("deleted_at", ""),
    }


def save_run(tenant_id: str, scope_kind: str, scope_id: str, snapshot: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """Persist a snapshot as a new run; returns the stored run (with id + run_at)."""
    data = _read()
    bucket = data.setdefault(tenant_id or "default", {})
    runs = bucket.setdefault(_key(scope_kind, scope_id), [])
    run = dict(snapshot)
    run["id"] = uuid.uuid4().hex[:16]
    run["run_at"] = _now()
    run["triggered_by"] = actor
    runs.insert(0, run)
    # Enforce the cap on ACTIVE (non-trashed) runs only, evicting the oldest active ones
    # beyond the cap — trashed runs are preserved in the bucket until restored or purged.
    active_positions = [i for i, r in enumerate(runs) if not r.get("deleted_at")]
    if len(active_positions) > _MAX_PER_SCOPE:
        for i in sorted(active_positions[_MAX_PER_SCOPE:], reverse=True):
            del runs[i]
    _write(data)
    # Record a compact trend point (performance score over time) for the trend chart.
    try:
        from app.core import coverage_trends

        sc = snapshot.get("scorecard", {}) or {}
        coverage_trends.record(
            "performance", tenant_id or "default", scope_kind, scope_id,
            pct=sc.get("workload_score"),
            extra={k: sc.get(k) for k in ("breaching", "approaching", "healthy", "resources_profiled")},
            demo=bool(snapshot.get("demo")),
        )
    except Exception:  # noqa: BLE001 - trend recording must never break a profile save
        pass
    return run


def list_runs(tenant_id: str, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Active run summaries (newest first) for a scope — trashed runs are excluded."""
    bucket = _read().get(tenant_id or "default", {})
    runs = bucket.get(_key(scope_kind, scope_id), [])
    return [_summary(r) for r in runs if not r.get("deleted_at")]


def list_trashed_runs(tenant_id: str, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Trashed (soft-deleted) run summaries for a scope, most-recently-deleted first."""
    bucket = _read().get(tenant_id or "default", {})
    runs = bucket.get(_key(scope_kind, scope_id), [])
    trashed = [_summary(r) for r in runs if r.get("deleted_at")]
    trashed.sort(key=lambda r: r.get("deleted_at", ""), reverse=True)
    return trashed


def get_run(tenant_id: str, run_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    """Full run snapshot by id (searches all scopes within the tenant). Trashed runs are
    excluded unless ``include_deleted`` is set."""
    bucket = _read().get(tenant_id or "default", {})
    for runs in bucket.values():
        for r in runs:
            if r.get("id") == run_id:
                if r.get("deleted_at") and not include_deleted:
                    return None
                return r
    return None


def latest_run(tenant_id: str, scope_kind: str, scope_id: str) -> dict[str, Any] | None:
    bucket = _read().get(tenant_id or "default", {})
    runs = bucket.get(_key(scope_kind, scope_id), [])
    for r in runs:  # newest-first; first active wins
        if not r.get("deleted_at"):
            return r
    return None


def latest_runs_for_scopes(
    tenant_id: str, scopes: list[tuple[str, str]]
) -> dict[str, dict[str, Any]]:
    """Latest non-trashed run SUMMARY per scope, reading the store ONCE (no N+1 file reads).

    ``scopes`` is a list of ``(scope_kind, scope_id)`` pairs; the result maps
    ``"<scope_kind>:<scope_id>"`` → run summary for the scopes that have at least one active
    run. Scopes with no runs are simply absent from the result."""
    bucket = _read().get(tenant_id or "default", {})
    out: dict[str, dict[str, Any]] = {}
    for scope_kind, scope_id in scopes:
        k = _key(scope_kind, scope_id)
        for r in bucket.get(k, []):  # newest-first; first active wins
            if not r.get("deleted_at"):
                out[k] = _summary(r)
                break
    return out


def delete_run(tenant_id: str, run_id: str) -> bool:
    """Soft-delete: move a run to the Trash (set ``deleted_at``). Hidden from history but
    restorable until purged. Returns False if not found or already trashed."""
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for runs in bucket.values():
        for r in runs:
            if r.get("id") == run_id:
                if r.get("deleted_at"):
                    return False
                r["deleted_at"] = _now()
                _write(data)
                return True
    return False


def restore_run(tenant_id: str, run_id: str) -> bool:
    """Restore a trashed run back into active history. Returns False if not found or not
    currently trashed."""
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for runs in bucket.values():
        for r in runs:
            if r.get("id") == run_id:
                if not r.get("deleted_at"):
                    return False
                r["deleted_at"] = ""
                _write(data)
                return True
    return False


def purge_run(tenant_id: str, run_id: str) -> bool:
    """Permanently delete a single run (hard delete), regardless of trash state."""
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for runs in bucket.values():
        for i, r in enumerate(runs):
            if r.get("id") == run_id:
                del runs[i]
                _write(data)
                return True
    return False


def empty_trash(tenant_id: str, scope_kind: str, scope_id: str) -> int:
    """Permanently delete every trashed run for a scope. Returns the count removed."""
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    k = _key(scope_kind, scope_id)
    runs = bucket.get(k) or []
    keep = [r for r in runs if not r.get("deleted_at")]
    removed = len(runs) - len(keep)
    if removed:
        bucket[k] = keep
        _write(data)
    return removed


def delete_scope_runs(tenant_id: str, scope_kind: str, scope_id: str) -> int:
    """Remove all run history for a scope (used to purge demo data). Returns count deleted."""
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    k = _key(scope_kind, scope_id)
    runs = bucket.get(k) or []
    n = len(runs)
    if k in bucket:
        del bucket[k]
        _write(data)
    return n


# --------------------------------------------------------------------------- cleanup
def _run_size(run: dict[str, Any]) -> int:
    try:
        return len(json.dumps(run, default=str))
    except (TypeError, ValueError):
        return 0


def list_all_runs(tenant_id: str) -> list[dict[str, Any]]:
    """Every run across EVERY scope for the tenant (active + trashed), each summary annotated
    with ``size_bytes`` — drives the cross-scope Cleanup tab. Newest-first."""
    bucket = _read().get(tenant_id or "default", {})
    out: list[dict[str, Any]] = []
    for runs in bucket.values():
        for r in runs:
            s = _summary(r)
            s["size_bytes"] = _run_size(r)
            out.append(s)
    out.sort(key=lambda r: r.get("run_at", ""), reverse=True)
    return out


def cleanup_stats(tenant_id: str) -> dict[str, Any]:
    """Aggregate totals for the Cleanup header strip — one store read."""
    runs = list_all_runs(tenant_id)
    active = [r for r in runs if not r.get("deleted_at")]
    trashed = [r for r in runs if r.get("deleted_at")]
    total_bytes = sum(r.get("size_bytes", 0) for r in runs)
    scopes = {f"{r.get('scope_kind')}:{r.get('scope_id')}" for r in runs}
    oldest = min((r.get("run_at", "") for r in active if r.get("run_at")), default="")
    return {
        "total_runs": len(runs),
        "active_runs": len(active),
        "trashed_runs": len(trashed),
        "total_bytes": total_bytes,
        "trashed_bytes": sum(r.get("size_bytes", 0) for r in trashed),
        "scopes": len(scopes),
        "oldest_run_at": oldest,
    }


def trash_runs(tenant_id: str, ids: list[str]) -> dict[str, int]:
    """Bulk soft-delete by id. Returns {count, freed_bytes} (bytes that BECAME trashed)."""
    idset = {i for i in ids if i}
    data = _read()
    bucket = data.get(tenant_id or "default", {})
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


def restore_runs(tenant_id: str, ids: list[str]) -> dict[str, int]:
    """Bulk restore by id. Returns {count}."""
    idset = {i for i in ids if i}
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    count = 0
    for runs in bucket.values():
        for r in runs:
            if r.get("id") in idset and r.get("deleted_at"):
                r["deleted_at"] = ""
                count += 1
    if count:
        _write(data)
    return {"count": count}


def purge_runs(tenant_id: str, ids: list[str]) -> dict[str, int]:
    """Bulk hard-delete by id (irreversible). Returns {count, freed_bytes}."""
    idset = {i for i in ids if i}
    data = _read()
    bucket = data.get(tenant_id or "default", {})
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

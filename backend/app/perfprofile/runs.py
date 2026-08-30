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

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "perfprofile_runs.json"
_MAX_PER_SCOPE = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


def _key(scope_kind: str, scope_id: str) -> str:
    return f"{scope_kind}:{scope_id}"


def _status(run: dict[str, Any]) -> str:
    """Normalize old runs (which predate explicit statuses) and current attempts."""
    explicit = str(run.get("status") or "").lower()
    if explicit in {"succeeded", "partial", "failed"}:
        return explicit
    return "failed" if run.get("error") else "succeeded"


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
        "status": _status(run),
        "warning": run.get("warning", ""),
        "error": run.get("error", ""),
        "collection": run.get("collection") or {},
        "completeness_pct": (run.get("collection") or {}).get("completeness_pct"),
        "triggered_by": run.get("triggered_by", ""),
        "deleted_at": run.get("deleted_at", ""),
    }


def save_run(
    tenant_id: str,
    scope_kind: str,
    scope_id: str,
    snapshot: dict[str, Any],
    *,
    actor: str = "",
    record_trend: bool | None = None,
) -> dict[str, Any]:
    """Persist a snapshot as a new run; returns the stored run (with id + run_at)."""
    run = dict(snapshot)
    run["status"] = _status(run)
    run["id"] = uuid.uuid4().hex[:16]
    run["run_at"] = _now()
    run["triggered_by"] = actor
    def _mutate(data: dict[str, Any]) -> None:
        bucket = data.setdefault(tenant_id or "default", {})
        runs = bucket.setdefault(_key(scope_kind, scope_id), [])
        runs.insert(0, run)
        # Enforce the cap on ACTIVE (non-trashed) runs only, evicting the oldest active ones
        # beyond the cap — trashed runs are preserved in the bucket until restored or purged.
        active_positions = [i for i, stored in enumerate(runs) if not stored.get("deleted_at")]
        if len(active_positions) > _MAX_PER_SCOPE:
            for i in sorted(active_positions[_MAX_PER_SCOPE:], reverse=True):
                del runs[i]

    jsonstore.mutate_json(_PATH, {}, _mutate)
    # Only complete, successful observations belong in the score trend.  A partial scan is
    # retained for diagnosis/history but must never move the estate's trend line.
    should_record = run["status"] == "succeeded" if record_trend is None else record_trend
    if should_record:
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


def latest_successful_run(
    tenant_id: str, scope_kind: str, scope_id: str
) -> dict[str, Any] | None:
    """Newest complete success; failed/partial attempts never displace the trusted result."""
    bucket = _read().get(tenant_id or "default", {})
    for run in bucket.get(_key(scope_kind, scope_id), []):
        if not run.get("deleted_at") and _status(run) == "succeeded":
            return run
    return None


def latest_usable_run(
    tenant_id: str, scope_kind: str, scope_id: str
) -> dict[str, Any] | None:
    """Newest trusted success, or the newest partial observation when no success exists."""
    bucket = _read().get(tenant_id or "default", {})
    partial: dict[str, Any] | None = None
    for run in bucket.get(_key(scope_kind, scope_id), []):
        if run.get("deleted_at"):
            continue
        status = _status(run)
        if status == "succeeded":
            return run
        if status == "partial" and partial is None:
            partial = run
    return partial


def find_run_by_trigger(
    tenant_id: str, scope_kind: str, scope_id: str, trigger: str
) -> dict[str, Any] | None:
    """Find an attempt by durable-worker idempotency trigger."""
    if not trigger:
        return None
    bucket = _read().get(tenant_id or "default", {})
    for run in bucket.get(_key(scope_kind, scope_id), []):
        if run.get("trigger") == trigger:
            return run
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
        partial: dict[str, Any] | None = None
        for r in bucket.get(k, []):  # newest-first; first active wins
            if r.get("deleted_at"):
                continue
            status = _status(r)
            if status == "succeeded":
                out[k] = _summary(r)
                break
            if status == "partial" and partial is None:
                partial = r
        else:
            if partial is not None:
                out[k] = _summary(partial)
    return out


def latest_attempts_for_scopes(
    tenant_id: str, scopes: list[tuple[str, str]]
) -> dict[str, dict[str, Any]]:
    """Newest attempt of any status per scope, used for Fleet status/error overlays."""
    bucket = _read().get(tenant_id or "default", {})
    out: dict[str, dict[str, Any]] = {}
    for scope_kind, scope_id in scopes:
        k = _key(scope_kind, scope_id)
        for run in bucket.get(k, []):
            if not run.get("deleted_at"):
                out[k] = _summary(run)
                break
    return out


def delete_run(tenant_id: str, run_id: str) -> bool:
    """Soft-delete: move a run to the Trash (set ``deleted_at``). Hidden from history but
    restorable until purged. Returns False if not found or already trashed."""
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("id") == run_id and not run.get("deleted_at"):
                    run["deleted_at"] = _now()
                    deleted = True
                    return

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return deleted


def restore_run(tenant_id: str, run_id: str) -> bool:
    """Restore a trashed run back into active history. Returns False if not found or not
    currently trashed."""
    restored = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal restored
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("id") == run_id and run.get("deleted_at"):
                    run["deleted_at"] = ""
                    restored = True
                    return

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return restored


def purge_run(tenant_id: str, run_id: str) -> bool:
    """Permanently delete a single run (hard delete), regardless of trash state."""
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for index, run in enumerate(runs):
                if run.get("id") == run_id:
                    del runs[index]
                    deleted = True
                    return

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return deleted


def empty_trash(tenant_id: str, scope_kind: str, scope_id: str) -> int:
    """Permanently delete every trashed run for a scope. Returns the count removed."""
    removed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal removed
        bucket = data.get(tenant_id or "default", {})
        key = _key(scope_kind, scope_id)
        runs = bucket.get(key) or []
        keep = [run for run in runs if not run.get("deleted_at")]
        removed = len(runs) - len(keep)
        if removed:
            bucket[key] = keep

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return removed


def delete_scope_runs(tenant_id: str, scope_kind: str, scope_id: str) -> int:
    """Remove all run history for a scope (used to purge demo data). Returns count deleted."""
    count = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal count
        bucket = data.get(tenant_id or "default", {})
        key = _key(scope_kind, scope_id)
        count = len(bucket.get(key) or [])
        bucket.pop(key, None)

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return count


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
    count = 0
    freed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal count, freed
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("id") in idset and not run.get("deleted_at"):
                    run["deleted_at"] = _now()
                    count += 1
                    freed += _run_size(run)

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return {"count": count, "freed_bytes": freed}


def restore_runs(tenant_id: str, ids: list[str]) -> dict[str, int]:
    """Bulk restore by id. Returns {count}."""
    idset = {i for i in ids if i}
    count = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal count
        bucket = data.get(tenant_id or "default", {})
        for runs in bucket.values():
            for run in runs:
                if run.get("id") in idset and run.get("deleted_at"):
                    run["deleted_at"] = ""
                    count += 1

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return {"count": count}


def purge_runs(tenant_id: str, ids: list[str]) -> dict[str, int]:
    """Bulk hard-delete by id (irreversible). Returns {count, freed_bytes}."""
    idset = {i for i in ids if i}
    count = 0
    freed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal count, freed
        bucket = data.get(tenant_id or "default", {})
        for key in list(bucket):
            keep: list[dict[str, Any]] = []
            for run in bucket[key]:
                if run.get("id") in idset:
                    count += 1
                    freed += _run_size(run)
                else:
                    keep.append(run)
            bucket[key] = keep

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return {"count": count, "freed_bytes": freed}

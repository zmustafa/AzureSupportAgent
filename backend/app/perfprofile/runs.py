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
    if len(runs) > _MAX_PER_SCOPE:
        del runs[_MAX_PER_SCOPE:]
    _write(data)
    return run


def list_runs(tenant_id: str, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Run summaries (newest first) for a scope."""
    bucket = _read().get(tenant_id or "default", {})
    runs = bucket.get(_key(scope_kind, scope_id), [])
    return [_summary(r) for r in runs]


def get_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    """Full run snapshot by id (searches all scopes within the tenant)."""
    bucket = _read().get(tenant_id or "default", {})
    for runs in bucket.values():
        for r in runs:
            if r.get("id") == run_id:
                return r
    return None


def latest_run(tenant_id: str, scope_kind: str, scope_id: str) -> dict[str, Any] | None:
    bucket = _read().get(tenant_id or "default", {})
    runs = bucket.get(_key(scope_kind, scope_id), [])
    return runs[0] if runs else None


def delete_run(tenant_id: str, run_id: str) -> bool:
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    for scope_key, runs in bucket.items():
        for i, r in enumerate(runs):
            if r.get("id") == run_id:
                del runs[i]
                _write(data)
                return True
    return False


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

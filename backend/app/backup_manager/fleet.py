"""Per-workload fleet summary store.

The Fleet grid needs one headline row per workload, instantly, without touching Azure. It
cannot read those rows out of the snapshot store: a full analysis document is heavy (up to
5000 inventory rows) and :data:`app.backup_manager.snapshot.MAX_SCOPES` keeps only the most
recent handful, so a sweep across more workloads than that would evict its own earlier results
and the grid would go blank halfway through.

So every completed analysis also writes a ~1 KB summary row here. The rows survive snapshot
eviction, cost nothing to read, and carry only derived numbers — never resource identifiers
beyond the workload id itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.backup_manager import service
from app.core import jsonstore

#: Sibling of the snapshot document; small enough to rewrite whole on every analysis.
_PATH = Path(__file__).resolve().parents[2] / ".data" / "backup_manager_fleet.json"

#: Bumped when the row shape changes; rows at another version are treated as absent so the
#: grid shows "never analyzed" rather than rendering fields that moved.
ROW_SCHEMA_VERSION = 1


def key(connection_id: str, workload_id: str) -> str:
    return f"{connection_id or 'default'}|{workload_id}"


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def summarize(snapshot: dict[str, Any], *, workload_id: str, connection_id: str) -> dict[str, Any]:
    """Derive the fleet row from a finished analysis snapshot.

    Protection percentage is deliberately computed against *protected items + gaps* rather
    than the raw resource count: a gap is a resource the detectors judged backup-eligible and
    found unprotected, so the ratio answers "of what should be protected, how much is?" —
    the same question the Gaps tab answers, from the same numbers."""
    summary = snapshot.get("summary") or {}
    counts = snapshot.get("counts") or {}
    protection = summary.get("protection") or {}
    jobs = summary.get("jobs") or {}
    rpo = summary.get("rpo") or {}
    posture = summary.get("posture") or {}
    cost = summary.get("cost") or {}
    dr = summary.get("dr") or {}

    protected = _int(protection.get("protected_items") or counts.get("protected_items"))
    gaps = _int(counts.get("gaps"))
    eligible = protected + gaps
    return {
        "schema_version": ROW_SCHEMA_VERSION,
        "workload_id": workload_id,
        "connection_id": connection_id,
        "run_at": str(snapshot.get("generated_at") or ""),
        "partial": bool(snapshot.get("partial")),
        "errors": sorted((snapshot.get("errors") or {}).keys()),
        "demo": bool(snapshot.get("demo")),
        "vaults": _int(protection.get("vaults") or counts.get("vaults")),
        "protected_items": protected,
        "stopped": _int(protection.get("stopped")),
        "orphaned": _int(protection.get("orphaned")),
        "policies": _int(protection.get("policies") or counts.get("policies")),
        "gaps": gaps,
        "pct_protected": round(protected * 100 / eligible) if eligible else None,
        "failed_jobs": _int(jobs.get("failed") or counts.get("failed_jobs")),
        "chronic_failures": _int(summary.get("chronic_failures")),
        "rpo_attainment_pct": rpo.get("attainment_pct"),
        "rpo_breached": _int(rpo.get("breached")),
        "posture_score": _int(posture.get("average_score")),
        "posture_band": str(posture.get("band") or ""),
        "red_vaults": _int(posture.get("red_vaults")),
        "vault_actions": _int(posture.get("actionable_count")),
        "dr_replicated": _int(dr.get("replicated_items")),
        "dr_unhealthy": _int(dr.get("unhealthy")),
        "monthly_cost": _float(cost.get("monthly_total")),
        "recoverable_monthly": _float(cost.get("recoverable_monthly")),
        "currency": str(cost.get("currency") or ""),
        "cost_confidence": str(cost.get("confidence") or ""),
    }


def write_row(tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
    def _mutate(data: dict[str, Any]) -> None:
        bucket = data.setdefault(tenant_id or "default", {})
        bucket[key(str(row.get("connection_id") or ""), str(row.get("workload_id") or ""))] = row

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return row


def read_rows(tenant_id: str) -> dict[str, dict[str, Any]]:
    """Every stored row for a tenant, keyed by ``connection|workload``. Rows written by an
    older schema are dropped on read so the grid falls back to "never analyzed"."""
    bucket = _read().get(tenant_id or "default", {})
    return {
        k: v for k, v in bucket.items()
        if isinstance(v, dict) and v.get("schema_version") == ROW_SCHEMA_VERSION
    }


def delete_rows(tenant_id: str, keys: list[str]) -> int:
    removed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal removed
        bucket = data.get(tenant_id or "default", {})
        for stored_key in keys:
            if bucket.pop(stored_key, None) is not None:
                removed += 1

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return removed


def age_seconds(row: dict[str, Any]) -> float | None:
    parsed = service.parse_iso(str(row.get("run_at") or ""))
    if parsed is None:
        return None
    return max(0.0, (service.now() - parsed).total_seconds())

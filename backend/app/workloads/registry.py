"""Workload definition registry (JSON, no secrets → no encryption).

Persisted as backend/.data/workloads.json, consistent with the other registries. A
workload's membership is a list of *nodes* at mixed levels. A node may carry an
``excludes`` list (child ARM ids removed from an otherwise-whole parent) so that
"select a whole RG, minus one resource" stays precise."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "workloads.json"

NODE_KINDS = ("mg", "subscription", "resource_group", "resource")

DEFAULTS: dict[str, Any] = {
    "name": "",
    "description": "",
    "connection_id": "",
    "tenant_id": "",
    # nodes: [{kind, id (ARM id), name, subscription_id?, resource_group?,
    #          resource_type?, location?, excludes?: [arm_id,...]}]
    "nodes": [],
    "tags": [],
    # How this workload was created/scoped: {kind: mg|subscription, id, name} when built
    # by Autopilot (drives the Refresh button); empty for hand-built workloads.
    "origin": {},
    # Cached type breakdown + counts (recomputed on every save). Shape:
    # {types: [{label, count}], total_resources, scope_counts: {...}}
    "summary": {},
    # AI reasoning captured when Autopilot proposed this workload.
    "reasoning": "",
    "confidence": 0.0,
    # Autopilot classification + business context (AI-inferred, user-editable).
    "workload_type": "",      # web_app | data_pipeline | ai_ml | networking | storage | identity | integration | other
    "environment": "",        # production | staging | development | test | dr | shared | unknown
    "criticality": "",        # critical | high | medium | low (drives downstream severity/SLA weighting)
    "data_classification": "",  # confidential | internal | public | unknown
    # Evidence the grouper used to justify membership (network/RBAC/dependency/provenance
    # signals). Shape: [{kind, detail}] — surfaced in the UI for trust.
    "evidence": [],
    "last_refreshed": "",
    "created_by": "",
    "created_at": "",
    "updated_at": "",
    # Soft-delete: when set (ISO timestamp), the workload is in the Trash — hidden from
    # the active list and from all consumers (assessments, architectures, chat scope),
    # but restorable until purged.
    "deleted_at": "",
}


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
    return {"workloads": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_workloads(include_deleted: bool = False) -> list[dict[str, Any]]:
    data = _read()
    out: list[dict[str, Any]] = []
    for wid, wl in data.get("workloads", {}).items():
        merged = json.loads(json.dumps(DEFAULTS))  # deep copy (nested dicts)
        merged.update(wl)
        merged["id"] = wid
        # Hide trashed workloads from the active list (and every consumer) unless asked.
        if not include_deleted and merged.get("deleted_at"):
            continue
        out.append(merged)
    out.sort(key=lambda w: w.get("name", "").lower())
    return out


def list_trashed_workloads() -> list[dict[str, Any]]:
    """Workloads currently in the Trash (soft-deleted), most-recently-deleted first."""
    out = [w for w in list_workloads(include_deleted=True) if w.get("deleted_at")]
    out.sort(key=lambda w: w.get("deleted_at", ""), reverse=True)
    return out


def get_workload(workload_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
    if not workload_id:
        return None
    for w in list_workloads(include_deleted=include_deleted):
        if w["id"] == workload_id:
            return w
    return None


def upsert_workload(wl: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    workloads = data.setdefault("workloads", {})
    wid = wl.get("id") or str(uuid.uuid4())
    existing = workloads.get(wid, {})
    merged = dict(existing)
    for key in DEFAULTS:
        if key in wl and wl[key] is not None:
            merged[key] = wl[key]
    # Recompute the cached summary from the node list (cheap, no Azure calls).
    from app.workloads.summarize import summarize_nodes

    merged["summary"] = summarize_nodes(merged.get("nodes", []))
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    workloads[wid] = merged
    _write(data)
    result = get_workload(wid)
    assert result is not None
    return result


def delete_workload(workload_id: str) -> bool:
    """Soft-delete: move the workload to the Trash (set ``deleted_at``). It's hidden
    everywhere but restorable until purged. Returns False if not found / already trashed."""
    data = _read()
    wl = data.get("workloads", {}).get(workload_id)
    if wl is None or wl.get("deleted_at"):
        return False
    wl["deleted_at"] = _now()
    wl["updated_at"] = _now()
    _write(data)
    return True


def restore_workload(workload_id: str) -> dict[str, Any] | None:
    """Restore a trashed workload back into the active list."""
    data = _read()
    wl = data.get("workloads", {}).get(workload_id)
    if wl is None or not wl.get("deleted_at"):
        return None
    wl["deleted_at"] = ""
    wl["updated_at"] = _now()
    _write(data)
    return get_workload(workload_id)


def purge_workload(workload_id: str) -> bool:
    """Permanently delete a single trashed workload (hard delete)."""
    data = _read()
    wl = data.get("workloads", {}).get(workload_id)
    if wl is None or not wl.get("deleted_at"):
        return False
    del data["workloads"][workload_id]
    _write(data)
    return True


def empty_trash() -> int:
    """Permanently delete every trashed workload. Returns the count removed."""
    data = _read()
    workloads = data.get("workloads", {})
    trashed = [wid for wid, wl in workloads.items() if wl.get("deleted_at")]
    for wid in trashed:
        del workloads[wid]
    if trashed:
        _write(data)
    return len(trashed)

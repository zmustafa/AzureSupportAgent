"""Inventory snapshots + drift. Persists periodic point-in-time captures of the resource
estate so the UI can show what was added / removed / changed over time. Mirrors the policy
snapshot pattern: a compact per-resource fingerprint is stored (not the full payload) so the
file stays lean even for large tenants.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "inventory_snapshots.json"
_MAX_SNAPSHOTS = 60
_MAX_FINGERPRINTS = 20000  # cap stored per-resource fingerprints to bound file size


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


def _fingerprints(resources: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """A compact {id_lower: {name,type,rg,sub,sku}} map used to diff two points in time."""
    fp: dict[str, dict[str, str]] = {}
    for r in resources[:_MAX_FINGERPRINTS]:
        rid = (r.get("id") or "").lower()
        if not rid:
            continue
        fp[rid] = {
            "name": r.get("name", ""),
            "type": r.get("type", ""),
            "rg": r.get("resource_group", ""),
            "sub": r.get("subscription_id", ""),
            "sku": r.get("sku", ""),
        }
    return fp


def _summary_dict(rec: dict[str, Any]) -> dict[str, Any]:
    """A snapshot without the bulky fingerprint map (for list views)."""
    return {k: v for k, v in rec.items() if k != "fingerprints"}


def save_snapshot(
    tenant_id: str, connection_id: str, payload: dict[str, Any], actor: str = ""
) -> dict[str, Any]:
    """Persist a compact snapshot from a full inventory payload. Returns the summary."""
    data = _read()
    sid = uuid.uuid4().hex[:12]
    resources = payload.get("resources") or []
    summary = payload.get("summary") or {}
    rec = {
        "id": sid,
        "tenant_id": tenant_id or "",
        "connection_id": connection_id or "",
        "created_at": _now(),
        "created_by": actor,
        "total_resources": summary.get("total_resources", len(resources)),
        "type_count": summary.get("type_count", 0),
        "subscription_count": summary.get("subscription_count", 0),
        "tag_coverage_pct": summary.get("tag_coverage_pct", 0),
        "fingerprints": _fingerprints(resources),
    }
    data[sid] = rec
    recs = sorted(data.values(), key=lambda s: s.get("created_at", ""), reverse=True)
    if len(recs) > _MAX_SNAPSHOTS:
        for old in recs[_MAX_SNAPSHOTS:]:
            data.pop(old["id"], None)
    _write(data)
    return _summary_dict(rec)


def list_snapshots(tenant_id: str, connection_id: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
    data = _read()
    out = [r for r in data.values() if (r.get("tenant_id") or "") in ("", tenant_id)]
    if connection_id:
        out = [r for r in out if (r.get("connection_id") or "") in ("", connection_id)]
    out.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return [_summary_dict(r) for r in out[:limit]]


def get_snapshot(tenant_id: str, snapshot_id: str) -> dict[str, Any] | None:
    rec = _read().get(snapshot_id)
    if not rec or (rec.get("tenant_id") or "") not in ("", tenant_id):
        return None
    return rec


def delete_snapshot(tenant_id: str, snapshot_id: str) -> bool:
    data = _read()
    rec = data.get(snapshot_id)
    if rec and (rec.get("tenant_id") or "") in ("", tenant_id):
        data.pop(snapshot_id, None)
        _write(data)
        return True
    return False


def latest_snapshot(tenant_id: str, connection_id: str | None = None) -> dict[str, Any] | None:
    snaps = list_snapshots(tenant_id, connection_id, limit=1)
    if not snaps:
        return None
    return get_snapshot(tenant_id, snaps[0]["id"])


def compute_drift(baseline: dict[str, Any], resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Diff a live resource list against a baseline snapshot's fingerprints.

    Returns {added, removed, changed, counts}, each list capped for transport."""
    base_fp: dict[str, dict[str, str]] = baseline.get("fingerprints") or {}
    cur_fp = _fingerprints(resources)
    cap = 500

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for rid, cur in cur_fp.items():
        if rid not in base_fp:
            added.append({"id": rid, **cur})
        else:
            old = base_fp[rid]
            diffs = [k for k in ("sku", "rg", "type") if (old.get(k, "") != cur.get(k, ""))]
            if diffs:
                changed.append({"id": rid, "name": cur.get("name", ""), "type": cur.get("type", ""),
                                "changes": {k: {"from": old.get(k, ""), "to": cur.get(k, "")} for k in diffs}})
    for rid, old in base_fp.items():
        if rid not in cur_fp:
            removed.append({"id": rid, **old})

    return {
        "baseline_id": baseline.get("id", ""),
        "baseline_at": baseline.get("created_at", ""),
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
        "added": added[:cap],
        "removed": removed[:cap],
        "changed": changed[:cap],
        "computed_at": _now(),
        "_ts": time.time(),
    }

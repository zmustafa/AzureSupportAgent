"""Tag drift snapshots and diff over time (F7).

Stores compact point-in-time tag snapshots per ``tenant : connection : scope`` in
``.data/tagintel_drift.json`` (bounded), and diffs any two snapshots to reveal keys added /
removed, value changes (with billing-tag changes spotlighted), and coverage deltas.

A snapshot is intentionally small — it records, per resource, only its tag dict — so a few
thousand resources stay well under a sensible file size. Mirrors the bounded-history pattern
used by perfprofile.runs.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.tagintel.analysis import classify_key, norm_key

_PATH = Path(__file__).resolve().parents[2] / ".data" / "tagintel_drift.json"
_MAX_SNAPSHOTS = 30  # per scope


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
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _key(tenant_id: str, connection_id: str, scope: str) -> str:
    return f"{tenant_id or 'default'}|{connection_id or ''}|{scope or ''}"


def save_snapshot(tenant_id: str, connection_id: str, scope: str, resources: list[dict[str, Any]],
                  *, coverage_pct: float = 0.0, actor: str = "") -> dict[str, Any]:
    """Persist a compact tag snapshot; returns its summary (id, taken_at, counts)."""
    data = _read()
    bucket = data.setdefault(_key(tenant_id, connection_id, scope), [])
    tag_map = {r.get("id", ""): (r.get("tags") or {}) for r in resources if r.get("id")}
    name_map = {r.get("id", ""): (r.get("name", "") or "") for r in resources if r.get("id")}
    distinct_keys = {k for tags in tag_map.values() for k in tags}
    snap = {
        "id": uuid.uuid4().hex,
        "taken_at": _now(),
        "actor": actor,
        "resource_count": len(tag_map),
        "distinct_keys": len(distinct_keys),
        "coverage_pct": round(coverage_pct, 1),
        "tags": tag_map,
        "names": name_map,
    }
    bucket.insert(0, snap)
    del bucket[_MAX_SNAPSHOTS:]
    _write(data)
    return _summary(snap)


def _summary(snap: dict[str, Any]) -> dict[str, Any]:
    return {k: snap[k] for k in ("id", "taken_at", "actor", "resource_count", "distinct_keys", "coverage_pct")}


def list_snapshots(tenant_id: str, connection_id: str, scope: str) -> list[dict[str, Any]]:
    return [_summary(s) for s in _read().get(_key(tenant_id, connection_id, scope), [])]


def _get(tenant_id: str, connection_id: str, scope: str, snap_id: str) -> dict[str, Any] | None:
    for s in _read().get(_key(tenant_id, connection_id, scope), []):
        if s.get("id") == snap_id:
            return s
    return None


def diff(tenant_id: str, connection_id: str, scope: str, base_id: str, head_id: str) -> dict[str, Any]:
    """Diff two snapshots (base = older, head = newer). Returns added/removed keys, value
    changes, billing-tag changes, and the coverage delta."""
    base = _get(tenant_id, connection_id, scope, base_id)
    head = _get(tenant_id, connection_id, scope, head_id)
    if not base or not head:
        return {"error": "snapshot not found"}

    base_tags: dict[str, dict] = base["tags"]
    head_tags: dict[str, dict] = head["tags"]
    base_names: dict[str, str] = base.get("names", {})
    head_names: dict[str, str] = head.get("names", {})

    def _name(rid: str) -> str:
        return head_names.get(rid) or base_names.get(rid) or (rid.rsplit("/", 1)[-1] if rid else rid)

    base_keys = {norm_key(k) for tags in base_tags.values() for k in tags}
    head_keys = {norm_key(k) for tags in head_tags.values() for k in tags}
    # Keep a representative original spelling per normalized key.
    spell = {norm_key(k): k for tags in {**base_tags, **head_tags}.values() for k in tags}

    added_keys = sorted(spell[n] for n in head_keys - base_keys)
    removed_keys = sorted(spell[n] for n in base_keys - head_keys)

    value_changes: list[dict[str, Any]] = []
    billing_changes: list[dict[str, Any]] = []
    # Per-key resource detail: which resources gained / lost each key.
    added_key_res: dict[str, list[dict[str, str]]] = {}
    removed_key_res: dict[str, list[dict[str, str]]] = {}
    # Per-resource change rollup (the "resources changed between these two points").
    changed: dict[str, dict[str, Any]] = {}

    def _touch(rid: str) -> dict[str, Any]:
        return changed.setdefault(rid, {"id": rid, "name": _name(rid), "added": [], "removed": [], "changed": []})

    # Resources present in both — compare their tag sets key-by-key.
    for rid in base_tags.keys() & head_tags.keys():
        b = {k.lower(): (k, v) for k, v in base_tags[rid].items()}
        h = {k.lower(): (k, v) for k, v in head_tags[rid].items()}
        for lk in h.keys() - b.keys():            # key added to THIS resource
            key = h[lk][0]
            added_key_res.setdefault(key, []).append({"id": rid, "name": _name(rid)})
            _touch(rid)["added"].append({"key": key, "to": h[lk][1]})
        for lk in b.keys() - h.keys():            # key removed from THIS resource
            key = b[lk][0]
            removed_key_res.setdefault(key, []).append({"id": rid, "name": _name(rid)})
            _touch(rid)["removed"].append({"key": key, "from": b[lk][1]})
        for lk in b.keys() & h.keys():            # value changed
            if str(b[lk][1]) != str(h[lk][1]):
                key = h[lk][0]
                change = {"id": rid, "name": _name(rid), "key": key, "from": b[lk][1], "to": h[lk][1]}
                value_changes.append(change)
                _touch(rid)["changed"].append({"key": key, "from": b[lk][1], "to": h[lk][1]})
                if classify_key(key) == "billing":
                    billing_changes.append(change)

    # Resources that appeared / disappeared entirely between the two snapshots.
    added_resources = [{"id": r, "name": _name(r)} for r in sorted(head_tags.keys() - base_tags.keys())]
    removed_resources = [{"id": r, "name": _name(r)} for r in sorted(base_tags.keys() - head_tags.keys())]
    for r in head_tags.keys() - base_tags.keys():
        _touch(r)["added"] += [{"key": k, "to": v} for k, v in head_tags[r].items()]
    for r in base_tags.keys() - head_tags.keys():
        _touch(r)["removed"] += [{"key": k, "from": v} for k, v in base_tags[r].items()]

    added_key_details = [{"key": k, "count": len(v), "resources": v[:200]} for k, v in
                         sorted(added_key_res.items(), key=lambda kv: -len(kv[1]))]
    removed_key_details = [{"key": k, "count": len(v), "resources": v[:200]} for k, v in
                           sorted(removed_key_res.items(), key=lambda kv: -len(kv[1]))]
    changed_resources = sorted(changed.values(), key=lambda c: -(len(c["added"]) + len(c["removed"]) + len(c["changed"])))

    return {
        "base": _summary(base),
        "head": _summary(head),
        "added_keys": added_keys,
        "removed_keys": removed_keys,
        "added_key_details": added_key_details,
        "removed_key_details": removed_key_details,
        "value_changes": value_changes[:300],
        "value_change_count": len(value_changes),
        "billing_changes": billing_changes[:100],
        "added_resources": added_resources,
        "removed_resources": removed_resources,
        "changed_resources": changed_resources[:300],
        "changed_resource_count": len(changed),
        "coverage_delta": round(head["coverage_pct"] - base["coverage_pct"], 1),
        "resource_delta": head["resource_count"] - base["resource_count"],
    }


"""Architecture *collections* (a.k.a. Categories / Solutions).

A collection is a user-managed, tenant-scoped grouping of whole architectures — think of
it as a folder or a named "solution" that several architecture diagrams belong to. This is
distinct from a resource *category* (the web/compute/data node taxonomy in ``catalog.py``)
and from a diagram *group* (a visual box inside one diagram). Persisted under
backend/.data/architecture_collections.json, consistent with the other JSON registries.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "architecture_collections.json"

# Curated default palette offered in the UI color picker (kept in sync with the resource
# category colors so the look is cohesive). The UI may still pick any hex.
DEFAULT_COLORS = (
    "#2563eb", "#7c3aed", "#dc2626", "#ea580c", "#16a34a",
    "#0d9488", "#0891b2", "#ca8a04", "#9333ea", "#6b7280",
)

DEFAULTS: dict[str, Any] = {
    "name": "",
    "description": "",
    "color": "#6b7280",
    "icon": "📁",
    "order": 0,
    "tenant_id": "",
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}

_FIELDS = [k for k in DEFAULTS if k not in ("created_at", "updated_at")]


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
    return {"collections": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge(cid: str, raw: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(raw)
    merged["id"] = cid
    return merged


def list_collections(tenant_id: str | None = None) -> list[dict[str, Any]]:
    data = _read()
    out = [_merge(cid, c) for cid, c in data.get("collections", {}).items()]
    if tenant_id is not None:
        out = [c for c in out if (c.get("tenant_id") or "") in ("", tenant_id)]
    # Manual order first, then name as a stable tiebreaker.
    out.sort(key=lambda c: (int(c.get("order", 0)), c.get("name", "").lower()))
    return out


def get_collection(collection_id: str) -> dict[str, Any] | None:
    data = _read()
    raw = data.get("collections", {}).get(collection_id)
    return _merge(collection_id, raw) if raw is not None else None


def upsert_collection(coll: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    colls = data.setdefault("collections", {})
    cid = coll.get("id") or str(uuid.uuid4())
    existing = colls.get(cid, {})
    merged = dict(existing)
    for key in _FIELDS:
        if key in coll and coll[key] is not None:
            merged[key] = coll[key]
    # New collections sort to the end unless an explicit order was supplied.
    if not existing and "order" not in coll:
        merged["order"] = _next_order(colls)
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    colls[cid] = merged
    _write(data)
    result = get_collection(cid)
    assert result is not None
    return result


def delete_collection(collection_id: str) -> bool:
    data = _read()
    if collection_id in data.get("collections", {}):
        del data["collections"][collection_id]
        _write(data)
        return True
    return False


def reorder_collections(ordered_ids: list[str]) -> None:
    """Apply a new manual order from a list of collection ids (index becomes order)."""
    data = _read()
    colls = data.get("collections", {})
    for idx, cid in enumerate(ordered_ids):
        if cid in colls:
            colls[cid]["order"] = idx
            colls[cid]["updated_at"] = _now()
    _write(data)


def _next_order(colls: dict[str, Any]) -> int:
    if not colls:
        return 0
    return max((int(c.get("order", 0)) for c in colls.values()), default=0) + 1

"""Saved graph views — named, restorable camera/filter/lens/scope snapshots.

File-backed JSON registry (``.data/graph_views.json``), tenant-scoped, mirroring the other
registries in this codebase. No secrets, so no encryption. A saved view stores everything
the frontend needs to restore a graph workspace: scope, expanded nodes, filters, active
lens, layout, and camera position.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "graph_views.json"

DEFAULTS: dict[str, Any] = {
    "name": "",
    "tenant_id": "",
    "connection_id": "",
    "scope_kind": "overview",   # overview | workload | subscription
    "scope_id": "",
    "lens": "none",
    "layout": "breadthfirst",
    "hidden_kinds": [],         # node kinds toggled off
    "expanded": [],             # node ids that were expanded
    "camera": {},               # {zoom, pan:{x,y}}
    "overlays": [],             # active overlay names (cost/rbac/change/…)
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"views": {}})
    return data if isinstance(data, dict) else {"views": {}}


def _merge(vid: str, raw: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(raw)
    merged["id"] = vid
    return merged


def list_views(tenant_id: str | None = None) -> list[dict[str, Any]]:
    out = [_merge(vid, v) for vid, v in _read().get("views", {}).items()]
    if tenant_id is not None:
        out = [v for v in out if (v.get("tenant_id") or "") in ("", tenant_id)]
    out.sort(key=lambda v: v.get("updated_at", ""), reverse=True)
    return out


def get_view(view_id: str) -> dict[str, Any] | None:
    raw = _read().get("views", {}).get(view_id)
    return _merge(view_id, raw) if raw is not None else None


def save_view(view: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    vid = view.get("id") or str(uuid.uuid4())
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        views = data.setdefault("views", {})
        existing = views.get(vid, {})
        merged = dict(existing)
        for key in DEFAULTS:
            if key in view and view[key] is not None:
                merged[key] = view[key]
        merged["created_by"] = existing.get("created_by") or actor
        merged["created_at"] = existing.get("created_at") or _now()
        merged["updated_at"] = _now()
        merged.pop("id", None)
        views[vid] = merged
        result.update(_merge(vid, merged))

    jsonstore.mutate_json(_PATH, {"views": {}}, _mutate)
    return result


def delete_view(view_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        views = data.get("views", {})
        if view_id in views:
            del views[view_id]
            deleted = True

    jsonstore.mutate_json(_PATH, {"views": {}}, _mutate)
    return deleted

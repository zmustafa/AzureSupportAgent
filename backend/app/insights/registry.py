"""Insight Pack library — JSON-backed CRUD (admin-managed), consistent with the other
registries (custom agents, workbooks). Packs are scope-agnostic definitions; scope +
schedule are supplied per assignment (a ScheduledTask). Built-in starter packs seed the
library on first use but remain fully editable/deletable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore
from app.insights import packfile, starters

_PATH = Path(__file__).resolve().parents[2] / ".data" / "insight_packs.json"

CATEGORIES: list[dict[str, str]] = [
    {"id": "security", "label": "Security & Exposure", "icon": "🛡️"},
    {"id": "change", "label": "Change & Drift", "icon": "📋"},
    {"id": "identity", "label": "Identity & Access", "icon": "🔐"},
    {"id": "cost", "label": "Cost & Governance", "icon": "💰"},
    {"id": "operations", "label": "Operations & Health", "icon": "📈"},
    {"id": "general", "label": "General", "icon": "🧩"},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"packs": {}, "seeded": False})
    if isinstance(data, dict) and isinstance(data.get("packs"), dict):
        return data
    return {"packs": {}, "seeded": False}


def _write(data: dict[str, Any]) -> None:
    """Replace the store for import/test compatibility; CRUD uses atomic mutations."""
    jsonstore.write_json(_PATH, data)


def _ensure_seeded(data: dict[str, Any]) -> dict[str, Any]:
    """Seed (and keep up to date) the built-in starter packs that ship with the app.

    Re-seeds whenever ``starters.SEED_VERSION`` changes: retired built-ins are removed,
    the current catalog is upserted as built-ins (refreshing shipped definitions while
    preserving each pack's original ``created_at``), and user-created packs are left
    untouched.
    """
    if data.get("seed_version") == starters.SEED_VERSION:
        return data
    builtin_ids = {s["id"] for s in starters.STARTERS}
    # Drop built-ins that are no longer shipped; never touch user-created packs.
    for pid in [pid for pid, p in data["packs"].items()
                if p.get("builtin") and pid not in builtin_ids]:
        del data["packs"][pid]
    now = _now()
    for s in starters.STARTERS:
        p = packfile.normalize(s)
        existing = data["packs"].get(s["id"]) or {}
        p["created_at"] = existing.get("created_at") or now
        p["updated_at"] = now
        p["created_by"] = existing.get("created_by") or "system"
        p["builtin"] = True
        # Preserve per-install runtime/organizational state across re-seeds.
        p["snoozed_until"] = existing.get("snoozed_until", "")
        p["pinned"] = existing.get("pinned", False)
        p["collection_ids"] = existing.get("collection_ids", []) or []
        data["packs"][p["id"]] = p
    data["seeded"] = True
    data["seed_version"] = starters.SEED_VERSION
    return data


def _seeded() -> dict[str, Any]:
    data = _read()
    if data.get("seed_version") == starters.SEED_VERSION:
        return data
    return jsonstore.mutate_json(_PATH, {"packs": {}, "seeded": False}, _ensure_seeded)


def _mutate(mutator) -> Any:  # noqa: ANN001
    result: Any = None

    def _apply(data: dict[str, Any]) -> None:
        nonlocal result
        _ensure_seeded(data)
        result = mutator(data)

    jsonstore.mutate_json(_PATH, {"packs": {}, "seeded": False}, _apply)
    return result


def list_packs() -> list[dict[str, Any]]:
    data = _seeded()
    out = [packfile.normalize(p) for p in data["packs"].values()]
    out.sort(key=lambda p: (p.get("category", ""), p.get("name", "").lower()))
    return out


def get_pack(pack_id: str) -> dict[str, Any] | None:
    data = _seeded()
    p = data["packs"].get(pack_id)
    return packfile.normalize(p) if p else None


def upsert_pack(pack: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """Create or update a pack. A missing/blank id creates a new pack."""
    p = packfile.normalize(pack)
    pid = (p.get("id") or "").strip()
    if not pid:
        pid = str(uuid.uuid4())
    p["id"] = pid

    def _apply(data: dict[str, Any]) -> dict[str, Any]:
        existing = data["packs"].get(pid)
        if existing is None:
            p["created_at"] = _now()
            p["created_by"] = actor or p.get("created_by") or ""
        else:
            p["created_at"] = existing.get("created_at") or _now()
            p["created_by"] = existing.get("created_by") or actor
            p["builtin"] = existing.get("builtin", False)  # builtin flag is not user-editable
            # Snooze is runtime state the edit form doesn't carry — preserve it across upserts.
            p["snoozed_until"] = p.get("snoozed_until") or existing.get("snoozed_until", "")
            # Pin + collection membership are organizational state the edit form doesn't carry.
            p["pinned"] = existing.get("pinned", False)
            p["collection_ids"] = existing.get("collection_ids", []) or []
        p["updated_at"] = _now()
        data["packs"][pid] = p
        return packfile.normalize(p)

    return _mutate(_apply)


def delete_pack(pack_id: str) -> bool:
    def _apply(data: dict[str, Any]) -> bool:
        if pack_id not in data["packs"]:
            return False
        del data["packs"][pack_id]
        return True

    return bool(_mutate(_apply))


def set_enabled(pack_id: str, enabled: bool) -> dict[str, Any] | None:
    def _apply(data: dict[str, Any]) -> dict[str, Any] | None:
        p = data["packs"].get(pack_id)
        if not p:
            return None
        p["enabled"] = bool(enabled)
        p["updated_at"] = _now()
        data["packs"][pack_id] = p
        return packfile.normalize(p)

    return _mutate(_apply)


def set_snooze(pack_id: str, until_iso: str) -> dict[str, Any] | None:
    """Mute a pack's notifications until ``until_iso`` (an empty string clears the snooze).
    Snoozed packs still run on schedule and record digests; the runner just won't notify."""
    def _apply(data: dict[str, Any]) -> dict[str, Any] | None:
        p = data["packs"].get(pack_id)
        if not p:
            return None
        p["snoozed_until"] = str(until_iso or "")
        p["updated_at"] = _now()
        data["packs"][pack_id] = p
        return packfile.normalize(p)

    return _mutate(_apply)


def clone_pack(pack_id: str, *, actor: str = "") -> dict[str, Any] | None:
    """Duplicate a pack (or a starter template) into a new, editable, non-builtin pack."""
    src = get_pack(pack_id) or starters.by_id(pack_id)
    if not src:
        return None
    p = packfile.normalize(src)
    p["id"] = ""
    p["name"] = f"{p['name']} (copy)"
    p["builtin"] = False
    return upsert_pack(p, actor=actor)


def set_pinned(pack_id: str, pinned: bool) -> dict[str, Any] | None:
    """Pin/unpin a pack so it surfaces in the Library's top section."""
    def _apply(data: dict[str, Any]) -> dict[str, Any] | None:
        p = data["packs"].get(pack_id)
        if not p:
            return None
        p["pinned"] = bool(pinned)
        p["updated_at"] = _now()
        data["packs"][pack_id] = p
        return packfile.normalize(p)

    return _mutate(_apply)


# ------------------------------------------------------------------ collections
# User-defined groupings for the Library. A pack may belong to zero or more collections
# (membership lives on the pack as ``collection_ids``); this store holds their names/icons.
def list_collections() -> list[dict[str, Any]]:
    data = _seeded()
    cols = [c for c in (data.get("collections") or []) if isinstance(c, dict) and c.get("id")]
    cols.sort(key=lambda c: str(c.get("name", "")).lower())
    return cols


def create_collection(name: str, *, icon: str = "📁", actor: str = "") -> dict[str, Any] | None:
    name = (name or "").strip()[:80]
    if not name:
        return None
    col = {"id": str(uuid.uuid4()), "name": name, "icon": (icon or "📁")[:8],
           "created_by": actor, "created_at": _now()}

    def _apply(data: dict[str, Any]) -> None:
        cols = list(data.get("collections") or [])
        cols.append(col)
        data["collections"] = cols

    _mutate(_apply)
    return col


def update_collection(collection_id: str, *, name: str | None = None, icon: str | None = None) -> dict[str, Any] | None:
    def _apply(data: dict[str, Any]) -> dict[str, Any] | None:
        cols = list(data.get("collections") or [])
        for collection in cols:
            if collection.get("id") == collection_id:
                if name is not None and name.strip():
                    collection["name"] = name.strip()[:80]
                if icon is not None and icon.strip():
                    collection["icon"] = icon.strip()[:8]
                data["collections"] = cols
                return collection
        return None

    return _mutate(_apply)


def delete_collection(collection_id: str) -> bool:
    """Remove a collection and detach it from every pack that referenced it."""
    def _apply(data: dict[str, Any]) -> bool:
        cols = list(data.get("collections") or [])
        remaining = [collection for collection in cols if collection.get("id") != collection_id]
        if len(remaining) == len(cols):
            return False
        data["collections"] = remaining
        for pack in data["packs"].values():
            if collection_id in (pack.get("collection_ids") or []):
                pack["collection_ids"] = [
                    cid for cid in pack["collection_ids"] if cid != collection_id
                ]
        return True

    return bool(_mutate(_apply))


def set_pack_collections(pack_id: str, collection_ids: list[str]) -> dict[str, Any] | None:
    """Replace a pack's collection membership (unknown collection ids are dropped)."""
    def _apply(data: dict[str, Any]) -> dict[str, Any] | None:
        p = data["packs"].get(pack_id)
        if not p:
            return None
        valid = {collection.get("id") for collection in (data.get("collections") or [])}
        p["collection_ids"] = [
            str(collection_id)
            for collection_id in (collection_ids or [])
            if str(collection_id) in valid
        ]
        p["updated_at"] = _now()
        data["packs"][pack_id] = p
        return packfile.normalize(p)

    return _mutate(_apply)

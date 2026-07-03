"""Insight Pack library — JSON-backed CRUD (admin-managed), consistent with the other
registries (custom agents, workbooks). Packs are scope-agnostic definitions; scope +
schedule are supplied per assignment (a ScheduledTask). Built-in starter packs seed the
library on first use but remain fully editable/deletable.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("packs"), dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"packs": {}, "seeded": False}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    _write(data)
    return data


def list_packs() -> list[dict[str, Any]]:
    data = _ensure_seeded(_read())
    out = [packfile.normalize(p) for p in data["packs"].values()]
    out.sort(key=lambda p: (p.get("category", ""), p.get("name", "").lower()))
    return out


def get_pack(pack_id: str) -> dict[str, Any] | None:
    data = _ensure_seeded(_read())
    p = data["packs"].get(pack_id)
    return packfile.normalize(p) if p else None


def upsert_pack(pack: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """Create or update a pack. A missing/blank id creates a new pack."""
    data = _ensure_seeded(_read())
    p = packfile.normalize(pack)
    pid = (p.get("id") or "").strip()
    if not pid or pid not in data["packs"]:
        if not pid:
            pid = str(uuid.uuid4())
        p["id"] = pid
        p["created_at"] = _now()
        p["created_by"] = actor or p.get("created_by") or ""
    else:
        existing = data["packs"][pid]
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
    _write(data)
    return packfile.normalize(p)


def delete_pack(pack_id: str) -> bool:
    data = _ensure_seeded(_read())
    if pack_id in data["packs"]:
        del data["packs"][pack_id]
        _write(data)
        return True
    return False


def set_enabled(pack_id: str, enabled: bool) -> dict[str, Any] | None:
    data = _ensure_seeded(_read())
    p = data["packs"].get(pack_id)
    if not p:
        return None
    p["enabled"] = bool(enabled)
    p["updated_at"] = _now()
    data["packs"][pack_id] = p
    _write(data)
    return packfile.normalize(p)


def set_snooze(pack_id: str, until_iso: str) -> dict[str, Any] | None:
    """Mute a pack's notifications until ``until_iso`` (an empty string clears the snooze).
    Snoozed packs still run on schedule and record digests; the runner just won't notify."""
    data = _ensure_seeded(_read())
    p = data["packs"].get(pack_id)
    if not p:
        return None
    p["snoozed_until"] = str(until_iso or "")
    p["updated_at"] = _now()
    data["packs"][pack_id] = p
    _write(data)
    return packfile.normalize(p)


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
    data = _ensure_seeded(_read())
    p = data["packs"].get(pack_id)
    if not p:
        return None
    p["pinned"] = bool(pinned)
    p["updated_at"] = _now()
    data["packs"][pack_id] = p
    _write(data)
    return packfile.normalize(p)


# ------------------------------------------------------------------ collections
# User-defined groupings for the Library. A pack may belong to zero or more collections
# (membership lives on the pack as ``collection_ids``); this store holds their names/icons.
def list_collections() -> list[dict[str, Any]]:
    data = _ensure_seeded(_read())
    cols = [c for c in (data.get("collections") or []) if isinstance(c, dict) and c.get("id")]
    cols.sort(key=lambda c: str(c.get("name", "")).lower())
    return cols


def create_collection(name: str, *, icon: str = "📁", actor: str = "") -> dict[str, Any] | None:
    name = (name or "").strip()[:80]
    if not name:
        return None
    data = _ensure_seeded(_read())
    cols = list(data.get("collections") or [])
    col = {"id": str(uuid.uuid4()), "name": name, "icon": (icon or "📁")[:8],
           "created_by": actor, "created_at": _now()}
    cols.append(col)
    data["collections"] = cols
    _write(data)
    return col


def update_collection(collection_id: str, *, name: str | None = None, icon: str | None = None) -> dict[str, Any] | None:
    data = _ensure_seeded(_read())
    cols = list(data.get("collections") or [])
    for c in cols:
        if c.get("id") == collection_id:
            if name is not None and name.strip():
                c["name"] = name.strip()[:80]
            if icon is not None and icon.strip():
                c["icon"] = icon.strip()[:8]
            data["collections"] = cols
            _write(data)
            return c
    return None


def delete_collection(collection_id: str) -> bool:
    """Remove a collection and detach it from every pack that referenced it."""
    data = _ensure_seeded(_read())
    cols = list(data.get("collections") or [])
    remaining = [c for c in cols if c.get("id") != collection_id]
    if len(remaining) == len(cols):
        return False
    data["collections"] = remaining
    for p in data["packs"].values():
        if collection_id in (p.get("collection_ids") or []):
            p["collection_ids"] = [c for c in p["collection_ids"] if c != collection_id]
    _write(data)
    return True


def set_pack_collections(pack_id: str, collection_ids: list[str]) -> dict[str, Any] | None:
    """Replace a pack's collection membership (unknown collection ids are dropped)."""
    data = _ensure_seeded(_read())
    p = data["packs"].get(pack_id)
    if not p:
        return None
    valid = {c.get("id") for c in (data.get("collections") or [])}
    p["collection_ids"] = [str(c) for c in (collection_ids or []) if str(c) in valid]
    p["updated_at"] = _now()
    data["packs"][pack_id] = p
    _write(data)
    return packfile.normalize(p)

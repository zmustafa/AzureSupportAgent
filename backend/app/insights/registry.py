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

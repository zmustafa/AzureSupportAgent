"""Per-tenant Estate Graph view preferences (server-side).

Remembers which graph *layout* ("view") the user last chose for each Azure tenant, so the
choice persists across browsers/devices and reloads — not just in one browser's
localStorage. File-backed JSON registry, mirroring the other ``.data`` stores. No secrets.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "graph_prefs.json"

# Canonical layout ids (match the frontend View menu + runLayout()).
VALID_LAYOUTS = ("organic", "hierarchy", "concentric")
DEFAULT_LAYOUT = "organic"


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
    return {"tenants": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _key(tenant_id: str) -> str:
    return (tenant_id or "").strip().lower() or "default"


def get_prefs(tenant_id: str) -> dict[str, Any]:
    """Return ``{layout, updated_at}`` for an Azure tenant (defaults to Organic)."""
    rec = _read().get("tenants", {}).get(_key(tenant_id)) or {}
    layout = rec.get("layout")
    if layout not in VALID_LAYOUTS:
        layout = DEFAULT_LAYOUT
    return {"layout": layout, "updated_at": rec.get("updated_at", "")}


def set_prefs(tenant_id: str, *, layout: str) -> dict[str, Any]:
    """Persist the chosen layout for an Azure tenant. Unknown layouts fall back to default."""
    layout = layout if layout in VALID_LAYOUTS else DEFAULT_LAYOUT
    data = _read()
    tenants = data.setdefault("tenants", {})
    tenants[_key(tenant_id)] = {"layout": layout, "updated_at": _now()}
    _write(data)
    return {"layout": layout, "updated_at": tenants[_key(tenant_id)]["updated_at"]}

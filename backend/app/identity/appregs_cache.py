"""Server-side cache for the Entra ID **App Registrations** snapshot.

The app-registrations pull is expensive (one Graph round-trip per application for owners /
permissions / credentials), so the normalised snapshot is cached — in-memory for instant
hits plus file-persisted (``backend/.data/appregs_cache.json``) so a restart stays fast.
Keyed per tenant + connection. Mirrors ``app.inventory.cache``.

The cache is PERMANENT (no TTL): a stored snapshot is reused indefinitely until an explicit
refresh overwrites it, so visiting the page never triggers a recompute on its own.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "appregs_cache.json"
_mem_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _mem_cache
    if _mem_cache is None:
        if _CACHE_PATH.exists():
            try:
                loaded = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                _mem_cache = loaded if isinstance(loaded, dict) else {}
            except (json.JSONDecodeError, OSError):
                _mem_cache = {}
        else:
            _mem_cache = {}
    return _mem_cache


def _persist() -> None:
    if _mem_cache is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_mem_cache), encoding="utf-8")
    except OSError:
        pass


def _key(tenant_id: str, connection_id: str) -> str:
    return f"{tenant_id or ''}|{connection_id or ''}"


def get(tenant_id: str, connection_id: str) -> dict[str, Any] | None:
    """Return the cached snapshot (with age metadata), or None if missing."""
    entry = _load().get(_key(tenant_id, connection_id))
    if not entry:
        return None
    age = time.time() - float(entry.get("ts", 0))
    return {
        "payload": entry.get("payload", {}),
        "fetched_at": entry.get("fetched_at", ""),
        "age_seconds": int(age),
    }


def set_(tenant_id: str, connection_id: str, payload: dict[str, Any]) -> str:
    """Store a snapshot, return the fetched_at ISO timestamp."""
    cache = _load()
    fetched_at = datetime.now(timezone.utc).isoformat()
    cache[_key(tenant_id, connection_id)] = {"payload": payload, "ts": time.time(), "fetched_at": fetched_at}
    _persist()
    return fetched_at


def delete_demo(tenant_id: str) -> int:
    """Remove any cached app-registration snapshots for the tenant that hold demo data
    (source == 'demo_dummy_data'). Real Graph-backed caches are left untouched. Returns count."""
    cache = _load()
    prefix = f"{tenant_id or ''}|"
    removed = 0
    for k in list(cache):
        if not k.startswith(prefix):
            continue
        payload = (cache[k] or {}).get("payload", {})
        if payload.get("source") == "demo_dummy_data":
            del cache[k]
            removed += 1
    if removed:
        _persist()
    return removed

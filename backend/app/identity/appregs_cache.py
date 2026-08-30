"""Server-side cache for the Entra ID **App Registrations** snapshot.

The app-registrations pull is expensive on large tenants, so the normalized snapshot is
cached — in-memory for instant hits plus file-persisted
(``backend/.data/appregs_cache.json``) so a restart stays fast. A separate checkpoint file
stores completed Graph pages during an in-progress refresh. It never replaces the last-good
snapshot and expires after 24 hours. Both stores are keyed per tenant + connection.

The cache is PERMANENT (no TTL): a stored snapshot is reused indefinitely until an explicit
refresh overwrites it, so visiting the page never triggers a recompute on its own.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "appregs_cache.json"
_CHECKPOINT_PATH = Path(__file__).resolve().parents[2] / ".data" / "appregs_refresh_checkpoints.json"
CHECKPOINT_TTL_SECONDS = 24 * 3600
_mem_cache: dict[str, Any] | None = None
_checkpoint_cache: dict[str, Any] | None = None
_checkpoint_cache_loaded_at = 0.0


def _load() -> dict[str, Any]:
    global _mem_cache
    loaded = jsonstore.read_json(_CACHE_PATH, {})
    _mem_cache = loaded if isinstance(loaded, dict) else {}
    return _mem_cache


def _load_checkpoints() -> dict[str, Any]:
    global _checkpoint_cache, _checkpoint_cache_loaded_at
    if (
        _checkpoint_cache is not None
        and time.monotonic() - _checkpoint_cache_loaded_at <= 0.5
    ):
        return _checkpoint_cache
    loaded = jsonstore.read_json(_CHECKPOINT_PATH, {})
    _checkpoint_cache = loaded if isinstance(loaded, dict) else {}
    _checkpoint_cache_loaded_at = time.monotonic()
    return _checkpoint_cache


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
    fetched_at = datetime.now(timezone.utc).isoformat()
    entry = {"payload": payload, "ts": time.time(), "fetched_at": fetched_at}
    global _mem_cache

    def _mutate(cache: dict[str, Any]) -> None:
        cache[_key(tenant_id, connection_id)] = entry

    try:
        _mem_cache = jsonstore.mutate_json(_CACHE_PATH, {}, _mutate, indent=None)
    except OSError:
        pass
    return fetched_at


def get_checkpoint(tenant_id: str, connection_id: str) -> dict[str, Any] | None:
    """Return an unexpired partial enumeration checkpoint, deleting stale state."""
    key = _key(tenant_id, connection_id)
    checkpoint = _load_checkpoints().get(key)
    if not isinstance(checkpoint, dict):
        return None
    age = time.time() - float(checkpoint.get("updated_ts") or 0)
    if age < 0 or age > CHECKPOINT_TTL_SECONDS:
        delete_checkpoint(tenant_id, connection_id)
        return None
    return json.loads(json.dumps(checkpoint))


def set_checkpoint(tenant_id: str, connection_id: str, checkpoint: dict[str, Any]) -> None:
    """Persist one completed Graph page without touching the last-good snapshot."""
    clean = json.loads(json.dumps(checkpoint))
    clean["updated_ts"] = time.time()
    global _checkpoint_cache, _checkpoint_cache_loaded_at

    def _mutate(cache: dict[str, Any]) -> None:
        cache[_key(tenant_id, connection_id)] = clean

    try:
        _checkpoint_cache = jsonstore.mutate_json(
            _CHECKPOINT_PATH, {}, _mutate, indent=None, separators=(",", ":")
        )
        _checkpoint_cache_loaded_at = time.monotonic()
    except OSError:
        pass


def delete_checkpoint(tenant_id: str, connection_id: str) -> bool:
    global _checkpoint_cache, _checkpoint_cache_loaded_at
    removed = False

    def _mutate(cache: dict[str, Any]) -> None:
        nonlocal removed
        removed = cache.pop(_key(tenant_id, connection_id), None) is not None

    try:
        _checkpoint_cache = jsonstore.mutate_json(
            _CHECKPOINT_PATH, {}, _mutate, indent=None, separators=(",", ":")
        )
        _checkpoint_cache_loaded_at = time.monotonic()
    except OSError:
        pass
    return removed


def delete_demo(tenant_id: str) -> int:
    """Remove any cached app-registration snapshots for the tenant that hold demo data
    (source == 'demo_dummy_data'). Real Graph-backed caches are left untouched. Returns count."""
    prefix = f"{tenant_id or ''}|"
    removed = 0
    global _mem_cache

    def _mutate(cache: dict[str, Any]) -> None:
        nonlocal removed
        for key in list(cache):
            if not key.startswith(prefix):
                continue
            payload = (cache[key] or {}).get("payload", {})
            if payload.get("source") == "demo_dummy_data":
                del cache[key]
                removed += 1

    try:
        _mem_cache = jsonstore.mutate_json(_CACHE_PATH, {}, _mutate, indent=None)
    except OSError:
        pass
    return removed

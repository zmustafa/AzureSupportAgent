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

_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "appregs_cache.json"
_CHECKPOINT_PATH = Path(__file__).resolve().parents[2] / ".data" / "appregs_refresh_checkpoints.json"
CHECKPOINT_TTL_SECONDS = 24 * 3600
_mem_cache: dict[str, Any] | None = None
_checkpoint_cache: dict[str, Any] | None = None


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


def _load_checkpoints() -> dict[str, Any]:
    global _checkpoint_cache
    if _checkpoint_cache is None:
        try:
            loaded = json.loads(_CHECKPOINT_PATH.read_text(encoding="utf-8"))
            _checkpoint_cache = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            _checkpoint_cache = {}
    return _checkpoint_cache


def _persist_checkpoints() -> None:
    if _checkpoint_cache is None:
        return
    try:
        _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CHECKPOINT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_checkpoint_cache, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_CHECKPOINT_PATH)
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
    _load_checkpoints()[_key(tenant_id, connection_id)] = clean
    _persist_checkpoints()


def delete_checkpoint(tenant_id: str, connection_id: str) -> bool:
    cache = _load_checkpoints()
    removed = cache.pop(_key(tenant_id, connection_id), None) is not None
    if removed:
        _persist_checkpoints()
    return removed


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

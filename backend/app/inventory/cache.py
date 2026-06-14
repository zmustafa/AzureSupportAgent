"""Server-side cache for the inventory payload (the resource list + facets is the slow,
expensive part — many Resource Graph queries). In-memory for instant hits + file-persisted
so a backend restart stays fast. Keyed per tenant + connection + scope. Mirrors the policy
cache.

``scope`` is the optional Azure scope selector (``""`` = whole tenant / all visible
subscriptions, ``sub:<id>`` = one subscription, ``mg:<id>`` = all subscriptions under a
management group). Each scope caches independently so switching scopes never re-scans an
already-collected one. An empty scope reuses the legacy ``tenant|connection`` key, so cache
entries collected before scopes existed stay valid.

The cache is PERMANENT (no TTL): a stored payload is reused indefinitely until an explicit
refresh (``force``) overwrites it, so the many Resource Graph queries run only when the user
asks for fresh data.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "inventory_cache.json"
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


def _norm_scope(scope: str) -> str:
    # Canonicalize multi-token scopes so order/dupes never split the cache.
    return ",".join(sorted({t.strip() for t in (scope or "").split(",") if t.strip()}))


def _key(tenant_id: str, connection_id: str, scope: str = "") -> str:
    base = f"{tenant_id or ''}|{connection_id or ''}"
    norm = _norm_scope(scope)
    # Empty scope keeps the legacy key so pre-scope cache entries remain valid.
    return f"{base}|{norm}" if norm else base


def get(tenant_id: str, connection_id: str, ttl: int | None = None, scope: str = "") -> dict[str, Any] | None:
    """Return the cached payload, or None if missing. By default the cache never expires
    (``ttl=None``); pass a positive ``ttl`` (seconds) to treat older entries as a miss."""
    entry = _load().get(_key(tenant_id, connection_id, scope))
    if not entry:
        return None
    age = time.time() - float(entry.get("ts", 0))
    if ttl is not None and age > ttl:
        return None
    return {"payload": entry.get("payload", {}), "fetched_at": entry.get("fetched_at", ""), "age_seconds": int(age)}


def set_(tenant_id: str, connection_id: str, payload: dict[str, Any], scope: str = "") -> str:
    """Store a payload, return the fetched_at ISO timestamp."""
    from datetime import datetime, timezone

    cache = _load()
    fetched_at = datetime.now(timezone.utc).isoformat()
    cache[_key(tenant_id, connection_id, scope)] = {"payload": payload, "ts": time.time(), "fetched_at": fetched_at}
    _persist()
    return fetched_at

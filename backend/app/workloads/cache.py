"""Async in-memory TTL cache for Azure discovery (the resource picker tree).

Discovery calls (MG children, RGs in a subscription, resources in an RG, facets) are slow
because each hits ARM REST or Azure Resource Graph live. This cache makes re-expansion and
re-opens instant, while keeping the data reasonably fresh.

Design notes (enterprise edge cases handled):
- **Per-connection keys** so switching/forgetting a connection never shows another's data.
- **Single-flight**: concurrent identical lookups share ONE upstream Azure call (a slow
  first expand won't be duplicated by impatient double-clicks or two users).
- **Negative-result short TTL**: an empty result (often a transient Azure error, since the
  discovery helpers return [] on failure) is cached only briefly so it self-heals, while a
  real non-empty result is cached for the full TTL.
- **LRU eviction with a hard cap** so a huge tenant can't grow memory unbounded.
- **Explicit invalidation** per connection (the picker's Refresh button) or globally.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_TTL = 86_400.0  # 24h: once loaded/prefetched, only re-fetch on Refresh or after a day
EMPTY_TTL = 60.0  # short TTL ONLY as a transient-failure self-heal (a live query returning
# [] could be a real empty scope OR a transient Azure error). Note: the prefetch + the
# subscription-expand sidecar populate every RG's resources via ``put()``, which always
# uses the full 24h TTL regardless of emptiness — so genuinely-empty resource groups stay
# cached for 24h. This short TTL only applies to a cold, un-warmed direct expand.
MAX_ENTRIES = 4000  # LRU hard cap (raised: 24h TTL keeps more entries resident)


@dataclass
class _Entry:
    value: Any
    cached_at: float  # epoch seconds when the value was computed
    expires_at: float


class DiscoveryCache:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._max = max_entries
        self._guard = asyncio.Lock()

    @staticmethod
    def key(connection_id: str, namespace: str, subkey: str = "") -> str:
        return f"{connection_id}\x1f{namespace}\x1f{subkey}"

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _evict_if_needed(self) -> None:
        while len(self._store) > self._max:
            self._store.popitem(last=False)  # drop the least-recently-used

    async def get_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[Any]],
        *,
        force: bool = False,
        ttl: float = DEFAULT_TTL,
        empty_ttl: float = EMPTY_TTL,
    ) -> tuple[Any, float, bool]:
        """Return (value, cached_at_epoch, from_cache).

        On a hit (and not ``force``) returns the cached value immediately. Otherwise
        computes under a per-key lock (single-flight) and stores the result, choosing a
        short TTL for empty results so transient failures self-heal."""
        now = time.time()
        if not force:
            entry = self._store.get(key)
            if entry is not None and entry.expires_at > now:
                self._store.move_to_end(key)  # mark as recently used
                return entry.value, entry.cached_at, True

        lock = await self._lock_for(key)
        async with lock:
            # Re-check: another waiter may have just populated it.
            now = time.time()
            if not force:
                entry = self._store.get(key)
                if entry is not None and entry.expires_at > now:
                    self._store.move_to_end(key)
                    return entry.value, entry.cached_at, True

            value = await compute()
            cached_at = time.time()
            is_empty = value is None or (isinstance(value, (list, dict, tuple, set)) and len(value) == 0)
            chosen_ttl = empty_ttl if is_empty else ttl
            self._store[key] = _Entry(value=value, cached_at=cached_at, expires_at=cached_at + chosen_ttl)
            self._store.move_to_end(key)
            async with self._guard:
                self._evict_if_needed()
            return value, cached_at, False

    def put(self, key: str, value: Any, *, ttl: float = DEFAULT_TTL) -> float:
        """Directly store a value (used by prefetch, which computes many keys from one
        upstream query). Returns the cached_at epoch."""
        cached_at = time.time()
        self._store[key] = _Entry(value=value, cached_at=cached_at, expires_at=cached_at + ttl)
        self._store.move_to_end(key)
        self._evict_if_needed()
        return cached_at

    def has_fresh(self, key: str) -> bool:
        """Return whether ``key`` has an unexpired entry without changing its LRU order.

        Background warmers use this cheap probe to avoid scheduling Azure calls for scopes
        that are already ready for an instant picker expansion.
        """
        entry = self._store.get(key)
        return entry is not None and entry.expires_at > time.time()

    def invalidate_connection(self, connection_id: str) -> int:
        """Drop all cached entries for one connection. Returns the count removed."""
        prefix = f"{connection_id}\x1f"
        keys = [k for k in self._store if k.startswith(prefix)]
        for k in keys:
            self._store.pop(k, None)
            self._locks.pop(k, None)
        return len(keys)

    def clear(self) -> None:
        self._store.clear()
        self._locks.clear()


# Process-wide singleton.
discovery_cache = DiscoveryCache()

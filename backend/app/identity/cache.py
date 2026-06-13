"""Persistent server-side cache for the Identity dashboard.

Identity snapshots are expensive to build (many Microsoft Graph round-trips), so they are
cached on the Azure Files volume (``backend/.data/identity_cache.json``) — the same place
the workloads/architectures registries live — so the cache survives deploys and restarts.

Layout::

    { "<tenant_id>": { "<days>": { generated_at, ttl_s, days, kpis, groups, errors, ... } } }

A per-(tenant, days) :class:`asyncio.Lock` ensures a thundering herd of concurrent loads
doesn't trigger several simultaneous Graph recomputes — the first request computes while
the rest await it, then read the freshly written snapshot.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "identity_cache.json"

# One lock per (tenant_id, days) bucket. Created lazily; never expires (cheap).
_locks: dict[tuple[str, int], asyncio.Lock] = {}


def get_lock(tenant_id: str, days: int) -> asyncio.Lock:
    """Return the shared recompute lock for a (tenant, expiry-window) bucket."""
    key = (tenant_id or "default", int(days))
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_snapshot(tenant_id: str, days: int) -> dict[str, Any] | None:
    """Return the stored snapshot for (tenant, days) regardless of freshness, or None."""
    bucket = _read().get(tenant_id or "default", {})
    snap = bucket.get(str(int(days)))
    return snap if isinstance(snap, dict) else None


def write_snapshot(tenant_id: str, days: int, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist a snapshot for (tenant, days) and return it."""
    data = _read()
    bucket = data.setdefault(tenant_id or "default", {})
    bucket[str(int(days))] = snapshot
    _write(data)
    return snapshot


def age_seconds(snapshot: dict[str, Any]) -> float | None:
    """Seconds since the snapshot was generated, or None if it has no timestamp."""
    ts = snapshot.get("generated_at")
    if not ts:
        return None
    try:
        gen = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - gen).total_seconds()


def is_fresh(snapshot: dict[str, Any], ttl_s: int) -> bool:
    """True when the snapshot is younger than ``ttl_s`` seconds."""
    age = age_seconds(snapshot)
    return age is not None and age < max(0, int(ttl_s))

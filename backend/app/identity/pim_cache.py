"""Persistent server-side cache for the PIM / JIT lifecycle review.

Mirrors :mod:`app.identity.cache` but keyed by tenant only (one PIM snapshot per tenant —
the review isn't windowed by an expiry horizon the way the credential dashboard is). The
snapshot is expensive to build (Graph round-trips + drift analysis), so it's cached on the
``backend/.data`` volume and only ever recomputed by the explicit Refresh action.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "pim_cache.json"

# One recompute lock per tenant. Created lazily; never expires (cheap).
_locks: dict[str, asyncio.Lock] = {}


def get_lock(tenant_id: str) -> asyncio.Lock:
    """Return the shared recompute lock for a tenant."""
    key = tenant_id or "default"
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


def read_snapshot(tenant_id: str) -> dict[str, Any] | None:
    """Return the stored PIM snapshot for a tenant regardless of freshness, or None."""
    snap = _read().get(tenant_id or "default")
    return snap if isinstance(snap, dict) else None


def write_snapshot(tenant_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist the PIM snapshot for a tenant and return it."""
    data = _read()
    data[tenant_id or "default"] = snapshot
    _write(data)
    return snapshot


def delete_snapshot(tenant_id: str) -> None:
    """Remove a tenant's PIM snapshot (used by demo teardown)."""
    data = _read()
    if (tenant_id or "default") in data:
        del data[tenant_id or "default"]
        _write(data)


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

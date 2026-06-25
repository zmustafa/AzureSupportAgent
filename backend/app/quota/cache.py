"""Persistent server-side cache for Quota snapshots.

A full scan makes many ARM round-trips (per region × per provider), so the latest snapshot is
cached on the data volume (``backend/.data/quota_cache.json``), keyed by ``(tenant, scope_id)``
where ``scope_id`` is the subscription id (or the demo scope). A per-key lock coalesces concurrent
recomputes. Mirrors the reservations / radar caches."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "quota_cache.json"

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def get_lock(tenant_id: str, scope_id: str) -> asyncio.Lock:
    key = (tenant_id or "default", scope_id)
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
    _PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def read_snapshot(tenant_id: str, scope_id: str) -> dict[str, Any] | None:
    bucket = _read().get("snapshots", {}).get(tenant_id or "default", {})
    snap = bucket.get(scope_id)
    return snap if isinstance(snap, dict) else None


def write_snapshot(tenant_id: str, scope_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    bucket = data.setdefault("snapshots", {}).setdefault(tenant_id or "default", {})
    bucket[scope_id] = snapshot
    _write(data)
    return snapshot


def delete_snapshot(tenant_id: str, scope_id: str) -> bool:
    data = _read()
    bucket = data.get("snapshots", {}).get(tenant_id or "default", {})
    if scope_id in bucket:
        del bucket[scope_id]
        _write(data)
        return True
    return False


def age_seconds(snapshot: dict[str, Any]) -> float | None:
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
    age = age_seconds(snapshot)
    return age is not None and age < max(0, int(ttl_s))

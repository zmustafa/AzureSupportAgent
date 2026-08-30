"""Persistent server-side cache for Telemetry Coverage snapshots.

Per-resource diagnostic-settings reads are slow (one call each), so snapshots are cached
on the Azure Files volume (``backend/.data/telemetry_coverage_cache.json``), keyed by
``(tenant, scope_kind, scope_id)``, with a per-key lock to coalesce concurrent recomputes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "telemetry_coverage_cache.json"

_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def get_lock(tenant_id: str, scope_kind: str, scope_id: str) -> asyncio.Lock:
    key = (tenant_id or "default", scope_kind, scope_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


def _key(scope_kind: str, scope_id: str) -> str:
    return f"{scope_kind}:{scope_id}"


def read_snapshot(tenant_id: str, scope_kind: str, scope_id: str) -> dict[str, Any] | None:
    bucket = _read().get(tenant_id or "default", {})
    snap = bucket.get(_key(scope_kind, scope_id))
    return snap if isinstance(snap, dict) else None


def write_snapshot(tenant_id: str, scope_kind: str, scope_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    def _mutate(data: dict[str, Any]) -> None:
        data.setdefault(tenant_id or "default", {})[_key(scope_kind, scope_id)] = snapshot

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return snapshot


def delete_snapshot(tenant_id: str, scope_kind: str, scope_id: str) -> bool:
    """Remove a single cached snapshot (used to purge demo data). True if one was deleted."""
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get(tenant_id or "default", {})
        key = _key(scope_kind, scope_id)
        if key in bucket:
            del bucket[key]
            deleted = True

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return deleted


def purge_errored() -> int:
    """Drop cached snapshots that recorded a hard scan failure. Returns the count removed.

    Coverage GETs are cached-only, so a persisted failure renders as the workload's posture
    until someone manually rescans. Failures are no longer written; this clears the ones
    already on disk. Idempotent — safe to run on every startup.
    """
    removed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal removed
        for bucket in data.values():
            if not isinstance(bucket, dict):
                continue
            for key in [
                stored_key for stored_key, value in bucket.items()
                if isinstance(value, dict) and str(value.get("error") or "").strip()
            ]:
                del bucket[key]
                removed += 1

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return removed


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

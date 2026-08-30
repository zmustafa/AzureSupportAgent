"""Persistent server-side cache for Reservations snapshots + weekly-digest run state.

Listing reservation orders and expanding each with its child reservations means several
ARM round-trips, so snapshots are cached on the data volume
(``backend/.data/reservations_cache.json``), keyed by ``(tenant, scope_id)``, with a
per-key lock to coalesce concurrent recomputes. Mirrors the Radar / coverage caches.

The same file also stores the digest scheduler's last-sent marker so the weekly push
fires at most once per scheduled period across restarts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "reservations_cache.json"

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def get_lock(tenant_id: str, scope_id: str) -> asyncio.Lock:
    key = (tenant_id or "default", scope_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


def read_snapshot(tenant_id: str, scope_id: str) -> dict[str, Any] | None:
    bucket = _read().get("snapshots", {}).get(tenant_id or "default", {})
    snap = bucket.get(scope_id)
    return snap if isinstance(snap, dict) else None


def write_snapshot(tenant_id: str, scope_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    def _mutate(data: dict[str, Any]) -> None:
        data.setdefault("snapshots", {}).setdefault(tenant_id or "default", {})[
            scope_id
        ] = snapshot

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return snapshot


def delete_snapshot(tenant_id: str, scope_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get("snapshots", {}).get(tenant_id or "default", {})
        if scope_id in bucket:
            del bucket[scope_id]
            deleted = True

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return deleted


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


# ----------------------------------------------------------------- digest run-state
def get_last_digest(tenant_id: str) -> dict[str, Any]:
    return _read().get("digest", {}).get(tenant_id or "default", {})


def set_last_digest(tenant_id: str, *, period_key: str, sent_at: str, summary: str) -> dict[str, Any]:
    entry = {"period_key": period_key, "sent_at": sent_at, "summary": summary}

    def _mutate(data: dict[str, Any]) -> None:
        data.setdefault("digest", {})[tenant_id or "default"] = entry

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return entry

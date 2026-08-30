"""Persistent server-side cache for Telemetry Intelligence snapshots.

Caches the timeline + triage + smart-detection inbox (each a multi-query operation) on the
Azure Files volume (``backend/.data/teleintel_cache.json``), keyed by ``(tenant, workload,
component)``, with a per-key lock. Mirrors the other coverage-detector caches."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "teleintel_cache.json"

_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def get_lock(tenant_id: str, scope_id: str, component_id: str) -> asyncio.Lock:
    key = (tenant_id or "default", scope_id, component_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


def _key(scope_id: str, component_id: str, kind: str) -> str:
    return f"{scope_id}|{component_id}|{kind}"


def read_snapshot(tenant_id: str, scope_id: str, component_id: str, kind: str) -> dict[str, Any] | None:
    bucket = _read().get(tenant_id or "default", {})
    snap = bucket.get(_key(scope_id, component_id, kind))
    return snap if isinstance(snap, dict) else None


def write_snapshot(tenant_id: str, scope_id: str, component_id: str, kind: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    def _mutate(data: dict[str, Any]) -> None:
        data.setdefault(tenant_id or "default", {})[
            _key(scope_id, component_id, kind)
        ] = snapshot

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return snapshot


def delete_scope(tenant_id: str, scope_id: str) -> int:
    """Remove every cached snapshot (all components/kinds) for a scope. Returns the count
    deleted. Used to purge demo data."""
    removed: list[str] = []

    def _mutate(data: dict[str, Any]) -> None:
        bucket = data.get(tenant_id or "default", {})
        prefix = f"{scope_id}|"
        removed.extend(key for key in list(bucket) if key.startswith(prefix))
        for key in removed:
            del bucket[key]

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return len(removed)


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

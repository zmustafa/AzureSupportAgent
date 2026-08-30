"""Tenant- and scope-isolated persistent cache for Alerts Manager snapshots."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "alert_analysis_cache.json"
_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}


def get_lock(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> asyncio.Lock:
    key = (tenant_id or "default", connection_id or "default", scope_kind, scope_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _read() -> dict[str, Any]:
    value = jsonstore.read_json(_PATH, {})
    return value if isinstance(value, dict) else {}


def _write(value: dict[str, Any]) -> None:
    jsonstore.write_json(_PATH, value)


def _key(connection_id: str, scope_kind: str, scope_id: str) -> str:
    return f"{connection_id or 'default'}:{scope_kind}:{scope_id}"


def read_snapshot(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> dict[str, Any] | None:
    value = _read().get(tenant_id or "default", {}).get(_key(connection_id, scope_kind, scope_id))
    return value if isinstance(value, dict) else None


def write_snapshot(
    tenant_id: str,
    connection_id: str,
    scope_kind: str,
    scope_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    def _mutate(data: dict[str, Any]) -> None:
        data.setdefault(tenant_id or "default", {})[
            _key(connection_id, scope_kind, scope_id)
        ] = snapshot

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return snapshot


def delete_snapshot(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> bool:
    key = _key(connection_id, scope_kind, scope_id)
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get(tenant_id or "default", {})
        if key in bucket:
            del bucket[key]
            deleted = True

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return deleted


def age_seconds(snapshot: dict[str, Any]) -> float | None:
    raw = snapshot.get("generated_at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def is_fresh(snapshot: dict[str, Any], ttl_s: int) -> bool:
    age = age_seconds(snapshot)
    return age is not None and age < max(0, int(ttl_s))

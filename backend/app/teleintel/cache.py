"""Persistent server-side cache for Telemetry Intelligence snapshots.

Caches the timeline + triage + smart-detection inbox (each a multi-query operation) on the
Azure Files volume (``backend/.data/teleintel_cache.json``), keyed by ``(tenant, workload,
component)``, with a per-key lock. Mirrors the other coverage-detector caches."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _key(scope_id: str, component_id: str, kind: str) -> str:
    return f"{scope_id}|{component_id}|{kind}"


def read_snapshot(tenant_id: str, scope_id: str, component_id: str, kind: str) -> dict[str, Any] | None:
    bucket = _read().get(tenant_id or "default", {})
    snap = bucket.get(_key(scope_id, component_id, kind))
    return snap if isinstance(snap, dict) else None


def write_snapshot(tenant_id: str, scope_id: str, component_id: str, kind: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    bucket = data.setdefault(tenant_id or "default", {})
    bucket[_key(scope_id, component_id, kind)] = snapshot
    _write(data)
    return snapshot


def delete_scope(tenant_id: str, scope_id: str) -> int:
    """Remove every cached snapshot (all components/kinds) for a scope. Returns the count
    deleted. Used to purge demo data."""
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    prefix = f"{scope_id}|"
    keys = [k for k in list(bucket) if k.startswith(prefix)]
    for k in keys:
        del bucket[k]
    if keys:
        _write(data)
    return len(keys)


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

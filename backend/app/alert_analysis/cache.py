"""Tenant- and scope-isolated persistent cache for Alerts Manager snapshots."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "alert_analysis_cache.json"
_MAX_SIDECARS = 128
_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
_legacy_guard = threading.Lock()
_legacy_cached: tuple[str, int, int, dict[str, Any]] | None = None


def get_lock(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> asyncio.Lock:
    key = (tenant_id or "default", connection_id or "default", scope_kind, scope_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _identity(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> tuple[str, str, str, str]:
    return (
        tenant_id or "default",
        connection_id or "default",
        scope_kind,
        scope_id,
    )


def _sidecar_path(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> Path:
    identity = _identity(tenant_id, connection_id, scope_kind, scope_id)
    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode("utf-8")).hexdigest()
    return _PATH.with_suffix("") / digest[:2] / f"{digest}.json"


def _key(connection_id: str, scope_kind: str, scope_id: str) -> str:
    return f"{connection_id or 'default'}:{scope_kind}:{scope_id}"


def _legacy_read() -> dict[str, Any]:
    """Parse the legacy monolith at most once per on-disk version without copying it."""
    global _legacy_cached
    try:
        stat = _PATH.stat()
    except OSError:
        return {}
    signature = (str(_PATH), stat.st_mtime_ns, stat.st_size)
    with _legacy_guard:
        cached = _legacy_cached
        if cached is not None and cached[:3] == signature:
            return cached[3]
        try:
            value = json.loads(_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            value = {}
        parsed = value if isinstance(value, dict) else {}
        _legacy_cached = (*signature, parsed)
        return parsed


def _prune_sidecars(current: Path) -> None:
    root = _PATH.with_suffix("")
    try:
        files = [path for path in root.glob("*/*.json") if path != current]
        excess = len(files) + 1 - _MAX_SIDECARS
        if excess <= 0:
            return
        files.sort(key=lambda path: path.stat().st_mtime_ns)
        for path in files[:excess]:
            path.unlink(missing_ok=True)
    except OSError:
        # Cache retention must never make a successful analysis fail.
        return


def read_snapshot(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> dict[str, Any] | None:
    identity = _identity(tenant_id, connection_id, scope_kind, scope_id)
    sidecar = _sidecar_path(*identity)
    record = jsonstore.read_json(sidecar, {})
    if isinstance(record, dict) and tuple(record.get("identity") or ()) == identity:
        snapshot = record.get("snapshot")
        return snapshot if isinstance(snapshot, dict) else None

    # Lazy migration keeps existing deployments readable without making every future
    # request copy every tenant and scope from the old monolithic document.
    value = _legacy_read().get(identity[0], {}).get(_key(identity[1], identity[2], identity[3]))
    if not isinstance(value, dict):
        return None
    snapshot = copy.deepcopy(value)
    write_snapshot(*identity, snapshot)
    return snapshot


def write_snapshot(
    tenant_id: str,
    connection_id: str,
    scope_kind: str,
    scope_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    identity = _identity(tenant_id, connection_id, scope_kind, scope_id)
    sidecar = _sidecar_path(*identity)
    jsonstore.write_json(
        sidecar,
        {"identity": list(identity), "snapshot": snapshot},
        indent=None,
        separators=(",", ":"),
    )
    _prune_sidecars(sidecar)
    return snapshot


def delete_snapshot(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> bool:
    global _legacy_cached
    identity = _identity(tenant_id, connection_id, scope_kind, scope_id)
    deleted = jsonstore.delete_json(_sidecar_path(*identity))
    key = _key(identity[1], identity[2], identity[3])

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get(identity[0], {})
        if key in bucket:
            del bucket[key]
            deleted = True

    if _PATH.exists():
        jsonstore.mutate_json(_PATH, {}, _mutate)
        with _legacy_guard:
            _legacy_cached = None
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

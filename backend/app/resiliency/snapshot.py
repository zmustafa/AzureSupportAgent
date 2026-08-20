"""Durable, per-scope Recovery Readiness snapshots.

Same contract as Backup Manager's: one analysis per ``(tenant, connection, scope)``,
computed only when the operator explicitly asks, and served unchanged to every tab until
they ask again. :func:`read` never computes — if nothing has been analyzed the shell comes
back with ``report_exists: False``, which is the UI's cue to offer the button instead of
silently firing a sweep.

Numbers that move under the reader while they are working a decision are worse than numbers
that are a day old.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("app.resiliency.snapshot")

SCHEMA_VERSION = 1

_PATH = Path(__file__).resolve().parents[2] / ".data" / "resiliency_snapshot.json"

MAX_SCOPES = 24
MAX_ROWS = {"resources": 5000, "breaches": 5000, "workloads": 500}

_locks: dict[str, asyncio.Lock] = {}


def set_path_for_tests(path: Path) -> None:
    global _PATH
    _PATH = path


def _key(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> str:
    return "|".join((str(tenant_id or "default"), str(connection_id or "default"),
                     str(scope_kind or ""), str(scope_id or "").lower()))


def get_lock(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> asyncio.Lock:
    key = _key(tenant_id, connection_id, scope_kind, scope_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _read_all() -> dict[str, Any]:
    if not _PATH.exists():
        return {}
    try:
        value = json.loads(_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("resiliency: unreadable snapshot store, starting empty: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


def _write_all(value: dict[str, Any]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(value), encoding="utf-8")
        tmp.replace(_PATH)
    except OSError as exc:
        log.warning("resiliency: could not persist snapshot: %s", exc)


def empty(scope_kind: str = "", scope_id: str = "", reason: str = "") -> dict[str, Any]:
    """The shell served when a scope has never been analyzed.

    Every section present and empty, so the UI renders its tabs without null guards;
    ``report_exists`` is what tells it to offer the Analyze button instead of the data."""
    return {
        "schema_version": SCHEMA_VERSION,
        "report_exists": False,
        "generated_at": "",
        "demo": False,
        "reason": reason,
        "scope": {"scope_kind": scope_kind, "scope_id": scope_id, "subscriptions": []},
        "summary": {"resources": 0, "by_scenario": {}, "protection": {},
                    "worst": {"scenario": "", "no_recovery_path": 0}},
        "resources": [],
        "breaches": [],
        "breach_summary": {},
        "workloads": [],
        "provenance": {},
        "truncation": {},
    }


def estate_unreadable(snapshot: dict[str, Any]) -> str:
    """Why the estate itself could not be read, or ``""`` when it was.

    Configuration is the only source that ENUMERATES resources. If it failed, every count
    downstream is zero because we saw nothing — not because there is nothing — and a green
    "0 with no recovery path" is then a statement about our own blindness. Keyed on
    configuration alone: ``protection`` is routinely unreadable (Backup Manager has not run
    for the scope) and that must stay a footnote, not a headline.
    """
    prov = (snapshot.get("provenance") or {}).get("configuration") or {}
    if not prov.get("unreadable"):
        return ""
    return str(prov.get("reason") or "").strip() or "The resource configuration could not be read."


def bound(snapshot: dict[str, Any]) -> dict[str, Any]:
    truncation = snapshot.setdefault("truncation", {})
    for key, limit in MAX_ROWS.items():
        rows = snapshot.get(key) or []
        if len(rows) > limit:
            snapshot[key] = rows[:limit]
            truncation[key] = {"exported": limit, "known_total": len(rows)}
    return snapshot


def read(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> dict[str, Any]:
    """Never computes. An un-analyzed scope returns the shell."""
    entry = _read_all().get(_key(tenant_id, connection_id, scope_kind, scope_id))
    if not isinstance(entry, dict) or entry.get("schema_version") != SCHEMA_VERSION:
        return empty(scope_kind, scope_id)
    return entry


def write(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str,
          snapshot: dict[str, Any]) -> None:
    store = _read_all()
    store[_key(tenant_id, connection_id, scope_kind, scope_id)] = bound(snapshot)
    if len(store) > MAX_SCOPES:
        ordered = sorted(store.items(), key=lambda kv: str(kv[1].get("generated_at") or ""))
        for key, _ in ordered[: len(store) - MAX_SCOPES]:
            store.pop(key, None)
    _write_all(store)


def clear(tenant_id: str = "", connection_id: str = "", scope_kind: str = "",
          scope_id: str = "") -> int:
    """Drop one scope, or everything when no scope is given."""
    store = _read_all()
    if not scope_id and not scope_kind:
        count = len(store)
        _write_all({})
        return count
    key = _key(tenant_id, connection_id, scope_kind, scope_id)
    removed = 1 if store.pop(key, None) is not None else 0
    _write_all(store)
    return removed


def list_scopes() -> list[dict[str, Any]]:
    return [
        {"key": key, "scope": entry.get("scope", {}),
         "generated_at": entry.get("generated_at", ""),
         "resources": len(entry.get("resources") or [])}
        for key, entry in _read_all().items() if isinstance(entry, dict)
    ]


__all__ = ["read", "write", "clear", "empty", "bound", "get_lock", "list_scopes",
           "estate_unreadable", "set_path_for_tests", "SCHEMA_VERSION"]

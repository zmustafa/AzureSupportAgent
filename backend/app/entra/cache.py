"""Persistent, per-domain snapshot cache for the Entra ID Support Agent.

Layout (mirrors ``rbac/cache.py``'s gzipped-sidecar approach, which is the only pattern in
this repo that survives real tenant sizes)::

    .data/entra/
      index.json                       per-tenant domain freshness / status / counts
      <tenant_id>/
        tenant.json.gz  people.json.gz  apps.json.gz  roles.json.gz  ca.json.gz
        findings.json.gz               last signal-evaluation output
        findings_state.json            USER state (suppressions, tickets, first_seen) — never
                                       rewritten by a collection run
        score_history.json             append-only, capped
        ca_baseline.json               policy-as-code baseline for drift

Rules enforced here:

* GET endpoints read this cache only — collection happens exclusively behind ``POST /refresh``.
* One :class:`asyncio.Lock` per ``(tenant, domain)`` prevents a thundering herd.
* **In-process memo keyed by (path, mtime, size)** so repeated reads inside one request
  cycle do not re-decompress. ``rbac/compose.py`` re-reads and re-gunzips every sidecar on
  *every* request with no memo — that is the single biggest perf issue in that module and
  it is deliberately not repeated here.
* Stale-while-error: a failed collection keeps the previous payload and records the error
  on the domain meta.
* Schema versioning: an older payload with no migration is reported as *not loaded* rather
  than crashing the page.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("app.entra.cache")

SCHEMA_VERSION = 1

_ROOT = Path(__file__).resolve().parents[2] / ".data" / "entra"
_INDEX = _ROOT / "index.json"

# One lock per (tenant, domain). Created lazily; never expires (cheap).
_locks: dict[tuple[str, str], asyncio.Lock] = {}

# (path, mtime_ns, size) -> parsed payload. Bounded by the number of (tenant, domain) pairs.
_memo: dict[str, tuple[int, int, Any]] = {}

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(name: str) -> str:
    """Filesystem-safe path component.

    Tenant ids are GUIDs in practice, but an id is never trusted in a path: separators are
    replaced, and a name consisting only of dots (``..``) is rejected outright because it
    would still traverse after substitution.
    """
    cleaned = _SAFE.sub("_", (name or "default").strip())
    if not cleaned or set(cleaned) <= {"."}:
        return "default"
    return cleaned


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tenant_dir(tenant_id: str) -> Path:
    return _ROOT / _safe(tenant_id)


def get_lock(tenant_id: str, domain: str) -> asyncio.Lock:
    key = (_safe(tenant_id), domain)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


# --------------------------------------------------------------------- gz sidecars
def _read_gz(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    cached = _memo.get(str(path))
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        raw = gzip.decompress(path.read_bytes())
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, gzip.BadGzipFile, json.JSONDecodeError, UnicodeDecodeError):
        log.warning("entra cache sidecar unreadable: %s", path.name)
        return None
    _memo[str(path)] = (st.st_mtime_ns, st.st_size, payload)
    return payload


def _write_gz(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    _memo.pop(str(path), None)


def read_domain(tenant_id: str, domain: str) -> dict[str, Any] | None:
    """Return the stored payload for one domain regardless of freshness, or None.

    A payload written by an incompatible schema version is treated as absent (the caller
    prompts a refresh) rather than raising."""
    payload = _read_gz(tenant_dir(tenant_id) / f"{_safe(domain)}.json.gz")
    if not isinstance(payload, dict):
        return None
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        return None
    return payload


def write_domain(tenant_id: str, domain: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist one domain payload and refresh its index entry."""
    body = dict(payload)
    body["schema_version"] = SCHEMA_VERSION
    body.setdefault("generated_at", now_iso())
    body.setdefault("domain", domain)
    _write_gz(tenant_dir(tenant_id) / f"{_safe(domain)}.json.gz", body)
    set_domain_meta(tenant_id, domain, meta_of(body))
    return body


def meta_of(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the index-level meta from a domain payload."""
    return {
        "name": payload.get("domain", ""),
        "status": payload.get("status", "ok"),
        "generated_at": payload.get("generated_at", ""),
        "item_count": int(payload.get("item_count") or 0),
        "duration_ms": int(payload.get("duration_ms") or 0),
        "error": payload.get("error", ""),
        "missing_permissions": payload.get("missing_permissions") or [],
        "truncated": bool(payload.get("truncated")),
        "notes": payload.get("notes") or [],
        "blockers": payload.get("blockers") or [],
    }


# ------------------------------------------------------------------------- index
def read_index() -> dict[str, Any]:
    if not _INDEX.exists():
        return {}
    try:
        data = json.loads(_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_index(data: dict[str, Any]) -> None:
    _INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_INDEX)


def tenant_index(tenant_id: str) -> dict[str, Any]:
    entry = read_index().get(_safe(tenant_id))
    return entry if isinstance(entry, dict) else {}


def set_domain_meta(tenant_id: str, domain: str, meta: dict[str, Any]) -> None:
    data = read_index()
    entry = data.setdefault(_safe(tenant_id), {"domains": {}, "schema_version": SCHEMA_VERSION})
    entry.setdefault("domains", {})[domain] = meta
    entry["schema_version"] = SCHEMA_VERSION
    entry["updated_at"] = now_iso()
    _write_index(data)


def mark_full_refresh(tenant_id: str) -> None:
    data = read_index()
    entry = data.setdefault(_safe(tenant_id), {"domains": {}, "schema_version": SCHEMA_VERSION})
    entry["last_full"] = now_iso()
    _write_index(data)


def set_tenant_meta(tenant_id: str, **fields: Any) -> None:
    """Set tenant-level index fields (licenses, permissions, ...)."""
    data = read_index()
    entry = data.setdefault(_safe(tenant_id), {"domains": {}, "schema_version": SCHEMA_VERSION})
    entry.update(fields)
    entry["updated_at"] = now_iso()
    _write_index(data)


def domain_meta(tenant_id: str, domain: str) -> dict[str, Any] | None:
    meta = (tenant_index(tenant_id).get("domains") or {}).get(domain)
    return meta if isinstance(meta, dict) else None


# ------------------------------------------------------------------- state files
def _state_path(tenant_id: str, name: str) -> Path:
    return tenant_dir(tenant_id) / f"{_safe(name)}.json"


def read_state(tenant_id: str, name: str, default: Any = None) -> Any:
    path = _state_path(tenant_id, name)
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default if default is not None else {}


def write_state(tenant_id: str, name: str, payload: Any) -> None:
    path = _state_path(tenant_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_score_history(tenant_id: str, entry: dict[str, Any], *, cap: int = 365) -> list[dict[str, Any]]:
    """Append one score point. Only successful FULL refreshes should call this."""
    hist = read_state(tenant_id, "score_history", [])
    if not isinstance(hist, list):
        hist = []
    hist.append(entry)
    hist = hist[-max(1, cap):]
    write_state(tenant_id, "score_history", hist)
    return hist


def score_history(tenant_id: str) -> list[dict[str, Any]]:
    hist = read_state(tenant_id, "score_history", [])
    return hist if isinstance(hist, list) else []


# ------------------------------------------------------------------------ helpers
def age_seconds(generated_at: str) -> float | None:
    if not generated_at:
        return None
    try:
        gen = datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        return None
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - gen).total_seconds()


def clear_memo() -> None:
    """Drop the in-process parse memo (tests, and after an out-of-band file change)."""
    _memo.clear()


def set_root_for_tests(path: Path) -> None:
    """Point the cache at a temp directory (pytest only)."""
    global _ROOT, _INDEX
    _ROOT = path
    _INDEX = path / "index.json"
    clear_memo()
    _locks.clear()

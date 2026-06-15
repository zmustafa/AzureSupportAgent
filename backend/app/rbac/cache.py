"""Per-scope + directory server-side cache for the RBAC access review.

A full tenant access scan is slow and uneven (in the sample run, one resource-group RBAC
collector took ~3 minutes). So the cache unit is the **scope**: each subscription / management
group / resource group keeps its own slice with its own freshness, and a single scope can be
refreshed while the rest stay served from cache. Tenant-global facts that don't belong to one
scope (Entra directory roles, role definitions, principal directory, the group-expansion graph)
live in a shared **directory** layer refreshed on its own cadence.

Layout on disk (Azure Files volume, same place the other registries live)::

    .data/rbac_cache.json                      # light index: per-(tenant) directory + scopes meta
    .data/rbac/<tenant>/<scope-hash>.json.gz   # one scope's rows (gzipped — rows are large)
    .data/rbac/<tenant>/directory.json.gz      # directory rows + role defs + principals + groups

The index holds only metadata (freshness, collector statuses, counts, ``rows_ref``) so reading
the Overview never inflates the whole row set; the heavy rows are pulled from a gzip sidecar
only when a grid actually needs them."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parents[2] / ".data"
_INDEX = _DATA / "rbac_cache.json"
_BLOBS = _DATA / "rbac"

# One recompute lock per (tenant, scope) bucket — the per-scope generalization of the identity
# dashboard's per-(tenant, days) lock. "directory" is a reserved scope key for the shared layer.
_locks: dict[tuple[str, str], asyncio.Lock] = {}

DIRECTORY_KEY = "directory"


def get_lock(tenant_id: str, scope: str) -> asyncio.Lock:
    """Shared recompute lock for a (tenant, scope) bucket (created lazily, never expires)."""
    key = (tenant_id or "default", scope or DIRECTORY_KEY)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_hash(scope: str) -> str:
    """Stable, filesystem-safe sidecar name for a scope id."""
    if scope == DIRECTORY_KEY:
        return DIRECTORY_KEY
    return hashlib.sha1((scope or "").encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- index I/O
def _read_index() -> dict[str, Any]:
    if _INDEX.exists():
        try:
            data = json.loads(_INDEX.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_index(data: dict[str, Any]) -> None:
    _INDEX.parent.mkdir(parents=True, exist_ok=True)
    _INDEX.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _tenant_bucket(data: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    bucket = data.setdefault(tenant_id or "default", {})
    bucket.setdefault("scopes", {})
    bucket.setdefault(DIRECTORY_KEY, {})
    return bucket


# --------------------------------------------------------------------------- sidecar I/O
def _blob_path(tenant_id: str, scope: str) -> Path:
    return _BLOBS / (tenant_id or "default") / f"{_scope_hash(scope)}.json.gz"


def _write_blob(tenant_id: str, scope: str, payload: dict[str, Any]) -> None:
    path = _blob_path(tenant_id, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path.write_bytes(gzip.compress(raw))


def _read_blob(tenant_id: str, scope: str) -> dict[str, Any]:
    path = _blob_path(tenant_id, scope)
    if not path.exists():
        return {}
    try:
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError):
        return {}


def _delete_blob(tenant_id: str, scope: str) -> None:
    try:
        _blob_path(tenant_id, scope).unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- freshness
def age_seconds(generated_at: str | None) -> float | None:
    """Seconds since an ISO timestamp, or None when absent/unparseable."""
    if not generated_at:
        return None
    try:
        gen = datetime.fromisoformat(generated_at)
    except (ValueError, TypeError):
        return None
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - gen).total_seconds()


def is_fresh(generated_at: str | None, ttl_s: int) -> bool:
    age = age_seconds(generated_at)
    return age is not None and age < max(0, int(ttl_s))


# --------------------------------------------------------------------------- scope slice
def write_scope(
    tenant_id: str,
    scope: str,
    *,
    meta: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist one scope's normalized rows (gzipped sidecar) + its index metadata.

    ``meta`` carries the scope's display fields, collector statuses, coverage and ``demo`` flag;
    this stamps ``generated_at``, ``row_count`` and the sidecar ``rows_ref`` onto it."""
    entry = dict(meta)
    entry["scope"] = scope
    entry["generated_at"] = entry.get("generated_at") or _now_iso()
    entry["row_count"] = len(rows)
    entry["rows_ref"] = _scope_hash(scope)
    _write_blob(tenant_id, scope, {"rows": rows})
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    bucket["scopes"][scope] = entry
    _write_index(data)
    return entry


def read_scope_meta(tenant_id: str, scope: str) -> dict[str, Any] | None:
    bucket = _tenant_bucket(_read_index(), tenant_id)
    entry = bucket["scopes"].get(scope)
    return entry if isinstance(entry, dict) else None


def read_scope_rows(tenant_id: str, scope: str) -> list[dict[str, Any]]:
    payload = _read_blob(tenant_id, scope)
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else []


def list_scope_meta(tenant_id: str) -> list[dict[str, Any]]:
    """All cached scope metadata for a tenant (no rows), newest-first by generated_at."""
    bucket = _tenant_bucket(_read_index(), tenant_id)
    scopes = [v for v in bucket["scopes"].values() if isinstance(v, dict)]
    scopes.sort(key=lambda s: str(s.get("generated_at", "")), reverse=True)
    return scopes


def all_scope_rows(tenant_id: str) -> list[dict[str, Any]]:
    """Concatenate every cached scope's rows (used to compose the effective-access grid)."""
    rows: list[dict[str, Any]] = []
    for meta in list_scope_meta(tenant_id):
        rows.extend(read_scope_rows(tenant_id, str(meta.get("scope", ""))))
    return rows


def delete_scope(tenant_id: str, scope: str) -> bool:
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    existed = bucket["scopes"].pop(scope, None) is not None
    _write_index(data)
    _delete_blob(tenant_id, scope)
    return existed


# --------------------------------------------------------------------------- directory layer
def write_directory(
    tenant_id: str,
    *,
    meta: dict[str, Any],
    rows: list[dict[str, Any]],
    role_defs: list[dict[str, Any]] | None = None,
    principals: list[dict[str, Any]] | None = None,
    groups: dict[str, Any] | None = None,
    management_groups: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Persist the tenant directory layer (Entra roles + SP-owner rows + the reference sets).

    ``groups`` is the group-expansion graph (group id → effective members) used to derive
    GroupTransitive effective rows when composing. ``management_groups`` maps a management-group
    id (lower-cased) → its display name so the scope tree + MG-scoped rows show names not GUIDs."""
    entry = dict(meta)
    entry["generated_at"] = entry.get("generated_at") or _now_iso()
    entry["row_count"] = len(rows)
    entry["role_def_count"] = len(role_defs or [])
    entry["principal_count"] = len(principals or [])
    entry["group_count"] = len(groups or {})
    entry["rows_ref"] = DIRECTORY_KEY
    _write_blob(
        tenant_id,
        DIRECTORY_KEY,
        {
            "rows": rows,
            "role_defs": role_defs or [],
            "principals": principals or [],
            "groups": groups or {},
            "management_groups": management_groups or {},
        },
    )
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    bucket[DIRECTORY_KEY] = entry
    _write_index(data)
    return entry


def read_directory_meta(tenant_id: str) -> dict[str, Any]:
    bucket = _tenant_bucket(_read_index(), tenant_id)
    entry = bucket.get(DIRECTORY_KEY)
    return entry if isinstance(entry, dict) else {}


def read_directory(tenant_id: str) -> dict[str, Any]:
    """Full directory payload: rows + role_defs + principals + groups + MG names (from the sidecar)."""
    payload = _read_blob(tenant_id, DIRECTORY_KEY)
    return {
        "rows": payload.get("rows") or [],
        "role_defs": payload.get("role_defs") or [],
        "principals": payload.get("principals") or [],
        "groups": payload.get("groups") or {},
        "management_groups": payload.get("management_groups") or {},
    }


# --------------------------------------------------------------------------- tenant-wide ops
def has_any(tenant_id: str) -> bool:
    """True when the tenant has at least one cached scope or a directory snapshot."""
    bucket = _tenant_bucket(_read_index(), tenant_id)
    return bool(bucket["scopes"]) or bool(bucket.get(DIRECTORY_KEY))


def delete_tenant(tenant_id: str) -> int:
    """Drop every cached scope + the directory for a tenant (demo purge). Returns scopes removed."""
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    n = len(bucket["scopes"])
    for scope in list(bucket["scopes"].keys()):
        _delete_blob(tenant_id, scope)
    _delete_blob(tenant_id, DIRECTORY_KEY)
    data[tenant_id or "default"] = {"scopes": {}, DIRECTORY_KEY: {}}
    _write_index(data)
    return n


def purge_demo(tenant_id: str) -> int:
    """Remove ONLY demo-flagged scope slices (and the directory layer if it is demo), leaving any
    real scan slices cached under the same tenant intact. Returns the number of scopes removed.

    This is the surgical counterpart to :func:`delete_tenant`: a "Remove demo data" action must
    never wipe a real access scan that happens to share the tenant cache with the demo dataset."""
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    removed = 0
    for scope, meta in list(bucket["scopes"].items()):
        if isinstance(meta, dict) and meta.get("demo"):
            _delete_blob(tenant_id, scope)
            del bucket["scopes"][scope]
            removed += 1
    if (bucket.get(DIRECTORY_KEY) or {}).get("demo"):
        _delete_blob(tenant_id, DIRECTORY_KEY)
        bucket[DIRECTORY_KEY] = {}
    _write_index(data)
    return removed


def is_demo(tenant_id: str) -> bool:
    """True when the tenant's cached snapshot was produced by the demo seeder."""
    bucket = _tenant_bucket(_read_index(), tenant_id)
    if (bucket.get(DIRECTORY_KEY) or {}).get("demo"):
        return True
    return any(bool(s.get("demo")) for s in bucket["scopes"].values() if isinstance(s, dict))


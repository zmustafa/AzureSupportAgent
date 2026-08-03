"""Per-scope + directory server-side cache for the IAM access review.

A full tenant access scan is slow and uneven (in the sample run, one resource-group RBAC
collector took ~3 minutes). So the cache unit is the **scope**: each subscription / management
group / resource group keeps its own slice with its own freshness, and a single scope can be
refreshed while the rest stay served from cache. Tenant-global facts that don't belong to one
scope (Entra directory roles, role definitions, principal directory, the group-expansion graph)
live in a shared **directory** layer refreshed on its own cadence.

Layout on disk (Azure Files volume, same place the other registries live)::

    .data/iam_cache.json                      # light index: per-(tenant) directory + scopes meta
    .data/iam/<tenant>/<scope-hash>.json.gz   # one scope's rows (gzipped — rows are large)
    .data/iam/<tenant>/directory.json.gz      # directory rows + role defs + principals + groups

The index holds only metadata (freshness, collector statuses, counts, ``rows_ref``) so reading
the Overview never inflates the whole row set; the heavy rows are pulled from a gzip sidecar
only when a grid actually needs them.

The pre-rename ``.data/rbac_cache.json`` / ``.data/rbac/`` locations are migrated in place on
first use (see :func:`_migrate_legacy_paths`) — without it every tenant would report "never
collected" after an upgrade and a full re-collect on a large estate looks exactly like data
loss."""
from __future__ import annotations

import asyncio
import functools
import gzip
import hashlib
import json
import logging
import shutil
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("app.iam.cache")

_DATA = Path(__file__).resolve().parents[2] / ".data"
_INDEX = _DATA / "iam_cache.json"
_BLOBS = _DATA / "iam"

_migrated = False


def _migrate_legacy_paths() -> None:
    """Move the pre-rename (``/rbac``) cache into place. Idempotent and never destructive.

    Runs once per process. The legacy names are derived from the *current* ``_INDEX`` /
    ``_BLOBS`` values rather than hard-coded against ``.data`` — tests monkeypatch those to a
    tmp dir, and a hard-coded legacy path would move the operator's real cache into it.

    If the new path already exists the legacy one is left alone rather than merged: a
    half-merged cache is worse than a stale one, and the legacy copy stays on disk as a
    manual fallback."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    legacy_index = _INDEX.parent / "rbac_cache.json"
    legacy_blobs = _BLOBS.parent / "rbac"
    try:
        if legacy_index.is_file() and not _INDEX.exists():
            shutil.move(str(legacy_index), str(_INDEX))
            log.info("iam cache: migrated %s -> %s", legacy_index.name, _INDEX.name)
        if legacy_blobs.is_dir() and not _BLOBS.exists():
            shutil.move(str(legacy_blobs), str(_BLOBS))
            log.info("iam cache: migrated %s/ -> %s/", legacy_blobs.name, _BLOBS.name)
    except OSError as exc:  # pragma: no cover - filesystem-specific
        # Never let a migration failure take the feature down; worst case is a cold cache.
        log.warning("iam cache: legacy path migration skipped (%s)", exc)

# One recompute lock per (tenant, scope) bucket — the per-scope generalization of the identity
# dashboard's per-(tenant, days) lock. "directory" is a reserved scope key for the shared layer.
_locks: dict[tuple[str, str], asyncio.Lock] = {}

DIRECTORY_KEY = "directory"
# The RBAC-bypass sweep lives in its own slice. A bypass row has NO principal — it is a property
# of a resource — so mixing it into the access rows would corrupt every per-principal pivot and
# every KPI in the product.
BYPASS_KEY = "bypass"
# The classified diff from the most recent run. Written by `store.save_run` so the SYNCHRONOUS
# findings context can read it without a database round-trip — signals are pure functions over a
# snapshot, and making one of them async would make all of them async.
DRIFT_KEY = "drift"
# Usage lives in its own slice with its own `generated_at`, and NEVER inside the access
# snapshot. The Activity Log is per-subscription and slow, so usage is collected by a separate
# schedulable job — which means access can be minutes old while usage is weeks old, and the UI
# has to be able to say so rather than implying one freshness for both.
USAGE_KEY = "usage"


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
    return hashlib.sha1((scope or "").encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


# --------------------------------------------------------------------------- index I/O
def _read_index() -> dict[str, Any]:
    _migrate_legacy_paths()
    if _INDEX.exists():
        try:
            data = json.loads(_INDEX.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# Monotonic write counter — bumped when the SOURCE data changes (scope rows, the directory
# layer, deletions) so in-process consumers (`compose.build_master_rows`, the escalation graph)
# can detect a real change reliably, independent of filesystem mtime granularity.
#
# Derived artefacts do NOT bump it, and that distinction is load-bearing. It used to be "any
# write at all", which had two consequences:
#
#   * the escalation graph invalidated ITSELF — persisting it bumped the very counter its
#     freshness stamp was compared against, so the 30-second build was re-paid on every single
#     request and the cache never once hit;
#   * writing a scanner baseline (nine per sweep) or a right-sizing analysis threw away the
#     master-rows memo, forcing a full recompose each time for data that had not changed.
#
# The version answers "have the rows changed", so only things that change the rows may bump it.
_write_seq = 0

# Writes now happen on worker threads (they gzip and hit the filesystem, and doing that on the
# event loop froze every request in the product). `_write_seq += 1` is a read-modify-write and
# is NOT atomic, so two concurrent scope writes could lose a bump — and a LOST bump is not a
# slow cache, it is a silently wrong one: consumers would keep serving a memo built from rows
# that have since changed. Every mutation of the counter goes through this lock.
_seq_lock = threading.Lock()


def _bump() -> None:
    global _write_seq
    with _seq_lock:
        _write_seq += 1


def cache_version() -> int:
    """Current source-data version; changes when scope rows or the directory layer change.

    Deliberately NOT a count of all writes — see :data:`_write_seq`."""
    return _write_seq


def cache_fingerprint() -> tuple[str, int]:
    """Identity of the cache CONTENTS a derived artefact was built from.

    What every in-process memo must key on, rather than the version alone. :data:`_write_seq` is
    a process-global counter that starts at zero and only counts writes made through THIS
    module, so two different stores — a real one and a test's temporary one, or two roots in one
    process — both read as version 0. A memo keyed on the version alone would then hand one
    store's results back for the other: not a slow cache, a silently wrong one.

    Pairing it with the store's own path makes repointing the cache invalidate every memo."""
    return (str(_INDEX), _write_seq)


def _write_index(data: dict[str, Any], *, bump: bool = True) -> None:
    _INDEX.parent.mkdir(parents=True, exist_ok=True)
    _INDEX.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if bump:
        _bump()


# Every index mutation is READ-MODIFY-WRITE over one shared JSON file:
#
#     data = _read_index(); data[...] = entry; _write_index(data)
#
# Two of those interleaving is a LOST UPDATE — the second writer's snapshot predates the first
# writer's change, so the first scope silently vanishes from the index while its sidecar blob
# sits on disk orphaned. The reader then reports a scope that was collected as never collected.
#
# This was latent rather than theoretical even before the refresh fanned out: writes already run
# on worker threads (`asyncio.to_thread(cache.write_scope, ...)`) while the API keeps serving
# requests that write baselines, drift and right-sizing. Refreshing three scopes at a time just
# makes it likely instead of rare.
#
# Reentrant because a guarded function may legitimately call another one.
_index_lock = threading.RLock()


def _index_guarded(fn: "Callable[..., Any]") -> "Callable[..., Any]":
    """Serialise a function that reads, mutates and rewrites the index."""

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        with _index_lock:
            return fn(*args, **kwargs)

    return _wrapped


def _tenant_bucket(data: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    bucket = data.setdefault(tenant_id or "default", {})
    bucket.setdefault("scopes", {})
    bucket.setdefault(DIRECTORY_KEY, {})
    return bucket


# --------------------------------------------------------------------------- sidecar I/O
def _blob_path(tenant_id: str, scope: str) -> Path:
    _migrate_legacy_paths()
    return _BLOBS / (tenant_id or "default") / f"{_scope_hash(scope)}.json.gz"


def _write_blob(tenant_id: str, scope: str, payload: dict[str, Any], *, bump: bool = True) -> None:
    path = _blob_path(tenant_id, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path.write_bytes(gzip.compress(raw))
    if bump:
        _bump()


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


# --------------------------------------------------------------------------- feature state
# Small per-tenant JSON documents that are NOT an access snapshot: scanner run baselines, the
# finding ledger. Kept in the same blob store so they inherit tenant isolation and the demo
# purge, but deliberately NOT in the scope index — nothing here is a collection result, and a
# state document must never be mistaken for evidence that a scan happened.
_STATE_PREFIX = "state__"

# The state documents this module knows how to clean up. Named here rather than in
# `scanners.py` so `delete_tenant` cannot fall out of sync with the writers.
SCANNER_STATE = "scanner_runs"
FINDINGS_LEDGER = "findings_ledger"


def state_key(name: str) -> str:
    return f"{_STATE_PREFIX}{name}"


def write_state(tenant_id: str, name: str, payload: dict[str, Any]) -> None:
    # State is not source data: a scanner recording its baseline must not invalidate the master
    # rows. A sweep writes nine of these, which used to force nine full recomposes.
    _write_blob(tenant_id, state_key(name), payload, bump=False)


def read_state(tenant_id: str, name: str) -> dict[str, Any]:
    return _read_blob(tenant_id, state_key(name))


def delete_state(tenant_id: str, name: str) -> None:
    _delete_blob(tenant_id, state_key(name))


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
@_index_guarded
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


@_index_guarded
def mark_scope_verified(tenant_id: str, scope: str, *, reason: str = "") -> dict[str, Any] | None:
    """Record that a delta refresh checked this scope and found no authorization activity.

    **``generated_at`` is deliberately left alone.** It is the time the rows were actually
    collected, and the freshness column reads it. Stamping it with the run time would make every
    scope report as freshly collected after a delta pass that did not look at it — "fresh" would
    then mean "we ran recently", which is not what a reader takes it to mean, and a scope whose
    collection genuinely failed days ago would be indistinguishable from one collected seconds
    ago. ``verified_at`` is a separate, additive fact: the data is old *and known to be current*."""
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    entry = bucket["scopes"].get(scope)
    if not isinstance(entry, dict):
        return None
    entry["verified_at"] = _now_iso()
    entry["verified_unchanged"] = True
    if reason:
        entry["verified_reason"] = reason
    _write_index(data)
    return entry


def purge_phantom_scopes(tenant_id: str) -> list[str]:
    """Delete cache slices whose "scope" is a job sentinel rather than an Azure scope.

    A ``mode=scope`` refresh arriving without a scope used to fall back to the ``__all__``
    sentinel and write a real slice for it. The entry then sat in the freshness table forever as
    a permanently-stale row with zero rows, inflating the scope count. New writes are blocked at
    the orchestrator; this clears the ones already on disk."""
    from app.iam.orchestrator import SENTINEL_SCOPES

    removed: list[str] = []
    for meta in list_scope_meta(tenant_id):
        scope = str(meta.get("scope", ""))
        if scope.strip() in SENTINEL_SCOPES:
            if delete_scope(tenant_id, scope):
                removed.append(scope)
    return removed


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


@_index_guarded
def delete_scope(tenant_id: str, scope: str) -> bool:
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    existed = bucket["scopes"].pop(scope, None) is not None
    _write_index(data)
    _delete_blob(tenant_id, scope)
    return existed


# --------------------------------------------------------------------------- directory layer
@_index_guarded
def write_directory(
    tenant_id: str,
    *,
    meta: dict[str, Any],
    rows: list[dict[str, Any]],
    role_defs: list[dict[str, Any]] | None = None,
    principals: list[dict[str, Any]] | None = None,
    groups: dict[str, Any] | None = None,
    management_groups: dict[str, str] | None = None,
    identities: dict[str, Any] | None = None,
    federated: list[dict[str, Any]] | None = None,
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
            # principalId -> managed-identity facts. This is what turns an unexplained GUID
            # service principal in the grid into "the identity of vm-prod-01".
            "identities": identities or {},
            "federated": federated or [],
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
        "identities": payload.get("identities") or {},
        "federated": payload.get("federated") or [],
    }


@_index_guarded
def write_bypass(
    tenant_id: str,
    *,
    meta: dict[str, Any],
    resources: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Persist the RBAC-bypass sweep in its own slice.

    ``resources`` is retained as well as ``rows`` because it is the DENOMINATOR — without it the
    "RBAC is the only door" percentage cannot be recomputed, and a ratio whose denominator is
    unknown is worse than no ratio."""
    entry = dict(meta)
    entry["generated_at"] = entry.get("generated_at") or _now_iso()
    entry["resource_count"] = len(resources)
    entry["finding_count"] = len(rows)
    entry["rows_ref"] = BYPASS_KEY
    _write_blob(tenant_id, BYPASS_KEY, {"resources": resources, "rows": rows, "summary": summary})
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    bucket[BYPASS_KEY] = entry
    _write_index(data)
    return entry


def read_bypass(tenant_id: str) -> dict[str, Any]:
    payload = _read_blob(tenant_id, BYPASS_KEY)
    return {
        "resources": payload.get("resources") or [],
        "rows": payload.get("rows") or [],
        "summary": payload.get("summary") or _empty_bypass_summary(),
    }


def _empty_bypass_summary() -> dict[str, Any]:
    """The shape a never-run sweep reports.

    An empty ``{}`` was worse than useless here: every consumer reads the keys, and a missing
    ``rbac_only_pct`` is not ``None`` — it is ``undefined``, which sailed straight past the
    frontend's "nothing assessed" branch and rendered the reassuring "X% of 0 assessed resources
    have RBAC as the only door" headline for a tenant that had never been swept."""
    return {
        "assessed": 0,
        "rbac_only": 0,
        "bypassed": 0,
        "rbac_only_pct": None,
        "findings": 0,
        "by_family": [],
        "by_severity": {"critical": 0, "error": 0, "warning": 0, "info": 0},
        "limitations": ["The non-RBAC access sweep has not run for this tenant. Nothing here is an all-clear."],
    }


def read_bypass_meta(tenant_id: str) -> dict[str, Any]:
    bucket = _tenant_bucket(_read_index(), tenant_id)
    entry = bucket.get(BYPASS_KEY)
    return entry if isinstance(entry, dict) else {}


@_index_guarded
def write_drift(tenant_id: str, payload: dict[str, Any]) -> None:
    """Persist the classified diff from the latest run.

    Only the most recent one is kept. The per-run history lives in `IamScanRun.diff_json`; this
    slice exists purely so the synchronous signal context can see it."""
    _write_blob(tenant_id, DRIFT_KEY, payload, bump=False)
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    bucket[DRIFT_KEY] = {
        "generated_at": _now_iso(),
        "change_count": len(payload.get("changes", []) or []),
        "available": bool(payload.get("available")),
    }
    # Derived from two runs of rows; it does not change the rows themselves.
    _write_index(data, bump=False)


def read_drift(tenant_id: str) -> dict[str, Any]:
    """The latest classified diff, or an explicitly-unavailable one.

    `available` is False for a tenant with a single scan. An empty change list with `available`
    True means "we compared and nothing moved"; with False it means "there was nothing to compare
    against". Collapsing those two is how a first run reports a clean bill of health."""
    payload = _read_blob(tenant_id, DRIFT_KEY)
    return {
        "changes": payload.get("changes") or [],
        "counts_by_class": payload.get("counts_by_class") or {},
        "total": int(payload.get("total") or 0),
        "truncated": bool(payload.get("truncated")),
        "worsening": int(payload.get("worsening") or 0),
        "available": bool(payload.get("available")),
        "baseline_run_id": payload.get("baseline_run_id", ""),
        "attribution": payload.get("attribution") or {},
        "note": payload.get("note", ""),
    }


@_index_guarded
def write_usage(tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the usage sweep with its OWN collection time.

    Deliberately not merged into the access snapshot: a tenant can have access collected minutes
    ago and usage collected three weeks ago, and every figure derived from usage has to carry
    that age rather than inheriting the access snapshot's."""
    entry = {
        "generated_at": _now_iso(),
        "window_days": int(payload.get("window_days") or 0),
        "status": payload.get("status", ""),
        "event_count": int(payload.get("event_count") or 0),
        "principal_count": len(payload.get("principals") or []),
        "source": payload.get("source", ""),
    }
    _write_blob(tenant_id, USAGE_KEY, payload)
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    bucket[USAGE_KEY] = entry
    _write_index(data)
    return entry


def read_usage(tenant_id: str) -> dict[str, Any]:
    """The usage sweep, or an explicitly-unmeasured payload.

    ``measured: False`` is the gate every consumer must check. An empty principal list with
    ``measured`` unset would read as "nobody used anything", which is the reading that gets
    somebody's access revoked."""
    payload = _read_blob(tenant_id, USAGE_KEY)
    if not payload:
        from app.iam import usage as usage_mod

        return {
            "window_days": 0, "start": "", "end": "", "source": "",
            "status": "", "subscriptions": 0, "event_count": 0, "principals": [],
            "measured": False,
            "notes": ["Usage has never been collected for this tenant."],
            "limitations": usage_mod.LIMITATIONS,
        }
    return payload


def read_usage_meta(tenant_id: str) -> dict[str, Any]:
    bucket = _tenant_bucket(_read_index(), tenant_id)
    entry = bucket.get(USAGE_KEY)
    return entry if isinstance(entry, dict) else {}


RIGHTSIZING_KEY = "rightsizing"

# The escalation graph. Persisted, not merely memoised, because building it on a realistic
# tenant takes 30 seconds and the in-process memo dies with the process — so every restart, and
# every switch between two connections, used to re-pay it in full.
ESCALATION_KEY = "escalation"


@_index_guarded
def write_escalation(
    tenant_id: str,
    graph: dict[str, Any],
    *,
    cache_version: int,
    min_confidence: str,
    duration_seconds: float,
) -> None:
    """Persist the escalation graph with the cache version it was derived from.

    The version is the freshness test — NOT a TTL. A TTL would either serve a graph that no
    longer matches the rows (silently wrong) or rebuild one that does (needlessly slow); the
    version answers exactly, because any write that changes the ROWS bumps it — and, critically,
    writing this graph does not."""
    _write_blob(tenant_id, ESCALATION_KEY, {
        "cache_version": int(cache_version),
        "min_confidence": min_confidence,
        "generated_at": _now_iso(),
        "duration_seconds": float(duration_seconds),
        "graph": graph,
    }, bump=False)
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    # Meta only — the graph itself is megabytes and has no business in the index, which is read
    # on nearly every request.
    bucket[ESCALATION_KEY] = {
        "cache_version": int(cache_version),
        "min_confidence": min_confidence,
        "generated_at": _now_iso(),
        "duration_seconds": float(duration_seconds),
        "nodes": len(graph.get("nodes") or []),
        "edges": len(graph.get("edges") or []),
        "paths": len(graph.get("paths") or []),
    }
    # Writing the graph must not change the version the graph is stamped with, or it
    # invalidates itself and the build is re-paid on every request forever.
    _write_index(data, bump=False)


def read_escalation(tenant_id: str) -> dict[str, Any]:
    return _read_blob(tenant_id, ESCALATION_KEY)


def read_escalation_meta(tenant_id: str) -> dict[str, Any]:
    bucket = _tenant_bucket(_read_index(), tenant_id)
    entry = bucket.get(ESCALATION_KEY)
    return entry if isinstance(entry, dict) else {}



@_index_guarded
def write_rightsizing(
    tenant_id: str,
    payload: dict[str, Any],
    *,
    cache_version: int | None = None,
    duration_seconds: float | None = None,
) -> None:
    """Persist the granted-vs-used analysis alongside the usage it derives from.

    Computed when usage is collected rather than on every findings read. It is pure CPU over the
    whole role catalogue — two seconds on a real tenant, and findings is a hot endpoint. It also
    correctly inherits usage's freshness: an analysis of week-old usage is a week-old analysis
    however recently it was recomputed.

    The blob stays the bare analysis because several consumers read it directly; the freshness
    stamp goes in the index instead. ``cache_version`` is the whole freshness test — rows, the
    directory and the usage sweep all bump it, and writing THIS does not."""
    _write_blob(tenant_id, RIGHTSIZING_KEY, payload, bump=False)
    if cache_version is None:
        return
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    bucket[RIGHTSIZING_KEY] = {
        "cache_version": int(cache_version),
        "generated_at": _now_iso(),
        "duration_seconds": float(duration_seconds or 0.0),
        "measured": bool(payload.get("measured")),
        "recommendations": len(payload.get("recommendations") or []),
        "assessed": int(payload.get("assessed") or 0),
    }
    _write_index(data, bump=False)


def read_rightsizing_meta(tenant_id: str) -> dict[str, Any]:
    entry = _tenant_bucket(_read_index(), tenant_id).get(RIGHTSIZING_KEY)
    return entry if isinstance(entry, dict) else {}


def read_rightsizing(tenant_id: str) -> dict[str, Any]:
    payload = _read_blob(tenant_id, RIGHTSIZING_KEY)
    if not payload:
        return {"measured": False, "recommendations": [], "assessed": 0,
                "excluded": [], "limitations": [], "notes": []}
    return payload


# --------------------------------------------------------------------------- tenant-wide ops
def has_any(tenant_id: str) -> bool:
    """True when the tenant has at least one cached scope or a directory snapshot."""
    bucket = _tenant_bucket(_read_index(), tenant_id)
    return bool(bucket["scopes"]) or bool(bucket.get(DIRECTORY_KEY))


@_index_guarded
def delete_tenant(tenant_id: str) -> int:
    """Drop every cached scope + the directory for a tenant (demo purge). Returns scopes removed."""
    data = _read_index()
    bucket = _tenant_bucket(data, tenant_id)
    n = len(bucket["scopes"])
    for scope in list(bucket["scopes"].keys()):
        _delete_blob(tenant_id, scope)
    _delete_blob(tenant_id, DIRECTORY_KEY)
    # State documents are not in the scope index, so they do not fall out with it. Leaving the
    # scanner baseline behind would make a re-added tenant's first scan report every finding as
    # "resolved" and then "new" against a run that no longer has any data behind it.
    for name in (SCANNER_STATE, FINDINGS_LEDGER):
        delete_state(tenant_id, name)
    data[tenant_id or "default"] = {"scopes": {}, DIRECTORY_KEY: {}}
    _write_index(data)
    return n


@_index_guarded
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


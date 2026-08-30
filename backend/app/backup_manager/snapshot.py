"""Durable, per-scope Backup Manager snapshots — the module's user-controlled cache.

Backup Manager reads an entire estate: nine Resource Graph queries, one ARM config read per
vault, a Cost Management round-trip and a Log Analytics query. That is far too slow to repeat
on every tab switch, and repeating it on a timer would make the numbers move under the
operator while they are working a decision.

So the module follows the Alerts Manager contract: one snapshot is computed per
``(tenant, connection, scope)`` when the operator explicitly asks for it, persisted here, and
served unchanged to every tab until they ask again. :func:`read_snapshot` never computes — if
nothing has been analyzed yet it returns an empty shell whose ``report_exists`` is ``False``,
which is the UI's cue to show "Analyze backups" instead of silently firing off a sweep.

Snapshots are bounded on both axes: row lists are capped when written (a large tenant's
seven-day job history would otherwise dominate the file) and the least recently generated
scopes are pruned, so this file cannot grow without limit the way an unbounded analysis cache
can.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.backup_manager import service
from app.core import jsonstore

log = logging.getLogger("app.backup_manager.snapshot")

# Bump when the snapshot shape changes so stale-shaped snapshots are treated as absent
# instead of being fed to a UI that expects different keys.
SNAPSHOT_SCHEMA_VERSION = 2

_PATH = Path(__file__).resolve().parents[2] / ".data" / "backup_manager_snapshot.json"

#: How many analyzed scopes to keep. Beyond this the oldest are dropped.
MAX_SCOPES = 24
#: Per-section row caps. Backup job history is the only list that grows unboundedly with
#: estate size; the others are naturally small but are capped for symmetry.
MAX_ROWS = {
    "instances": 5000, "jobs": 2000, "gaps": 2000, "policies": 5000,
    "vaults": 5000, "replicated_items": 5000, "recovery_plans": 5000,
    "compliance": 5000, "chronic": 5000, "failure_clusters": 5000,
}

_locks: dict[str, asyncio.Lock] = {}


def get_lock(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> asyncio.Lock:
    """One lock per scope so two concurrent analyses cannot interleave their writes."""
    key = _key(tenant_id, connection_id, scope_kind, scope_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _key(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> str:
    return "|".join((
        str(tenant_id or "default"), str(connection_id or "default"),
        str(scope_kind or ""), str(scope_id or "").lower(),
    ))


def _read() -> dict[str, Any]:
    value = jsonstore.read_json(_PATH, {})
    return value if isinstance(value, dict) else {}


def empty_snapshot(scope_kind: str, scope_id: str, reason: str = "") -> dict[str, Any]:
    """The shell served when a scope has never been analyzed.

    Every section is present and empty so the UI can render its tabs without null guards;
    ``report_exists`` is what tells it to offer the Analyze button instead of the data."""
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "report_exists": False,
        "generated_at": "",
        "demo": False,
        "reason": reason,
        "scope": {"scope_kind": scope_kind, "scope_id": scope_id, "subscriptions": []},
        "errors": {},
        "warnings": {},
        "source_details": {},
        "job_window_days": 0,
        "counts": {},
        "summary": {},
        "inventory": {"rows": [], "facets": {"datasource_types": [], "states": [], "vaults": []},
                      "total_count": 0, "truncated": False},
        "jobs": {"rows": [], "summary": {}, "total_count": 0, "truncated": False},
        "job_analysis": {"clusters": [], "chronic": [], "congestion": {}},
        "policies": {"policies": [], "duplicate_groups": [], "summary": {}},
        "compliance": {"rows": [], "tiers": []},
        "posture": {"vaults": [], "average_score": 0, "band": "green", "red_vaults": 0,
                    "actionable_count": 0, "capacity": {}},
        "vaults": {"vaults": [], "capacity": {}},
        "gaps": {"gaps": [], "coverage_gaps": [], "vaults": [], "policies": [], "summary": {},
                 "truncated": False},
        "dr": {"summary": {}, "rpo": {}, "items": [], "recovery_plans": []},
        "cost": {},
        "truncation": {},
    }


def bound(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Cap the row lists that scale with estate size, flagging any truncation."""
    truncation = snapshot.setdefault("truncation", {})

    def cap(section: dict[str, Any], key: str, limit_key: str, label: str) -> None:
        rows = section.get(key) or []
        if len(rows) > MAX_ROWS[limit_key]:
            section[key] = rows[: MAX_ROWS[limit_key]]
            truncation[label] = {"exported": MAX_ROWS[limit_key], "known_total": len(rows)}

    inventory = snapshot.get("inventory")
    if isinstance(inventory, dict):
        rows = inventory.get("rows") or []
        if len(rows) > MAX_ROWS["instances"]:
            inventory["rows"] = rows[: MAX_ROWS["instances"]]
            inventory["truncated"] = True
            truncation["Protected items"] = {"exported": MAX_ROWS["instances"], "known_total": len(rows)}
    jobs = snapshot.get("jobs")
    if isinstance(jobs, dict):
        rows = jobs.get("rows") or []
        if len(rows) > MAX_ROWS["jobs"]:
            # Newest first is already the collector's order, so the tail is the oldest history.
            jobs["rows"] = rows[: MAX_ROWS["jobs"]]
            jobs["truncated"] = True
            truncation["Backup jobs"] = {"exported": MAX_ROWS["jobs"], "known_total": len(rows)}
    gaps = snapshot.get("gaps")
    if isinstance(gaps, dict):
        rows = gaps.get("gaps") or []
        if len(rows) > MAX_ROWS["gaps"]:
            gaps["gaps"] = rows[: MAX_ROWS["gaps"]]
            gaps["truncated"] = True
            truncation["Protection gaps"] = {"exported": MAX_ROWS["gaps"], "known_total": len(rows)}
    for section_name, key, limit_key, label in (
        ("policies", "policies", "policies", "Policies"),
        ("vaults", "vaults", "vaults", "Vaults"),
        ("compliance", "rows", "compliance", "Policy compliance"),
    ):
        section = snapshot.get(section_name)
        if isinstance(section, dict):
            cap(section, key, limit_key, label)
    dr = snapshot.get("dr")
    if isinstance(dr, dict):
        cap(dr, "items", "replicated_items", "Replicated items")
        cap(dr, "recovery_plans", "recovery_plans", "Recovery plans")
    job_analysis = snapshot.get("job_analysis")
    if isinstance(job_analysis, dict):
        cap(job_analysis, "chronic", "chronic", "Chronic failures")
        cap(job_analysis, "clusters", "failure_clusters", "Failure clusters")
    return snapshot


def read_snapshot(
    tenant_id: str, connection_id: str, scope_kind: str, scope_id: str,
) -> dict[str, Any] | None:
    """The stored snapshot for a scope, or ``None``. Never computes."""
    value = _read().get(_key(tenant_id, connection_id, scope_kind, scope_id))
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    return value


def write_snapshot(
    tenant_id: str, connection_id: str, scope_kind: str, scope_id: str, snapshot: dict[str, Any],
) -> dict[str, Any]:
    snapshot["schema_version"] = SNAPSHOT_SCHEMA_VERSION
    snapshot["report_exists"] = True
    bound(snapshot)

    def _mutate(data: dict[str, Any]) -> None:
        data[_key(tenant_id, connection_id, scope_kind, scope_id)] = snapshot
        if len(data) > MAX_SCOPES:
            stale_keys = sorted(
                data, key=lambda stored_key: str(data[stored_key].get("generated_at") or "")
            )[:-MAX_SCOPES]
            for stale in stale_keys:
                data.pop(stale, None)

    try:
        jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    except OSError as exc:  # a snapshot we cannot persist is still usable in this request
        log.warning("backup_manager: could not persist snapshot: %s", exc)
    return snapshot


def delete_snapshot(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        key = _key(tenant_id, connection_id, scope_kind, scope_id)
        if key in data:
            del data[key]
            deleted = True

    try:
        jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    except OSError as exc:
        log.warning("backup_manager: could not persist snapshot: %s", exc)
    return deleted


def list_scopes(tenant_id: str) -> list[dict[str, Any]]:
    """Every stored snapshot for a tenant with its size and age — drives the Cleanup tab.

    The store is capped at :data:`MAX_SCOPES` and evicts the oldest scope on write, so an
    operator who can see what is held (and drop what they no longer need) controls which
    analyses survive instead of discovering the eviction by an empty tab."""
    prefix = f"{str(tenant_id or 'default')}|"
    out: list[dict[str, Any]] = []
    for stored_key, value in _read().items():
        if not stored_key.startswith(prefix) or not isinstance(value, dict):
            continue
        parts = stored_key.split("|")
        if len(parts) != 4:
            continue
        try:
            size = len(json.dumps(value, default=str))
        except (TypeError, ValueError):
            size = 0
        counts = value.get("counts") or {}
        scope = value.get("scope") or {}
        out.append({
            "key": stored_key,
            "connection_id": parts[1] if parts[1] != "default" else "",
            "scope_kind": parts[2],
            "scope_id": parts[3],
            "scope_name": str(scope.get("scope_name") or parts[3]),
            "subscription_count": int(scope.get("subscription_count") or len(scope.get("subscriptions") or [])),
            "generated_at": str(value.get("generated_at") or ""),
            "age_seconds": age_seconds(value),
            "size_bytes": size,
            "schema_version": value.get("schema_version"),
            "schema_stale": value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION,
            "partial": bool(value.get("partial")),
            "demo": bool(value.get("demo")),
            "protected_items": int(counts.get("protected_items", 0) or 0),
            "gaps": int(counts.get("gaps", 0) or 0),
        })
    out.sort(key=lambda row: str(row.get("generated_at") or ""), reverse=True)
    return out


def delete_keys(keys: list[str]) -> dict[str, int]:
    """Purge stored snapshots by their exact store key. Returns count + bytes freed."""
    removed = 0
    freed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal removed, freed
        for stored_key in keys:
            value = data.pop(stored_key, None)
            if value is None:
                continue
            removed += 1
            try:
                freed += len(json.dumps(value, default=str))
            except (TypeError, ValueError):
                pass

    try:
        jsonstore.mutate_json(_PATH, {}, _mutate, indent=None)
    except OSError as exc:
        log.warning("backup_manager: could not persist snapshot: %s", exc)
    return {"count": removed, "freed_bytes": freed}



def age_seconds(snapshot: dict[str, Any]) -> float | None:
    generated = str(snapshot.get("generated_at") or "")
    if not generated:
        return None
    parsed = service.parse_iso(generated)
    if parsed is None:
        return None
    return max(0.0, (service.now() - parsed).total_seconds())

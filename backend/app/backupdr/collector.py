"""Backup & DR Coverage computation.

Evaluates each in-scope resource's backup/DR posture against the per-type protection
reference and produces the matrix rows (one cell per relevant check), a per-resource
red/amber/green status, the top scorecard, the DR-pair list, and the flat gap list.

``compute_coverage`` is a pure function over already-fetched ``resources`` + a
``state_by_resource`` map of backup facts + a ``dr_pairs`` list, so it's unit-testable and
powers the demo seed. ``collect_coverage`` resolves the scope and gathers those facts from
Azure Resource Graph (+ gated ``az`` fallback for deeper job/ASR data)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.backupdr.reference import load_reference
from app.core.coverage_resources import build_all_resources

log = logging.getLogger("app.backupdr.collector")
CELL_GREEN = "green"
CELL_AMBER = "amber"
CELL_RED = "red"
CELL_NA = "na"

_STATUS_RANK = {CELL_RED: 0, CELL_AMBER: 1, CELL_GREEN: 2, CELL_NA: 3}
_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("value") or []
    return data if isinstance(data, list) else []


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


# --------------------------------------------------------------------------- cell logic
def _evaluate_cell(check: str, st: dict[str, Any], resource_region: str, sla_hours: int) -> dict[str, Any]:
    """Return {status, value, detail} for one matrix check given the backup state facts."""
    def cell(status: str, value: str, detail: str = "") -> dict[str, Any]:
        return {"check": check, "status": status, "value": value, "detail": detail}

    if check == "backup_enabled":
        return cell(CELL_GREEN, "Enabled", "Protected by a backup vault.") if st.get("backup_enabled") \
            else cell(CELL_RED, "Not enabled", "No backup protection configured.")

    if check == "policy":
        if not st.get("backup_enabled"):
            return cell(CELL_RED, "—", "No backup, so no policy.")
        return cell(CELL_GREEN, st.get("policy") or "Attached") if st.get("policy") \
            else cell(CELL_AMBER, "None", "Backup enabled but no policy attached.")

    if check == "retention":
        if not st.get("backup_enabled"):
            return cell(CELL_RED, "—")
        days = int(st.get("retention_days") or 0)
        if days <= 0:
            return cell(CELL_AMBER, "Unknown")
        return cell(CELL_GREEN if days >= 30 else CELL_AMBER, f"{days}d",
                    "" if days >= 30 else "Retention below the recommended 30 days.")

    if check == "last_job":
        if not st.get("backup_enabled"):
            return cell(CELL_RED, "—")
        status = (st.get("last_job_status") or "").lower()
        age = st.get("last_job_age_hours")
        age_txt = f"{int(age)}h ago" if isinstance(age, (int, float)) else "unknown"
        if status not in ("succeeded", "completed", "success"):
            return cell(CELL_RED, f"{status or 'none'}", f"Last job {status or 'missing'} ({age_txt}).")
        if isinstance(age, (int, float)) and age > sla_hours:
            return cell(CELL_AMBER, f"OK · {age_txt}", f"Last successful job older than the {sla_hours}h SLA.")
        return cell(CELL_GREEN, f"OK · {age_txt}")

    if check == "geo_redundancy":
        return cell(CELL_GREEN, "Geo") if st.get("geo_redundant") \
            else cell(CELL_AMBER, "Local only", "No geo/offsite redundancy.")

    if check == "offsite_region":
        br = st.get("backup_region") or ""
        if not br:
            return cell(CELL_NA, "—", "Backup destination region unknown.")
        same = br.lower() == (resource_region or "").lower()
        return cell(CELL_AMBER if same else CELL_GREEN, br,
                    "Same region as the resource — not offsite." if same else "Offsite (different region).")

    if check == "dr_pair":
        if st.get("dr_pair"):
            tgt = st.get("dr_target_region") or "?"
            return cell(CELL_GREEN, f"→ {tgt}")
        return cell(CELL_AMBER, "None", "No DR replication pair configured.")

    if check == "encryption":
        enc = (st.get("encryption") or "").lower()
        if enc == "cmk":
            return cell(CELL_GREEN, "CMK")
        if enc == "pmk":
            return cell(CELL_AMBER, "PMK", "Platform-managed key; CMK recommended for this tier.")
        return cell(CELL_RED, "None", "No encryption configured.")

    if check == "soft_delete":
        sd = st.get("soft_delete")
        purge = st.get("purge_protection")
        if sd and purge:
            return cell(CELL_GREEN, "On + purge")
        if sd:
            return cell(CELL_AMBER, "Soft-delete only", "Purge protection is off.")
        return cell(CELL_RED, "Off", "Soft-delete is disabled — permanent deletion possible.")

    if check == "restore_test":
        age = st.get("last_restore_test_age_days")
        if age is None:
            return cell(CELL_AMBER, "Never", "No restore/failover test on record.")
        return cell(CELL_GREEN if age <= 180 else CELL_AMBER, f"{int(age)}d ago",
                    "" if age <= 180 else "Last restore test older than 180 days.")

    if check == "pitr":
        val = st.get("pitr")
        if val is None:
            return cell(CELL_AMBER, "Unknown", "Could not determine continuous-backup (PITR) status.")
        return cell(CELL_GREEN, "Continuous") if val \
            else cell(CELL_RED, "Periodic only", "Continuous backup (point-in-time restore) is not enabled.")

    if check == "persistence":
        val = st.get("persistence")
        if val is None:
            return cell(CELL_AMBER, "Unknown", "Could not determine data-persistence (RDB/AOF) status.")
        return cell(CELL_GREEN, st.get("persistence_mode") or "Enabled") if val \
            else cell(CELL_RED, "Off", "No RDB/AOF persistence — a restart or failure loses all data.")

    if check == "geo_dr_pair":
        if st.get("geo_dr_pair"):
            tgt = st.get("dr_target_region") or "paired"
            return cell(CELL_GREEN, f"→ {tgt}")
        return cell(CELL_AMBER, "None", "No Geo-DR alias / paired namespace configured.")

    return cell(CELL_NA, "—")


def _worst(cells: list[dict[str, Any]]) -> str:
    worst = CELL_GREEN
    for c in cells:
        if c["status"] == CELL_NA:
            continue
        if _STATUS_RANK.get(c["status"], 3) < _STATUS_RANK.get(worst, 3):
            worst = c["status"]
    return worst


def _cell_severity(status: str) -> str:
    return {"red": "error", "amber": "warning"}.get(status, "info")


# --------------------------------------------------------------------------- public API
def compute_coverage(
    resources: list[dict[str, Any]],
    state_by_resource: dict[str, dict[str, Any]],
    dr_pairs: list[dict[str, Any]],
    *,
    sla_hours: int = 24,
    stale_drill_days: int = 180,
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ref = reference if reference is not None else load_reference()
    ref_types: dict[str, Any] = ref.get("types", {})

    groups: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    n_total = n_protected = n_offsite = n_recent_job = 0

    for res in resources:
        rtype = str(res.get("type", "")).lower()
        spec = ref_types.get(rtype)
        if not spec:
            continue
        rid = str(res.get("id", "")).lower()
        st = state_by_resource.get(rid, {})
        region = res.get("location", "") or res.get("region", "")
        checks = spec.get("checks", []) or []
        cells = [_evaluate_cell(c, st, region, sla_hours) for c in checks]
        cell_by_check = {c["check"]: c for c in cells}
        status = _worst(cells)

        n_total += 1
        if st.get("backup_enabled") or rtype == "microsoft.keyvault/vaults":
            n_protected += 1 if (st.get("backup_enabled") or st.get("soft_delete")) else 0
        # Offsite = geo redundancy OR backup region different from resource region.
        br = (st.get("backup_region") or "")
        offsite = bool(st.get("geo_redundant")) or (br and br.lower() != (region or "").lower())
        if offsite:
            n_offsite += 1
        lj = cell_by_check.get("last_job")
        if lj and lj["status"] == CELL_GREEN:
            n_recent_job += 1

        g = groups.setdefault(
            rtype,
            {
                "resource_type": rtype,
                "display": spec.get("display", rtype),
                "category": spec.get("category", "other"),
                "note": spec.get("note", ""),
                "checks": checks,
                "rows": [],
                "red": 0,
                "amber": 0,
                "green": 0,
            },
        )
        g[status] = g.get(status, 0) + 1
        g["rows"].append(
            {
                "resource_id": res.get("id", ""),
                "resource_name": res.get("name", ""),
                "resource_group": res.get("resourceGroup", res.get("resource_group", "")),
                "subscription_id": res.get("subscriptionId", res.get("subscription_id", "")),
                "region": region,
                "backup_region": br,
                "status": status,
                "cells": cells,
                "state": st,
            }
        )

        if status != CELL_GREEN:
            bad = [c for c in cells if c["status"] in (CELL_RED, CELL_AMBER)]
            gaps.append(
                {
                    "resource_id": res.get("id", ""),
                    "resource_name": res.get("name", ""),
                    "resource_type": rtype,
                    "resource_group": res.get("resourceGroup", res.get("resource_group", "")),
                    "subscription_id": res.get("subscriptionId", res.get("subscription_id", "")),
                    "region": region,
                    "backup_region": br,
                    "status": status,
                    "failed_checks": [c["check"] for c in bad],
                    "vault_name": st.get("vault_name", ""),
                    "policy": st.get("policy", ""),
                    "dr_target_region": st.get("dr_target_region", ""),
                    "severity": _cell_severity(status),
                }
            )

    def _pct(num: int, denom: int) -> int:
        return round(100 * num / denom) if denom else 100

    group_list = sorted(groups.values(), key=lambda g: g["display"].lower())
    for g in group_list:
        denom = len(g["rows"])
        g["coverage_pct"] = _pct(g.get("green", 0), denom)

    # DR pairs + stale-drill flags.
    dr_out: list[dict[str, Any]] = []
    for p in dr_pairs:
        age = p.get("last_failover_test_age_days")
        stale = (age is None) or (isinstance(age, (int, float)) and age > stale_drill_days)
        health = (p.get("replication_health") or "").lower()
        healthy = health in ("healthy", "normal")
        dr_out.append(
            {
                "name": p.get("name", ""),
                "primary_region": p.get("primary_region", ""),
                "secondary_region": p.get("secondary_region", ""),
                "replication_health": p.get("replication_health", "Unknown"),
                "healthy": healthy,
                "last_failover_test_age_days": age,
                "stale": stale,
                "protected_items": p.get("protected_items", 0),
            }
        )
    # Most recent drill = smallest age (if any).
    ages = [p.get("last_failover_test_age_days") for p in dr_pairs if isinstance(p.get("last_failover_test_age_days"), (int, float))]
    last_drill_days = min(ages) if ages else None

    gaps.sort(key=lambda x: (_SEVERITY_RANK.get(x["severity"], 3), x["resource_type"], x["resource_name"]))

    return {
        "generated_at": _now_iso(),
        "scorecard": {
            "total": n_total,
            "protected": n_protected,
            "pct_protected": _pct(n_protected, n_total),
            "pct_offsite": _pct(n_offsite, n_total),
            "pct_recent_job": _pct(n_recent_job, n_total),
            "dr_pairs": len(dr_out),
            "dr_pairs_stale": sum(1 for d in dr_out if d["stale"]),
            "dr_pairs_unhealthy": sum(1 for d in dr_out if not d["healthy"]),
            "last_drill_days": last_drill_days,
        },
        "groups": group_list,
        "dr_pairs": dr_out,
        "gaps": gaps,
        "all_resources": build_all_resources(resources, ref_types),
    }


# --------------------------------------------------------------------------- ARG gather
async def _query_resources(predicates: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.assessments.runner import query_resources_batched

    return await query_resources_batched(
        predicates,
        connection,
        projection="id, name, type, resourceGroup, subscriptionId, location, properties, sku, tags",
    )


async def _query_vaults(subscriptions: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Recovery Services + Backup vaults (id, name, region) for offsite-region resolution."""
    from app.exec.command_runner import run_kql_capture

    if not subscriptions:
        return []
    joined = ", ".join(f"'{_esc(s)}'" for s in subscriptions)
    kql = (
        "resources | where type in~ ('microsoft.recoveryservices/vaults', 'microsoft.dataprotection/backupvaults') "
        f"| where subscriptionId in~ ({joined}) "
        "| project id, name, type, location, properties | take 500"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    return _parse_rows(cap.stdout) if cap.ok else []


# SQL logical-server / managed-instance types and the child database types they expand to.
_SQL_SERVER_TO_DB = {
    "microsoft.sql/servers": "microsoft.sql/servers/databases",
    "microsoft.sql/managedinstances": "microsoft.sql/managedinstances/databases",
}


def _is_sql_system_db(res: dict[str, Any]) -> bool:
    """True for the ``master`` system database, which is not a user-managed backup concern."""
    rtype = str(res.get("type", "")).lower()
    if rtype not in ("microsoft.sql/servers/databases", "microsoft.sql/managedinstances/databases"):
        return False
    # ARG names child DBs as "<server>/<db>"; compare the trailing segment.
    name = str(res.get("name", "")).split("/")[-1].strip().lower()
    return name == "master"


async def _expand_sql_databases(
    resources: list[dict[str, Any]], connection: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Fetch the child databases of any SQL logical servers / managed instances in scope.

    A workload often references the *server* resource (``microsoft.sql/servers``), but the
    backup/DR posture (PITR, geo-redundancy, retention) lives on its *databases*. When a
    server is in scope we pull its databases via Resource Graph so they're evaluated even if
    the database resources weren't independently in the workload's scope."""
    from app.exec.command_runner import run_kql_capture

    server_ids = [
        str(r.get("id", "")) for r in resources if str(r.get("type", "")).lower() in _SQL_SERVER_TO_DB
    ]
    server_ids = [s for s in server_ids if s]
    if not server_ids:
        return []

    # Match databases whose id is under one of the server ids (id == "<server>/databases/<db>").
    prefixes = " or ".join(f"id startswith '{_esc(sid)}/'" for sid in server_ids)
    db_types = ", ".join(f"'{t}'" for t in sorted(set(_SQL_SERVER_TO_DB.values())))
    kql = (
        f"Resources | where type in~ ({db_types}) and ({prefixes}) "
        "| project id, name, type, resourceGroup, subscriptionId, location, properties, sku, tags "
        "| order by name asc | take 1000"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    return _parse_rows(cap.stdout) if cap.ok else []


def _state_from_arg(res: dict[str, Any]) -> dict[str, Any]:
    """Best-effort backup/DR facts derivable from a resource's ARG properties alone.

    Deep facts (protected-item status, last job, ASR health) require MCP / gated az and are
    layered on in collect_coverage; this provides the type-intrinsic signals (geo flags,
    soft-delete, encryption) so the page is useful even without command execution."""
    rtype = str(res.get("type", "")).lower()
    props = res.get("properties") if isinstance(res.get("properties"), dict) else {}
    sku = res.get("sku") if isinstance(res.get("sku"), dict) else {}
    st: dict[str, Any] = {}

    if rtype == "microsoft.keyvault/vaults":
        st["soft_delete"] = bool(props.get("enableSoftDelete", True))
        st["purge_protection"] = bool(props.get("enablePurgeProtection", False))
        st["encryption"] = "pmk"
    elif rtype == "microsoft.storage/storageaccounts":
        sku_name = str(sku.get("name", "")).lower()
        st["geo_redundant"] = "grs" in sku_name or "ragrs" in sku_name
        st["encryption"] = "pmk"
        st["soft_delete"] = True
    elif rtype == "microsoft.dbforpostgresql/flexibleservers":
        backup = props.get("backup") if isinstance(props.get("backup"), dict) else {}
        st["geo_redundant"] = str(backup.get("geoRedundantBackup", "")).lower() == "enabled"
        st["retention_days"] = backup.get("backupRetentionDays")
        st["backup_enabled"] = True  # PG Flexible always has automated backup
        ha = props.get("highAvailability") if isinstance(props.get("highAvailability"), dict) else {}
        st["dr_pair"] = str(ha.get("mode", "")).lower() in ("zoneredundant", "samezone") or bool(props.get("replica"))
        st["encryption"] = "pmk"
    elif rtype in ("microsoft.sql/servers/databases", "microsoft.sql/managedinstances/databases"):
        st["backup_enabled"] = True  # PITR always on
        st["encryption"] = "pmk"
        st["geo_redundant"] = str(props.get("requestedBackupStorageRedundancy", "")).lower() in ("geo", "geozone")
    elif rtype == "microsoft.dbformysql/flexibleservers":
        backup = props.get("backup") if isinstance(props.get("backup"), dict) else {}
        st["geo_redundant"] = str(backup.get("geoRedundantBackup", "")).lower() == "enabled"
        st["retention_days"] = backup.get("backupRetentionDays")
        st["backup_enabled"] = True  # MySQL Flexible always has automated backup
        ha = props.get("highAvailability") if isinstance(props.get("highAvailability"), dict) else {}
        st["dr_pair"] = str(ha.get("mode", "")).lower() in ("zoneredundant", "samezone") or bool(props.get("replicationRole"))
        st["encryption"] = "pmk"
    elif rtype == "microsoft.documentdb/databaseaccounts":
        # Continuous backup => PITR; multiple locations => geo redundancy / DR.
        bp = props.get("backupPolicy") if isinstance(props.get("backupPolicy"), dict) else {}
        st["pitr"] = str(bp.get("type", "")).lower() == "continuous"
        locations = props.get("locations") if isinstance(props.get("locations"), list) else []
        st["geo_redundant"] = len(locations) > 1
        st["dr_pair"] = bool(props.get("enableMultipleWriteLocations")) or len(locations) > 1
        st["encryption"] = "cmk" if props.get("keyVaultKeyUri") else "pmk"
    elif rtype == "microsoft.cache/redis":
        redis_cfg = props.get("redisConfiguration") if isinstance(props.get("redisConfiguration"), dict) else {}
        aof = str(redis_cfg.get("aof-backup-enabled", "")).lower() == "true"
        rdb = str(redis_cfg.get("rdb-backup-enabled", "")).lower() == "true"
        st["persistence"] = aof or rdb
        st["persistence_mode"] = "AOF" if aof else ("RDB" if rdb else "")
        # Geo-replication (linkedServers) is only on Premium; best-effort flag.
        st["geo_redundant"] = bool(props.get("linkedServers"))
        st["dr_pair"] = bool(props.get("linkedServers"))
        st["encryption"] = "pmk"
    elif rtype == "microsoft.containerregistry/registries":
        sku_name = str(sku.get("name", "")).lower()
        policies = props.get("policies") if isinstance(props.get("policies"), dict) else {}
        sd = policies.get("softDeletePolicy") if isinstance(policies.get("softDeletePolicy"), dict) else {}
        st["soft_delete"] = str(sd.get("status", "")).lower() == "enabled"
        # Geo-replication requires Premium; replications are sub-resources, so flag by SKU heuristic.
        st["geo_redundant"] = sku_name == "premium"
        st["encryption"] = "pmk"
    elif rtype in ("microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces"):
        # Geo-DR pairing is a 'disasterRecoveryConfig' sub-resource; when paired, the
        # namespace exposes an alias (alternateName) / partnerNamespace in properties.
        st["geo_dr_pair"] = bool(props.get("alternateName") or props.get("partnerNamespace"))
        st["encryption"] = "cmk" if (props.get("encryption") or {}).get("keyVaultProperties") else "pmk"
    return st


# --------------------------------------------------------------------------- orchestrator
async def collect_coverage(
    connection: dict[str, Any] | None,
    *,
    scope_kind: str,
    scope_id: str,
    workload: dict[str, Any] | None,
    sla_hours: int,
    stale_drill_days: int,
    scan_cap: int,
) -> dict[str, Any]:
    from app.assessments.runner import _resolve_scope, scope_predicate_batches

    subscriptions: list[str] = []
    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        predicate = scope.get("predicate") or ""
        subscriptions = list(scope.get("subscriptions") or [])
        for sub, _rg in scope.get("rg_pairs") or []:
            if sub not in subscriptions:
                subscriptions.append(sub)
        if scope.get("error") and not predicate:
            return _empty_snapshot(scope_kind, scope_id, error=scope["error"])
        predicates = scope_predicate_batches(scope)
    elif scope_kind == "subscription" and scope_id:
        predicates = [f"subscriptionId =~ '{_esc(scope_id)}'"]
        subscriptions = [scope_id]
    else:
        return _empty_snapshot(scope_kind, scope_id, error="No resolvable scope.")

    try:
        resources = await _query_resources(predicates, connection)
        # Expand SQL logical servers / managed instances into their databases so the DB-level
        # backup/DR posture shows even when only the server resource is in the workload scope.
        extra_dbs = await _expand_sql_databases(resources, connection)
        if extra_dbs:
            have = {str(r.get("id", "")).lower() for r in resources}
            resources.extend(d for d in extra_dbs if str(d.get("id", "")).lower() not in have)
        # Drop SQL 'master' system databases — not a user-managed backup concern.
        resources = [r for r in resources if not _is_sql_system_db(r)]
        sub_guids = subscriptions or sorted({str(r.get("subscriptionId", "")) for r in resources if r.get("subscriptionId")})
        vaults = await _query_vaults(sub_guids, connection)
    except RuntimeError as exc:
        return _empty_snapshot(scope_kind, scope_id, error=str(exc)[:300])

    ref_types = load_reference().get("types", {})
    targets = [r for r in resources if str(r.get("type", "")).lower() in ref_types][:scan_cap]

    # Type-intrinsic state from ARG (always available).
    state_by_resource: dict[str, dict[str, Any]] = {}
    for r in targets:
        state_by_resource[str(r.get("id", "")).lower()] = _state_from_arg(r)

    # Deep facts (protected items, last job, ASR) would be layered here via MCP/az; in the
    # read-only-without-exec case we proceed with the ARG-derived state. DR pairs likewise
    # come from ASR queries when available.
    dr_pairs: list[dict[str, Any]] = []
    note = ""
    _ = vaults  # vault region resolution would map protected items → vault.location here

    snap = compute_coverage(
        resources, state_by_resource, dr_pairs, sla_hours=sla_hours, stale_drill_days=stale_drill_days
    )
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": (workload or {}).get("name") if scope_kind == "workload" else scope_id,
            "connection_configured": connection is not None,
            "source": "azure_resource_graph",
            "demo": False,
            "error": note,
        }
    )
    return snap


def _empty_snapshot(scope_kind: str, scope_id: str, *, error: str) -> dict[str, Any]:
    return {
        "generated_at": _now_iso(),
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_id,
        "connection_configured": False,
        "source": "azure_resource_graph",
        "demo": False,
        "scorecard": {
            "total": 0, "protected": 0, "pct_protected": 100, "pct_offsite": 100,
            "pct_recent_job": 100, "dr_pairs": 0, "dr_pairs_stale": 0, "dr_pairs_unhealthy": 0,
            "last_drill_days": None,
        },
        "groups": [],
        "dr_pairs": [],
        "gaps": [],
        "all_resources": [],
        "error": error,
    }

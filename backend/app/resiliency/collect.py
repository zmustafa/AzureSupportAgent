"""The one collector Recovery Readiness owns: redundancy and native-PaaS-backup config.

Everything else this module reasons about is already collected by somebody else. These two
things are not, and they are the difference between "is it redundant" and "can it be
recovered":

* **redundancy** — ``zones[]``, ``zoneRedundant``, storage SKU replication, Cosmos write
  regions, AKS pool zones. Scattered across a dozen resource types and never gathered as
  one coherent set.
* **native PaaS backup** — Cosmos ``backupIntervalInMinutes``, SQL point-in-time retention,
  PostgreSQL ``geoRedundantBackup``, Storage point-in-time restore. Backup Manager is
  vault-centric and cannot see any of it, which is exactly the gap that makes a PaaS estate
  look unprotected when it is not, and protected when it is not.

Read-only, Resource Graph only, and fail-soft per source: one unsupported table degrades
that slice to ``unknown`` rather than failing the sweep.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.backup_manager import service

log = logging.getLogger("app.resiliency.collect")

MAX_ROWS = 5000


def _as_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


# Resource Graph hands ``properties`` back as dynamic OR as a string depending on the query,
# so every projection re-parses it. Same defense as ``backup_manager.inventory._q``.
_BASE = (
    "Resources\n"
    "| where type in~ ({types})\n"
    "| extend p = parse_json(tostring(properties))\n"
    "| extend s = parse_json(tostring(sku))\n"
    "| project id = tolower(id), name, type = tolower(type), location,\n"
    "          resourceGroup, subscriptionId, zones,\n"
    "          skuName = tostring(s.name), skuTier = tostring(s.tier),\n"
    "          props = p\n"
)

#: Types we can say something meaningful about. Anything outside this list is reported as
#: `unknown`, never as unprotected — see the join.
SUPPORTED_TYPES: tuple[str, ...] = (
    "microsoft.compute/virtualmachines",
    "microsoft.compute/disks",
    "microsoft.storage/storageaccounts",
    "microsoft.sql/servers/databases",
    "microsoft.documentdb/databaseaccounts",
    "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.dbformysql/flexibleservers",
    "microsoft.containerservice/managedclusters",
    "microsoft.network/applicationgateways",
    "microsoft.network/loadbalancers",
    "microsoft.network/publicipaddresses",
    "microsoft.cache/redis",
    "microsoft.web/sites",
    "microsoft.web/serverfarms",
    "microsoft.keyvault/vaults",
    "microsoft.search/searchservices",
    "microsoft.cdn/profiles",
    "microsoft.network/trafficmanagerprofiles",
    "microsoft.logic/workflows",
)


def config_query(types: tuple[str, ...] = SUPPORTED_TYPES) -> str:
    return _BASE.format(types=", ".join(f"'{t}'" for t in types))


# --------------------------------------------------------------------------- shaping
_ZRS_SKUS = {"standard_zrs", "standard_gzrs", "standard_ragzrs", "premium_zrs"}
_GEO_SKUS = {"standard_grs", "standard_ragrs", "standard_gzrs", "standard_ragzrs"}


def _storage_replication(sku_name: str) -> str:
    name = (sku_name or "").strip().lower()
    if not name:
        return ""
    if name in {"standard_ragzrs"}:
        return "RA-GZRS"
    if name in {"standard_gzrs"}:
        return "GZRS"
    if name in {"standard_ragrs"}:
        return "RA-GRS"
    if name in {"standard_grs"}:
        return "GRS"
    if name in _ZRS_SKUS:
        return "ZRS"
    return "LRS"


def shape(row: dict[str, Any]) -> dict[str, Any]:
    """One Resource Graph row into the resiliency configuration shape.

    Per-type, because "is this zone redundant" is a different property on every service and
    there is no generic answer. An unrecognized type yields ``zone_redundant: None`` — the
    honest value, distinct from ``False``.
    """
    rtype = str(row.get("type") or "").lower()
    props = _as_json(row.get("props")) or {}
    if not isinstance(props, dict):
        props = {}
    zones = [str(z) for z in (row.get("zones") or []) if z]
    sku_name = str(row.get("skuName") or "")

    zone_redundant: bool | None = None
    replication = ""
    native: dict[str, Any] = {"kind": "unknown"}
    size_gb: int | None = None

    if rtype == "microsoft.storage/storageaccounts":
        replication = _storage_replication(sku_name)
        zone_redundant = replication in {"ZRS", "GZRS", "RA-GZRS"}
        policy = props.get("restorePolicy") or {}
        if isinstance(policy, dict) and policy.get("enabled"):
            native = {"kind": "storage_pitr", "interval_minutes": 5,
                      "retention_days": policy.get("days"),
                      "geo_redundant": replication in {"GRS", "RA-GRS", "GZRS", "RA-GZRS"}}
        else:
            native = {"kind": "none"}

    elif rtype == "microsoft.sql/servers/databases":
        zone_redundant = bool(props.get("zoneRedundant"))
        # Point-in-time restore is always on for Azure SQL; the retention is what varies.
        retention = props.get("earliestRestoreDate")
        native = {"kind": "sql_pitr", "interval_minutes": 10,
                  "retention_days": props.get("backupRetentionDays") or (7 if retention else None),
                  "geo_redundant": str(props.get("requestedBackupStorageRedundancy") or
                                       props.get("currentBackupStorageRedundancy") or
                                       "").lower().startswith("geo")}
        max_bytes = props.get("maxSizeBytes")
        if isinstance(max_bytes, (int, float)) and max_bytes > 0:
            size_gb = int(max_bytes // (1024 ** 3))

    elif rtype == "microsoft.documentdb/databaseaccounts":
        locations = props.get("locations") or props.get("readLocations") or []
        multi_write = bool(props.get("enableMultipleWriteLocations"))
        zone_redundant = any(bool(loc.get("isZoneRedundant"))
                             for loc in locations if isinstance(loc, dict))
        replication = "multi-region-write" if multi_write else (
            "multi-region-read" if len(locations) > 1 else "single-region")
        policy = props.get("backupPolicy") or {}
        ptype = str(policy.get("type") or "").lower() if isinstance(policy, dict) else ""
        if ptype == "continuous":
            native = {"kind": "cosmos_continuous", "interval_minutes": 1, "geo_redundant": True}
        elif isinstance(policy, dict):
            periodic = policy.get("periodicModeProperties") or {}
            interval = periodic.get("backupIntervalInMinutes")
            native = {"kind": "cosmos_periodic",
                      "interval_minutes": int(interval) if interval else None,
                      "retention_days": (int(periodic["backupRetentionIntervalInHours"]) // 24
                                         if periodic.get("backupRetentionIntervalInHours") else None),
                      "geo_redundant": str(periodic.get("backupStorageRedundancy") or
                                           "").lower().startswith("geo")}

    elif rtype == "microsoft.dbforpostgresql/flexibleservers":
        ha = props.get("highAvailability") or {}
        mode = str(ha.get("mode") or "").lower() if isinstance(ha, dict) else ""
        zone_redundant = mode == "zoneredundant"
        backup = props.get("backup") or {}
        geo = str(backup.get("geoRedundantBackup") or "").lower() == "enabled"
        native = {"kind": "pg_backup", "interval_minutes": 10,
                  "retention_days": backup.get("backupRetentionDays"), "geo_redundant": geo}
        storage = props.get("storage") or {}
        if isinstance(storage, dict) and storage.get("storageSizeGB"):
            size_gb = int(storage["storageSizeGB"])

    elif rtype == "microsoft.dbformysql/flexibleservers":
        ha = props.get("highAvailability") or {}
        zone_redundant = str((ha or {}).get("mode") or "").lower() == "zoneredundant"
        backup = props.get("backup") or {}
        native = {"kind": "pg_backup", "interval_minutes": 10,
                  "retention_days": backup.get("backupRetentionDays"),
                  "geo_redundant": str(backup.get("geoRedundantBackup") or "").lower() == "enabled"}

    elif rtype == "microsoft.containerservice/managedclusters":
        pools = props.get("agentPoolProfiles") or []
        pool_zones = {str(z) for pool in pools if isinstance(pool, dict)
                      for z in (pool.get("availabilityZones") or [])}
        zones = zones or sorted(pool_zones)
        zone_redundant = len(pool_zones) > 1
        native = {"kind": "none"}

    elif rtype == "microsoft.compute/virtualmachines":
        zone_redundant = len(zones) > 1
        native = {"kind": "none"}

    elif rtype == "microsoft.compute/disks":
        replication = "ZRS" if (sku_name or "").lower().endswith("zrs") else "LRS"
        zone_redundant = replication == "ZRS"
        native = {"kind": "none"}
        if props.get("diskSizeGB"):
            size_gb = int(props["diskSizeGB"])

    elif rtype in ("microsoft.network/applicationgateways", "microsoft.network/loadbalancers",
                   "microsoft.network/publicipaddresses"):
        zone_redundant = len(zones) > 1
        native = {"kind": "none"}

    elif rtype in ("microsoft.cdn/profiles", "microsoft.network/trafficmanagerprofiles"):
        zone_redundant = True  # global services have no zonal footprint to lose
        replication = "global"
        native = {"kind": "none"}

    return {
        "id": service.canonical_id(str(row.get("id") or "")),
        "name": str(row.get("name") or ""),
        "type": rtype,
        "location": str(row.get("location") or ""),
        "resource_group": str(row.get("resourceGroup") or ""),
        "subscription_id": str(row.get("subscriptionId") or ""),
        "zones": zones,
        "zone_redundant": zone_redundant,
        "replication": replication,
        "sku": sku_name,
        "native_backup": native,
        "size_gb": size_gb,
        "soft_delete": None,
    }


# --------------------------------------------------------------------------- collection
async def collect(
    connection: dict[str, Any], subscriptions: list[str], *, max_rows: int = MAX_ROWS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Configuration for every supported resource in scope.

    Returns ``(rows, meta)``. ``meta`` carries ``error`` and ``partial`` so an unreadable or
    truncated sweep is reported as such — never as an empty estate.
    """
    rows, metadata, error = await service.arg_safe_detailed(
        connection, config_query(), subscriptions, max_rows=max_rows,
    )
    if error:
        log.info("resiliency: configuration query degraded: %s", error)
    shaped = [shape(r) for r in rows]
    shaped = [r for r in shaped if r["id"]]
    return shaped, {
        "error": error,
        "partial": bool(metadata.get("partial")),
        "source_total": metadata.get("source_total"),
        "count": len(shaped),
    }


def collect_demo(scope_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The synthetic estate, in the same shape ``collect`` returns."""
    from app import demo_catalog

    rows = demo_catalog.resiliency_for(scope_id)
    return rows, {"error": "", "partial": False, "source_total": len(rows), "count": len(rows)}


__all__ = ["collect", "collect_demo", "shape", "config_query", "SUPPORTED_TYPES"]

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
    # `master` is the Azure SQL system database. It is created with the server, cannot be
    # deleted or restored on its own, and carries no customer data — so a recovery verdict
    # against it is noise that inflates every count on the page.
    "| where not(type =~ 'microsoft.sql/servers/databases' and name =~ 'master')\n"
    "| extend p = parse_json(tostring(properties))\n"
    "| extend s = parse_json(tostring(sku))\n"
    "| project id = tolower(id), name, type = tolower(type), location,\n"
    "          resourceGroup, subscriptionId, zones,\n"
    "          skuName = tostring(s.name), skuTier = tostring(s.tier),\n"
    "          props = p\n"
)

#: Types we can say something meaningful about. Anything outside this list is reported as
#: `unknown`, never as unprotected — see the join.
#:
#: Adding a type here is the easy half. It must ALSO be classified: a `shape()` branch, and a
#: decision in `model.STATELESS_TYPES` / `model.GLOBAL_TYPES` /
#: `derive._SELF_HEALING_TYPES`. `test_every_supported_type_is_classified_somewhere` enforces
#: that, because a collected-but-unclassified type is `unknown` everywhere — noise, not
#: coverage.
SUPPORTED_TYPES: tuple[str, ...] = (
    "microsoft.compute/virtualmachines",
    "microsoft.compute/disks",
    "microsoft.compute/virtualmachinescalesets",
    "microsoft.storage/storageaccounts",
    "microsoft.sql/servers/databases",
    "microsoft.sql/managedinstances",
    "microsoft.documentdb/databaseaccounts",
    "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.dbformysql/flexibleservers",
    "microsoft.netapp/netappaccounts/capacitypools/volumes",
    "microsoft.containerservice/managedclusters",
    "microsoft.network/applicationgateways",
    "microsoft.network/loadbalancers",
    "microsoft.network/publicipaddresses",
    "microsoft.network/azurefirewalls",
    "microsoft.network/natgateways",
    "microsoft.network/virtualnetworkgateways",
    "microsoft.network/bastionhosts",
    "microsoft.cache/redis",
    "microsoft.cache/redisenterprise",
    "microsoft.web/sites",
    "microsoft.web/serverfarms",
    "microsoft.web/staticsites",
    "microsoft.app/containerapps",
    "microsoft.app/managedenvironments",
    "microsoft.desktopvirtualization/hostpools",
    "microsoft.keyvault/vaults",
    "microsoft.search/searchservices",
    "microsoft.cdn/profiles",
    "microsoft.network/trafficmanagerprofiles",
    "microsoft.logic/workflows",
    "microsoft.containerregistry/registries",
    "microsoft.apimanagement/service",
    "microsoft.eventhub/namespaces",
    "microsoft.servicebus/namespaces",
    "microsoft.datafactory/factories",
    # The backup estate itself: a vault whose storage never leaves the region takes every
    # recovery point with it when that region is lost.
    "microsoft.recoveryservices/vaults",
    "microsoft.dataprotection/backupvaults",
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


def shape(row: dict[str, Any], blob_services: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """One Resource Graph row into the resiliency configuration shape.

    Per-type, because "is this zone redundant" is a different property on every service and
    there is no generic answer. An unrecognized type yields ``zone_redundant: None`` — the
    honest value, distinct from ``False``.

    ``blob_services`` carries the per-account ``blobServices/default`` bodies that Resource
    Graph cannot supply (see :func:`collect_blob_services`). Absent means "not read", which is
    deliberately not the same as "nothing configured".
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
    soft_delete: bool | None = None
    holds_data: bool | None = None

    if rtype == "microsoft.storage/storageaccounts":
        replication = _storage_replication(sku_name)
        zone_redundant = replication in {"ZRS", "GZRS", "RA-GZRS"}
        # restorePolicy, deleteRetentionPolicy and containerDeleteRetentionPolicy live on the
        # blobServices/default CHILD, which Resource Graph does not index at all — verified
        # against a live tenant. Reading them off the account silently reported every storage
        # account as having no point-in-time copy.
        geo = replication in {"GRS", "RA-GRS", "GZRS", "RA-GZRS"}
        blob = (blob_services or {}).get(service.canonical_id(str(row.get("id") or "")))
        if blob is None:
            native = {"kind": "unknown"}
            soft_delete = None
        else:
            restore = blob.get("restorePolicy") or {}
            if isinstance(restore, dict) and restore.get("enabled"):
                native = {"kind": "storage_pitr", "interval_minutes": 5,
                          "retention_days": restore.get("days"), "geo_redundant": geo}
            else:
                native = {"kind": "none"}
            blob_delete = blob.get("deleteRetentionPolicy") or {}
            container_delete = blob.get("containerDeleteRetentionPolicy") or {}
            soft_delete = bool(blob_delete.get("enabled")) or bool(container_delete.get("enabled"))

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
        # Its own kind, not PostgreSQL's: the log-backup cadence is 5 minutes rather than 10,
        # and the deleted-server how-to is a different page. Sharing one kind meant a change to
        # either silently moved the other.
        native = {"kind": "mysql_backup", "interval_minutes": 5,
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

    elif rtype == "microsoft.cache/redis":
        config = props.get("redisConfiguration") or {}
        if not isinstance(config, dict):
            config = {}
        rdb = str(config.get("rdb-backup-enabled") or "").lower() == "true"
        aof = str(config.get("aof-backup-enabled") or "").lower() == "true"
        # A cache has nothing to lose; a Redis with persistence on is a data store. The type
        # cannot tell you which, so the configuration decides.
        holds_data = rdb or aof
        if rdb:
            frequency = config.get("rdb-backup-frequency")
            native = {"kind": "redis_rdb",
                      "interval_minutes": int(frequency) if frequency else None}
        elif aof:
            native = {"kind": "redis_aof", "interval_minutes": 1}
        else:
            native = {"kind": "none"}

    elif rtype == "microsoft.cache/redisenterprise":
        # Azure Managed Redis, the service Azure Cache for Redis is being retired in favour
        # of. `zones` comes back EMPTY even on a zone-redundant cluster, so the read-only
        # `redundancyMode` is the only property that answers the question: "ZR" is zone
        # redundant, "LR" is a single zone. Reading `zones` here reported a cluster verified
        # zone redundant as not zone redundant.
        mode = str(props.get("redundancyMode") or "").strip().upper()
        if mode:
            zone_redundant = mode == "ZR"
        elif zones:
            zone_redundant = len(zones) > 1
        # `native` deliberately stays "unknown". Persistence is configured on the child
        # `databases` resource, which Resource Graph does not index at all, so this row
        # cannot tell a pure cache from a persisted data store. Defaulting it to "none"
        # would report a persisted database as having no recovery path, and treating the
        # cluster as stateless would report it as redeployable. Both are claims the data
        # does not support.

    elif rtype == "microsoft.compute/virtualmachinescalesets":
        zone_redundant = len(zones) > 1
        profile = props.get("virtualMachineProfile") or {}
        storage = (profile.get("storageProfile") or {}) if isinstance(profile, dict) else {}
        disks = (storage.get("dataDisks") or []) if isinstance(storage, dict) else []
        # Most scale sets are stateless web tiers that are redeployed rather than restored.
        # Attached data disks are the signal that this one is not.
        holds_data = bool(disks)
        native = {"kind": "none"}

    elif rtype == "microsoft.sql/managedinstances":
        zone_redundant = bool(props.get("zoneRedundant"))
        # No retention here on purpose: `backupRetentionDays` is a property of the
        # DATABASES, not of the instance, so reading it off the instance always yields None.
        native = {"kind": "sql_pitr", "interval_minutes": 10,
                  "geo_redundant": str(props.get("requestedBackupStorageRedundancy")
                                       or "").lower().startswith("geo")}
        storage_gb = props.get("storageSizeInGB")
        if storage_gb:
            size_gb = int(storage_gb)

    elif rtype == "microsoft.netapp/netappaccounts/capacitypools/volumes":
        zone_redundant = len(zones) > 1
        protection = props.get("dataProtection") or {}
        if not isinstance(protection, dict):
            protection = {}
        snapshots = bool((protection.get("snapshot") or {}).get("snapshotPolicyId"))
        vaulted = bool((protection.get("backup") or {}).get("backupPolicyId"))
        if (protection.get("replication") or {}).get("remoteVolumeResourceId"):
            replication = "cross-region"
        # Snapshots live ON the volume and die with it; only a vaulted backup is independent
        # storage. Collapsing both into one kind lost exactly the distinction deletion needs.
        if vaulted:
            native = {"kind": "anf_backup"}
        elif snapshots:
            native = {"kind": "anf_snapshot"}
        else:
            native = {"kind": "none"}
        threshold = props.get("usageThreshold")
        if threshold:
            size_gb = int(int(threshold) // (1024 ** 3))

    elif rtype in ("microsoft.recoveryservices/vaults",
                   "microsoft.dataprotection/backupvaults"):
        token = str(
            (props.get("redundancySettings") or {}).get("standardTierStorageRedundancy")
            or ((props.get("storageSettings") or [{}])[0] or {}).get("type") or "").lower()
        if token:
            replication = "GRS" if "geo" in token else "ZRS" if "zone" in token else "LRS"
            zone_redundant = "zone" in token or "geo" in token
        security = props.get("securitySettings") or {}
        soft = (security.get("softDeleteSettings") or {}) if isinstance(security, dict) else {}
        state = str(soft.get("softDeleteState") or "").lower()
        soft_delete = state in ("enabled", "alwayson") if state else None
        native = {"kind": "none"}

    elif rtype == "microsoft.containerregistry/registries":
        zone_redundant = str(props.get("zoneRedundancy") or "").lower() == "enabled"
        policies = props.get("policies") or {}
        soft = (policies.get("softDeletePolicy") or {}) if isinstance(policies, dict) else {}
        if soft:
            soft_delete = str(soft.get("status") or "").lower() == "enabled"
        native = {"kind": "none"}

    elif rtype == "microsoft.apimanagement/service":
        extra = props.get("additionalLocations")
        if isinstance(extra, list) and extra:
            replication = "multi-region"
        zone_redundant = len(zones) > 1 if zones else None
        native = {"kind": "none"}

    elif rtype in ("microsoft.eventhub/namespaces", "microsoft.servicebus/namespaces",
                   "microsoft.app/managedenvironments", "microsoft.app/containerapps"):
        flag = props.get("zoneRedundant")
        zone_redundant = bool(flag) if flag is not None else (len(zones) > 1 if zones else None)
        native = {"kind": "none"}

    elif rtype in ("microsoft.network/azurefirewalls", "microsoft.network/natgateways",
                   "microsoft.network/virtualnetworkgateways",
                   "microsoft.network/bastionhosts",
                   "microsoft.desktopvirtualization/hostpools",
                   "microsoft.datafactory/factories"):
        zone_redundant = len(zones) > 1 if zones else None
        native = {"kind": "none"}

    elif rtype == "microsoft.web/staticsites":
        zone_redundant = True  # content is served globally, with no zonal footprint
        replication = "global"
        native = {"kind": "none"}

    elif rtype in ("microsoft.cdn/profiles", "microsoft.network/trafficmanagerprofiles"):
        zone_redundant = True  # global services have no zonal footprint to lose
        replication = "global"
        native = {"kind": "none"}

    elif rtype == "microsoft.keyvault/vaults":
        # Soft delete is what stands between a deleted vault and every key it held. Purge
        # protection is what stops that window being skipped, so it strengthens the answer
        # rather than replacing it.
        soft_delete = bool(props.get("enableSoftDelete"))
        purge = bool(props.get("enablePurgeProtection"))
        native = {"kind": "keyvault_soft_delete" if soft_delete else "none",
                  "retention_days": props.get("softDeleteRetentionInDays"),
                  "purge_protection": purge}

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
        "soft_delete": soft_delete,
        "holds_data": holds_data,
    }


# --------------------------------------------------------------------------- ARM supplements
# Two facts this module needs are NOT in Resource Graph. Verified against a live tenant, not
# assumed: `microsoft.storage/storageaccounts/blobservices` returns zero rows tenant-wide, and
# `microsoft.authorization/locks` is absent from both `Resources` and `authorizationresources`
# even though the table itself works. Both therefore come from ARM, and both are fail-soft:
# an unreadable source degrades that slice to `unknown`, never to "nothing configured".
_BLOB_SERVICE_API = "2023-05-01"
_LOCKS_API = "2016-09-01"

#: Storage accounts are a small slice of a typical estate, but not a bounded one. Cap the
#: fan-out so a storage-heavy tenant cannot turn one sweep into thousands of serial calls.
MAX_BLOB_SERVICE_CALLS = 400
_BLOB_SERVICE_CONCURRENCY = 12


async def collect_blob_services(
    connection: dict[str, Any], storage_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], str]:
    """``blobServices/default`` per storage account, from ARM.

    Returns ``(by_canonical_id, error)``. An account missing from the map was NOT read, which
    :func:`shape` renders as ``unknown`` rather than as absent protection — the distinction the
    original bug erased."""
    import asyncio

    ids = [service.canonical_id(i) for i in storage_ids if i]
    ids = [i for i in ids if i]
    if not ids:
        return {}, ""
    truncated = ""
    if len(ids) > MAX_BLOB_SERVICE_CALLS:
        truncated = (f"Only the first {MAX_BLOB_SERVICE_CALLS} of {len(ids)} storage accounts "
                     "were read for blob-service settings.")
        ids = ids[:MAX_BLOB_SERVICE_CALLS]

    try:
        token = await service.token_for(connection)
    except Exception as exc:  # noqa: BLE001 - a token failure degrades the slice, never the sweep
        return {}, service.safe_error(str(exc))

    out: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    sem = asyncio.Semaphore(_BLOB_SERVICE_CONCURRENCY)

    async def one(rid: str) -> None:
        async with sem:
            try:
                body, status, error = await service.arm_get_with(
                    token, f"{rid}/blobServices/default", _BLOB_SERVICE_API)
            except Exception as exc:  # noqa: BLE001
                errors.append(service.safe_error(str(exc)))
                return
            if error or not isinstance(body, dict):
                # 404 is a real answer for accounts with no blob service (e.g. FileStorage):
                # nothing to restore there, which is not the same as unreadable.
                if status == 404:
                    out[rid] = {}
                else:
                    errors.append(error or f"HTTP {status}")
                return
            props = body.get("properties")
            out[rid] = props if isinstance(props, dict) else {}

    await asyncio.gather(*(one(rid) for rid in ids))
    reason = truncated
    if errors:
        head = errors[0]
        reason = (f"{reason} " if reason else "") + (
            f"{len(errors)} storage account(s) could not be read: {head}")
    return out, reason


def _lock_scope_kind(scope: str) -> str:
    parts = [p for p in scope.strip("/").split("/") if p]
    if len(parts) <= 2:
        return "subscription"
    if len(parts) <= 4:
        return "resource_group"
    return "resource"


async def collect_locks(
    connection: dict[str, Any], subscriptions: list[str],
) -> tuple[list[dict[str, Any]], str]:
    """Every management lock in scope, from ARM — one call per subscription, not per resource.

    Returns ``(locks, error)`` where each lock carries the lowercased ARM scope it applies to.
    Locks INHERIT, so callers must prefix-match rather than compare for equality."""
    import asyncio

    subs = [str(s).strip() for s in (subscriptions or []) if str(s).strip()]
    if not subs:
        return [], ""
    try:
        token = await service.token_for(connection)
    except Exception as exc:  # noqa: BLE001
        return [], service.safe_error(str(exc))

    out: list[dict[str, Any]] = []
    errors: list[str] = []

    async def one(sub: str) -> None:
        try:
            body, status, error = await service.arm_get_with(
                token, f"/subscriptions/{sub}/providers/Microsoft.Authorization/locks", _LOCKS_API)
        except Exception as exc:  # noqa: BLE001
            errors.append(service.safe_error(str(exc)))
            return
        if error or not isinstance(body, dict):
            errors.append(error or f"HTTP {status}")
            return
        for item in body.get("value") or []:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id") or "")
            marker = "/providers/microsoft.authorization/locks/"
            lowered = raw_id.lower()
            if marker not in lowered:
                continue
            scope = lowered.split(marker, 1)[0].rstrip("/")
            out.append({
                "scope": scope,
                "scope_kind": _lock_scope_kind(scope),
                "level": str((item.get("properties") or {}).get("level") or ""),
                "name": str(item.get("name") or ""),
                "notes": str((item.get("properties") or {}).get("notes") or ""),
            })

    await asyncio.gather(*(one(sub) for sub in subs))
    return out, ("; ".join(errors[:3]) if errors else "")


# --------------------------------------------------------------------------- membership
def _is_or_under(resource_id: str, parent: str) -> bool:
    return resource_id == parent or resource_id.startswith(parent.rstrip("/") + "/")


def expand_members(rows: list[dict[str, Any]], member_ids: set[str]) -> set[str]:
    """Workload member ids, plus the managed disks attached to member VMs.

    Workload discovery classifies disks as child noise and leaves them out of the node set
    (``sculpt.NOISE_TYPE_SUBSTRINGS``). A VM's recovery story is mostly its disks, so
    filtering on the node set alone would drop the resources most likely to have no
    recovery path at all.
    """
    members = {m.lower() for m in member_ids if m}
    if not members:
        return members
    attached: set[str] = set()
    for row in rows:
        if str(row.get("type") or "").lower() != "microsoft.compute/virtualmachines":
            continue
        rid = str(row.get("id") or "").lower()
        if not any(_is_or_under(rid, m) for m in members):
            continue
        profile = (_as_json(row.get("props")) or {}).get("storageProfile") or {}
        if not isinstance(profile, dict):
            continue
        for disk in [profile.get("osDisk") or {}, *(profile.get("dataDisks") or [])]:
            managed = disk.get("managedDisk") if isinstance(disk, dict) else None
            disk_id = str((managed or {}).get("id") or "").lower()
            if disk_id:
                attached.add(disk_id)
    return members | attached


# --------------------------------------------------------------------------- collection
async def collect(
    connection: dict[str, Any], subscriptions: list[str], *, max_rows: int = MAX_ROWS,
    member_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Configuration for every supported resource in scope.

    ``member_ids`` narrows the result to one workload. Resource Graph can only filter by
    subscription, so without it a workload scope returns every resource in the workload's
    subscriptions — the whole subscription, labeled as the workload.

    Returns ``(rows, meta)``. ``meta`` carries ``error`` and ``partial`` so an unreadable or
    truncated sweep is reported as such — never as an empty estate.
    """
    rows, metadata, error = await service.arg_safe_detailed(
        connection, config_query(), subscriptions, max_rows=max_rows,
    )
    if error:
        log.info("resiliency: configuration query degraded: %s", error)
    if member_ids is not None:
        members = expand_members(rows, member_ids)
        rows = [r for r in rows
                if any(_is_or_under(str(r.get("id") or "").lower(), m) for m in members)]

    # Storage blob settings are an ARM-only supplement; fetched AFTER member filtering so a
    # workload scope does not pay for accounts it excluded.
    storage_ids = [str(r.get("id") or "") for r in rows
                   if str(r.get("type") or "").lower() == "microsoft.storage/storageaccounts"]
    blob_services, blob_error = await collect_blob_services(connection, storage_ids)
    if blob_error:
        log.info("resiliency: blob-service settings degraded: %s", blob_error)

    shaped = [shape(r, blob_services) for r in rows]
    shaped = [r for r in shaped if r["id"]]
    return shaped, {
        "error": error,
        "partial": bool(metadata.get("partial")),
        "source_total": metadata.get("source_total"),
        "count": len(shaped),
        "blob_service_error": blob_error,
    }


def collect_demo(scope_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The synthetic estate, in the same shape ``collect`` returns."""
    from app import demo_catalog

    rows = demo_catalog.resiliency_for(scope_id)
    return rows, {"error": "", "partial": False, "source_total": len(rows), "count": len(rows)}


__all__ = ["collect", "collect_demo", "shape", "config_query", "SUPPORTED_TYPES"]

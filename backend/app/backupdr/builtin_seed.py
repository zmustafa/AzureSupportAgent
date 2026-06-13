"""Built-in reference seed: backup/DR protection requirements per resource type.

This is the FALLBACK/seed only — admins edit a persisted, versioned copy in the JSON
registry (reference.py). Each type declares which protection ``checks`` apply (the matrix
columns that are relevant), with a severity + "why" used for findings and the UI.

Check keys (stable identifiers, also drive synthetic finding check_ids):
    backup_enabled     resource is protected by a backup/recovery vault
    policy             a backup policy is attached
    retention          policy retention meets the recommended minimum
    last_job           a successful backup job within the SLA window
    geo_redundancy     offsite/geo redundancy (GRS, geo-backup, paired region)
    offsite_region     backup destination region differs from the resource region
    dr_pair            a DR/replication pair is configured (where applicable)
    encryption         encrypted (CMK preferred where required)
    soft_delete        soft-delete / purge-protection enabled (Key Vault, vaults)
    restore_test       a restore / failover test performed recently
"""
from __future__ import annotations

from typing import Any

BUILTIN_SEED_VERSION = 2

# All known check keys with default metadata (label + why). Per-type specs reference these.
CHECK_META: dict[str, dict[str, str]] = {
    "backup_enabled": {"label": "Backup Enabled", "why": "Resource is not protected by any backup — total data-loss exposure on failure."},
    "policy": {"label": "Policy", "why": "No backup policy attached, so schedule/retention are undefined."},
    "retention": {"label": "Retention", "why": "Retention is below the recommended minimum for this workload tier."},
    "last_job": {"label": "Last Job", "why": "No successful backup within the SLA window — recovery point may be stale or missing."},
    "geo_redundancy": {"label": "Geo-Redundancy", "why": "No geo/offsite redundancy — a regional outage loses the only copy."},
    "offsite_region": {"label": "Backup Region", "why": "Backup destination is in the same region as the resource — a regional outage takes both down."},
    "dr_pair": {"label": "DR Pair", "why": "No disaster-recovery replication pair configured."},
    "encryption": {"label": "Encryption", "why": "Encryption is not configured to the required standard (CMK where mandated)."},
    "soft_delete": {"label": "Soft-Delete", "why": "Soft-delete / purge protection is off — backups or secrets can be permanently deleted."},
    "restore_test": {"label": "Last Restore Test", "why": "No recent restore/failover test — recoverability is unproven."},
    "pitr": {"label": "Point-in-Time Restore", "why": "Continuous/point-in-time backup is not enabled — only coarse periodic restore points exist."},
    "persistence": {"label": "Persistence", "why": "Data persistence (RDB/AOF) is off — a restart or failure loses all cached data."},
    "geo_dr_pair": {"label": "Geo-DR Pairing", "why": "No Geo-DR alias/paired namespace — a regional outage loses the messaging entity and its metadata."},
}


def _t(display: str, category: str, checks: list[str], *, note: str = "") -> dict[str, Any]:
    return {"display": display, "category": category, "note": note, "checks": list(checks)}


BUILTIN_TYPES: dict[str, dict[str, Any]] = {
    "microsoft.compute/virtualmachines": _t(
        "Virtual Machine", "compute",
        ["backup_enabled", "policy", "retention", "last_job", "offsite_region", "geo_redundancy", "dr_pair", "encryption", "restore_test"],
        note="VMs should be backed up to a Recovery Services Vault with GRS and ideally an ASR replication pair.",
    ),
    "microsoft.sql/servers/databases": _t(
        "SQL Database", "data",
        ["backup_enabled", "retention", "last_job", "geo_redundancy", "offsite_region", "dr_pair", "encryption", "restore_test"],
        note="SQL DB needs PITR with adequate retention and a geo-replica/failover group for DR.",
    ),
    "microsoft.sql/managedinstances/databases": _t(
        "SQL Managed Instance DB", "data",
        ["backup_enabled", "retention", "last_job", "geo_redundancy", "offsite_region", "dr_pair", "encryption", "restore_test"],
        note="SQL MI needs PITR + a failover group for DR.",
    ),
    "microsoft.dbforpostgresql/flexibleservers": _t(
        "PostgreSQL Flexible Server", "data",
        ["backup_enabled", "retention", "last_job", "geo_redundancy", "offsite_region", "dr_pair", "encryption", "restore_test"],
        note="PG Flexible needs geo-redundant backup and ideally a read replica in the paired region.",
    ),
    "microsoft.storage/storageaccounts": _t(
        "Storage Account / Files", "data",
        ["backup_enabled", "policy", "retention", "last_job", "geo_redundancy", "offsite_region", "soft_delete", "encryption"],
        note="Azure Files should be backed up via a vault; the account should use RA-GRS for offsite redundancy.",
    ),
    "microsoft.containerservice/managedclusters": _t(
        "AKS Cluster (PVCs)", "compute",
        ["backup_enabled", "policy", "last_job", "offsite_region", "restore_test"],
        note="AKS persistent volumes should be backed up with the AKS Backup extension to a Backup Vault.",
    ),
    "microsoft.keyvault/vaults": _t(
        "Key Vault", "security",
        ["soft_delete", "encryption"],
        note="Key Vault must have soft-delete AND purge protection enabled to prevent permanent loss.",
    ),
    "microsoft.web/sites": _t(
        "App Service", "compute",
        ["backup_enabled", "retention", "last_job"],
        note="App Service (Standard+) supports scheduled backup of content + config to a storage account.",
    ),
    "microsoft.web/sites/functions": _t(
        "Function App", "compute",
        ["backup_enabled", "retention", "last_job"],
        note="Function apps should back up content/config; keep deployment in source control as the primary recovery path.",
    ),
    "microsoft.apimanagement/service": _t(
        "API Management", "integration",
        ["backup_enabled", "last_job", "dr_pair"],
        note="APIM supports backup-to-storage; multi-region deployment provides DR.",
    ),
    "microsoft.documentdb/databaseaccounts": _t(
        "Cosmos DB", "data",
        ["pitr", "geo_redundancy", "offsite_region", "dr_pair", "encryption"],
        note="Cosmos should use continuous backup (PITR) and multi-region with the paired region for DR.",
    ),
    "microsoft.cache/redis": _t(
        "Redis Cache", "data",
        ["persistence", "geo_redundancy", "dr_pair", "encryption"],
        note="Premium Redis should enable RDB/AOF persistence and geo-replication to a paired cache for DR.",
    ),
    "microsoft.dbformysql/flexibleservers": _t(
        "MySQL Flexible Server", "data",
        ["backup_enabled", "retention", "last_job", "geo_redundancy", "offsite_region", "dr_pair", "encryption", "restore_test"],
        note="MySQL Flexible needs geo-redundant backup and ideally a read replica in the paired region.",
    ),
    "microsoft.compute/virtualmachinescalesets": _t(
        "VM Scale Set", "compute",
        ["backup_enabled", "policy", "last_job", "dr_pair", "encryption"],
        note="VMSS instances should be backed up; use zone-redundant or cross-region replication for DR.",
    ),
    "microsoft.containerregistry/registries": _t(
        "Container Registry", "containers",
        ["geo_redundancy", "soft_delete", "encryption"],
        note="Premium ACR should be geo-replicated to the paired region and have soft-delete enabled for image recovery.",
    ),
    "microsoft.servicebus/namespaces": _t(
        "Service Bus", "integration",
        ["geo_dr_pair", "encryption"],
        note="Premium Service Bus should use a Geo-DR alias paired to a secondary namespace.",
    ),
    "microsoft.eventhub/namespaces": _t(
        "Event Hubs", "integration",
        ["geo_dr_pair", "encryption"],
        note="Dedicated/Premium Event Hubs should use a Geo-DR alias paired to a secondary namespace.",
    ),
}


def builtin_reference() -> dict[str, Any]:
    import copy

    return {
        "version": 0,
        "updated_at": "",
        "updated_by": "",
        "builtin_seed_version": BUILTIN_SEED_VERSION,
        "types": copy.deepcopy(BUILTIN_TYPES),
    }

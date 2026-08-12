"""Unprotected-resource detection and the remediation planner.

Two gap sources feed one queue:

* **live detection** — every backup-eligible resource in scope minus every datasource that is
  already a protected item or backup instance.  This is authoritative and needs no prior scan;
* **Backup & DR Coverage** — the read-only detector's cached gap list, ingested so a finding
  raised there can be remediated here without re-scanning.

Remediation targets a vault + policy and produces the exact ARM body that will be submitted
once approved, so the reviewer sees the real payload rather than a description of it.
"""
from __future__ import annotations

from typing import Any

from app.backup_manager import service

# --------------------------------------------------------------------------- eligibility
# Backup-eligible resource types and how Backup Manager would protect them.
# ``mechanism`` selects the ARM contract; ``datasource_type`` is the literal string the
# DataProtection API expects.
ELIGIBLE_TYPES: dict[str, dict[str, Any]] = {
    "microsoft.compute/virtualmachines": {
        "display": "Virtual machine",
        "mechanism": "rsv_vm",
        "vault_kind": "recovery_services",
        "backup_management_type": "AzureIaasVM",
        "severity": "critical",
    },
    "microsoft.compute/disks": {
        "display": "Managed disk",
        "mechanism": "dataprotection",
        "vault_kind": "backup",
        "datasource_type": "Microsoft.Compute/disks",
        "resource_type": "Microsoft.Compute/disks",
        "severity": "warning",
    },
    "microsoft.storage/storageaccounts": {
        "display": "Storage account (blobs)",
        "mechanism": "dataprotection",
        "vault_kind": "backup",
        "datasource_type": "Microsoft.Storage/storageAccounts/blobServices",
        "resource_type": "Microsoft.Storage/storageAccounts/blobServices",
        "child_suffix": "/blobServices/default",
        "severity": "error",
    },
    "microsoft.containerservice/managedclusters": {
        "display": "AKS cluster",
        "mechanism": "dataprotection",
        "vault_kind": "backup",
        "datasource_type": "Microsoft.ContainerService/managedClusters",
        "resource_type": "Microsoft.ContainerService/managedClusters",
        "severity": "error",
    },
    "microsoft.dbforpostgresql/flexibleservers": {
        "display": "PostgreSQL flexible server",
        "mechanism": "dataprotection",
        "vault_kind": "backup",
        "datasource_type": "Microsoft.DBforPostgreSQL/flexibleServers",
        "resource_type": "Microsoft.DBforPostgreSQL/flexibleServers",
        "severity": "error",
    },
}

# Types with meaningful native protection that Backup Manager reports on but never enrols in
# a vault — protecting them is a service-level setting, not a vault operation.
NATIVE_ONLY_TYPES = {
    "microsoft.sql/servers/databases": "SQL Database point-in-time restore and long-term retention",
    "microsoft.documentdb/databaseaccounts": "Cosmos DB continuous or periodic backup",
    "microsoft.keyvault/vaults": "Key Vault soft delete and purge protection",
}

DETECTION_QUERY = """
resources
| where type in~ ({types})
| project id = tolower(id), rawId = id, name, type = tolower(type), location,
    resourceGroup, subscriptionId, tags = tostring(tags)
""".strip()


def detection_query() -> str:
    types = ", ".join(f"'{service.kql_escape(t)}'" for t in ELIGIBLE_TYPES)
    return DETECTION_QUERY.format(types=types)


async def detect(
    connection: dict[str, Any], estate: dict[str, Any], *, subscriptions: set[str] | None = None,
) -> dict[str, Any]:
    """Every eligible resource in scope that has no backup instance pointing at it."""
    rows, metadata, error = await service.arg_safe_detailed(
        connection, detection_query(), subscriptions, max_rows=20000,
    )
    protected = {i.get("datasource_id") for i in estate.get("instances", []) if i.get("datasource_id")}
    # A storage account is protected via its blob service child id, so match on the prefix too.
    protected_prefixes = {p.rsplit("/blobservices/", 1)[0] for p in protected if "/blobservices/" in p}

    gaps: list[dict[str, Any]] = []
    eligible_total = 0
    for row in rows:
        rtype = str(row.get("type") or "").lower()
        spec = ELIGIBLE_TYPES.get(rtype)
        if not spec:
            continue
        eligible_total += 1
        rid = service.canonical_id(row.get("id") or "")
        if rid in protected or rid in protected_prefixes:
            continue
        gaps.append({
            "gap_id": service.canonical_hash({"kind": "unprotected", "id": rid}),
            "source": "live",
            "resource_id": str(row.get("rawId") or row.get("id") or ""),
            "resource_name": str(row.get("name") or ""),
            "resource_type": rtype,
            "display_type": spec["display"],
            "resource_group": str(row.get("resourceGroup") or ""),
            "subscription_id": str(row.get("subscriptionId") or ""),
            "location": str(row.get("location") or ""),
            "mechanism": spec["mechanism"],
            "target_vault_kind": spec["vault_kind"],
            "severity": spec["severity"],
            "reason": "No backup instance protects this resource.",
        })
    gaps.sort(key=lambda g: ({"critical": 0, "error": 1, "warning": 2}.get(g["severity"], 3), g["resource_type"], g["resource_name"]))
    return {
        "gaps": gaps,
        "eligible_total": eligible_total,
        "protected_total": eligible_total - len(gaps),
        "coverage_pct": round(100 * (eligible_total - len(gaps)) / eligible_total) if eligible_total else 100,
        "error": error,
        "source_detail": metadata,
        "truncated": bool(metadata.get("partial")),
        "total_count": metadata.get("source_total"),
        "native_only": [{"type": t, "note": n} for t, n in sorted(NATIVE_ONLY_TYPES.items())],
    }


def ingest_coverage_gaps(tenant_id: str, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Cached Backup & DR Coverage gaps, converted into Backup Manager's gap shape.

    Read-only and cache-only: the detector owns its own scan lifecycle, so this never triggers
    one. Gaps whose resource type Backup Manager cannot enrol are still surfaced, marked
    ``actionable=False``, so the coverage finding is never silently dropped."""
    from app.backupdr import cache as coverage_cache

    snapshot = coverage_cache.read_snapshot(tenant_id, scope_kind, scope_id) or {}
    out: list[dict[str, Any]] = []
    for gap in snapshot.get("gaps", []) or []:
        rtype = str(gap.get("resource_type") or "").lower()
        spec = ELIGIBLE_TYPES.get(rtype)
        rid = service.canonical_id(gap.get("resource_id") or "")
        out.append({
            "gap_id": service.canonical_hash({"kind": "coverage", "id": rid, "checks": gap.get("failed_checks")}),
            "source": "coverage",
            "resource_id": str(gap.get("resource_id") or ""),
            "resource_name": str(gap.get("resource_name") or ""),
            "resource_type": rtype,
            "display_type": (spec or {}).get("display", rtype),
            "resource_group": str(gap.get("resource_group") or ""),
            "subscription_id": str(gap.get("subscription_id") or ""),
            "location": str(gap.get("region") or ""),
            "mechanism": (spec or {}).get("mechanism", ""),
            "target_vault_kind": (spec or {}).get("vault_kind", ""),
            "severity": str(gap.get("severity") or "warning"),
            "failed_checks": [str(c) for c in gap.get("failed_checks") or []],
            "actionable": bool(spec),
            "reason": "Flagged by Backup & DR Coverage: " + ", ".join(str(c) for c in gap.get("failed_checks") or []),
        })
    return out


def ingest_coverage_gaps_for_scope(
    tenant_id: str, scope_kind: str, scope_id: str, subscriptions: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Coverage findings for a scope, merging cached subscription scans for management groups."""
    if scope_kind != "management_group":
        rows = ingest_coverage_gaps(tenant_id, scope_kind, scope_id)
        return rows, {
            "available_snapshots": 1 if rows else 0,
            "missing_snapshots": 0 if rows else 1,
            "partial": False,
        }
    merged: dict[str, dict[str, Any]] = {}
    available = 0
    missing = 0
    from app.backupdr import cache as coverage_cache

    for subscription_id in sorted(set(subscriptions)):
        if coverage_cache.read_snapshot(tenant_id, "subscription", subscription_id) is None:
            missing += 1
            continue
        available += 1
        for row in ingest_coverage_gaps(tenant_id, "subscription", subscription_id):
            merged.setdefault(str(row.get("gap_id") or service.canonical_hash(row)), row)
    return list(merged.values()), {
        "available_snapshots": available,
        "missing_snapshots": missing,
        "partial": missing > 0,
        "not_measured": available == 0,
    }


# --------------------------------------------------------------------------- ARM bodies
def _vm_container_name(resource_group: str, vm_name: str) -> str:
    return f"IaasVMContainer;iaasvmcontainerv2;{resource_group};{vm_name}"


def _vm_item_name(resource_group: str, vm_name: str) -> str:
    return f"vm;iaasvmcontainerv2;{resource_group};{vm_name}"


def rsv_protected_item_id(vault_id: str, resource_group: str, vm_name: str) -> str:
    """ARM id of the protected item that will represent this VM inside the vault."""
    return (
        f"{vault_id.rstrip('/')}/backupFabrics/Azure"
        f"/protectionContainers/{_vm_container_name(resource_group, vm_name)}"
        f"/protectedItems/{_vm_item_name(resource_group, vm_name)}"
    )


def backup_instance_id(vault_id: str, instance_name: str) -> str:
    return f"{vault_id.rstrip('/')}/backupInstances/{instance_name}"


def build_vm_protection_body(*, vm_id: str, policy_id: str) -> dict[str, Any]:
    """Enable-protection body for an Azure VM in a Recovery Services vault."""
    return {
        "properties": {
            "protectedItemType": "Microsoft.Compute/virtualMachines",
            "sourceResourceId": vm_id,
            "policyId": policy_id,
        }
    }


def build_dataprotection_instance(
    *, resource_id: str, resource_name: str, location: str, resource_type: str,
    datasource_type: str, policy_id: str, friendly_name: str,
) -> dict[str, Any]:
    """Backup-instance body for a Backup vault datasource (disk / blob / AKS / PostgreSQL)."""
    datasource = {
        "objectType": "Datasource",
        "resourceID": resource_id,
        "resourceLocation": location,
        "resourceName": resource_name,
        "resourceType": resource_type,
        "resourceUri": resource_id,
        "datasourceType": datasource_type,
    }
    return {
        "properties": {
            "objectType": "BackupInstance",
            "friendlyName": friendly_name,
            "dataSourceInfo": datasource,
            "policyInfo": {"policyId": policy_id},
        }
    }


def instance_name_for(resource_name: str, resource_id: str) -> str:
    """Deterministic, ARM-safe backup-instance name (stable across replays of a plan)."""
    digest = service.canonical_hash({"id": service.canonical_id(resource_id)})[:12]
    safe = "".join(ch for ch in str(resource_name or "item") if ch.isalnum() or ch in "-_")[:40] or "item"
    return f"{safe}-{digest}"


def plan_item(gap: dict[str, Any], vault: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Turn one gap plus a chosen vault/policy into a fully-specified remediation item.

    Returns a ``blocked`` item (never a partially-formed one) when the combination is invalid,
    so validation failures surface in the preview rather than at apply time."""
    spec = ELIGIBLE_TYPES.get(gap.get("resource_type", ""), {})
    resource_id = str(gap.get("resource_id") or "")
    vault_id = str(vault.get("id") or "")
    policy_id = str(policy.get("arm_id") or policy.get("id") or "")

    def blocked(reason: str) -> dict[str, Any]:
        return {**base, "status": "blocked", "reason": reason}

    base = {
        "gap_id": gap.get("gap_id", ""),
        "resource_id": resource_id,
        "resource_name": gap.get("resource_name", ""),
        "resource_type": gap.get("resource_type", ""),
        "display_type": gap.get("display_type", ""),
        "location": gap.get("location", ""),
        "subscription_id": gap.get("subscription_id", ""),
        "resource_group": gap.get("resource_group", ""),
        "vault_id": vault_id,
        "vault_name": vault.get("name", ""),
        "vault_kind": vault.get("kind", ""),
        "policy_id": policy_id,
        "policy_name": policy.get("name", ""),
        "mechanism": spec.get("mechanism", ""),
    }
    if not spec:
        return blocked("Backup Manager cannot enrol this resource type in a vault.")
    if not vault_id or not policy_id:
        return blocked("A target vault and policy are required.")
    if vault.get("kind") != spec.get("vault_kind"):
        return blocked(
            f"{spec['display']} protection requires a "
            f"{'Backup vault' if spec['vault_kind'] == 'backup' else 'Recovery Services vault'}."
        )
    if service.canonical_id(policy.get("vault_id", "")) != service.canonical_id(vault_id):
        return blocked("The selected policy belongs to a different vault.")
    if str(gap.get("subscription_id") or "") != str(vault.get("subscription_id") or ""):
        return blocked("Cross-subscription protection is not supported here; choose a vault in the resource's subscription.")

    if spec["mechanism"] == "rsv_vm":
        if str(policy.get("backup_management_type") or "").lower() != "azureiaasvm":
            return blocked("Select an Azure VM backup policy.")
        target_id = rsv_protected_item_id(vault_id, gap.get("resource_group", ""), gap.get("resource_name", ""))
        return {
            **base,
            "status": "ready",
            "target_id": target_id,
            "api_version": service.RSV_BACKUP_API,
            "body": build_vm_protection_body(vm_id=resource_id, policy_id=policy_id),
            "summary": f"Protect {gap.get('resource_name')} in {vault.get('name')} using {policy.get('name')}",
        }

    child_suffix = spec.get("child_suffix") or ""
    datasource_id = f"{resource_id}{child_suffix}"
    instance_name = instance_name_for(gap.get("resource_name", ""), datasource_id)
    target_id = backup_instance_id(vault_id, instance_name)
    return {
        **base,
        "status": "ready",
        "target_id": target_id,
        "api_version": service.DP_API,
        "body": build_dataprotection_instance(
            resource_id=datasource_id,
            resource_name=str(gap.get("resource_name") or ""),
            location=str(gap.get("location") or vault.get("location") or ""),
            resource_type=str(spec.get("resource_type") or ""),
            datasource_type=str(spec.get("datasource_type") or ""),
            policy_id=policy_id,
            friendly_name=str(gap.get("resource_name") or instance_name),
        ),
        "summary": f"Protect {gap.get('resource_name')} in {vault.get('name')} using {policy.get('name')}",
        "requires_validation": True,
    }


async def validate_dataprotection_item(connection: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Run the Backup vault ``validateForBackup`` preflight so a missing role assignment is
    caught during review instead of after approval."""
    if item.get("status") != "ready" or item.get("mechanism") != "dataprotection":
        return item
    token = await service.token_for(connection)
    vault_id = item["vault_id"]
    body = {"backupInstance": (item.get("body") or {}).get("properties", {})}
    submission = await service.arm_submit(
        token, "POST", f"{vault_id}/validateForBackup", body=body, api_version=service.DP_API,
    )
    if submission.ok:
        return {**item, "validated": True}
    return {**item, "status": "blocked", "validated": False, "reason": submission.error or "Validation failed."}

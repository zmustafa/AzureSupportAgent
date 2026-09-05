"""Synthetic Backup Manager estate for review and demo without a live Azure tenant.

Built from the shared per-workload catalog (:mod:`app.demo_catalog`) and passed through the
*production* shaping and assembly code in :mod:`app.backup_manager.inventory`, so demo mode
exercises the same schema, scoring, and rollups a real tenant does — a demo that diverged
from production would hide exactly the bugs it should catch.

Tier drives the story: green vaults are hardened and current, amber vaults are locally
redundant with stale jobs, red vaults have soft delete off and failing backups.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.backup_manager import inventory, service
from app.demo_catalog import CONTOSO_ID, resources_for, workload_meta

DEMO_WORKLOAD_ID = CONTOSO_ID
DEMO_SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"

_ELIGIBLE = ("microsoft.compute/virtualmachines", "microsoft.compute/disks",
             "microsoft.storage/storageaccounts", "microsoft.dbforpostgresql/flexibleservers",
             "microsoft.containerservice/managedclusters")


def _iso(hours_ago: float) -> str:
    return (service.now() - timedelta(hours=hours_ago)).isoformat()


def _vault_id(subscription: str, resource_group: str, name: str, *, kind: str = "recovery_services") -> str:
    provider = "Microsoft.RecoveryServices/vaults" if kind == "recovery_services" else "Microsoft.DataProtection/backupVaults"
    return f"/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/{provider}/{name}"


def _vault_row(
    *, subscription: str, resource_group: str, name: str, location: str, kind: str, tier: str,
) -> dict[str, Any]:
    """A vault row in raw Resource Graph shape so ``shape_vault`` does the real work."""
    if kind == "recovery_services":
        security = {
            "green": {"softDeleteSettings": {"state": "AlwaysOn", "retentionDurationInDays": 30},
                      "immutabilitySettings": {"state": "Locked"}},
            "amber": {"softDeleteSettings": {"state": "Enabled", "retentionDurationInDays": 14},
                      "immutabilitySettings": {"state": "Unlocked"}},
            "red": {"softDeleteSettings": {"state": "Disabled"},
                    "immutabilitySettings": {"state": "Disabled"}},
        }[tier]
        redundancy = {
            "green": {"standardTierStorageRedundancy": "GeoRedundant", "crossRegionRestore": "Enabled"},
            "amber": {"standardTierStorageRedundancy": "LocallyRedundant", "crossRegionRestore": "Disabled"},
            "red": {"standardTierStorageRedundancy": "LocallyRedundant", "crossRegionRestore": "Disabled"},
        }[tier]
        monitoring = {"azureMonitorAlertSettings": {"alertsForAllJobFailures": "Enabled" if tier != "red" else "Disabled"}}
        storage: list[dict[str, Any]] = []
        features: dict[str, Any] = {}
    else:
        security = {
            "green": {"softDeleteSettings": {"state": "AlwaysOn", "retentionDurationInDays": 30},
                      "immutabilitySettings": {"state": "Unlocked"}},
            "amber": {"softDeleteSettings": {"state": "On", "retentionDurationInDays": 14},
                      "immutabilitySettings": {"state": "Disabled"}},
            "red": {"softDeleteSettings": {"state": "Off"}, "immutabilitySettings": {"state": "Disabled"}},
        }[tier]
        storage = [{"datastoreType": "VaultStore", "type": "GeoRedundant" if tier == "green" else "LocallyRedundant"}]
        redundancy = {}
        features = {"crossSubscriptionRestoreSettings": {"state": "Enabled" if tier == "green" else "Disabled"}}
        monitoring = {"azureMonitorAlertSettings": {"alertsForAllJobFailures": "Enabled" if tier == "green" else "Disabled"}}

    import json

    return {
        "id": _vault_id(subscription, resource_group, name, kind=kind),
        "name": name,
        "type": "microsoft.recoveryservices/vaults" if kind == "recovery_services" else "microsoft.dataprotection/backupvaults",
        "location": location,
        "resourceGroup": resource_group,
        "subscriptionId": subscription,
        "tags": json.dumps({"env": "demo"}),
        "skuName": "RS0" if kind == "recovery_services" else "Standard",
        "identityType": "SystemAssigned",
        "provisioningState": "Succeeded",
        "publicNetworkAccess": "Disabled" if tier == "green" else "Enabled",
        "securitySettings": json.dumps(security),
        "storageSettings": json.dumps(storage),
        "redundancySettings": json.dumps(redundancy),
        "featureSettings": json.dumps(features),
        "monitoringSettings": json.dumps(monitoring),
        "encryption": json.dumps({"keyVaultProperties": {"keyUri": "https://demo.vault.azure.net/keys/k"}} if tier == "green" else {}),
        "privateEndpointConnections": json.dumps([{"id": "pe-1"}] if tier == "green" else []),
        "privateEndpointStateForBackup": "Enabled" if tier == "green" else "None",
    }


def build_demo_estate(scope_id: str = CONTOSO_ID) -> dict[str, Any]:
    """A complete synthetic estate in the same shape ``collect_estate`` returns."""
    meta = workload_meta(scope_id)
    region = meta.get("primary_region") or "eastus"
    resources = resources_for(scope_id)
    subscription = str(resources[0]["subscriptionId"]) if resources else DEMO_SUBSCRIPTION

    vault_specs = [
        ("rsv-demo-prod", "recovery_services", "green", "rg-backup-prod"),
        ("rsv-demo-regional", "recovery_services", "amber", "rg-backup-regional"),
        ("bv-demo-data", "backup", "amber", "rg-backup-data"),
        ("rsv-demo-legacy", "recovery_services", "red", "rg-backup-legacy"),
    ]
    vault_rows = [
        _vault_row(subscription=subscription, resource_group=rg, name=name, location=region, kind=kind, tier=tier)
        for name, kind, tier, rg in vault_specs
    ]
    vaults = [inventory.shape_vault(row) for row in vault_rows]
    for vault, (_n, _k, tier, _rg) in zip(vaults, vault_specs):
        vault["mua_enabled"] = tier == "green"
        vault["mua_resource_guard_id"] = "/subscriptions/demo/resourceGuards/rg-guard" if tier == "green" else ""
        vault["diagnostics_enabled"] = tier != "red"
        vault["diagnostics_workspaces"] = ["/subscriptions/demo/workspaces/law-demo"] if tier != "red" else []

    prod_vault, regional_vault, data_vault, legacy_vault = vaults

    rsv_items: list[dict[str, Any]] = []
    dp_instances: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    protected_targets = [r for r in resources if r["type"] in _ELIGIBLE]

    for index, res in enumerate(protected_targets):
        tier = res["tier"]
        # Leave every third red-tier resource unprotected so the Gaps tab has real content.
        if tier == "red" and index % 3 == 0:
            continue
        rtype = res["type"]
        resource_group = str(res.get("resourceGroup") or "rg-demo")
        if rtype == "microsoft.compute/virtualmachines":
            vault = prod_vault if tier == "green" else (regional_vault if tier == "amber" else legacy_vault)
            rp_age = {"green": 6.0, "amber": 40.0, "red": 260.0}[tier]
            status = {"green": "Healthy", "amber": "Healthy", "red": "Unhealthy"}[tier]
            last_status = {"green": "Completed", "amber": "Completed", "red": "Failed"}[tier]
            item_id = (
                f"{vault['id']}/backupFabrics/Azure/protectionContainers/"
                f"IaasVMContainer;iaasvmcontainerv2;{resource_group};{res['name']}"
                f"/protectedItems/vm;iaasvmcontainerv2;{resource_group};{res['name']}"
            )
            rsv_items.append(inventory.shape_rsv_item({
                "id": item_id,
                "name": f"vm;iaasvmcontainerv2;{resource_group};{res['name']}",
                "resourceGroup": vault["resource_group"],
                "subscriptionId": subscription,
                "location": region,
                "friendlyName": res["name"],
                "datasourceId": res["id"].lower(),
                "backupManagementType": "AzureIaasVM",
                "workloadType": "VM",
                "protectedItemType": "Microsoft.Compute/virtualMachines",
                "protectionState": "Protected",
                "protectionStatus": status,
                "healthStatus": "Passed" if tier != "red" else "ActionRequired",
                "lastBackupStatus": last_status,
                "lastBackupTime": _iso(rp_age),
                "lastRecoveryPoint": _iso(rp_age) if tier != "red" else "",
                "policyId": f"{vault['id']}/backupPolicies/DefaultPolicy".lower(),
                "policyName": "DefaultPolicy",
                "isArchiveEnabled": "false",
                "lastBackupErrorCode": "UserErrorGuestAgentStatusUnavailable" if tier == "red" else "",
                "lastBackupErrorMessage": "The VM agent is unable to communicate." if tier == "red" else "",
            }))
        else:
            datasource_type = {
                "microsoft.storage/storageaccounts": "Microsoft.Storage/storageAccounts/blobServices",
                "microsoft.dbforpostgresql/flexibleservers": "Microsoft.DBforPostgreSQL/flexibleServers",
                "microsoft.containerservice/managedclusters": "Microsoft.ContainerService/managedClusters",
                "microsoft.compute/disks": "Microsoft.Compute/disks",
            }[rtype]
            suffix = "/blobServices/default" if rtype == "microsoft.storage/storageaccounts" else ""
            dp_instances.append(inventory.shape_dp_instance({
                "id": f"{data_vault['id']}/backupInstances/{res['name']}-instance",
                "name": f"{res['name']}-instance",
                "resourceGroup": data_vault["resource_group"],
                "subscriptionId": subscription,
                "location": region,
                "friendlyName": res["name"],
                "datasourceId": f"{res['id'].lower()}{suffix}",
                "datasourceType": datasource_type,
                "datasourceName": res["name"],
                "currentProtectionState": "ProtectionConfigured" if tier != "red" else "ProtectionError",
                "protectionStatus": "Healthy" if tier != "red" else "Unhealthy",
                "protectionErrorCode": "UserErrorMissingRoleAssignment" if tier == "red" else "",
                "protectionErrorMessage": "The vault identity is missing a required role." if tier == "red" else "",
                "policyId": f"{data_vault['id']}/backupPolicies/DailyBlobPolicy".lower(),
                "policyName": "DailyBlobPolicy",
                "provisioningState": "Succeeded",
            }))

    # Jobs: a healthy majority, one clustered failure across several items, and a chronically
    # failing red-tier item so the chronic-failure detector has something real to find.
    for index, item in enumerate([*rsv_items, *dp_instances]):
        chronic = bool(item.get("last_error_code"))
        kind = item["vault_kind"]
        for offset in range(3):
            failed = chronic
            start = _iso(6 + offset * 24)
            jobs.append(inventory.shape_job({
                "id": f"{item['vault_id']}/backupJobs/job-{index}-{offset}",
                "name": f"job-{index}-{offset}",
                "resourceGroup": item.get("resource_group", ""),
                "subscriptionId": subscription,
                "operation": "Backup",
                "status": "Failed" if failed else "Completed",
                "startTime": start,
                "endTime": start,
                "duration": "PT18M",
                "entityFriendlyName": item.get("friendly_name", ""),
                "backupManagementType": item.get("backup_management_type", ""),
                "datasourceId": item.get("datasource_id", ""),
                "errorCode": item.get("last_error_code", "") if failed else "",
                "errorMessage": item.get("last_error_message", "") if failed else "",
            }, kind=kind))

    policies = [
        inventory.shape_rsv_policy({
            "id": f"{vault['id']}/backupPolicies/DefaultPolicy",
            "name": "DefaultPolicy",
            "resourceGroup": vault["resource_group"],
            "subscriptionId": subscription,
            "location": region,
            "backupManagementType": "AzureIaasVM",
            "policyType": "V2",
            "protectedItemsCount": 0,
            "timeZone": "UTC",
            "instantRpRetentionRangeInDays": 2,
            "schedulePolicy": '{"scheduleRunFrequency":"Daily","scheduleRunTimes":["2026-01-01T02:00:00Z"]}',
            "retentionPolicy": '{"dailySchedule":{"retentionDuration":{"count":%d,"durationType":"Days"}}}'
                               % (90 if vault is prod_vault else 7),
            "workLoadType": "VM",
        })
        for vault in (prod_vault, regional_vault, legacy_vault)
    ]
    policies.append(inventory.shape_dp_policy({
        "id": f"{data_vault['id']}/backupPolicies/DailyBlobPolicy",
        "name": "DailyBlobPolicy",
        "resourceGroup": data_vault["resource_group"],
        "subscriptionId": subscription,
        "location": region,
        "datasourceTypes": '["Microsoft.Storage/storageAccounts/blobServices"]',
        "policyRules": '[{"lifecycles":[{"deleteAfter":{"objectType":"AbsoluteDeleteOption","duration":"P30D"}}],'
                       '"trigger":{"schedule":{"repeatingTimeIntervals":["R/2026-01-01T03:00:00+00:00/P1D"]}}}]',
        "objectType": "BackupPolicy",
    }))

    replication = [
        inventory.shape_replication({
            "id": f"{prod_vault['id']}/replicationFabrics/{region}/replicationProtectionContainers/c1/replicationProtectedItems/{res['name']}",
            "name": res["name"],
            "resourceGroup": prod_vault["resource_group"],
            "subscriptionId": subscription,
            "location": region,
            "friendlyName": res["name"],
            "protectedItemType": "AzureVm",
            "protectionState": "Protected",
            "protectionStateDescription": "Protected",
            "replicationHealth": "Normal" if res["tier"] == "green" else "Warning",
            "failoverHealth": "Normal",
            "testFailoverState": "None",
            "lastSuccessfulTestFailoverTime": _iso(24 * (45 if res["tier"] == "green" else 400)),
            "lastSuccessfulFailoverTime": "",
            "primaryFabricFriendlyName": region,
            "recoveryFabricFriendlyName": "westus",
            "policyFriendlyName": "24-hour-retention-policy",
            "activeLocation": "Primary",
            "rpoInSeconds": 300 if res["tier"] == "green" else 1800,
            "lastRpoCalculatedTime": _iso(0.2),
            "recoveryAzureVMName": f"{res['name']}-dr",
            "healthErrors": "[]" if res["tier"] == "green" else '[{"errorCode":"ReplicationHealthCritical","errorMessage":"Replication lag detected.","errorLevel":"Error"}]',
        })
        for res in resources if res["type"] == "microsoft.compute/virtualmachines"
    ][:4]

    recovery_plans = [inventory.shape_recovery_plan({
        "id": f"{prod_vault['id']}/replicationRecoveryPlans/rp-demo",
        "name": "rp-demo",
        "resourceGroup": prod_vault["resource_group"],
        "subscriptionId": subscription,
        "friendlyName": "Contoso failover plan",
        "primaryFabricFriendlyName": region,
        "recoveryFabricFriendlyName": "westus",
        "lastPlannedFailoverTime": "",
        "lastTestFailoverTime": _iso(24 * 210),
        "currentScenarioName": "",
        "currentScenarioStatus": "None",
        "replicationProviders": '["A2A"]',
        "groups": '[{"replicationProtectedItems":[{"id":"1"},{"id":"2"}]}]',
    })]

    live_ids = {r["id"].lower() for r in resources}
    # A dedicated orphan: a protected item whose source VM was deleted but whose backup data
    # (and bill) lives on. Appended rather than repointing a real item so the Gaps tab stays
    # consistent with the Inventory tab.
    orphan_vault = regional_vault
    orphan_container = "IaasVMContainer;iaasvmcontainerv2;rg-retired;retired-app-vm"
    rsv_items.append(inventory.shape_rsv_item({
        "id": f"{orphan_vault['id']}/backupFabrics/Azure/protectionContainers/{orphan_container}"
              f"/protectedItems/vm;iaasvmcontainerv2;rg-retired;retired-app-vm",
        "name": "vm;iaasvmcontainerv2;rg-retired;retired-app-vm",
        "resourceGroup": orphan_vault["resource_group"],
        "subscriptionId": subscription,
        "location": region,
        "friendlyName": "retired-app-vm",
        "datasourceId": f"/subscriptions/{subscription}/resourcegroups/rg-retired/providers/microsoft.compute/virtualmachines/retired-app-vm",
        "backupManagementType": "AzureIaasVM",
        "workloadType": "VM",
        "protectedItemType": "Microsoft.Compute/virtualMachines",
        "protectionState": "ProtectionStopped",
        "protectionStatus": "Healthy",
        "healthStatus": "Passed",
        "lastBackupStatus": "Completed",
        "lastBackupTime": _iso(24 * 95),
        "lastRecoveryPoint": _iso(24 * 95),
        "policyId": f"{orphan_vault['id']}/backupPolicies/DefaultPolicy".lower(),
        "policyName": "DefaultPolicy",
        "isArchiveEnabled": "false",
        "lastBackupErrorCode": "",
        "lastBackupErrorMessage": "",
    }))

    estate = inventory.build_estate(
        vaults=vaults,
        rsv_items=rsv_items,
        dp_instances=dp_instances,
        rsv_jobs=[j for j in jobs if j["vault_kind"] == "recovery_services"],
        dp_jobs=[j for j in jobs if j["vault_kind"] == "backup"],
        rsv_policies=[p for p in policies if p["vault_kind"] == "recovery_services"],
        dp_policies=[p for p in policies if p["vault_kind"] == "backup"],
        replication=replication,
        recovery_plans=recovery_plans,
        live_resource_ids=live_ids,
        errors={},
        scope={"workload_id": scope_id, "demo": True, "subscriptions": [subscription]},
    )
    estate["demo"] = True
    return estate


def demo_gaps(scope_id: str = CONTOSO_ID) -> dict[str, Any]:
    """Unprotected resources for the demo estate (the ones deliberately left out)."""
    from app.backup_manager import gaps as gap_ops

    estate = build_demo_estate(scope_id)
    protected = {i.get("datasource_id") for i in estate["instances"]}
    protected_prefixes = {p.rsplit("/blobservices/", 1)[0] for p in protected if "/blobservices/" in p}
    rows: list[dict[str, Any]] = []
    eligible = 0
    for res in resources_for(scope_id):
        spec = gap_ops.ELIGIBLE_TYPES.get(res["type"])
        if not spec:
            continue
        eligible += 1
        rid = res["id"].lower()
        if rid in protected or rid in protected_prefixes:
            continue
        rows.append({
            "gap_id": service.canonical_hash({"kind": "unprotected", "id": rid}),
            "source": "live",
            "resource_id": res["id"],
            "resource_name": res["name"],
            "resource_type": res["type"],
            "display_type": spec["display"],
            "resource_group": str(res.get("resourceGroup") or "rg-demo"),
            "subscription_id": str(res.get("subscriptionId") or ""),
            "location": res["location"],
            "mechanism": spec["mechanism"],
            "target_vault_kind": spec["vault_kind"],
            "severity": spec["severity"],
            "reason": "No backup instance protects this resource.",
        })
    return {
        "gaps": rows,
        "eligible_total": eligible,
        "protected_total": eligible - len(rows),
        "coverage_pct": round(100 * (eligible - len(rows)) / eligible) if eligible else 100,
        "error": "",
        "native_only": [{"type": t, "note": n} for t, n in sorted(gap_ops.NATIVE_ONLY_TYPES.items())],
        "demo": True,
    }

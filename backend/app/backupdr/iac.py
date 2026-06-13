"""Generate Bicep / runbook remediation artifacts for backup/DR coverage gaps.

Two formats, both read-only download artifacts (never applied by the app):
    bicep    — a Recovery Services Vault + backup policy + protected-item association per
               gap, with preserved parameters (vaultName, policyName, region).
    runbook  — a parameterized PowerShell/az runbook (e.g. enable VM backup, set up a
               PostgreSQL geo-replica) with variables for vault/policy/region/targetRegion.
"""
from __future__ import annotations

import re
from typing import Any


def _ident(s: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9]+", "_", s or "").strip("_")
    if not out:
        out = "res"
    if out[0].isdigit():
        out = f"r_{out}"
    return out


def _bicep_block(gap: dict[str, Any]) -> str:
    sym = _ident(f"{gap.get('resource_name','res')}")
    rid = gap.get("resource_id", "")
    rtype = gap.get("resource_type", "")
    return "\n".join(
        [
            f"// ---- {gap.get('resource_name','resource')} ({rtype}) ----",
            f"// Failed checks: {', '.join(gap.get('failed_checks', []))}",
            f"resource {sym}_vault 'Microsoft.RecoveryServices/vaults@2023-04-01' = {{",
            "  name: vaultName",
            "  location: region",
            "  sku: { name: 'RS0', tier: 'Standard' }",
            "  properties: {}",
            "}",
            f"resource {sym}_policy 'Microsoft.RecoveryServices/vaults/backupPolicies@2023-04-01' = {{",
            f"  parent: {sym}_vault",
            "  name: policyName",
            "  properties: {",
            "    backupManagementType: 'AzureIaasVM'",
            "    schedulePolicy: { schedulePolicyType: 'SimpleSchedulePolicy', scheduleRunFrequency: 'Daily' }",
            "    retentionPolicy: { retentionPolicyType: 'LongTermRetentionPolicy' }",
            "  }",
            "}",
            f"// TODO: associate protected item for {rid}",
            "",
        ]
    )


def _runbook_for(gap: dict[str, Any]) -> str:
    rtype = gap.get("resource_type", "")
    name = gap.get("resource_name", "<name>")
    rg = gap.get("resource_group", "<rg>")
    lines = [f"# ---- {name} ({rtype}) — failed: {', '.join(gap.get('failed_checks', []))} ----"]
    if rtype == "microsoft.compute/virtualmachines":
        lines += [
            "az backup protection enable-for-vm `",
            "  --resource-group $resourceGroup `",
            "  --vault-name $vaultName `",
            f"  --vm {name} `",
            "  --policy-name $policyName",
        ]
    elif rtype == "microsoft.dbforpostgresql/flexibleservers":
        lines += [
            "# Create a geo read-replica in the paired region",
            "az postgres flexible-server replica create `",
            f"  --replica-name {name}-replica `",
            f"  --source-server {name} `",
            "  --resource-group $resourceGroup `",
            "  --location $targetRegion",
        ]
    elif rtype in ("microsoft.sql/servers/databases", "microsoft.sql/managedinstances/databases"):
        lines += [
            "# Add the database to a failover group in the secondary region",
            "az sql failover-group create `",
            "  --name $failoverGroupName `",
            "  --partner-server $partnerServer `",
            "  --resource-group $resourceGroup `",
            f"  --add-db {name}",
        ]
    elif rtype == "microsoft.keyvault/vaults":
        lines += [
            "az keyvault update --name " + name + " --resource-group $resourceGroup `",
            "  --enable-soft-delete true --enable-purge-protection true",
        ]
    elif rtype == "microsoft.storage/storageaccounts":
        lines += [
            "# Enable Azure Files backup via the vault + RA-GRS for offsite redundancy",
            f"az storage account update --name {name} --resource-group $resourceGroup --sku Standard_RAGRS",
        ]
    else:
        lines += [f"# TODO: remediation steps for {rtype}"]
    lines.append("")
    return "\n".join(lines)


def generate_iac(gaps: list[dict[str, Any]], fmt: str) -> str:
    fmt = (fmt or "bicep").lower()

    if fmt == "runbook":
        header = [
            "# Backup/DR remediation runbook — review parameters, then run via your pipeline.",
            "# Read-only artifact; this app does not apply changes.",
            "param([string]$resourceGroup, [string]$vaultName='rsv-prod', [string]$policyName='DailyPolicy',",
            "      [string]$region='eastus', [string]$targetRegion='westus',",
            "      [string]$failoverGroupName='fg-prod', [string]$partnerServer='sql-secondary')",
            "",
        ]
        body = "\n".join(_runbook_for(g) for g in gaps)
        return "\n".join(header) + ("\n" + body if body else "")

    header = [
        "// Bicep generated from backup/DR coverage gaps — review parameters, then deploy.",
        "// Read-only artifact; this app does not apply changes.",
        "",
        "@description('Recovery Services Vault name.')",
        "param vaultName string = 'rsv-prod'",
        "@description('Backup policy name.')",
        "param policyName string = 'DailyPolicy'",
        "@description('Primary region for the vault.')",
        "param region string = resourceGroup().location",
        "",
    ]
    body = "\n".join(_bicep_block(g) for g in gaps)
    return "\n".join(header) + ("\n" + body if body else "")

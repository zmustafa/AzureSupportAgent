"""Curated starter workbooks seeded on first run.

These give the Workbooks feature immediate value: common Azure support/posture queries
using Azure Resource Graph (KQL) and the az CLI, each with AI'fication configured so the
output is summarized + severity-classified, and several wired as dashboard tiles."""
from __future__ import annotations

from typing import Any

STARTER_WORKBOOKS: list[dict[str, Any]] = [
    {
        "name": "Public storage accounts",
        "description": "Storage accounts whose firewall default action allows public network access.",
        "runtime": "kql",
        "body": (
            "Resources "
            "| where type =~ 'microsoft.storage/storageAccounts' "
            "| extend acl = tostring(properties.networkAcls.defaultAction) "
            "| where acl =~ 'Allow' "
            "| project name, resourceGroup, location, subscriptionId"
        ),
        "params": [],
        "kind": "read",
        "tags": ["security", "storage"],
        "aify": {
            "enabled": True,
            "modes": ["summary", "severity", "extract", "diff"],
            "schema": "count of public storage accounts and their names",
        },
        "alert": {"enabled": True, "min_severity": "warning"},
        "tile": {"enabled": True, "label": "Public storage accounts", "format": "severity", "metric_key": "count"},
    },
    {
        "name": "Storage connectivity triage",
        "description": "Network rules, allowed IPs and private endpoints for one storage account — why a client can or can't connect.",
        "runtime": "kql",
        "body": (
            "Resources "
            "| where type =~ 'microsoft.storage/storageAccounts' and name =~ '{{account}}' "
            "| extend defaultAction = tostring(properties.networkAcls.defaultAction), "
            "ipRules = properties.networkAcls.ipRules, "
            "vnetRules = properties.networkAcls.virtualNetworkRules, "
            "pe = properties.privateEndpointConnections "
            "| project name, resourceGroup, location, defaultAction, ipRules, vnetRules, pe"
        ),
        "params": [
            {"key": "account", "label": "Storage account name", "type": "text", "default": "", "required": True, "help": "e.g. drdtest14"}
        ],
        "kind": "read",
        "tags": ["support", "storage", "networking"],
        "aify": {
            "enabled": True,
            "modes": ["summary", "severity", "extract"],
            "schema": "defaultAction, list of allowed IPs, whether a private endpoint exists, and a plain-English reason a client might be blocked",
        },
        "alert": {"enabled": False, "min_severity": "warning"},
        "tile": {"enabled": False, "label": "", "format": "text", "metric_key": ""},
    },
    {
        "name": "Expiring certificates (Key Vault, 30 days)",
        "description": "Key Vault certificates expiring within 30 days.",
        "runtime": "az",
        "body": "az keyvault certificate list --vault-name {{vault}} --query \"[].{name:name, expires:attributes.expires}\" -o json",
        "params": [
            {"key": "vault", "label": "Key Vault name", "type": "text", "default": "", "required": True, "help": "Target Key Vault"}
        ],
        "kind": "read",
        "tags": ["security", "keyvault"],
        "aify": {
            "enabled": True,
            "modes": ["summary", "severity", "extract"],
            "schema": "list of certificates expiring within 30 days with days remaining; severity critical if any expire within 7 days",
        },
        "alert": {"enabled": True, "min_severity": "warning"},
        "tile": {"enabled": True, "label": "Certs expiring (30d)", "format": "severity", "metric_key": "expiring_count"},
    },
    {
        "name": "Orphaned managed disks",
        "description": "Managed disks that are unattached (no owning VM) — candidates for cleanup.",
        "runtime": "kql",
        "body": (
            "Resources "
            "| where type =~ 'microsoft.compute/disks' "
            "| where tostring(properties.diskState) =~ 'Unattached' "
            "| project name, resourceGroup, location, sizeGb = toint(properties.diskSizeGB), sku = sku.name"
        ),
        "params": [],
        "kind": "read",
        "tags": ["cost", "compute"],
        "aify": {
            "enabled": True,
            "modes": ["summary", "severity", "extract", "diff"],
            "schema": "count of orphaned disks and total GB",
        },
        "alert": {"enabled": False, "min_severity": "warning"},
        "tile": {"enabled": True, "label": "Orphaned disks", "format": "number", "metric_key": "count"},
    },
    {
        "name": "NSG rules allowing Any inbound",
        "description": "Network security group rules that allow inbound traffic from any source.",
        "runtime": "kql",
        "body": (
            "Resources "
            "| where type =~ 'microsoft.network/networkSecurityGroups' "
            "| mv-expand rule = properties.securityRules "
            "| extend direction = tostring(rule.properties.direction), "
            "access = tostring(rule.properties.access), "
            "src = tostring(rule.properties.sourceAddressPrefix) "
            "| where direction =~ 'Inbound' and access =~ 'Allow' and (src == '*' or src =~ 'Internet' or src == '0.0.0.0/0') "
            "| project nsg = name, resourceGroup, ruleName = tostring(rule.name), port = tostring(rule.properties.destinationPortRange)"
        ),
        "params": [],
        "kind": "read",
        "tags": ["security", "networking"],
        "aify": {
            "enabled": True,
            "modes": ["summary", "severity", "extract"],
            "schema": "count of risky open inbound rules and the most exposed ports",
        },
        "alert": {"enabled": True, "min_severity": "error"},
        "tile": {"enabled": True, "label": "Open inbound NSG rules", "format": "severity", "metric_key": "count"},
    },
    {
        "name": "VMs without backup protection",
        "description": "Virtual machines not protected by an Azure Backup recovery vault.",
        "runtime": "kql",
        "body": (
            "Resources "
            "| where type =~ 'microsoft.compute/virtualMachines' "
            "| project vm = name, resourceGroup, location, id "
            "| join kind=leftouter ( "
            "  RecoveryServicesResources "
            "  | where type =~ 'microsoft.recoveryservices/vaults/backupfabrics/protectioncontainers/protecteditems' "
            "  | project protectedId = tolower(tostring(properties.sourceResourceId)) "
            ") on $left.id == $right.protectedId "
            "| where isempty(protectedId) "
            "| project vm, resourceGroup, location"
        ),
        "params": [],
        "kind": "read",
        "tags": ["compliance", "backup"],
        "aify": {
            "enabled": True,
            "modes": ["summary", "severity", "extract"],
            "schema": "count of unprotected VMs",
        },
        "alert": {"enabled": False, "min_severity": "warning"},
        "tile": {"enabled": True, "label": "VMs without backup", "format": "number", "metric_key": "count"},
    },
    {
        "name": "Tag compliance (owner tag missing)",
        "description": "Resources missing an 'owner' tag — a quick governance compliance check.",
        "runtime": "kql",
        "body": (
            "Resources "
            "| extend hasOwner = isnotempty(tostring(tags['owner'])) "
            "| summarize total = count(), missing = countif(hasOwner == false) "
            "| extend compliancePct = toint(100.0 * (total - missing) / total)"
        ),
        "params": [],
        "kind": "read",
        "tags": ["governance", "tags"],
        "aify": {
            "enabled": True,
            "modes": ["summary", "severity", "extract", "diff"],
            "schema": "total resources, missing owner tag count, compliance percentage",
        },
        "alert": {"enabled": False, "min_severity": "warning"},
        "tile": {"enabled": True, "label": "Owner-tag compliance %", "format": "number", "metric_key": "compliancePct"},
    },
    {
        "name": "Resource health snapshot",
        "description": "Resources currently reporting an unavailable/degraded health status.",
        "runtime": "kql",
        "body": (
            "HealthResources "
            "| where type =~ 'microsoft.resourcehealth/availabilitystatuses' "
            "| extend status = tostring(properties.availabilityState) "
            "| where status != 'Available' "
            "| project resource = tostring(properties.targetResourceType), status, location"
        ),
        "params": [],
        "kind": "read",
        "tags": ["support", "health"],
        "aify": {
            "enabled": True,
            "modes": ["summary", "severity", "extract"],
            "schema": "count of unhealthy resources and their states",
        },
        "alert": {"enabled": True, "min_severity": "error"},
        "tile": {"enabled": True, "label": "Unhealthy resources", "format": "severity", "metric_key": "count"},
    },
]

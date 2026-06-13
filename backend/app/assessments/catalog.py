"""Curated assessment check catalog (Azure Well-Architected aligned).

Each check is a deterministic Azure Resource Graph (KQL) control that flags VIOLATING
resources, plus metadata used for scoring, compliance mapping, and remediation. The
runner scopes every check's query to the assessed workload and classifies pass/fail
from whether any resources are flagged. An optional AI layer adds an executive summary
and per-finding rationale (hybrid engine).

Design notes:
- ``kql`` is the pipeline fragment AFTER ``Resources | where <scope>`` — it should both
  filter to the relevant resource type(s) AND keep only resources that VIOLATE the
  control, projecting id/name/type/resourceGroup/subscriptionId.
- ``resource_types`` drives applicability: a check is N/A for a workload that contains
  none of those ARM types (so a storage control doesn't penalize a network-only app).
- ``severity`` sets the default ``weight`` for 0-100 pillar scoring.
- ``frameworks`` maps the control to CIS Azure Foundations + NIST 800-53 Rev.5 IDs.
"""
from __future__ import annotations

from typing import Any

PILLARS = ("security", "reliability", "cost", "operations", "performance")

PILLAR_META: dict[str, dict[str, str]] = {
    "security": {"label": "Security", "icon": "🛡️"},
    "reliability": {"label": "Reliability", "icon": "🔄"},
    "cost": {"label": "Cost Optimization", "icon": "💰"},
    "operations": {"label": "Operational Excellence", "icon": "⚙️"},
    "performance": {"label": "Performance Efficiency", "icon": "⚡"},
}

# Default scoring weight per severity (higher = more impact on the pillar score).
SEVERITY_WEIGHT = {"critical": 10, "error": 6, "warning": 3, "info": 1}

# Standard projection appended to most queries for consistent flagged-resource output.
_PROJECT = "| project id, name, type, resourceGroup, subscriptionId"

# Common billable/primary resource types used by governance (tagging) controls that
# should apply whenever a workload contains any meaningful managed resource.
_COMMON_TYPES = [
    "microsoft.compute/virtualmachines",
    "microsoft.compute/disks",
    "microsoft.storage/storageaccounts",
    "microsoft.sql/servers",
    "microsoft.web/sites",
    "microsoft.web/serverfarms",
    "microsoft.containerservice/managedclusters",
    "microsoft.documentdb/databaseaccounts",
    "microsoft.keyvault/vaults",
    "microsoft.network/loadbalancers",
    "microsoft.network/applicationgateways",
    "microsoft.network/publicipaddresses",
]
_COMMON_TYPES_KQL = ", ".join(f"'{t}'" for t in _COMMON_TYPES)



def _check(
    cid: str,
    pillar: str,
    title: str,
    description: str,
    severity: str,
    resource_types: list[str],
    kql: str,
    remediation: str,
    *,
    frameworks: dict[str, list[str]] | None = None,
    remediation_command: str = "",
    weight: int | None = None,
) -> dict[str, Any]:
    return {
        "id": cid,
        "pillar": pillar,
        "title": title,
        "description": description,
        "severity": severity,
        "weight": weight if weight is not None else SEVERITY_WEIGHT.get(severity, 3),
        "resource_types": [t.lower() for t in resource_types],
        "kql": kql.strip(),
        "remediation": remediation,
        "remediation_command": remediation_command,
        "frameworks": frameworks or {},
    }


# ===================== SECURITY =====================
_SECURITY: list[dict[str, Any]] = [
    _check(
        "sec_storage_public_blob",
        "security",
        "Storage accounts allow public blob access",
        "Storage accounts with allowBlobPublicAccess enabled can expose blob containers "
        "anonymously to the internet.",
        "error",
        ["microsoft.storage/storageaccounts"],
        f"| where type =~ 'microsoft.storage/storageaccounts' "
        f"| where tobool(properties.allowBlobPublicAccess) == true {_PROJECT}",
        "Disable public blob access on the storage account unless anonymous access is "
        "explicitly required.",
        frameworks={"cis": ["CIS Azure 3.7"], "nist": ["AC-3", "SC-7"]},
        remediation_command="az storage account update --name <name> --resource-group <rg> --allow-blob-public-access false",
    ),
    _check(
        "sec_storage_https_only",
        "security",
        "Storage accounts allow insecure HTTP transfer",
        "Storage accounts without 'secure transfer required' accept unencrypted HTTP "
        "requests, exposing data in transit.",
        "error",
        ["microsoft.storage/storageaccounts"],
        f"| where type =~ 'microsoft.storage/storageaccounts' "
        f"| where tobool(properties.supportsHttpsTrafficOnly) == false {_PROJECT}",
        "Enable 'Secure transfer required' so only HTTPS is accepted.",
        frameworks={"cis": ["CIS Azure 3.1"], "nist": ["SC-8"]},
        remediation_command="az storage account update --name <name> --resource-group <rg> --https-only true",
    ),
    _check(
        "sec_storage_shared_key",
        "security",
        "Storage accounts permit shared key access",
        "Allowing shared-key (account key) access weakens identity-based controls; prefer "
        "Azure AD authorization.",
        "warning",
        ["microsoft.storage/storageaccounts"],
        f"| where type =~ 'microsoft.storage/storageaccounts' "
        f"| where isnull(properties.allowSharedKeyAccess) or tobool(properties.allowSharedKeyAccess) == true {_PROJECT}",
        "Disable shared key access and use Azure AD (Entra) authorization for data plane access.",
        frameworks={"cis": ["CIS Azure 3.x"], "nist": ["AC-2", "IA-2"]},
        remediation_command="az storage account update --name <name> --resource-group <rg> --allow-shared-key-access false",
    ),
    _check(
        "sec_nsg_mgmt_open",
        "security",
        "NSGs allow inbound management ports from the internet",
        "Network security groups permitting inbound SSH (22) or RDP (3389) from any source "
        "(0.0.0.0/0 / Internet) expose management surfaces to brute force.",
        "critical",
        ["microsoft.network/networksecuritygroups"],
        "| where type =~ 'microsoft.network/networksecuritygroups' "
        "| mv-expand rule = properties.securityRules "
        "| extend dir = tostring(rule.properties.direction), acc = tostring(rule.properties.access), "
        "src = tostring(rule.properties.sourceAddressPrefix), "
        "ports = strcat(tostring(rule.properties.destinationPortRange), ' ', tostring(rule.properties.destinationPortRanges)) "
        "| where dir =~ 'Inbound' and acc =~ 'Allow' and (src == '*' or src == '0.0.0.0/0' or src =~ 'Internet') "
        "and (ports has '22' or ports has '3389' or ports has '*') "
        "| summarize by id, name, type, resourceGroup, subscriptionId",
        "Restrict inbound management rules to specific source IPs, use Azure Bastion / "
        "just-in-time VM access, and remove 0.0.0.0/0 allow rules.",
        frameworks={"cis": ["CIS Azure 6.1", "CIS Azure 6.2"], "nist": ["SC-7", "AC-17"]},
    ),
    _check(
        "sec_public_ip",
        "security",
        "Resources have public IP addresses",
        "Public IP addresses increase the externally reachable attack surface; confirm each "
        "is required and protected.",
        "warning",
        ["microsoft.network/publicipaddresses"],
        f"| where type =~ 'microsoft.network/publicipaddresses' "
        f"| where isnotempty(properties.ipAddress) {_PROJECT}",
        "Remove unused public IPs; front required ones with a firewall/WAF and restrict via NSGs.",
        frameworks={"nist": ["SC-7"]},
    ),
    _check(
        "sec_kv_purge_protection",
        "security",
        "Key Vaults without purge protection",
        "Without purge protection a deleted vault (and its keys/secrets) can be permanently "
        "removed before the retention period, risking data loss or tampering.",
        "error",
        ["microsoft.keyvault/vaults"],
        f"| where type =~ 'microsoft.keyvault/vaults' "
        f"| where isnull(properties.enablePurgeProtection) or tobool(properties.enablePurgeProtection) == false {_PROJECT}",
        "Enable purge protection (and soft-delete) on every Key Vault.",
        frameworks={"cis": ["CIS Azure 8.4"], "nist": ["SC-12", "CP-9"]},
        remediation_command="az keyvault update --name <name> --resource-group <rg> --enable-purge-protection true",
    ),
    _check(
        "sec_kv_public_network",
        "security",
        "Key Vaults allow public network access",
        "Key Vaults reachable from all networks are exposed beyond your private network "
        "boundary.",
        "warning",
        ["microsoft.keyvault/vaults"],
        f"| where type =~ 'microsoft.keyvault/vaults' "
        f"| where tostring(properties.publicNetworkAccess) =~ 'Enabled' or isnull(properties.publicNetworkAccess) {_PROJECT}",
        "Set public network access to Disabled and use private endpoints / trusted services.",
        frameworks={"nist": ["SC-7"]},
    ),
    _check(
        "sec_sql_public_access",
        "security",
        "SQL servers allow public network access",
        "Azure SQL logical servers with public network access enabled are reachable over the "
        "internet (subject to firewall rules).",
        "error",
        ["microsoft.sql/servers"],
        f"| where type =~ 'microsoft.sql/servers' "
        f"| where tostring(properties.publicNetworkAccess) =~ 'Enabled' {_PROJECT}",
        "Disable public network access and use private endpoints; if public access is needed, "
        "scope firewall rules tightly.",
        frameworks={"cis": ["CIS Azure 4.x"], "nist": ["SC-7"]},
        remediation_command="az sql server update --name <name> --resource-group <rg> --enable-public-network false",
    ),
    _check(
        "sec_webapp_https_only",
        "security",
        "App Services not enforcing HTTPS-only",
        "Web apps / function apps without HTTPS-only allow plaintext HTTP, exposing traffic and "
        "session tokens.",
        "error",
        ["microsoft.web/sites"],
        f"| where type =~ 'microsoft.web/sites' "
        f"| where tobool(properties.httpsOnly) == false or isnull(properties.httpsOnly) {_PROJECT}",
        "Enable HTTPS Only on each App Service and set the minimum TLS version to 1.2+.",
        frameworks={"cis": ["CIS Azure 9.2"], "nist": ["SC-8", "SC-23"]},
        remediation_command="az webapp update --name <name> --resource-group <rg> --set httpsOnly=true",
    ),
    _check(
        "sec_disk_unencrypted",
        "security",
        "Managed disks without customer-managed key encryption",
        "Disks using only platform-managed keys (no CMK / double encryption) may not meet "
        "stricter data-at-rest requirements.",
        "warning",
        ["microsoft.compute/disks"],
        "| where type =~ 'microsoft.compute/disks' "
        "| where isnull(properties.encryption.type) or tostring(properties.encryption.type) =~ 'EncryptionAtRestWithPlatformKey' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Apply a disk encryption set with a customer-managed key where regulatory requirements "
        "demand CMK.",
        frameworks={"cis": ["CIS Azure 7.x"], "nist": ["SC-28"]},
    ),
    _check(
        "sec_aks_public_api",
        "security",
        "AKS clusters expose a public API server",
        "Managed Kubernetes clusters with a public API server endpoint widen the control-plane "
        "attack surface.",
        "warning",
        ["microsoft.containerservice/managedclusters"],
        "| where type =~ 'microsoft.containerservice/managedclusters' "
        "| where isnull(properties.apiServerAccessProfile.enablePrivateCluster) "
        "or tobool(properties.apiServerAccessProfile.enablePrivateCluster) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable private cluster mode or restrict authorized IP ranges on the API server.",
        frameworks={"nist": ["SC-7", "AC-3"]},
    ),
    _check(
        "sec_cosmos_public",
        "security",
        "Cosmos DB accounts allow public network access",
        "Cosmos DB accounts reachable from all networks are exposed beyond the private boundary.",
        "warning",
        ["microsoft.documentdb/databaseaccounts"],
        f"| where type =~ 'microsoft.documentdb/databaseaccounts' "
        f"| where tostring(properties.publicNetworkAccess) =~ 'Enabled' or isnull(properties.publicNetworkAccess) {_PROJECT}",
        "Set public network access to Disabled and use private endpoints / IP firewall rules.",
        frameworks={"nist": ["SC-7"]},
    ),
]


# ===================== RELIABILITY =====================
_RELIABILITY: list[dict[str, Any]] = [
    _check(
        "rel_vm_no_zone",
        "reliability",
        "Virtual machines not deployed across availability zones",
        "VMs without an availability zone share a single datacenter fault/maintenance domain "
        "and won't survive a zone outage.",
        "error",
        ["microsoft.compute/virtualmachines"],
        "| where type =~ 'microsoft.compute/virtualmachines' "
        "| where isnull(zones) or array_length(zones) == 0 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Deploy VMs across availability zones (or at minimum an availability set) for "
        "zone-redundant or rack-level resilience.",
        frameworks={"nist": ["CP-2", "CP-10"]},
    ),
    _check(
        "rel_storage_lrs",
        "reliability",
        "Storage accounts using locally-redundant storage (LRS)",
        "LRS keeps all replicas in one datacenter; a zone or regional outage can cause data "
        "unavailability or loss.",
        "warning",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| extend skuName = tostring(sku.name) "
        "| where skuName endswith 'LRS' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Use zone-redundant (ZRS) or geo-redundant (GRS/GZRS) replication for production data.",
        frameworks={"nist": ["CP-9", "CP-6"]},
        remediation_command="az storage account update --name <name> --resource-group <rg> --sku Standard_ZRS",
    ),
    _check(
        "rel_pip_basic_sku",
        "reliability",
        "Public IPs using the Basic SKU",
        "Basic-SKU public IPs are not zone-redundant and are being retired; they lack the "
        "resilience of Standard SKU.",
        "warning",
        ["microsoft.network/publicipaddresses"],
        "| where type =~ 'microsoft.network/publicipaddresses' "
        "| where tostring(sku.name) =~ 'Basic' or isnull(sku.name) "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Migrate to Standard SKU public IPs (zone-redundant) before Basic SKU retirement.",
        frameworks={"nist": ["CP-2"]},
    ),
    _check(
        "rel_lb_basic_sku",
        "reliability",
        "Load balancers using the Basic SKU",
        "Basic load balancers have no SLA and aren't zone-redundant; Standard SKU is required "
        "for resilient production workloads.",
        "warning",
        ["microsoft.network/loadbalancers"],
        "| where type =~ 'microsoft.network/loadbalancers' "
        "| where tostring(sku.name) =~ 'Basic' or isnull(sku.name) "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Recreate load balancers with the Standard SKU for an SLA and zone redundancy.",
        frameworks={"nist": ["CP-2"]},
    ),
    _check(
        "rel_sql_no_zone",
        "reliability",
        "SQL databases without zone redundancy",
        "Azure SQL databases without zone-redundancy don't survive a single-zone failure within "
        "the region.",
        "warning",
        ["microsoft.sql/servers/databases"],
        "| where type =~ 'microsoft.sql/servers/databases' "
        "| where name !~ 'master' "
        "| where isnull(properties.zoneRedundant) or tobool(properties.zoneRedundant) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable zone redundancy on premium/business-critical and general-purpose (where "
        "supported) SQL databases.",
        frameworks={"nist": ["CP-2", "CP-10"]},
    ),
    _check(
        "rel_appplan_single_instance",
        "reliability",
        "App Service plans running a single instance",
        "App Service plans with capacity < 2 have no in-region redundancy; a single instance "
        "is a single point of failure.",
        "warning",
        ["microsoft.web/serverfarms"],
        "| where type =~ 'microsoft.web/serverfarms' "
        "| where toint(sku.capacity) < 2 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Scale out to 2+ instances (and enable zone redundancy on supported tiers).",
        frameworks={"nist": ["CP-2"]},
        remediation_command="az appservice plan update --name <name> --resource-group <rg> --number-of-workers 2",
    ),
    _check(
        "rel_disk_lrs",
        "reliability",
        "Managed disks using locally-redundant storage",
        "LRS managed disks keep all replicas in one datacenter; consider ZRS disks for "
        "zone-resilient workloads.",
        "info",
        ["microsoft.compute/disks"],
        "| where type =~ 'microsoft.compute/disks' "
        "| where tostring(sku.name) endswith 'LRS' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Use zone-redundant (ZRS) managed disks for workloads that require zone resilience.",
        frameworks={"nist": ["CP-9"]},
    ),
    _check(
        "rel_cosmos_single_region",
        "reliability",
        "Cosmos DB accounts in a single region",
        "Single-region Cosmos DB accounts have no geo-failover; a regional outage causes "
        "unavailability.",
        "warning",
        ["microsoft.documentdb/databaseaccounts"],
        "| where type =~ 'microsoft.documentdb/databaseaccounts' "
        "| where array_length(properties.locations) <= 1 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Add a secondary region and (where appropriate) enable automatic failover / "
        "multi-region writes.",
        frameworks={"nist": ["CP-2", "CP-6"]},
    ),
    _check(
        "rel_aks_single_nodepool",
        "reliability",
        "AKS clusters with a single node / no zones",
        "AKS clusters whose system node pool has a single node or no availability zones can't "
        "tolerate a node or zone failure.",
        "warning",
        ["microsoft.containerservice/managedclusters"],
        "| where type =~ 'microsoft.containerservice/managedclusters' "
        "| mv-expand pool = properties.agentPoolProfiles "
        "| extend cnt = toint(pool.count), zones = pool.availabilityZones "
        "| where cnt < 2 or isnull(zones) or array_length(zones) == 0 "
        "| summarize by id, name, type, resourceGroup, subscriptionId",
        "Run at least 2-3 nodes spread across availability zones for the system node pool.",
        frameworks={"nist": ["CP-2", "CP-10"]},
    ),
    _check(
        "rel_appgw_no_zone",
        "reliability",
        "Application Gateways without zone redundancy",
        "Application Gateways not spread across zones won't survive a single-zone outage.",
        "warning",
        ["microsoft.network/applicationgateways"],
        "| where type =~ 'microsoft.network/applicationgateways' "
        "| where isnull(zones) or array_length(zones) == 0 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Deploy zone-redundant Application Gateway v2 across multiple availability zones.",
        frameworks={"nist": ["CP-2"]},
    ),
]


# ===================== COST OPTIMIZATION =====================
_COST: list[dict[str, Any]] = [
    _check(
        "cost_disk_unattached",
        "cost",
        "Unattached managed disks",
        "Managed disks in the 'Unattached' state are not connected to any VM yet still incur "
        "full storage charges every month.",
        "warning",
        ["microsoft.compute/disks"],
        f"| where type =~ 'microsoft.compute/disks' "
        f"| where tostring(properties.diskState) =~ 'Unattached' {_PROJECT}",
        "Delete or snapshot-and-delete disks that are no longer attached to a VM.",
        remediation_command="az disk delete --name <name> --resource-group <rg> --yes",
    ),
    _check(
        "cost_pip_unassociated",
        "cost",
        "Unassociated public IP addresses",
        "Standard-SKU public IPs that aren't associated with any NIC, load balancer, gateway "
        "or NAT gateway are billed hourly while doing nothing.",
        "warning",
        ["microsoft.network/publicipaddresses"],
        f"| where type =~ 'microsoft.network/publicipaddresses' "
        f"| where isnull(properties.ipConfiguration) and isnull(properties.natGateway) {_PROJECT}",
        "Delete public IPs that are no longer associated with a resource.",
        remediation_command="az network public-ip delete --name <name> --resource-group <rg>",
    ),
    _check(
        "cost_nic_unattached",
        "cost",
        "Network interfaces not attached to a VM",
        "Orphaned network interfaces (not bound to a VM or private endpoint) clutter the "
        "environment and can hold otherwise-reclaimable IPs.",
        "info",
        ["microsoft.network/networkinterfaces"],
        f"| where type =~ 'microsoft.network/networkinterfaces' "
        f"| where isnull(properties.virtualMachine) and isnull(properties.privateEndpoint) {_PROJECT}",
        "Delete network interfaces that are no longer attached to any resource.",
        remediation_command="az network nic delete --name <name> --resource-group <rg>",
    ),
    _check(
        "cost_snapshot_aged",
        "cost",
        "Disk snapshots older than 90 days",
        "Long-lived snapshots accumulate storage cost; many are stale backups that are no "
        "longer needed.",
        "info",
        ["microsoft.compute/snapshots"],
        "| where type =~ 'microsoft.compute/snapshots' "
        "| where todatetime(properties.timeCreated) < ago(90d) "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Review and delete stale snapshots, or move them to a lifecycle/retention policy.",
        remediation_command="az snapshot delete --name <name> --resource-group <rg>",
    ),
    _check(
        "cost_empty_appserviceplan",
        "cost",
        "App Service plans with no apps",
        "An App Service plan with zero hosted apps still bills for its reserved compute "
        "capacity.",
        "warning",
        ["microsoft.web/serverfarms"],
        "| where type =~ 'microsoft.web/serverfarms' "
        "| where toint(properties.numberOfSites) == 0 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Delete empty App Service plans or consolidate apps onto fewer plans.",
        remediation_command="az appservice plan delete --name <name> --resource-group <rg> --yes",
    ),
    _check(
        "cost_appgw_no_autoscale",
        "cost",
        "Application Gateway v2 without autoscaling",
        "v2 Application Gateways with a fixed instance count can't scale down during low "
        "traffic, paying for idle capacity.",
        "info",
        ["microsoft.network/applicationgateways"],
        "| where type =~ 'microsoft.network/applicationgateways' "
        "| where tostring(properties.sku.tier) has 'v2' and isnull(properties.autoscaleConfiguration) "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable autoscaling (min/max capacity) on v2 Application Gateways to match demand.",
    ),
]


# ===================== OPERATIONAL EXCELLENCE =====================
_OPERATIONS: list[dict[str, Any]] = [
    _check(
        "ops_missing_owner_tag",
        "operations",
        "Resources missing an 'owner' tag",
        "Resources without an owner tag are hard to attribute for support, on-call, and "
        "lifecycle decisions.",
        "warning",
        _COMMON_TYPES,
        f"| where type in~ ({_COMMON_TYPES_KQL}) "
        f"| where isempty(tostring(tags['owner'])) and isempty(tostring(tags['Owner'])) {_PROJECT}",
        "Apply a consistent 'owner' tag (and enforce it with Azure Policy).",
        frameworks={"nist": ["CM-8"]},
    ),
    _check(
        "ops_missing_env_tag",
        "operations",
        "Resources missing an 'environment' tag",
        "Without an environment tag (prod/test/dev) it's hard to apply the right guardrails, "
        "alerting, and change controls.",
        "info",
        _COMMON_TYPES,
        f"| where type in~ ({_COMMON_TYPES_KQL}) "
        f"| where isempty(tostring(tags['environment'])) and isempty(tostring(tags['Environment'])) "
        f"and isempty(tostring(tags['env'])) {_PROJECT}",
        "Apply a consistent 'environment' tag across all resources.",
        frameworks={"nist": ["CM-8"]},
    ),
    _check(
        "ops_vm_no_boot_diagnostics",
        "operations",
        "Virtual machines without boot diagnostics",
        "Boot diagnostics capture serial console output and screenshots needed to triage "
        "boot/OS failures; without it, diagnosing a non-booting VM is far harder.",
        "warning",
        ["microsoft.compute/virtualmachines"],
        "| where type =~ 'microsoft.compute/virtualmachines' "
        "| where isnull(properties.diagnosticsProfile.bootDiagnostics.enabled) "
        "or tobool(properties.diagnosticsProfile.bootDiagnostics.enabled) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable boot diagnostics (managed storage) on all VMs.",
        frameworks={"nist": ["AU-12", "SI-4"]},
        remediation_command="az vm boot-diagnostics enable --name <name> --resource-group <rg>",
    ),
    _check(
        "ops_vm_unmanaged_disks",
        "operations",
        "Virtual machines using unmanaged disks",
        "VMs with unmanaged (storage-account VHD) OS disks miss managed-disk reliability, "
        "encryption defaults, and simplified operations.",
        "warning",
        ["microsoft.compute/virtualmachines"],
        "| where type =~ 'microsoft.compute/virtualmachines' "
        "| where isnotempty(tostring(properties.storageProfile.osDisk.vhd.uri)) "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Migrate VMs from unmanaged VHDs to managed disks.",
        frameworks={"nist": ["CM-2"]},
    ),
    _check(
        "ops_aks_no_monitoring",
        "operations",
        "AKS clusters without Container Insights monitoring",
        "Without the monitoring (omsagent / Container Insights) add-on, clusters lack the "
        "metrics and logs needed for operational visibility and troubleshooting.",
        "warning",
        ["microsoft.containerservice/managedclusters"],
        "| where type =~ 'microsoft.containerservice/managedclusters' "
        "| where isnull(properties.addonProfiles.omsagent) "
        "or tobool(properties.addonProfiles.omsagent.enabled) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable the Container Insights (monitoring) add-on on every AKS cluster.",
        frameworks={"nist": ["AU-6", "SI-4"]},
        remediation_command="az aks enable-addons --addons monitoring --name <name> --resource-group <rg>",
    ),
    _check(
        "ops_aks_no_autoupgrade",
        "operations",
        "AKS clusters without an auto-upgrade channel",
        "Clusters with no auto-upgrade channel drift out of support and require manual, "
        "error-prone version upgrades.",
        "info",
        ["microsoft.containerservice/managedclusters"],
        "| where type =~ 'microsoft.containerservice/managedclusters' "
        "| where isnull(properties.autoUpgradeProfile.upgradeChannel) "
        "or tostring(properties.autoUpgradeProfile.upgradeChannel) in~ ('none', '') "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Set an auto-upgrade channel (e.g. 'stable' or 'patch') on AKS clusters.",
        frameworks={"nist": ["CM-3"]},
    ),
]


# ===================== PERFORMANCE EFFICIENCY =====================
_PERFORMANCE: list[dict[str, Any]] = [
    _check(
        "perf_disk_standard_hdd",
        "performance",
        "Managed disks on Standard HDD",
        "Standard HDD (Standard_LRS) disks have the lowest IOPS/throughput; latency-sensitive "
        "workloads benefit from Standard SSD or Premium SSD.",
        "info",
        ["microsoft.compute/disks"],
        "| where type =~ 'microsoft.compute/disks' "
        "| where tostring(sku.name) startswith 'Standard_LRS' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Upgrade performance-sensitive disks to Standard SSD or Premium SSD.",
    ),
    _check(
        "perf_appplan_constrained_tier",
        "performance",
        "App Service plans on Free/Shared/Basic tiers",
        "Free, Shared and Basic App Service tiers have no autoscale and limited compute, "
        "constraining throughput under load.",
        "info",
        ["microsoft.web/serverfarms"],
        "| where type =~ 'microsoft.web/serverfarms' "
        "| where tostring(sku.tier) in~ ('Free', 'Shared', 'Basic') "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Move production workloads to Standard/Premium tiers that support scale-out and "
        "better performance.",
    ),
    _check(
        "perf_sql_basic_tier",
        "performance",
        "SQL databases on the Basic tier",
        "Basic-tier SQL databases have very limited DTUs/storage and are unsuitable for "
        "anything beyond light dev/test workloads.",
        "info",
        ["microsoft.sql/servers/databases"],
        "| where type =~ 'microsoft.sql/servers/databases' "
        "| where name !~ 'master' "
        "| where tostring(sku.tier) =~ 'Basic' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Scale databases to Standard/Premium or vCore tiers sized to the workload.",
    ),
    _check(
        "perf_aks_no_autoscaler",
        "performance",
        "AKS node pools without the cluster autoscaler",
        "Node pools without autoscaling can't add capacity under load (risking throttling) "
        "or remove it when idle.",
        "info",
        ["microsoft.containerservice/managedclusters"],
        "| where type =~ 'microsoft.containerservice/managedclusters' "
        "| mv-expand pool = properties.agentPoolProfiles "
        "| extend autoscale = tobool(pool.enableAutoScaling) "
        "| where isnull(autoscale) or autoscale == false "
        "| summarize by id, name, type, resourceGroup, subscriptionId",
        "Enable the cluster autoscaler with sensible min/max node counts per node pool.",
    ),
    _check(
        "perf_storage_v1",
        "performance",
        "Storage accounts using the legacy v1 kind",
        "General-purpose v1 storage accounts can't use premium performance tiers or newer "
        "features and have a less efficient pricing/perf profile than v2.",
        "info",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tostring(kind) in~ ('Storage', 'StorageV1') "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Upgrade general-purpose v1 storage accounts to general-purpose v2.",
        remediation_command="az storage account update --name <name> --resource-group <rg> --set kind=StorageV2",
    ),
    _check(
        "perf_appgw_fixed_low_capacity",
        "performance",
        "Application Gateways with fixed low capacity",
        "Application Gateways without autoscale and a fixed capacity under 2 instances can "
        "bottleneck throughput and have no headroom for spikes.",
        "info",
        ["microsoft.network/applicationgateways"],
        "| where type =~ 'microsoft.network/applicationgateways' "
        "| where isnull(properties.autoscaleConfiguration) and toint(properties.sku.capacity) < 2 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable autoscaling or raise the fixed instance count to handle peak load.",
    ),
]


ALL_CHECKS: list[dict[str, Any]] = (
    _SECURITY + _RELIABILITY + _COST + _OPERATIONS + _PERFORMANCE
)

# ISO/IEC 27001:2022 Annex A control mappings, keyed by check id. Added centrally so the
# same run yields a WAF score AND ISO compliance coverage.
_ISO_MAP: dict[str, list[str]] = {
    "sec_storage_public_blob": ["A.5.10", "A.8.3"],
    "sec_storage_https_only": ["A.8.24", "A.5.14"],
    "sec_storage_shared_key": ["A.5.15", "A.8.2"],
    "sec_nsg_mgmt_open": ["A.8.20", "A.8.22"],
    "sec_public_ip": ["A.8.20", "A.8.22"],
    "sec_kv_purge_protection": ["A.8.24", "A.8.13"],
    "sec_kv_public_network": ["A.8.20", "A.8.24"],
    "sec_sql_public_access": ["A.8.20", "A.8.22"],
    "sec_webapp_https_only": ["A.8.24"],
    "sec_disk_unencrypted": ["A.8.24"],
    "sec_aks_public_api": ["A.8.20", "A.8.22"],
    "sec_cosmos_public": ["A.8.20"],
    "rel_vm_no_zone": ["A.5.29", "A.8.14"],
    "rel_storage_lrs": ["A.8.13", "A.8.14"],
    "rel_pip_basic_sku": ["A.8.14"],
    "rel_lb_basic_sku": ["A.8.14"],
    "rel_sql_no_zone": ["A.5.29", "A.8.14"],
    "rel_appplan_single_instance": ["A.8.14"],
    "rel_disk_lrs": ["A.8.13"],
    "rel_cosmos_single_region": ["A.5.29", "A.8.14"],
    "rel_aks_single_nodepool": ["A.8.14"],
    "rel_appgw_no_zone": ["A.8.14"],
}
for _c in ALL_CHECKS:
    iso = _ISO_MAP.get(_c["id"])
    if iso:
        _c["frameworks"]["iso"] = iso

_BY_ID = {c["id"]: c for c in ALL_CHECKS}

# Framework display metadata for the compliance coverage view.
FRAMEWORK_META: dict[str, dict[str, str]] = {
    "cis": {"label": "CIS Azure Foundations", "icon": "🛡️"},
    "nist": {"label": "NIST 800-53 Rev.5", "icon": "🏛️"},
    "iso": {"label": "ISO/IEC 27001:2022", "icon": "📜"},
}


def checks_for(pillars: list[str], *, include_custom: bool = True) -> list[dict[str, Any]]:
    """All checks (shipped + enabled custom) belonging to the requested pillars."""
    want = set(p.lower() for p in pillars)
    out = [c for c in ALL_CHECKS if c["pillar"] in want]
    if include_custom:
        try:
            from app.assessments.custom_checks import enabled_custom_checks

            out = out + enabled_custom_checks(list(want))
        except Exception:  # noqa: BLE001 - custom checks optional
            pass
    return out


def compliance_coverage(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate findings into a per-framework compliance report.

    For each framework (CIS/NIST/ISO), maps each referenced control id to the worst
    status across the checks that cite it (fail > error > not_applicable > pass), so the
    same assessment run yields a compliance coverage view alongside the WAF score."""
    rank = {"fail": 3, "error": 2, "not_applicable": 1, "pass": 0}
    out: dict[str, Any] = {}
    for fw in ("cis", "nist", "iso"):
        controls: dict[str, dict[str, Any]] = {}
        for f in findings:
            fws = (f.get("frameworks") or {}).get(fw) or []
            for ctrl in fws:
                entry = controls.setdefault(ctrl, {"control": ctrl, "status": "pass", "checks": []})
                entry["checks"].append({"check_id": f.get("check_id"), "title": f.get("title"), "status": f.get("status")})
                if rank.get(f.get("status", "pass"), 0) > rank.get(entry["status"], 0):
                    entry["status"] = f.get("status", "pass")
        items = sorted(controls.values(), key=lambda c: c["control"])
        total = len([c for c in items if c["status"] != "not_applicable"])
        passed = len([c for c in items if c["status"] == "pass"])
        failed = len([c for c in items if c["status"] in ("fail", "error")])
        out[fw] = {
            **FRAMEWORK_META.get(fw, {"label": fw, "icon": "📋"}),
            "controls": items,
            "total": total,
            "passed": passed,
            "failed": failed,
            "coverage": round(100 * passed / total) if total else None,
        }
    return out



def get_check(check_id: str) -> dict[str, Any] | None:
    if check_id in _BY_ID:
        return _BY_ID[check_id]
    try:
        from app.assessments.custom_checks import get_custom_check

        return get_custom_check(check_id)
    except Exception:  # noqa: BLE001
        return None


def detection_predicate(check_id: str) -> str:
    """The check's detection logic as a single Resource Graph (KQL) boolean predicate over
    the ``resources`` table — i.e. the set of resources it flags as violating. This is
    exactly the what-if predicate the Safe-Rollout Planner needs, so a finding can be
    enforced as a policy without re-deriving (or re-running) anything. Returns '' if a
    check has no extractable ``where`` body."""
    c = get_check(check_id)
    if not c:
        return ""
    kql = c.get("kql", "") or ""
    clauses: list[str] = []
    for part in kql.split("|"):
        seg = part.strip()
        if seg.lower().startswith("where "):
            body = seg[6:].strip()
            if body:
                clauses.append(f"({body})")
    return " and ".join(clauses)



def _check_public(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c["id"],
        "pillar": c["pillar"],
        "title": c["title"],
        "description": c["description"],
        "severity": c["severity"],
        "weight": c.get("weight", 3),
        "resource_types": c.get("resource_types", []),
        "frameworks": c.get("frameworks", {}),
        "remediation": c.get("remediation", ""),
        "remediation_command": c.get("remediation_command", ""),
        "custom": c.get("custom", False),
    }


def public_catalog() -> dict[str, Any]:
    """The shipped + custom catalog grouped by pillar, with metadata for the UI."""
    by_pillar: dict[str, list[dict[str, Any]]] = {p: [] for p in PILLARS}
    for c in ALL_CHECKS:
        by_pillar.setdefault(c["pillar"], []).append(_check_public(c))
    try:
        from app.assessments.custom_checks import list_custom_checks

        for c in list_custom_checks():
            by_pillar.setdefault(c["pillar"], []).append(_check_public(c))
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.core.app_settings import assessment_score_bands

        bands = assessment_score_bands()
    except Exception:  # noqa: BLE001
        bands = {"good": 80, "warn": 50}
    return {
        "pillars": [
            {"id": p, **PILLAR_META[p], "check_count": len(by_pillar.get(p, []))}
            for p in PILLARS
        ],
        "frameworks": FRAMEWORK_META,
        "checks": by_pillar,
        "score_bands": bands,
    }


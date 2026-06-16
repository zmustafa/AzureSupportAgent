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
- ``frameworks`` maps the control to compliance control ids across multiple frameworks:
  CIS Azure Foundations Benchmark (pinned via ``CIS_VERSION``), NIST 800-53 Rev.5,
  ISO/IEC 27001:2022 Annex A, Microsoft Cloud Security Benchmark (MCSB) and PCI DSS v4.0.
  CIS/NIST are declared inline on each control; ISO/MCSB/PCI are applied centrally by
  check id (see ``_ISO_MAP`` / ``_MCSB_MAP`` / ``_PCI_MAP``) so adding a framework needs
  no per-control edits.

Two control kinds exist:
- Resource Graph (ARG/KQL) controls built with ``_check`` — the deterministic default.
- Metric-backed controls built with ``_metric_check`` — evaluated per in-scope resource
  against an Azure Monitor metric threshold (e.g. idle-VM CPU). These carry a ``metric``
  config and an empty ``kql``; the runner routes them through the metrics path.
"""
from __future__ import annotations

from typing import Any

# CIS Microsoft Azure Foundations Benchmark version that the ``cis`` control ids below are
# pinned to. Centralised so the benchmark edition is unambiguous and easy to bump.
CIS_VERSION = "v5.0.0"

# Version of the shipped control catalog. Stamped onto every AssessmentRun so historical
# runs are reproducible and drift/diff across catalog edits is meaningful. Bump whenever
# controls are added/removed/materially changed.
CATALOG_VERSION = "2026.06.3"

# Version of the per-finding result schema (shape of each entry in findings_json). Bump
# when the finding dict gains/changes fields the UI or scoring relies on.
FINDING_SCHEMA_VERSION = 2

PILLARS = ("security", "reliability", "cost", "operations", "performance")

PILLAR_META: dict[str, dict[str, str]] = {
    "security": {"label": "Security", "icon": "🛡️"},
    "reliability": {"label": "Reliability", "icon": "🔄"},
    "cost": {"label": "Cost Optimization", "icon": "💰"},
    "operations": {"label": "Operational Excellence", "icon": "⚙️"},
    "performance": {"label": "Performance Efficiency", "icon": "⚡"},
}

# Named assessment packs map a recognised Microsoft methodology to the pillar(s) it covers,
# so a user can launch "WARA" / "WASA" / a full WAF review by name. A pack is just a
# convenient pillar bundle — the same deterministic + manual + signal controls run underneath.
PACKS: dict[str, dict[str, Any]] = {
    "waf": {
        "label": "Well-Architected Review (all pillars)",
        "short": "WAF",
        "icon": "🏛️",
        "pillars": list(PILLARS),
        "description": "Full Azure Well-Architected Framework review across Security, "
        "Reliability, Cost, Operational Excellence, and Performance.",
    },
    "wara": {
        "label": "Well-Architected Reliability Assessment",
        "short": "WARA",
        "icon": "🔄",
        "pillars": ["reliability"],
        "description": "Reliability-pillar deep dive (Microsoft WARA / APRL aligned): "
        "availability zones, multi-region DR, backup/recovery, SLAs, and Advisor signals.",
    },
    "wasa": {
        "label": "Well-Architected Security Assessment",
        "short": "WASA",
        "icon": "🛡️",
        "pillars": ["security"],
        "description": "Security-pillar deep dive: exposure, encryption, identity, network "
        "isolation, and key management, mapped to CIS/NIST/ISO/MCSB/PCI.",
    },
}


def pack_pillars(pack_id: str) -> list[str] | None:
    """Resolve a pack id (e.g. 'wara') to its pillar list, or None if unknown."""
    p = PACKS.get((pack_id or "").lower())
    if not p:
        return None
    return [x for x in p["pillars"] if x in PILLARS]


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
    kind: str = "graph",
    impact: str = "",
    effort: str = "",
    sub_category: str = "",
    source: str = "built-in",
    learn_more: list[str] | None = None,
    arg_table: str = "Resources",
    expectation: str = "",
    profile: str = "",
    scope_mode: str = "",
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
        # --- WARA/APRL-aligned metadata (all optional, backward compatible) ---
        "kind": kind,  # graph | metric | manual | signal
        "impact": impact,  # high | medium | low (business impact of the finding)
        "effort": effort,  # low | medium | high (remediation effort)
        "sub_category": sub_category,  # WAF sub-pillar, e.g. "High availability"
        "source": source,  # built-in | aprl | advisor | custom | cis-v5
        "learn_more": list(learn_more or []),  # documentation URLs
        # --- ARG table + evaluation mode (CIS subscription-scoped controls) ---
        "arg_table": arg_table,  # Resources | securityresources | authorizationresources | …
        "expectation": expectation,  # "" = violation mode; "present" = existence/absence mode
        "profile": profile,  # "" | "L1" | "L2" (CIS profile level)
        "scope_mode": scope_mode,  # "" = scoped; "tenant" = no scope filter (tenant-wide governance)
    }


# WAF/APRL reliability sub-categories used to slice the reliability score like WARA.
SUB_CATEGORIES = (
    "High availability",
    "Disaster recovery",
    "Scalability",
    "Monitoring & alerting",
    "Service upgrade & retirement",
    "Governance",
    "Other",
)


def _metric_check(
    cid: str,
    pillar: str,
    title: str,
    description: str,
    severity: str,
    resource_types: list[str],
    metric: dict[str, Any],
    remediation: str,
    *,
    frameworks: dict[str, list[str]] | None = None,
    remediation_command: str = "",
    weight: int | None = None,
    impact: str = "",
    effort: str = "",
    sub_category: str = "",
    source: str = "built-in",
    learn_more: list[str] | None = None,
) -> dict[str, Any]:
    """A metric-backed control. Unlike an ARG control it has no KQL predicate; instead the
    runner pulls an Azure Monitor metric for each in-scope resource and flags those whose
    aggregated value violates a threshold (e.g. idle VMs whose peak CPU stayed low).

    ``metric`` keys:
    - ``metric``        Azure Monitor metric name (e.g. 'Percentage CPU').
    - ``aggregation``   server-side aggregation requested ('Average'|'Maximum'|'Total'|…).
    - ``evaluate``      how to reduce the time-series to one number ('avg'|'max'|'min').
    - ``comparison``    'lt'|'le'|'gt'|'ge' — resource is flagged when value <cmp> threshold.
    - ``threshold``     numeric threshold.
    - ``lookback_days`` window length (days) of metric history to evaluate.
    - ``interval``      metric grain (ISO-8601 duration, e.g. 'PT1H').
    - ``unit``          display unit for the flagged value (e.g. '%').

    ``kql`` is intentionally empty so ``detection_predicate`` returns '' — metric checks
    can't be enforced as a static what-if policy, which is correct.
    """
    c = _check(
        cid, pillar, title, description, severity, resource_types, "", remediation,
        frameworks=frameworks, remediation_command=remediation_command, weight=weight,
        kind="metric", impact=impact, effort=effort, sub_category=sub_category,
        source=source, learn_more=learn_more,
    )
    c["metric"] = {
        "metric": metric["metric"],
        "aggregation": metric.get("aggregation", "Average"),
        "evaluate": metric.get("evaluate", "avg"),
        "comparison": metric.get("comparison", "lt"),
        "threshold": float(metric.get("threshold", 0)),
        "lookback_days": int(metric.get("lookback_days", 7)),
        "interval": metric.get("interval", "PT1H"),
        "unit": metric.get("unit", ""),
    }
    return c


def _manual_check(
    cid: str,
    pillar: str,
    title: str,
    description: str,
    severity: str,
    resource_types: list[str],
    remediation: str,
    *,
    frameworks: dict[str, list[str]] | None = None,
    impact: str = "",
    effort: str = "",
    sub_category: str = "",
    source: str = "built-in",
    learn_more: list[str] | None = None,
    profile: str = "",
) -> dict[str, Any]:
    """A manual-attestation control (no automated query). Many WAF/APRL recommendations can't
    be verified from Resource Graph — they need a reviewer to confirm. A manual control
    surfaces as ``manual`` (pending) and is EXCLUDED from the auto-score until a human records
    an attestation (pass/fail/N/A), at which point it scores like any other control."""
    c = _check(
        cid, pillar, title, description, severity, resource_types, "", remediation,
        frameworks=frameworks, kind="manual", impact=impact, effort=effort,
        sub_category=sub_category, source=source, learn_more=learn_more, profile=profile,
    )
    return c


def _signal_check(
    cid: str,
    pillar: str,
    title: str,
    description: str,
    severity: str,
    signal: dict[str, Any],
    remediation: str,
    *,
    resource_types: list[str] | None = None,
    frameworks: dict[str, list[str]] | None = None,
    impact: str = "",
    effort: str = "",
    sub_category: str = "",
    source: str = "advisor",
    learn_more: list[str] | None = None,
    profile: str = "",
) -> dict[str, Any]:
    """A live-signal control backed by a platform data plane (e.g. Azure Advisor) rather than
    a resource-config predicate. ``signal`` config drives the runner:
    - ``provider`` currently ``advisor``.
    - ``category`` Advisor category to match (e.g. ``HighAvailability``).

    The control flags resources that have an open recommendation in that category. Applicable
    whenever the scope contains ANY resource (``resource_types`` empty == always applicable)."""
    c = _check(
        cid, pillar, title, description, severity, resource_types or [], "", remediation,
        frameworks=frameworks, kind="signal", impact=impact, effort=effort,
        sub_category=sub_category, source=source, learn_more=learn_more, profile=profile,
    )
    c["signal"] = {
        "provider": signal.get("provider", "advisor"),
        "category": signal.get("category", ""),
    }
    return c


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
        frameworks={"nist": ["AC-2", "IA-2"]},
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
        remediation_command="az keyvault update --name <name> --resource-group <rg> --public-network-access Disabled",
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
        frameworks={"nist": ["SC-7"]},
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
        frameworks={"cis": ["CIS Azure 7.3"], "nist": ["SC-28"]},
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
        remediation_command="az cosmosdb update --name <name> --resource-group <rg> --public-network-access DISABLED",
    ),
    _check(
        "sec_storage_min_tls",
        "security",
        "Storage accounts allow TLS below 1.2",
        "Storage accounts whose minimum TLS version is below 1.2 accept weak, deprecated TLS "
        "connections that are vulnerable to downgrade and known protocol attacks.",
        "error",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tostring(properties.minimumTlsVersion) !in~ ('TLS1_2', 'TLS1_3') "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Set the minimum TLS version to 1.2 (or later) on each storage account.",
        frameworks={"cis": ["CIS Azure 3.15"], "nist": ["SC-8"]},
        remediation_command="az storage account update --name <name> --resource-group <rg> --min-tls-version TLS1_2",
    ),
    _check(
        "sec_storage_net_default_allow",
        "security",
        "Storage accounts default to allowing all networks",
        "Storage accounts whose network default action is 'Allow' are reachable from every "
        "network; the firewall should default to 'Deny' and explicitly allow trusted ranges.",
        "warning",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where isnull(properties.networkAcls.defaultAction) or tostring(properties.networkAcls.defaultAction) =~ 'Allow' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Set the storage firewall default action to Deny and allow only required VNets / IP "
        "ranges (and trusted Azure services).",
        frameworks={"cis": ["CIS Azure 3.8"], "nist": ["SC-7", "AC-3"]},
        remediation_command="az storage account update --name <name> --resource-group <rg> --default-action Deny",
    ),
    _check(
        "sec_sql_aad_only",
        "security",
        "SQL servers allow SQL (non-Entra) authentication",
        "SQL logical servers without Azure AD (Entra)-only authentication still accept "
        "password-based SQL logins, which are harder to govern, rotate, and audit.",
        "warning",
        ["microsoft.sql/servers"],
        "| where type =~ 'microsoft.sql/servers' "
        "| where isnull(properties.administrators.azureADOnlyAuthentication) "
        "or tobool(properties.administrators.azureADOnlyAuthentication) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Set an Entra admin and enable Azure AD-only authentication on the SQL server.",
        frameworks={"nist": ["IA-2", "AC-2"]},
        remediation_command="az sql server ad-only-auth enable --resource-group <rg> --server <name>",
    ),
    _check(
        "sec_kv_rbac",
        "security",
        "Key Vaults using legacy access policies (not RBAC)",
        "Key Vaults with vault access policies instead of Azure RBAC can't use fine-grained, "
        "centrally-audited role assignments and are harder to govern at scale.",
        "warning",
        ["microsoft.keyvault/vaults"],
        "| where type =~ 'microsoft.keyvault/vaults' "
        "| where isnull(properties.enableRbacAuthorization) or tobool(properties.enableRbacAuthorization) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Migrate the vault to the Azure RBAC permission model and assign least-privilege roles.",
        frameworks={"cis": ["CIS Azure 8.5"], "nist": ["AC-3", "AC-6"]},
        remediation_command="az keyvault update --name <name> --resource-group <rg> --enable-rbac-authorization true",
    ),
    _check(
        "sec_kv_soft_delete",
        "security",
        "Key Vaults without soft-delete enabled",
        "Without soft-delete a deleted vault, key, or secret is unrecoverable, risking "
        "permanent data loss and breaking key-backed services.",
        "error",
        ["microsoft.keyvault/vaults"],
        "| where type =~ 'microsoft.keyvault/vaults' "
        "| where isnull(properties.enableSoftDelete) or tobool(properties.enableSoftDelete) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable soft-delete (and purge protection) on every Key Vault.",
        frameworks={"cis": ["CIS Azure 8.4"], "nist": ["CP-9", "SC-12"]},
        remediation_command="az keyvault update --name <name> --resource-group <rg> --enable-soft-delete true",
    ),
    _check(
        "sec_webapp_min_tls",
        "security",
        "App Services allow TLS below 1.2",
        "Web/function apps whose minimum TLS version is 1.0 or 1.1 accept weak, deprecated TLS, "
        "exposing traffic to downgrade attacks.",
        "error",
        ["microsoft.web/sites"],
        "| where type =~ 'microsoft.web/sites' "
        "| where tostring(properties.siteConfig.minTlsVersion) in~ ('1.0', '1.1') "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Set the minimum TLS version to 1.2 (or later) on each App Service.",
        frameworks={"nist": ["SC-8", "SC-23"]},
        remediation_command="az webapp config set --name <name> --resource-group <rg> --min-tls-version 1.2",
    ),
    _check(
        "sec_webapp_ftps",
        "security",
        "App Services allow plaintext FTP deployment",
        "App Services with FTP state 'AllAllowed' permit unencrypted FTP for content deployment, "
        "exposing credentials and code in transit.",
        "warning",
        ["microsoft.web/sites"],
        "| where type =~ 'microsoft.web/sites' "
        "| where tostring(properties.siteConfig.ftpsState) =~ 'AllAllowed' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Set FTP state to 'FTPS only' or 'Disabled' on each App Service.",
        frameworks={"nist": ["SC-8"]},
        remediation_command="az webapp config set --name <name> --resource-group <rg> --ftps-state Disabled",
    ),
    _check(
        "sec_webapp_no_managed_identity",
        "security",
        "App Services without a managed identity",
        "App Services with no managed identity tend to rely on secrets/connection strings in "
        "config instead of identity-based access to Azure resources.",
        "info",
        ["microsoft.web/sites"],
        "| where type =~ 'microsoft.web/sites' "
        "| where isnull(identity) or tostring(identity.type) in~ ('', 'None') "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable a system- or user-assigned managed identity and use it for downstream access.",
        frameworks={"nist": ["IA-2", "IA-5"]},
        remediation_command="az webapp identity assign --name <name> --resource-group <rg>",
    ),
    _check(
        "sec_acr_admin_user",
        "security",
        "Container registries with the admin user enabled",
        "ACR admin user is a single shared username/password with push/pull rights — a "
        "long-lived credential that bypasses per-identity RBAC and auditing.",
        "warning",
        ["microsoft.containerregistry/registries"],
        "| where type =~ 'microsoft.containerregistry/registries' "
        "| where tobool(properties.adminUserEnabled) == true "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Disable the admin user and use Entra identities / tokens with scoped RBAC instead.",
        frameworks={"nist": ["AC-2", "AC-6", "IA-2"]},
        remediation_command="az acr update --name <name> --resource-group <rg> --admin-enabled false",
    ),
    _check(
        "sec_acr_public_network",
        "security",
        "Container registries allow public network access",
        "ACRs reachable from all networks are exposed beyond the private boundary; registries "
        "holding production images should be private.",
        "warning",
        ["microsoft.containerregistry/registries"],
        "| where type =~ 'microsoft.containerregistry/registries' "
        "| where tostring(properties.publicNetworkAccess) =~ 'Enabled' or isnull(properties.publicNetworkAccess) "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Set public network access to Disabled and use private endpoints (Premium SKU).",
        frameworks={"nist": ["SC-7"]},
        remediation_command="az acr update --name <name> --resource-group <rg> --public-network-enabled false",
    ),
    _check(
        "sec_aks_local_accounts",
        "security",
        "AKS clusters with local (non-Entra) accounts enabled",
        "AKS clusters that keep static local admin kubeconfig accounts enabled allow access "
        "that bypasses Entra ID and Kubernetes RBAC auditing.",
        "warning",
        ["microsoft.containerservice/managedclusters"],
        "| where type =~ 'microsoft.containerservice/managedclusters' "
        "| where isnull(properties.disableLocalAccounts) or tobool(properties.disableLocalAccounts) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Disable local accounts and use Entra ID integration with Kubernetes RBAC.",
        frameworks={"nist": ["IA-2", "AC-2"]},
        remediation_command="az aks update --name <name> --resource-group <rg> --disable-local-accounts",
    ),
    _check(
        "sec_aks_no_rbac",
        "security",
        "AKS clusters without Kubernetes RBAC",
        "Clusters with Kubernetes RBAC disabled can't enforce least-privilege authorization "
        "inside the cluster, so any authenticated user may have broad access.",
        "error",
        ["microsoft.containerservice/managedclusters"],
        "| where type =~ 'microsoft.containerservice/managedclusters' "
        "| where tobool(properties.enableRBAC) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Recreate the cluster with Kubernetes RBAC enabled (RBAC can't be toggled in place).",
        frameworks={"nist": ["AC-3", "AC-6"]},
    ),
    _check(
        "sec_cosmos_local_auth",
        "security",
        "Cosmos DB accounts with key-based (local) auth enabled",
        "Cosmos DB accounts that allow key-based auth rely on long-lived shared keys instead of "
        "Entra identity, weakening rotation and audit.",
        "warning",
        ["microsoft.documentdb/databaseaccounts"],
        "| where type =~ 'microsoft.documentdb/databaseaccounts' "
        "| where isnull(properties.disableLocalAuth) or tobool(properties.disableLocalAuth) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Disable local (key) auth and use Entra ID RBAC for data-plane access.",
        frameworks={"nist": ["IA-2", "AC-2"]},
    ),
    _check(
        "sec_nsg_db_ports_open",
        "security",
        "NSGs allow inbound database ports from the internet",
        "NSG rules permitting inbound database ports (SQL 1433, MySQL 3306, PostgreSQL 5432, "
        "Redis 6379, MongoDB 27017) from any source expose datastores directly to the internet.",
        "critical",
        ["microsoft.network/networksecuritygroups"],
        "| where type =~ 'microsoft.network/networksecuritygroups' "
        "| mv-expand rule = properties.securityRules "
        "| extend dir = tostring(rule.properties.direction), acc = tostring(rule.properties.access), "
        "src = tostring(rule.properties.sourceAddressPrefix), "
        "ports = strcat(tostring(rule.properties.destinationPortRange), ' ', tostring(rule.properties.destinationPortRanges)) "
        "| where dir =~ 'Inbound' and acc =~ 'Allow' and (src == '*' or src == '0.0.0.0/0' or src =~ 'Internet') "
        "and (ports has '1433' or ports has '3306' or ports has '5432' or ports has '6379' or ports has '27017' or ports has '*') "
        "| summarize by id, name, type, resourceGroup, subscriptionId",
        "Remove internet-facing database rules; reach datastores over private endpoints / VNet "
        "and restrict NSG sources to specific application subnets.",
        frameworks={"nist": ["SC-7", "AC-17"]},
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
    _check(
        "rel_appservice_no_zone",
        "reliability",
        "App Service plans without zone redundancy",
        "App Service plans without zone redundancy keep all instances in one zone and won't "
        "survive a single-zone outage.",
        "warning",
        ["microsoft.web/serverfarms"],
        "| where type =~ 'microsoft.web/serverfarms' "
        "| where isnull(properties.zoneRedundant) or tobool(properties.zoneRedundant) == false "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable zone redundancy on supported (Premium v2/v3) App Service plans with 2+ instances.",
        frameworks={"nist": ["CP-2", "CP-10"]},
    ),
    _check(
        "rel_aks_free_sla",
        "reliability",
        "AKS clusters on the Free tier (no uptime SLA)",
        "AKS clusters on the Free tier have no financially-backed control-plane uptime SLA; "
        "production clusters should use the Standard tier.",
        "warning",
        ["microsoft.containerservice/managedclusters"],
        "| where type =~ 'microsoft.containerservice/managedclusters' "
        "| where isnull(sku.tier) or tostring(sku.tier) =~ 'Free' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Move production clusters to the Standard tier for the control-plane uptime SLA.",
        frameworks={"nist": ["CP-2"]},
        remediation_command="az aks update --name <name> --resource-group <rg> --tier standard",
    ),
    _check(
        "rel_vmss_no_zone",
        "reliability",
        "VM scale sets not spread across availability zones",
        "Virtual machine scale sets without availability zones share a single zone's fault and "
        "maintenance domains and won't survive a zone outage.",
        "warning",
        ["microsoft.compute/virtualmachinescalesets"],
        "| where type =~ 'microsoft.compute/virtualmachinescalesets' "
        "| where isnull(zones) or array_length(zones) == 0 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Deploy scale sets across 2-3 availability zones for zone-resilient capacity.",
        frameworks={"nist": ["CP-2", "CP-10"]},
        sub_category="High availability",
        impact="high",
        effort="high",
    ),
    # ---------------- Native reliability expansion (WARA/APRL-aligned) ----------------
    _check(
        "rel_vm_single_instance",
        "reliability",
        "Single-instance VMs with no zone, availability set, or scale set",
        "A VM that is not in an availability zone, an availability set, or a scale set is a "
        "single point of failure with no platform SLA for instance uptime.",
        "warning",
        ["microsoft.compute/virtualmachines"],
        "| where type =~ 'microsoft.compute/virtualmachines' "
        "| where (isnull(zones) or array_length(zones) == 0) "
        "and isnull(properties.availabilitySet) and isnull(properties.virtualMachineScaleSet) "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Place the VM in an availability zone, an availability set, or a scale set; for "
        "stateless workloads prefer zone-spread scale sets behind a Standard Load Balancer.",
        frameworks={"nist": ["CP-2", "CP-10"]},
        sub_category="High availability",
        impact="high",
        effort="high",
        learn_more=["https://learn.microsoft.com/azure/reliability/availability-zones-overview"],
    ),
    _check(
        "rel_firewall_no_zone",
        "reliability",
        "Azure Firewall not deployed across availability zones",
        "An Azure Firewall without availability zones is a zonal single point of failure for "
        "all traffic that transits it.",
        "warning",
        ["microsoft.network/azurefirewalls"],
        "| where type =~ 'microsoft.network/azurefirewalls' "
        "| where isnull(zones) or array_length(zones) < 2 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Redeploy the firewall pinned to 2-3 availability zones (zones are set at creation).",
        frameworks={"nist": ["CP-2"]},
        sub_category="High availability",
        impact="high",
        effort="high",
    ),
    _check(
        "rel_vnet_gateway_no_az_sku",
        "reliability",
        "VPN/ExpressRoute gateways on non-zone-redundant SKUs",
        "Virtual network gateways whose SKU is not zone-redundant (…AZ) run in a single zone, "
        "so a zone outage drops hybrid connectivity.",
        "warning",
        ["microsoft.network/virtualnetworkgateways"],
        "| where type =~ 'microsoft.network/virtualnetworkgateways' "
        "| extend skuName = tostring(properties.sku.name) "
        "| where isnotempty(skuName) and skuName !endswith 'AZ' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Migrate to a zone-redundant gateway SKU (e.g. VpnGw1AZ / ErGw1AZ) for zone resilience.",
        frameworks={"nist": ["CP-2"]},
        sub_category="High availability",
        impact="medium",
        effort="high",
    ),
    _check(
        "rel_traffic_manager_single_endpoint",
        "reliability",
        "Traffic Manager profiles with fewer than two endpoints",
        "A Traffic Manager profile with a single endpoint provides no failover target, so it "
        "can't deliver the multi-region availability it exists to provide.",
        "warning",
        ["microsoft.network/trafficmanagerprofiles"],
        "| where type =~ 'microsoft.network/trafficmanagerprofiles' "
        "| extend n = array_length(properties.endpoints) "
        "| where isnull(n) or n < 2 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Add at least one healthy secondary endpoint (different region) and configure failover/priority routing.",
        frameworks={"nist": ["CP-2"]},
        sub_category="Disaster recovery",
        impact="high",
        effort="medium",
    ),
    _check(
        "rel_servicebus_basic_tier",
        "reliability",
        "Service Bus namespaces on the Basic tier",
        "Service Bus Basic has no topics, no geo-disaster-recovery pairing, and no zone "
        "redundancy — unsuitable for resilient messaging.",
        "warning",
        ["microsoft.servicebus/namespaces"],
        "| where type =~ 'microsoft.servicebus/namespaces' "
        "| where tolower(tostring(sku.name)) == 'basic' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Upgrade to Standard or Premium; use Premium with zone redundancy + geo-DR for critical workloads.",
        frameworks={"nist": ["CP-2"]},
        sub_category="High availability",
        impact="medium",
        effort="medium",
    ),
    _check(
        "rel_eventhub_basic_tier",
        "reliability",
        "Event Hubs namespaces on the Basic tier",
        "Event Hubs Basic has a 1-day retention cap and no geo-DR or zone redundancy, limiting "
        "resilience and recovery for streaming pipelines.",
        "info",
        ["microsoft.eventhub/namespaces"],
        "| where type =~ 'microsoft.eventhub/namespaces' "
        "| where tolower(tostring(sku.name)) == 'basic' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Move to Standard/Premium and enable zone redundancy (and geo-DR for critical streams).",
        frameworks={"nist": ["CP-2"]},
        sub_category="High availability",
        impact="low",
        effort="medium",
    ),
    _check(
        "rel_redis_basic_tier",
        "reliability",
        "Azure Cache for Redis on the Basic tier",
        "Basic-tier Redis is a single node with no replication and no SLA — a restart or node "
        "failure causes a full cache outage and data loss.",
        "warning",
        ["microsoft.cache/redis"],
        "| where type =~ 'microsoft.cache/redis' "
        "| where tolower(tostring(properties.sku.name)) == 'basic' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Upgrade to Standard (replicated) or Premium (zone-redundant) for an availability SLA.",
        frameworks={"nist": ["CP-2"]},
        sub_category="High availability",
        impact="high",
        effort="medium",
    ),
    _check(
        "rel_postgres_flexible_no_ha",
        "reliability",
        "PostgreSQL Flexible Server without high availability",
        "A Flexible Server with high availability disabled has no standby replica, so planned or "
        "unplanned downtime takes the database fully offline.",
        "warning",
        ["microsoft.dbforpostgresql/flexibleservers"],
        "| where type =~ 'microsoft.dbforpostgresql/flexibleservers' "
        "| where isnull(properties.highAvailability.mode) or tostring(properties.highAvailability.mode) =~ 'Disabled' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable zone-redundant high availability (a standby in another zone) for production databases.",
        frameworks={"nist": ["CP-2", "CP-10"]},
        sub_category="High availability",
        impact="high",
        effort="medium",
    ),
    _check(
        "rel_mysql_flexible_no_ha",
        "reliability",
        "MySQL Flexible Server without high availability",
        "A MySQL Flexible Server with high availability disabled has no standby replica, so any "
        "downtime takes the database fully offline.",
        "warning",
        ["microsoft.dbformysql/flexibleservers"],
        "| where type =~ 'microsoft.dbformysql/flexibleservers' "
        "| where isnull(properties.highAvailability.mode) or tostring(properties.highAvailability.mode) =~ 'Disabled' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Enable zone-redundant high availability for production databases.",
        frameworks={"nist": ["CP-2", "CP-10"]},
        sub_category="High availability",
        impact="high",
        effort="medium",
    ),
    _check(
        "rel_apim_developer_sku",
        "reliability",
        "API Management on the Developer SKU",
        "The API Management Developer tier carries no SLA and can't scale out or span zones — it "
        "is not intended to front production traffic.",
        "warning",
        ["microsoft.apimanagement/service"],
        "| where type =~ 'microsoft.apimanagement/service' "
        "| where tolower(tostring(sku.name)) == 'developer' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Use Standard/Premium for production; Premium adds multi-region + zone redundancy.",
        frameworks={"nist": ["CP-2"]},
        sub_category="High availability",
        impact="medium",
        effort="high",
    ),
    _check(
        "rel_container_app_min_replicas_zero",
        "reliability",
        "Container Apps that can scale to zero replicas",
        "A Container App with minReplicas 0 has cold starts and no always-on instance, so the "
        "first request after idle can fail or time out — risky for latency-sensitive services.",
        "info",
        ["microsoft.app/containerapps"],
        "| where type =~ 'microsoft.app/containerapps' "
        "| extend minr = toint(properties.template.scale.minReplicas) "
        "| where isnull(minr) or minr < 1 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Set minReplicas to at least 1 (2+ for zone resilience) for always-on production apps.",
        frameworks={"nist": ["CP-2"]},
        sub_category="Scalability",
        impact="low",
        effort="low",
    ),
    # ---------------- Manual attestation controls (reviewer-verified) ----------------
    _manual_check(
        "rel_manual_dr_drill",
        "reliability",
        "Disaster-recovery failover tested recently",
        "A documented DR failover drill has been executed for this workload within the last 6 "
        "months and met its recovery objectives. Cannot be verified from resource configuration.",
        "error",
        [],
        "Run and document a failover drill to the secondary region; capture the achieved RTO/RPO.",
        sub_category="Disaster recovery",
        impact="high",
        effort="high",
        learn_more=["https://learn.microsoft.com/azure/reliability/disaster-recovery-overview-for-azure-services"],
    ),
    _manual_check(
        "rel_manual_rto_rpo",
        "reliability",
        "RTO and RPO targets are defined",
        "Recovery Time Objective and Recovery Point Objective targets are agreed with the "
        "business and documented for this workload.",
        "warning",
        [],
        "Define and document RTO/RPO per business criticality and align backup/DR design to them.",
        sub_category="Disaster recovery",
        impact="high",
        effort="medium",
    ),
    _manual_check(
        "rel_manual_health_model",
        "reliability",
        "Workload health model and SLO defined",
        "The workload has a defined health model (what 'healthy' means) and a service-level "
        "objective, with alerting wired to user-facing signals.",
        "warning",
        [],
        "Define an SLO and a layered health model; alert on user-impacting symptoms, not just resource metrics.",
        sub_category="Monitoring & alerting",
        impact="medium",
        effort="medium",
    ),
    # ---------------- Live platform signal (Azure Advisor) ----------------
    _signal_check(
        "rel_advisor_high_availability",
        "reliability",
        "Open Azure Advisor reliability recommendations",
        "Azure Advisor has open High Availability recommendations for in-scope resources — "
        "Microsoft's own reliability guidance flagged a gap on these resources.",
        "warning",
        {"provider": "advisor", "category": "HighAvailability"},
        "Review and action the Advisor High Availability recommendations for each flagged resource.",
        sub_category="Other",
        impact="medium",
        effort="medium",
        learn_more=["https://learn.microsoft.com/azure/advisor/advisor-reference-reliability-recommendations"],
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
    _check(
        "cost_nat_gateway_orphaned",
        "cost",
        "NAT gateways not associated with any subnet",
        "A NAT gateway with no associated subnets does no work yet still bills hourly for the "
        "gateway resource.",
        "warning",
        ["microsoft.network/natgateways"],
        "| where type =~ 'microsoft.network/natgateways' "
        "| where isnull(properties.subnets) or array_length(properties.subnets) == 0 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Delete NAT gateways that aren't associated with any subnet.",
        remediation_command="az network nat gateway delete --name <name> --resource-group <rg>",
    ),
    _metric_check(
        "cost_vm_idle",
        "cost",
        "Virtual machines that appear idle (very low CPU)",
        "VMs whose peak CPU stayed below 5% over the last week are likely idle or oversized and "
        "are paying for compute capacity they don't use.",
        "warning",
        ["microsoft.compute/virtualmachines"],
        {
            "metric": "Percentage CPU",
            "aggregation": "Average",
            "evaluate": "max",
            "comparison": "lt",
            "threshold": 5.0,
            "lookback_days": 7,
            "interval": "PT1H",
            "unit": "%",
        },
        "Deallocate, downsize, or reclaim idle VMs (confirm they aren't intentional standby).",
        frameworks={"nist": ["CM-8"]},
        remediation_command="az vm deallocate --name <name> --resource-group <rg>",
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
    _check(
        "ops_missing_costcenter_tag",
        "operations",
        "Resources missing a 'cost center' tag",
        "Resources without a cost-center tag can't be charged back or governed by FinOps "
        "policy, making spend attribution and accountability difficult.",
        "info",
        _COMMON_TYPES,
        f"| where type in~ ({_COMMON_TYPES_KQL}) "
        f"| where isempty(tostring(tags['costcenter'])) and isempty(tostring(tags['CostCenter'])) "
        f"and isempty(tostring(tags['cost-center'])) {_PROJECT}",
        "Apply a consistent cost-center tag and enforce it with Azure Policy.",
        frameworks={"nist": ["CM-8"]},
    ),
    _check(
        "ops_law_short_retention",
        "operations",
        "Log Analytics workspaces with short data retention",
        "Workspaces retaining data for fewer than 30 days limit incident investigation and may "
        "fall short of audit/compliance retention requirements.",
        "warning",
        ["microsoft.operationalinsights/workspaces"],
        "| where type =~ 'microsoft.operationalinsights/workspaces' "
        "| where toint(properties.retentionInDays) < 30 "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Increase workspace retention to at least 30 days (longer for regulated workloads).",
        frameworks={"nist": ["AU-11", "AU-6"]},
        remediation_command="az monitor log-analytics workspace update --workspace-name <name> --resource-group <rg> --retention-time 30",
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
    _metric_check(
        "perf_vm_cpu_saturated",
        "performance",
        "Virtual machines with sustained high CPU",
        "VMs whose average CPU exceeded 85% over the last week are likely under-provisioned, "
        "risking latency, queueing, and throttling under load.",
        "warning",
        ["microsoft.compute/virtualmachines"],
        {
            "metric": "Percentage CPU",
            "aggregation": "Average",
            "evaluate": "avg",
            "comparison": "gt",
            "threshold": 85.0,
            "lookback_days": 7,
            "interval": "PT1H",
            "unit": "%",
        },
        "Resize to a larger SKU or scale out (e.g. via a scale set) to add CPU headroom.",
        remediation_command="az vm resize --name <name> --resource-group <rg> --size <larger-sku>",
    ),
]


# ===================== CIS MICROSOFT AZURE FOUNDATIONS BENCHMARK v5.0.0 =====================
# Automated recommendations from the CIS Azure Foundations Benchmark v5.0.0 "Combined
# Profiles" sheet (Assessment Status = Automated). Each control carries its exact v5
# recommendation number and profile level (L1 = baseline, L2 = defense-in-depth) so the
# compliance view can slice by profile. Three evaluation shapes are used:
#   - graph violation: flag resources whose config violates the control (the default).
#   - graph existence (expectation="present"): flag in-scope subscriptions MISSING a required
#     control (Defender plans, activity-log alerts, Bastion, App Insights, security contacts).
#   - manual: data-plane / Entra-tenant controls not queryable via Resource Graph — surfaced
#     as a reviewer attestation so they're tracked and scored once answered.
# Controls that exactly duplicate an existing shipped check are NOT re-authored here; their
# v5 number is appended to the existing check's `cis` mapping via _CIS_V5_ALIAS below.

# CIS L1/L2 profile → default severity for the security pillar score.
_CIS_SEV = {"L1": "error", "L2": "warning"}


def _cis(
    rec: str, profile: str, title: str, description: str,
    resource_types: list[str], kql: str, remediation: str,
    *, remediation_command: str = "", arg_table: str = "Resources",
    expectation: str = "", severity: str | None = None, learn_more: list[str] | None = None,
    scope_mode: str = "",
) -> dict[str, Any]:
    """Construct a CIS v5 control (graph violation or existence). The check id is derived
    from the recommendation number (e.g. 9.2.1 → cis_9_2_1)."""
    return _check(
        "cis_" + rec.replace(".", "_"),
        "security",
        title,
        description,
        severity or _CIS_SEV.get(profile, "warning"),
        resource_types,
        kql,
        remediation,
        frameworks={"cis": [f"CIS Azure {rec}"]},
        remediation_command=remediation_command,
        kind="graph",
        source="cis-v5",
        profile=profile,
        arg_table=arg_table,
        expectation=expectation,
        learn_more=learn_more or [],
        scope_mode=scope_mode,
    )


def _cis_manual(
    rec: str, profile: str, title: str, description: str, remediation: str,
    *, resource_types: list[str] | None = None, learn_more: list[str] | None = None,
) -> dict[str, Any]:
    """A CIS v5 control that can't be evaluated from Resource Graph (data-plane key/secret
    expiry, diagnostic settings, Entra tenant policy). Surfaced as a reviewer attestation."""
    return _manual_check(
        "cis_" + rec.replace(".", "_"),
        "security",
        title,
        description,
        _CIS_SEV.get(profile, "warning"),
        resource_types or [],
        remediation,
        frameworks={"cis": [f"CIS Azure {rec}"]},
        source="cis-v5",
        profile=profile,
        learn_more=learn_more or [],
    )


def _cis_graph(
    rec: str, profile: str, title: str, description: str, remediation: str,
    *, path: str, field: str, op: str, expected: Any = None,
    resource_types: list[str] | None = None, learn_more: list[str] | None = None,
) -> dict[str, Any]:
    """A CIS v5 Entra tenant-identity control evaluated via Microsoft Graph (not Resource
    Graph). The runner GETs ``path`` and PASSes when ``field`` satisfies ``op``/``expected``;
    otherwise the whole Entra tenant is flagged as the failing subject."""
    c = _check(
        "cis_" + rec.replace(".", "_"),
        "security", title, description,
        _CIS_SEV.get(profile, "warning"),
        resource_types or [],
        "",  # no KQL — evaluated via Microsoft Graph
        remediation,
        frameworks={"cis": [f"CIS Azure {rec}"]},
        kind="graph_api",
        source="cis-v5",
        profile=profile,
        learn_more=learn_more or [],
    )
    c["graph_check"] = {"path": path, "field": field, "op": op, "expected": expected}
    return c


def _cis_rest(
    rec: str, profile: str, title: str, description: str, remediation: str,
    *, mode: str, categories: list[str] | None = None,
    resource_types: list[str] | None = None, learn_more: list[str] | None = None,
    remediation_command: str = "",
) -> dict[str, Any]:
    """A CIS v5 control evaluated via control-plane ARM REST, because the underlying config
    (diagnostic settings, App Service HTTP logs) is not surfaced in Resource Graph. ``mode``
    selects the runner's evaluator (diag_exists | diag_categories | app_httplogs)."""
    c = _check(
        "cis_" + rec.replace(".", "_"),
        "security", title, description,
        _CIS_SEV.get(profile, "warning"),
        resource_types or [],
        "",  # no KQL — evaluated via ARM REST
        remediation,
        frameworks={"cis": [f"CIS Azure {rec}"]},
        remediation_command=remediation_command,
        kind="arm_rest",
        source="cis-v5",
        profile=profile,
        learn_more=learn_more or [],
    )
    c["rest_check"] = {"mode": mode, "categories": list(categories or [])}
    return c


def _cis_defender(rec: str, plan: str, plan_label: str) -> dict[str, Any]:
    """A CIS v5 Microsoft Defender for Cloud plan control (all L2): existence check that flags
    each in-scope subscription whose Defender plan ``plan`` is not on the Standard tier."""
    return _cis(
        rec, "L2",
        f"Microsoft Defender for {plan_label} is not enabled",
        f"CIS {rec}: Microsoft Defender for {plan_label} should be set to 'On' (Standard tier) "
        f"on every subscription to provide threat protection for {plan_label}.",
        [],
        f"| where type =~ 'microsoft.security/pricings' and name =~ '{plan}' "
        "| where tostring(properties.pricingTier) =~ 'Standard' "
        "| project subscriptionId",
        f"Enable Microsoft Defender for {plan_label} (Standard tier) on the subscription.",
        remediation_command=f"az security pricing create -n {plan} --tier Standard",
        arg_table="securityresources",
        expectation="present",
        learn_more=["https://learn.microsoft.com/azure/defender-for-cloud/enable-enhanced-security"],
    )


def _cis_activity_alert(rec: str, op: str, label: str) -> dict[str, Any]:
    """A CIS v5 Activity Log Alert existence control (all L1): flags each in-scope subscription
    that has no enabled Activity Log alert on the administrative operation ``op``."""
    op_l = op.lower()
    return _cis(
        rec, "L1",
        f"No Activity Log Alert for '{label}'",
        f"CIS {rec}: an Activity Log Alert should exist for '{label}' so that "
        f"administrative changes are detected and investigated.",
        [],
        "| where type =~ 'microsoft.insights/activitylogalerts' "
        "| where tobool(properties.enabled) == true "
        "| mv-expand cond = properties.condition.allOf "
        "| where tostring(cond.field) =~ 'operationName' "
        f"| where tostring(cond['equals']) =~ '{op_l}' "
        "| project subscriptionId",
        f"Create an Activity Log Alert for the operation '{op}'.",
        arg_table="Resources",
        expectation="present",
        learn_more=["https://learn.microsoft.com/azure/azure-monitor/alerts/activity-log-alerts"],
    )


def _cis_no_private_endpoint(rec: str, profile: str, target_type: str, label: str) -> dict[str, Any]:
    """A CIS v5 'use private endpoints' control: flag in-scope resources of ``target_type``
    that have NO private endpoint pointing at them. The private-endpoint sub-query is
    deliberately unscoped (a PE can live in a different resource group than its target)."""
    return _cis(
        rec, profile,
        f"{label} not accessed via a private endpoint",
        f"CIS {rec}: {label} should be reachable only over a private endpoint. This flags "
        f"{label.lower()} that have no private endpoint targeting them.",
        [target_type],
        f"| where type =~ '{target_type}' "
        "| extend _lid = tolower(id) "
        "| join kind=leftouter (resources "
        "| where type =~ 'microsoft.network/privateendpoints' "
        "| mv-expand _c = properties.privateLinkServiceConnections "
        "| project _tid = tolower(tostring(_c.properties.privateLinkServiceId)) "
        "| where isnotempty(_tid) | distinct _tid) on $left._lid == $right._tid "
        "| where isempty(_tid) "
        "| project id, name, type, resourceGroup, subscriptionId",
        f"Create a private endpoint for the {label.lower()} and disable public network access.",
        learn_more=["https://learn.microsoft.com/azure/private-link/private-endpoint-overview"],
    )


def _cis_lock(
    rec: str, profile: str, level: str, types: list[str], label: str, summary: str,
) -> dict[str, Any]:
    """A CIS v5 'resource locks' control: flag in-scope resources of the given ``types`` that
    are NOT protected by a management lock of ``level`` ('CanNotDelete' or 'ReadOnly') at
    their own scope OR an inherited (resource-group / subscription) scope. Lock inheritance is
    resolved by matching any lock whose scope is a prefix of the resource id (joined within the
    subscription, then prefix-filtered, so an RG/subscription lock correctly covers children)."""
    type_list = ", ".join(f"'{t}'" for t in types)
    return _cis(
        rec, profile, label,
        summary,
        types,
        f"| where type in~ ({type_list}) "
        "| extend _rid = tolower(id) "
        "| join kind=leftouter (resources "
        "| where type =~ 'microsoft.authorization/locks' "
        f"| where tostring(properties.level) =~ '{level}' "
        "| extend _lscope = tolower(tostring(split(id, '/providers/Microsoft.Authorization/locks/')[0])) "
        "| project subscriptionId, _lscope) on subscriptionId "
        "| extend _covered = iff(isempty(_lscope), false, _rid startswith _lscope) "
        "| summarize _anyCov = max(_covered) by id, name, type, resourceGroup, subscriptionId "
        "| where _anyCov != true "
        "| project id, name, type, resourceGroup, subscriptionId",
        f"Apply a '{level}' management lock to the resource (or its resource group / subscription).",
        remediation_command=(
            f"az lock create --name <lockName> --lock-type {level} "
            "--resource-name <name> --resource-group <rg> --resource-type <type>"
        ),
        learn_more=["https://learn.microsoft.com/azure/azure-resource-manager/management/lock-resources"],
    )


_CIS_V5: list[dict[str, Any]] = [
    # ---------- 2.x Analytics — Azure Databricks ----------
    _cis(
        "2.1.1", "L1",
        "Azure Databricks not deployed in a customer-managed VNet",
        "CIS 2.1.1: Databricks workspaces should be deployed into a customer-managed VNet "
        "(VNet injection) for network control and private connectivity.",
        ["microsoft.databricks/workspaces"],
        "| where type =~ 'microsoft.databricks/workspaces' "
        "| where isempty(tostring(properties.parameters.customVirtualNetworkId.value)) "
        f"{_PROJECT}",
        "Redeploy the Databricks workspace with VNet injection (customer-managed VNet).",
    ),
    _cis(
        "2.1.9", "L1",
        "Azure Databricks 'No Public IP' is not enabled",
        "CIS 2.1.9: 'No Public IP' (secure cluster connectivity) should be enabled so Databricks "
        "cluster nodes have no public IPs.",
        ["microsoft.databricks/workspaces"],
        "| where type =~ 'microsoft.databricks/workspaces' "
        "| where tobool(properties.parameters.enableNoPublicIp.value) != true "
        f"{_PROJECT}",
        "Redeploy the workspace with Secure Cluster Connectivity ('No Public IP') enabled.",
    ),
    _cis(
        "2.1.10", "L1",
        "Azure Databricks allows public network access",
        "CIS 2.1.10: 'Allow Public Network Access' should be Disabled so the Databricks "
        "workspace is reachable only over private endpoints.",
        ["microsoft.databricks/workspaces"],
        "| where type =~ 'microsoft.databricks/workspaces' "
        "| where tostring(properties.publicNetworkAccess) =~ 'Enabled' or isnull(properties.publicNetworkAccess) "
        f"{_PROJECT}",
        "Set Public Network Access to Disabled and use private endpoints.",
    ),
    # ---------- 5.x Identity — RBAC ----------
    _cis(
        "5.27", "L1",
        "Subscriptions without between 2 and 3 Owners",
        "CIS 5.27: each subscription should have a minimum of 2 and a maximum of 3 Owner role "
        "assignments — too few risks lockout, too many widens privileged exposure.",
        [],
        "| where type =~ 'microsoft.authorization/roleassignments' "
        "| extend roleId = tolower(tostring(properties.roleDefinitionId)) "
        "| where roleId endswith '8e3af657-a8ff-443c-a75c-2fe8c4bcb635' "  # Owner role definition
        "| where tolower(tostring(properties.scope)) == strcat('/subscriptions/', tolower(subscriptionId)) "
        "| summarize owners = count() by subscriptionId "
        "| where owners < 2 or owners > 3 "
        "| project id = strcat('/subscriptions/', subscriptionId), name = subscriptionId, "
        "type = 'microsoft.resources/subscriptions', resourceGroup = '', subscriptionId",
        "Adjust subscription Owner assignments to between 2 and 3 accounts.",
        arg_table="authorizationresources",
    ),
    # ---------- 6.1.2.x Activity Log Alerts (existence) ----------
    _cis_activity_alert("6.1.2.1", "Microsoft.Authorization/policyAssignments/write", "Create Policy Assignment"),
    _cis_activity_alert("6.1.2.2", "Microsoft.Authorization/policyAssignments/delete", "Delete Policy Assignment"),
    _cis_activity_alert("6.1.2.3", "Microsoft.Network/networkSecurityGroups/write", "Create or Update NSG"),
    _cis_activity_alert("6.1.2.4", "Microsoft.Network/networkSecurityGroups/delete", "Delete NSG"),
    _cis_activity_alert("6.1.2.5", "Microsoft.Security/securitySolutions/write", "Create or Update Security Solution"),
    _cis_activity_alert("6.1.2.6", "Microsoft.Security/securitySolutions/delete", "Delete Security Solution"),
    _cis_activity_alert("6.1.2.7", "Microsoft.Sql/servers/firewallRules/write", "Create or Update SQL Server Firewall Rule"),
    _cis_activity_alert("6.1.2.8", "Microsoft.Sql/servers/firewallRules/delete", "Delete SQL Server Firewall Rule"),
    _cis_activity_alert("6.1.2.9", "Microsoft.Network/publicIPAddresses/write", "Create or Update Public IP Address"),
    _cis_activity_alert("6.1.2.10", "Microsoft.Network/publicIPAddresses/delete", "Delete Public IP Address"),
    _cis(
        "6.1.2.11", "L1",
        "No Activity Log Alert for Service Health",
        "CIS 6.1.2.11: a Service Health activity log alert should exist so the team is notified "
        "of Azure service incidents, planned maintenance, and health advisories.",
        [],
        "| where type =~ 'microsoft.insights/activitylogalerts' "
        "| where tobool(properties.enabled) == true "
        "| mv-expand cond = properties.condition.allOf "
        "| where tostring(cond.field) =~ 'category' and tostring(cond['equals']) =~ 'ServiceHealth' "
        "| project subscriptionId",
        "Create a Service Health activity log alert with an action group.",
        expectation="present",
    ),
    # ---------- 6.1.3.1 Application Insights (existence) ----------
    _cis(
        "6.1.3.1", "L2",
        "No Application Insights configured in the subscription",
        "CIS 6.1.3.1: Application Insights should be configured to provide application-level "
        "telemetry, performance, and failure diagnostics.",
        [],
        "| where type =~ 'microsoft.insights/components' | project subscriptionId",
        "Create an Application Insights resource and instrument your applications.",
        expectation="present",
    ),
    _cis(
        "6.1.1.5", "L2",
        "NSG flow logs disabled or not sent to Log Analytics",
        "CIS 6.1.1.5: Network Security Group flow logs should be captured and sent to Log "
        "Analytics. This flags NSG-targeted flow logs that are disabled or have no Log Analytics "
        "(traffic analytics) workspace configured. (NSGs with no flow log at all aren't surfaced "
        "by this check — create a flow log for every NSG.)",
        ["microsoft.network/networkwatchers/flowlogs"],
        "| where type =~ 'microsoft.network/networkwatchers/flowlogs' "
        "| where tolower(tostring(properties.targetResourceId)) has 'networksecuritygroups' "
        "| extend _en = tobool(properties.enabled), "
        "_ws = tostring(properties.flowAnalyticsConfiguration.networkWatcherFlowAnalyticsConfiguration.workspaceResourceId) "
        "| where _en != true or isempty(_ws) "
        "| project id, name = tostring(split(id, '/')[-1]), type, resourceGroup, subscriptionId",
        "Enable the NSG flow log and configure traffic analytics to a Log Analytics workspace.",
    ),
    _cis(
        "6.1.5", "L2",
        "Monitored production resources on Basic/Free/Consumption SKUs",
        "CIS 6.1.5: Basic, Free, Shared, and Consumption SKUs should not be used on production "
        "artifacts that require monitoring and an SLA — these tiers lack the diagnostic depth and "
        "availability guarantees of Standard/Premium. Review flagged resources and upgrade those "
        "that are production workloads.",
        [
            "microsoft.web/serverfarms",
            "microsoft.cache/redis",
            "microsoft.dbforpostgresql/flexibleservers",
            "microsoft.dbformysql/flexibleservers",
            "microsoft.sql/servers/databases",
            "microsoft.containerregistry/registries",
        ],
        "| where type in~ ('microsoft.web/serverfarms', 'microsoft.cache/redis', "
        "'microsoft.dbforpostgresql/flexibleservers', 'microsoft.dbformysql/flexibleservers', "
        "'microsoft.sql/servers/databases', 'microsoft.containerregistry/registries') "
        "| extend _tier = tostring(sku.tier), _sku = tostring(sku.name) "
        "| where _tier in~ ('Free', 'Basic', 'Shared', 'Consumption') "
        "or _sku in~ ('Free', 'Basic', 'Shared', 'Consumption', 'F1', 'D1', 'Y1') "
        "| project id, name, type, resourceGroup, subscriptionId",
        "Move production resources to a Standard/Premium SKU that meets your monitoring and SLA needs.",
    ),
    _cis_lock(
        "6.2", "L2", "CanNotDelete",
        [
            "microsoft.compute/virtualmachines",
            "microsoft.sql/servers",
            "microsoft.keyvault/vaults",
            "microsoft.documentdb/databaseaccounts",
        ],
        "Mission-critical resources without a delete lock",
        "CIS 6.2: resource locks should be set for mission-critical Azure resources so they "
        "cannot be accidentally deleted. This flags critical resource types (VMs, SQL servers, "
        "Key Vaults, Cosmos DB) with no 'CanNotDelete' lock at their own, resource-group, or "
        "subscription scope. Review and lock the ones that are genuinely mission-critical.",
    ),
    # ---------- 7.x Networking ----------
    _cis(
        "7.3", "L1",
        "NSGs allow inbound UDP from the internet",
        "CIS 7.3: NSG rules permitting inbound UDP from any source (Internet / 0.0.0.0/0) expose "
        "UDP services to the internet and should be evaluated and restricted.",
        ["microsoft.network/networksecuritygroups"],
        "| where type =~ 'microsoft.network/networksecuritygroups' "
        "| mv-expand rule = properties.securityRules "
        "| extend dir = tostring(rule.properties.direction), acc = tostring(rule.properties.access), "
        "prot = tostring(rule.properties.protocol), src = tostring(rule.properties.sourceAddressPrefix) "
        "| where dir =~ 'Inbound' and acc =~ 'Allow' and (prot =~ 'Udp' or prot == '*') "
        "and (src == '*' or src == '0.0.0.0/0' or src =~ 'Internet') "
        "| summarize by id, name, type, resourceGroup, subscriptionId",
        "Restrict inbound UDP rules to specific source IPs; remove 0.0.0.0/0 / Internet allow rules.",
    ),
    _cis(
        "7.10", "L2",
        "Application Gateway without WAF enabled",
        "CIS 7.10: internet-facing Application Gateways should run the WAF_v2 tier so a Web "
        "Application Firewall inspects traffic.",
        ["microsoft.network/applicationgateways"],
        "| where type =~ 'microsoft.network/applicationgateways' "
        "| where tostring(properties.sku.tier) !in~ ('WAF', 'WAF_v2') "
        f"{_PROJECT}",
        "Upgrade the Application Gateway to the WAF_v2 tier and attach a WAF policy.",
    ),
    _cis(
        "7.11", "L1",
        "Subnets not associated with a network security group",
        "CIS 7.11: every subnet (except the gateway/AzureBastion/AzureFirewall subnets) should "
        "be associated with an NSG to filter traffic.",
        ["microsoft.network/virtualnetworks"],
        "| where type =~ 'microsoft.network/virtualnetworks' "
        "| mv-expand subnet = properties.subnets "
        "| extend sname = tostring(subnet.name), nsg = tostring(subnet.properties.networkSecurityGroup.id) "
        "| where sname !in~ ('GatewaySubnet', 'AzureBastionSubnet', 'AzureFirewallSubnet', 'RouteServerSubnet') "
        "| where isempty(nsg) "
        "| project id, name = strcat(name, '/', sname), type, resourceGroup, subscriptionId",
        "Associate each workload subnet with an appropriately-scoped NSG.",
    ),
    _cis(
        "7.12", "L1",
        "Application Gateway SSL policy allows TLS below 1.2",
        "CIS 7.12: the Application Gateway SSL policy minimum protocol version should be TLSv1_2 "
        "(or higher) to reject weak, deprecated TLS.",
        ["microsoft.network/applicationgateways"],
        "| where type =~ 'microsoft.network/applicationgateways' "
        "| extend minv = tostring(properties.sslPolicy.minProtocolVersion) "
        "| where minv in~ ('TLSv1_0', 'TLSv1_1') "
        f"{_PROJECT}",
        "Set the Application Gateway SSL policy minimum protocol version to TLSv1_2 or higher.",
    ),
    _cis(
        "7.13", "L1",
        "Application Gateway without HTTP/2 enabled",
        "CIS 7.13: HTTP/2 should be enabled on Application Gateways for performance and to match "
        "modern client expectations.",
        ["microsoft.network/applicationgateways"],
        "| where type =~ 'microsoft.network/applicationgateways' "
        "| where tobool(properties.enableHttp2) != true "
        f"{_PROJECT}",
        "Enable HTTP/2 on the Application Gateway.",
    ),
    _cis(
        "7.14", "L2",
        "WAF policy without request body inspection",
        "CIS 7.14: Application Gateway WAF policies should have request body inspection enabled "
        "so request payloads are evaluated by the firewall.",
        ["microsoft.network/applicationgatewaywebapplicationfirewallpolicies"],
        "| where type =~ 'microsoft.network/applicationgatewaywebapplicationfirewallpolicies' "
        "| where tobool(properties.policySettings.requestBodyCheck) != true "
        f"{_PROJECT}",
        "Enable request body inspection in the WAF policy's policy settings.",
    ),
    # ---------- 8.1.x Microsoft Defender for Cloud plans (existence, all L2) ----------
    _cis_defender("8.1.1.1", "CloudPosture", "Cloud Security Posture Management (CSPM)"),
    _cis_defender("8.1.2.1", "Api", "APIs"),
    _cis_defender("8.1.3.1", "VirtualMachines", "Servers"),
    _cis_defender("8.1.4.1", "Containers", "Containers"),
    _cis_defender("8.1.5.1", "StorageAccounts", "Storage"),
    _cis_defender("8.1.6.1", "AppServices", "App Service"),
    _cis_defender("8.1.7.1", "CosmosDbs", "Azure Cosmos DB"),
    _cis_defender("8.1.7.2", "OpenSourceRelationalDatabases", "Open-Source Relational Databases"),
    _cis_defender("8.1.7.3", "SqlServers", "Azure SQL Databases"),
    _cis_defender("8.1.7.4", "SqlServerVirtualMachines", "SQL Servers on Machines"),
    _cis_defender("8.1.8.1", "KeyVaults", "Key Vault"),
    _cis_defender("8.1.9.1", "Arm", "Resource Manager"),
    _cis(
        "8.1.13", "L1",
        "No Security Contact email configured",
        "CIS 8.1.13: a Security Contact email should be configured in Microsoft Defender for "
        "Cloud so security alerts and attack-path notifications reach the right people.",
        [],
        "| where type =~ 'microsoft.security/securitycontacts' "
        "| where isnotempty(tostring(properties.emails)) or isnotempty(tostring(properties.email)) "
        "| project subscriptionId",
        "Configure a Security Contact email (and notification settings) in Defender for Cloud.",
        arg_table="securityresources",
        expectation="present",
    ),
    # ---------- 8.3.x Key Vault config ----------
    # 8.3.5 purge protection, 8.3.6 RBAC, 8.3.7 public network access overlap existing checks
    # (see _CIS_V5_ALIAS). The data-plane ones (key/secret expiry, rotation) are manual below.
    # ---------- 8.4.1 Azure Bastion (existence) ----------
    _cis(
        "8.4.1", "L2",
        "No Azure Bastion host in the subscription",
        "CIS 8.4.1: an Azure Bastion host should exist so administrators reach VMs over TLS "
        "without exposing RDP/SSH to the internet.",
        [],
        "| where type =~ 'microsoft.network/bastionhosts' | project subscriptionId",
        "Deploy an Azure Bastion host and use it for VM management.",
        expectation="present",
    ),
    # ---------- 8.5 DDoS Network Protection ----------
    _cis(
        "8.5", "L2",
        "Virtual networks without Azure DDoS Network Protection",
        "CIS 8.5: Azure DDoS Network Protection should be enabled on virtual networks that host "
        "internet-facing workloads to absorb volumetric attacks.",
        ["microsoft.network/virtualnetworks"],
        "| where type =~ 'microsoft.network/virtualnetworks' "
        "| where tobool(properties.enableDdosProtection) != true "
        f"{_PROJECT}",
        "Enable Azure DDoS Network Protection on the virtual network.",
    ),
    # ---------- 9.x Storage ----------
    _cis(
        "9.1.1", "L1",
        "Azure File Shares without soft delete enabled",
        "CIS 9.1.1: soft delete for Azure File Shares should be enabled so deleted shares can be "
        "recovered within the retention period.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts/fileservices' "
        "| where tobool(properties.shareDeleteRetentionPolicy.enabled) != true "
        "| project id, name = split(id, '/')[8], type, resourceGroup, subscriptionId",
        "Enable soft delete for file shares with a sufficient retention period.",
    ),
    _cis(
        "9.1.2", "L1",
        "SMB file shares allowing SMB protocol below 3.1.1",
        "CIS 9.1.2: the SMB 'protocol version' for Azure file shares should be SMB 3.1.1 or higher. "
        "Leaving the default (or allowing SMB 2.1 / 3.0) permits weaker, less secure dialects.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts/fileservices' "
        "| extend _smbVer = tostring(properties.protocolSettings.smb.versions) "
        "| where isempty(_smbVer) or _smbVer has 'SMB2.1' or _smbVer has 'SMB3.0' "
        "| project id, name = split(id, '/')[8], type, resourceGroup, subscriptionId",
        "Set the file service SMB protocol version to 'SMB3.1.1' only (remove SMB2.1 / SMB3.0).",
        remediation_command=(
            "az storage account file-service-properties update --account-name <sa> "
            "--resource-group <rg> --versions SMB3.1.1"
        ),
    ),
    _cis(
        "9.1.3", "L1",
        "SMB file shares allowing weaker channel encryption than AES-256-GCM",
        "CIS 9.1.3: the SMB 'channel encryption' for Azure file shares should be AES-256-GCM or "
        "higher. Leaving the default (or allowing AES-128-CCM / AES-128-GCM) permits weaker "
        "in-transit encryption.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts/fileservices' "
        "| extend _enc = tostring(properties.protocolSettings.smb.channelEncryption) "
        "| where isempty(_enc) or _enc has 'AES-128' "
        "| project id, name = split(id, '/')[8], type, resourceGroup, subscriptionId",
        "Set the file service SMB channel encryption to 'AES-256-GCM' only.",
        remediation_command=(
            "az storage account file-service-properties update --account-name <sa> "
            "--resource-group <rg> --channel-encryption AES-256-GCM"
        ),
    ),
    _cis(
        "9.2.1", "L1",
        "Blob storage without blob soft delete enabled",
        "CIS 9.2.1: soft delete for blobs should be enabled so deleted blobs can be recovered "
        "within the retention period.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts/blobservices' "
        "| where tobool(properties.deleteRetentionPolicy.enabled) != true "
        "| project id, name = split(id, '/')[8], type, resourceGroup, subscriptionId",
        "Enable soft delete for blobs with a sufficient retention period.",
    ),
    _cis(
        "9.2.2", "L1",
        "Blob storage without container soft delete enabled",
        "CIS 9.2.2: soft delete for containers should be enabled so deleted containers can be "
        "recovered within the retention period.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts/blobservices' "
        "| where tobool(properties.containerDeleteRetentionPolicy.enabled) != true "
        "| project id, name = split(id, '/')[8], type, resourceGroup, subscriptionId",
        "Enable soft delete for containers with a sufficient retention period.",
    ),
    _cis(
        "9.2.3", "L2",
        "Blob storage without versioning enabled",
        "CIS 9.2.3: blob versioning should be enabled to automatically retain previous versions "
        "of objects for recovery from modification or deletion.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts/blobservices' "
        "| where tobool(properties.isVersioningEnabled) != true "
        "| project id, name = split(id, '/')[8], type, resourceGroup, subscriptionId",
        "Enable blob versioning on the storage account.",
    ),
    _cis(
        "9.3.5", "L2",
        "Storage accounts not allowing trusted Azure services",
        "CIS 9.3.5: 'Allow Azure services on the trusted services list to access this storage "
        "account' should be enabled so trusted first-party services keep working under a deny "
        "firewall default.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tostring(properties.networkAcls.defaultAction) =~ 'Deny' "
        "| where tostring(properties.networkAcls.bypass) !has 'AzureServices' "
        f"{_PROJECT}",
        "Set the storage firewall to allow trusted Microsoft services (bypass = AzureServices).",
    ),
    _cis(
        "9.3.7", "L1",
        "Storage accounts with cross-tenant replication enabled",
        "CIS 9.3.7: cross-tenant object replication should be disabled to prevent data being "
        "replicated to a storage account in another tenant.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tobool(properties.allowCrossTenantReplication) == true "
        f"{_PROJECT}",
        "Disable cross-tenant replication on the storage account.",
        remediation_command="az storage account update --name <name> --resource-group <rg> --allow-cross-tenant-replication false",
    ),
    _cis(
        "9.3.2.2", "L1",
        "Storage accounts allow public network access",
        "CIS 9.3.2.2: 'Public Network Access' should be Disabled so the storage account is "
        "reachable only over private endpoints / selected networks.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tostring(properties.publicNetworkAccess) =~ 'Enabled' "
        f"{_PROJECT}",
        "Set Public Network Access to Disabled and use private endpoints.",
        remediation_command="az storage account update --name <name> --resource-group <rg> --public-network-access Disabled",
    ),
    _cis(
        "9.3.3.1", "L1",
        "Storage accounts not defaulting to Microsoft Entra authorization",
        "CIS 9.3.3.1: 'Default to Microsoft Entra authorization in the Azure portal' should be "
        "enabled so the portal uses identity-based access rather than account keys.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tobool(properties.defaultToOAuthAuthentication) != true "
        f"{_PROJECT}",
        "Enable 'Default to Microsoft Entra authorization' on the storage account.",
        remediation_command="az storage account update --name <name> --resource-group <rg> --default-to-oauth-authentication true",
    ),
    _cis(
        "9.3.11", "L2",
        "Critical storage accounts not geo-redundant (GRS)",
        "CIS 9.3.11: critical storage accounts should use geo-redundant storage (GRS/RA-GRS/"
        "GZRS) so data survives a regional outage.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tostring(sku.name) in~ ('Standard_LRS', 'Standard_ZRS', 'Premium_LRS', 'Premium_ZRS') "
        f"{_PROJECT}",
        "Reconfigure critical storage accounts to a geo-redundant SKU (GRS / RA-GRS / GZRS).",
    ),
    _cis_lock(
        "9.3.9", "L1", "CanNotDelete",
        ["microsoft.storage/storageaccounts"],
        "Storage accounts without a delete lock",
        "CIS 9.3.9: an Azure Resource Manager 'CanNotDelete' lock should be applied to storage "
        "accounts so they cannot be accidentally or maliciously deleted. This flags storage "
        "accounts with no 'CanNotDelete' lock at their own, resource-group, or subscription scope.",
    ),
    _cis_lock(
        "9.3.10", "L2", "ReadOnly",
        ["microsoft.storage/storageaccounts"],
        "Storage accounts without a read-only lock",
        "CIS 9.3.10: an Azure Resource Manager 'ReadOnly' lock should be considered for storage "
        "accounts whose configuration must not change. This flags storage accounts with no "
        "'ReadOnly' lock at their own, resource-group, or subscription scope. (ReadOnly is "
        "restrictive — apply only where appropriate.)",
    ),
    # ---------- Phase 3: data-plane controls (manual attestation) ----------
    _cis_manual(
        "8.3.1", "L1", "Expiration date set for all keys in RBAC Key Vaults",
        "CIS 8.3.1: every key in RBAC-model Key Vaults should have an expiration date. Key/secret "
        "expiry lives in the Key Vault data plane and isn't queryable via Resource Graph.",
        "Set an expiration date on every Key Vault key; enforce via policy.",
        resource_types=["microsoft.keyvault/vaults"],
    ),
    _cis_manual(
        "8.3.2", "L1", "Expiration date set for all keys in non-RBAC Key Vaults",
        "CIS 8.3.2: every key in access-policy (non-RBAC) Key Vaults should have an expiration date.",
        "Set an expiration date on every Key Vault key.",
        resource_types=["microsoft.keyvault/vaults"],
    ),
    _cis_manual(
        "8.3.3", "L1", "Expiration date set for all secrets in RBAC Key Vaults",
        "CIS 8.3.3: every secret in RBAC-model Key Vaults should have an expiration date.",
        "Set an expiration date on every Key Vault secret.",
        resource_types=["microsoft.keyvault/vaults"],
    ),
    _cis_manual(
        "8.3.4", "L1", "Expiration date set for all secrets in non-RBAC Key Vaults",
        "CIS 8.3.4: every secret in access-policy (non-RBAC) Key Vaults should have an expiration date.",
        "Set an expiration date on every Key Vault secret.",
        resource_types=["microsoft.keyvault/vaults"],
    ),
    _cis_manual(
        "8.3.9", "L2", "Automatic key rotation enabled in Key Vault",
        "CIS 8.3.9: automatic key rotation should be configured on Key Vault keys (data-plane "
        "rotation policy, not in Resource Graph).",
        "Configure an automatic rotation policy on Key Vault keys.",
        resource_types=["microsoft.keyvault/vaults"],
    ),
    _cis_manual(
        "8.3.11", "L1", "Key Vault certificate validity period ≤ 12 months",
        "CIS 8.3.11: certificate validity period should be 12 months or less (data-plane "
        "certificate policy, not in Resource Graph).",
        "Set certificate policy validity to ≤ 12 months and reissue long-lived certificates.",
        resource_types=["microsoft.keyvault/vaults"],
    ),
    _cis_no_private_endpoint("8.3.8", "L2", "microsoft.keyvault/vaults", "Key Vaults"),
    _cis_manual(
        "9.3.1.1", "L1", "Storage Account key rotation reminders enabled",
        "CIS 9.3.1.1: 'Enable key rotation reminders' should be on for each storage account "
        "(account key policy, not reliably in Resource Graph).",
        "Enable key-rotation reminders on the storage account.",
        resource_types=["microsoft.storage/storageaccounts"],
    ),
    _cis(
        "9.3.1.2", "L1",
        "Storage Account access keys not regenerated in 90 days",
        "CIS 9.3.1.2: storage account access keys should be regenerated periodically. This flags "
        "accounts whose oldest key was created more than 90 days ago.",
        ["microsoft.storage/storageaccounts"],
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| extend _k1 = todatetime(properties.keyCreationTime.key1), _k2 = todatetime(properties.keyCreationTime.key2) "
        "| extend _oldest = min_of(_k1, _k2) "
        "| where isnotempty(_oldest) and _oldest < ago(90d) "
        f"{_PROJECT}",
        "Regenerate (rotate) the storage account access keys; automate rotation on a schedule.",
    ),
    _cis_no_private_endpoint("9.3.2.1", "L2", "microsoft.storage/storageaccounts", "Storage Accounts"),
    _cis_rest(
        "6.1.1.1", "L1", "Diagnostic Setting exists for Subscription Activity Logs",
        "CIS 6.1.1.1: a diagnostic setting should export the subscription Activity Log to a Log "
        "Analytics workspace / storage / Event Hub. Evaluated live via ARM REST per subscription.",
        "Create a subscription Activity Log diagnostic setting capturing all categories.",
        mode="diag_exists",
        learn_more=["https://learn.microsoft.com/azure/azure-monitor/essentials/activity-log"],
    ),
    _cis_rest(
        "6.1.1.2", "L1", "Activity Log diagnostic captures appropriate categories",
        "CIS 6.1.1.2: the Activity Log diagnostic setting should capture Administrative, Alert, "
        "Policy and Security categories. Evaluated live via ARM REST per subscription.",
        "Update the Activity Log diagnostic setting to include all recommended categories.",
        mode="diag_categories",
        categories=["Administrative", "Alert", "Policy", "Security"],
    ),
    _cis_rest(
        "6.1.1.4", "L1", "Logging enabled for Azure Key Vault",
        "CIS 6.1.1.4: AuditEvent diagnostic logging should be enabled for every Key Vault. "
        "Evaluated live via ARM REST (diagnostic settings) per in-scope Key Vault.",
        "Enable AuditEvent diagnostic logging on each Key Vault.",
        mode="diag_resource",
        categories=["AuditEvent"],
        resource_types=["microsoft.keyvault/vaults"],
    ),
    _cis_rest(
        "6.1.1.6", "L2", "App Service 'HTTP logs' logging enabled",
        "CIS 6.1.1.6: App Service HTTP logs should be enabled. Evaluated live via ARM REST "
        "(config/logs) per in-scope App Service.",
        "Enable HTTP logs diagnostic logging on each App Service.",
        mode="app_httplogs",
        resource_types=["microsoft.web/sites"],
    ),
    _cis_manual(
        "8.1.3.3", "L2", "Defender for Servers endpoint protection enabled",
        "CIS 8.1.3.3: the Defender for Servers 'Endpoint protection' (MDE integration) component "
        "should be On (Defender plan extension config, not in ARG).",
        "Enable the Endpoint protection component on the Defender for Servers plan.",
    ),
    _cis_manual(
        "8.1.10", "L1", "Defender for Cloud checks VM OS for updates",
        "CIS 8.1.10: Defender for Cloud should be configured to check VM operating systems for "
        "missing updates (system-updates assessment).",
        "Enable the OS-update assessment in Defender for Cloud.",
    ),
    _cis(
        "8.1.12", "L1",
        "Defender email notifications do not target subscription Owners",
        "CIS 8.1.12: Defender for Cloud email notifications should include the Owner role so "
        "subscription owners receive security alerts. Flags subscriptions whose security contact "
        "does not notify the Owner role.",
        [],
        "| where type =~ 'microsoft.security/securitycontacts' "
        "| where tostring(properties.notificationsByRole.roles) has 'Owner' "
        "| project subscriptionId",
        "Set Defender for Cloud email notifications to include the Owner role.",
        arg_table="securityresources",
        expectation="present",
    ),
    _cis(
        "8.1.14", "L1",
        "Defender alert-severity notifications not enabled",
        "CIS 8.1.14: Defender for Cloud should notify about alerts at a chosen severity (or higher). "
        "Flags subscriptions whose security contact has alert notifications turned off.",
        [],
        "| where type =~ 'microsoft.security/securitycontacts' "
        "| where tostring(properties.alertNotifications.state) =~ 'On' "
        "or tostring(properties.alertNotifications) =~ 'On' "
        "| project subscriptionId",
        "Enable Defender for Cloud alert notifications (severity High or higher).",
        arg_table="securityresources",
        expectation="present",
    ),
    _cis(
        "8.1.15", "L1",
        "Defender attack-path notifications not enabled",
        "CIS 8.1.15: Defender for Cloud should notify about attack paths at a chosen risk level "
        "(or higher). Flags subscriptions whose security contact has no attack-path notification "
        "source enabled.",
        [],
        "| where type =~ 'microsoft.security/securitycontacts' "
        "| mv-expand _src = properties.notificationsSources "
        "| where tostring(_src.sourceType) =~ 'AttackPath' "
        "| project subscriptionId",
        "Configure Defender for Cloud to notify on attack paths at the chosen risk level.",
        arg_table="securityresources",
        expectation="present",
    ),
    _cis(
        "5.23", "L1",
        "Custom roles granting full-control at subscription scope",
        "CIS 5.23: no custom RBAC role should grant full control (action '*') with a subscription "
        "in its assignable scopes — these are de-facto subscription administrator roles that bypass "
        "built-in role review. Evaluated tenant-wide.",
        [],
        "| where type =~ 'microsoft.authorization/roledefinitions' "
        "| where tostring(properties.type) =~ 'CustomRole' "
        "| mv-expand _act = properties.permissions[0].actions "
        "| where tostring(_act) == '*' "
        "| where tostring(properties.assignableScopes) contains '/subscriptions/' "
        "| project id, name = tostring(properties.roleName), type = 'microsoft.authorization/roledefinitions', resourceGroup = '', subscriptionId = '' "
        "| distinct id, name, type, resourceGroup, subscriptionId",
        "Remove full-control custom roles scoped to subscriptions; use least-privilege built-in roles.",
        arg_table="authorizationresources",
        scope_mode="tenant",
    ),
    _cis(
        "5.3.3", "L1",
        "User Access Administrator assigned at subscription scope",
        "CIS 5.3.3: use of the 'User Access Administrator' role should be restricted. This surfaces "
        "each assignment of that role at subscription scope for review.",
        [],
        "| where type =~ 'microsoft.authorization/roleassignments' "
        "| extend _rid = tolower(tostring(properties.roleDefinitionId)) "
        "| where _rid endswith '18d7d88d-d35e-4fb5-a5c3-7773c20a72d9' "
        "| where tolower(tostring(properties.scope)) == strcat('/subscriptions/', tolower(subscriptionId)) "
        "| project id, name = tostring(properties.principalId), type = 'microsoft.authorization/roleassignments', resourceGroup = '', subscriptionId",
        "Review and minimize User Access Administrator assignments; remove unneeded ones.",
        arg_table="authorizationresources",
    ),
    _cis(
        "7.4", "L1",
        "NSGs allow inbound HTTP(S) from the internet",
        "CIS 7.4: NSG rules permitting inbound HTTP/HTTPS (80/443) from any source (Internet / "
        "0.0.0.0/0) expose web endpoints directly; front them with a WAF/firewall and restrict sources.",
        ["microsoft.network/networksecuritygroups"],
        "| where type =~ 'microsoft.network/networksecuritygroups' "
        "| mv-expand rule = properties.securityRules "
        "| extend dir = tostring(rule.properties.direction), acc = tostring(rule.properties.access), "
        "prot = tostring(rule.properties.protocol), src = tostring(rule.properties.sourceAddressPrefix), "
        "ports = strcat(tostring(rule.properties.destinationPortRange), ' ', tostring(rule.properties.destinationPortRanges)) "
        "| where dir =~ 'Inbound' and acc =~ 'Allow' and (prot =~ 'Tcp' or prot == '*') "
        "and (src == '*' or src == '0.0.0.0/0' or src =~ 'Internet') "
        "and (ports has '80' or ports has '443' or ports has '*') "
        "| summarize by id, name, type, resourceGroup, subscriptionId",
        "Restrict internet-facing 80/443 NSG rules; front public web with a WAF and limit sources.",
    ),
    _cis(
        "7.5", "L2",
        "NSG flow logs with retention below 90 days",
        "CIS 7.5: NSG flow log retention should be at least 90 days for adequate forensic history.",
        ["microsoft.network/networkwatchers/flowlogs"],
        "| where type =~ 'microsoft.network/networkwatchers/flowlogs' "
        "| where tolower(tostring(properties.targetResourceId)) has 'networksecuritygroups' "
        "| extend _days = toint(properties.retentionPolicy.days) "
        "| where isnull(_days) or _days < 90 "
        "| project id, name = tostring(split(id, '/')[-1]), type, resourceGroup, subscriptionId",
        "Set NSG flow log retention to at least 90 days.",
    ),
    _cis(
        "7.6", "L2",
        "Regions with VNets but no Network Watcher",
        "CIS 7.6: Network Watcher should be enabled in every region that hosts a virtual network. "
        "This flags (subscription, region) pairs that have VNets but no Network Watcher.",
        ["microsoft.network/virtualnetworks"],
        "| where type =~ 'microsoft.network/virtualnetworks' "
        "| distinct subscriptionId, location "
        "| join kind=leftouter (resources "
        "| where type =~ 'microsoft.network/networkwatchers' "
        "| distinct subscriptionId, location | extend _hasNW = true) on subscriptionId, location "
        "| where isnull(_hasNW) "
        "| project id = strcat('/subscriptions/', subscriptionId, '/providers/Microsoft.Network/locations/', location), "
        "name = location, type = 'microsoft.network/networkwatchers', resourceGroup = '', subscriptionId",
        "Enable Network Watcher in each region that hosts virtual networks.",
    ),
    _cis(
        "7.8", "L2",
        "VNet flow logs with retention below 90 days",
        "CIS 7.8: virtual network flow log retention should be at least 90 days.",
        ["microsoft.network/networkwatchers/flowlogs"],
        "| where type =~ 'microsoft.network/networkwatchers/flowlogs' "
        "| where tolower(tostring(properties.targetResourceId)) has 'virtualnetworks' "
        "| extend _days = toint(properties.retentionPolicy.days) "
        "| where isnull(_days) or _days < 90 "
        "| project id, name = tostring(split(id, '/')[-1]), type, resourceGroup, subscriptionId",
        "Set virtual network flow log retention to at least 90 days.",
    ),
    _cis(
        "7.9", "L2",
        "VPN Gateway point-to-site not restricted to Microsoft Entra ID authentication",
        "CIS 7.9: for Azure VPN Gateway point-to-site configurations, the 'Authentication type' "
        "should be 'Azure Active Directory' (Microsoft Entra ID) only — certificate or RADIUS "
        "authentication is weaker and harder to govern centrally.",
        ["microsoft.network/virtualnetworkgateways"],
        "| where type =~ 'microsoft.network/virtualnetworkgateways' "
        "| where isnotnull(properties.vpnClientConfiguration) "
        "| extend _authTypes = properties.vpnClientConfiguration.vpnAuthenticationTypes "
        "| where isnull(_authTypes) or array_length(_authTypes) != 1 or tostring(_authTypes[0]) !~ 'AAD' "
        f"{_PROJECT}",
        "Configure the VPN Gateway point-to-site 'Authentication type' to Azure Active Directory "
        "(Microsoft Entra ID) only.",
    ),
    _cis(
        "7.16", "L2",
        "No Network Security Perimeter configured in the subscription",
        "CIS 7.16: an Azure Network Security Perimeter (NSP) should be used to secure Azure "
        "Platform-as-a-Service resources behind an explicit network boundary. This flags "
        "subscriptions with no Network Security Perimeter resource.",
        [],
        "| where type =~ 'microsoft.network/networksecurityperimeters' | project subscriptionId",
        "Create a Network Security Perimeter and associate your PaaS resources with it.",
        expectation="present",
        learn_more=["https://learn.microsoft.com/azure/private-link/network-security-perimeter-concepts"],
    ),
    _cis(
        "7.15", "L2",
        "WAF policy without bot protection",
        "CIS 7.15: the Application Gateway WAF policy should enable the bot protection managed rule "
        "set so known malicious bots are blocked.",
        ["microsoft.network/applicationgatewaywebapplicationfirewallpolicies"],
        "| where type =~ 'microsoft.network/applicationgatewaywebapplicationfirewallpolicies' "
        "| where tostring(properties.managedRules.managedRuleSets) !has 'BotManager' "
        f"{_PROJECT}",
        "Add the Bot Manager managed rule set to the WAF policy.",
    ),
    _cis_manual(
        "2.1.2", "L1", "NSGs configured for Databricks subnets",
        "CIS 2.1.2: the Databricks workspace subnets should have NSGs with the required Databricks "
        "rules.",
        "Associate the required NSGs with the Databricks host/container subnets.",
        resource_types=["microsoft.databricks/workspaces"],
    ),
    _cis_rest(
        "2.1.7", "L1", "Diagnostic log delivery configured for Databricks",
        "CIS 2.1.7: diagnostic log delivery should be configured for the Databricks workspace. "
        "Evaluated live via ARM REST (diagnostic settings) per in-scope workspace.",
        "Configure diagnostic settings to deliver Databricks logs to a workspace/storage/Event Hub.",
        mode="diag_resource",
        resource_types=["microsoft.databricks/workspaces"],
    ),
    _cis_no_private_endpoint("2.1.11", "L2", "microsoft.databricks/workspaces", "Azure Databricks workspaces"),
    # ---------- Phase 4: Entra ID tenant policy (Microsoft Graph) ----------
    _cis_graph(
        "5.1.1", "L1", "Microsoft Entra 'security defaults' enabled",
        "CIS 5.1.1: 'security defaults' should be enabled in Microsoft Entra ID (tenant-wide "
        "identity policy). Evaluated live via Microsoft Graph.",
        "Enable security defaults in Microsoft Entra ID (or an equivalent Conditional Access baseline).",
        path="/policies/identitySecurityDefaultsEnforcementPolicy",
        field="isEnabled", op="is_true",
        learn_more=["https://learn.microsoft.com/entra/fundamentals/security-defaults"],
    ),
    _cis_manual(
        "5.1.2", "L1", "Multifactor authentication enabled for all users",
        "CIS 5.1.2: MFA should be enabled for all users. Requires per-user registration state / "
        "Conditional Access evaluation across the tenant, recorded as a reviewer attestation.",
        "Require MFA for all users via Conditional Access or security defaults.",
    ),
    _cis_graph(
        "5.4", "L1", "Restrict non-admin users from creating tenants",
        "CIS 5.4: 'Restrict non-admin users from creating tenants' should be Yes. Evaluated live "
        "via Microsoft Graph (authorizationPolicy.defaultUserRolePermissions.allowedToCreateTenants).",
        "Set 'Restrict non-admin users from creating tenants' to Yes in Entra user settings.",
        path="/policies/authorizationPolicy",
        field="defaultUserRolePermissions.allowedToCreateTenants", op="is_false",
    ),
    _cis_graph(
        "5.14", "L1", "Users cannot register applications",
        "CIS 5.14: 'Users can register applications' should be No. Evaluated live via Microsoft "
        "Graph (authorizationPolicy.defaultUserRolePermissions.allowedToCreateApps).",
        "Set 'Users can register applications' to No in Entra user settings.",
        path="/policies/authorizationPolicy",
        field="defaultUserRolePermissions.allowedToCreateApps", op="is_false",
    ),
    _cis_graph(
        "5.15", "L1", "Guest user access restrictions tightened",
        "CIS 5.15: guest users' access should be restricted to properties and memberships of their "
        "own directory objects (the most restrictive role). Evaluated live via Microsoft Graph "
        "(authorizationPolicy.guestUserRoleId).",
        "Set guest user access to the most restrictive option in external collaboration settings.",
        path="/policies/authorizationPolicy",
        field="guestUserRoleId", op="equals", expected="2af84b1e-32c8-42b7-82bc-daa82404023b",
    ),
    _cis_graph(
        "5.16", "L2", "Guest invite restrictions tightened",
        "CIS 5.16: guest invitations should be limited to specific admin roles (or no one). "
        "Evaluated live via Microsoft Graph (authorizationPolicy.allowInvitesFrom).",
        "Restrict who can invite guests to specific admin roles in external collaboration settings.",
        path="/policies/authorizationPolicy",
        field="allowInvitesFrom", op="in", expected=["none", "adminsAndGuestInviters"],
    ),
]


ALL_CHECKS: list[dict[str, Any]] = (
    _SECURITY + _RELIABILITY + _COST + _OPERATIONS + _PERFORMANCE + _CIS_V5
)

# ISO/IEC 27001:2022 Annex A control mappings, keyed by check id. Added centrally so the
# same run yields a WAF score AND ISO compliance coverage.
_ISO_MAP: dict[str, list[str]] = {
    "sec_storage_public_blob": ["A.5.10", "A.8.3"],
    "sec_storage_https_only": ["A.8.24", "A.5.14"],
    "sec_storage_shared_key": ["A.5.15", "A.8.2"],
    "sec_storage_min_tls": ["A.8.24"],
    "sec_storage_net_default_allow": ["A.8.20", "A.8.22"],
    "sec_nsg_mgmt_open": ["A.8.20", "A.8.22"],
    "sec_nsg_db_ports_open": ["A.8.20", "A.8.22"],
    "sec_public_ip": ["A.8.20", "A.8.22"],
    "sec_kv_purge_protection": ["A.8.24", "A.8.13"],
    "sec_kv_public_network": ["A.8.20", "A.8.24"],
    "sec_kv_rbac": ["A.5.15", "A.8.2"],
    "sec_kv_soft_delete": ["A.8.13"],
    "sec_sql_public_access": ["A.8.20", "A.8.22"],
    "sec_sql_aad_only": ["A.5.15", "A.8.2"],
    "sec_webapp_https_only": ["A.8.24"],
    "sec_webapp_min_tls": ["A.8.24"],
    "sec_webapp_ftps": ["A.8.24"],
    "sec_webapp_no_managed_identity": ["A.5.16"],
    "sec_disk_unencrypted": ["A.8.24"],
    "sec_aks_public_api": ["A.8.20", "A.8.22"],
    "sec_aks_local_accounts": ["A.5.15", "A.8.2"],
    "sec_aks_no_rbac": ["A.8.3"],
    "sec_cosmos_public": ["A.8.20"],
    "sec_cosmos_local_auth": ["A.5.15", "A.8.2"],
    "sec_acr_admin_user": ["A.5.15", "A.8.2"],
    "sec_acr_public_network": ["A.8.20"],
    "rel_vm_no_zone": ["A.5.29", "A.8.14"],
    "rel_storage_lrs": ["A.8.13", "A.8.14"],
    "rel_pip_basic_sku": ["A.8.14"],
    "rel_lb_basic_sku": ["A.8.14"],
    "rel_sql_no_zone": ["A.5.29", "A.8.14"],
    "rel_appplan_single_instance": ["A.8.14"],
    "rel_appservice_no_zone": ["A.8.14"],
    "rel_disk_lrs": ["A.8.13"],
    "rel_cosmos_single_region": ["A.5.29", "A.8.14"],
    "rel_aks_single_nodepool": ["A.8.14"],
    "rel_aks_free_sla": ["A.8.14"],
    "rel_vmss_no_zone": ["A.8.14"],
    "rel_appgw_no_zone": ["A.8.14"],
    "ops_vm_no_boot_diagnostics": ["A.8.15", "A.8.16"],
    "ops_aks_no_monitoring": ["A.8.15", "A.8.16"],
    "ops_law_short_retention": ["A.8.15"],
}

# Microsoft Cloud Security Benchmark (MCSB) v1 control mappings, keyed by check id.
_MCSB_MAP: dict[str, list[str]] = {
    "sec_storage_public_blob": ["NS-2", "DP-8"],
    "sec_storage_https_only": ["DP-3"],
    "sec_storage_min_tls": ["DP-3"],
    "sec_storage_shared_key": ["IM-1"],
    "sec_storage_net_default_allow": ["NS-2"],
    "sec_nsg_mgmt_open": ["NS-1"],
    "sec_nsg_db_ports_open": ["NS-1"],
    "sec_public_ip": ["NS-1"],
    "sec_kv_purge_protection": ["DP-8"],
    "sec_kv_public_network": ["NS-2"],
    "sec_kv_rbac": ["PA-7"],
    "sec_kv_soft_delete": ["DP-8"],
    "sec_sql_public_access": ["NS-2"],
    "sec_sql_aad_only": ["IM-1"],
    "sec_webapp_https_only": ["DP-3"],
    "sec_webapp_min_tls": ["DP-3"],
    "sec_webapp_ftps": ["DP-3"],
    "sec_webapp_no_managed_identity": ["IM-3"],
    "sec_disk_unencrypted": ["DP-5"],
    "sec_aks_public_api": ["NS-2"],
    "sec_aks_local_accounts": ["IM-1"],
    "sec_aks_no_rbac": ["PA-7"],
    "sec_cosmos_public": ["NS-2"],
    "sec_cosmos_local_auth": ["IM-1"],
    "sec_acr_admin_user": ["IM-1"],
    "sec_acr_public_network": ["NS-2"],
    "rel_storage_lrs": ["BR-2"],
    "rel_disk_lrs": ["BR-2"],
    "ops_vm_no_boot_diagnostics": ["LT-3"],
    "ops_aks_no_monitoring": ["LT-3"],
    "ops_law_short_retention": ["LT-6"],
}

# PCI DSS v4.0 requirement mappings (top-level requirement), keyed by check id.
_PCI_MAP: dict[str, list[str]] = {
    "sec_storage_public_blob": ["PCI DSS 1", "PCI DSS 7"],
    "sec_storage_https_only": ["PCI DSS 4"],
    "sec_storage_min_tls": ["PCI DSS 4"],
    "sec_storage_shared_key": ["PCI DSS 8"],
    "sec_storage_net_default_allow": ["PCI DSS 1"],
    "sec_nsg_mgmt_open": ["PCI DSS 1"],
    "sec_nsg_db_ports_open": ["PCI DSS 1"],
    "sec_public_ip": ["PCI DSS 1"],
    "sec_kv_purge_protection": ["PCI DSS 3"],
    "sec_kv_public_network": ["PCI DSS 1"],
    "sec_kv_rbac": ["PCI DSS 7"],
    "sec_kv_soft_delete": ["PCI DSS 3"],
    "sec_sql_public_access": ["PCI DSS 1"],
    "sec_sql_aad_only": ["PCI DSS 8"],
    "sec_webapp_https_only": ["PCI DSS 4"],
    "sec_webapp_min_tls": ["PCI DSS 4"],
    "sec_webapp_ftps": ["PCI DSS 4"],
    "sec_webapp_no_managed_identity": ["PCI DSS 8"],
    "sec_disk_unencrypted": ["PCI DSS 3"],
    "sec_aks_public_api": ["PCI DSS 1"],
    "sec_aks_local_accounts": ["PCI DSS 8"],
    "sec_aks_no_rbac": ["PCI DSS 7"],
    "sec_cosmos_public": ["PCI DSS 1"],
    "sec_cosmos_local_auth": ["PCI DSS 8"],
    "sec_acr_admin_user": ["PCI DSS 8"],
    "sec_acr_public_network": ["PCI DSS 1"],
    "ops_vm_no_boot_diagnostics": ["PCI DSS 10"],
    "ops_aks_no_monitoring": ["PCI DSS 10"],
    "ops_law_short_retention": ["PCI DSS 10"],
}

# Apply the centrally-maintained framework maps so each check carries every framework's
# control ids without per-control edits. ``cis``/``nist`` are declared inline on the check.
_CENTRAL_MAPS: dict[str, dict[str, list[str]]] = {"iso": _ISO_MAP, "mcsb": _MCSB_MAP, "pci": _PCI_MAP}
for _c in ALL_CHECKS:
    for _fw, _map in _CENTRAL_MAPS.items():
        _ids = _map.get(_c["id"])
        if _ids:
            _c["frameworks"][_fw] = _ids

# Backfill WAF reliability sub-categories for the original reliability controls (the newer
# controls declare their own ``sub_category``). Applied centrally so the whole reliability
# pillar can be sliced by sub-pillar (HA / DR / Scalability / …) like a WARA report.
_SUB_CATEGORY_MAP: dict[str, str] = {
    "rel_vm_no_zone": "High availability",
    "rel_storage_lrs": "Disaster recovery",
    "rel_pip_basic_sku": "High availability",
    "rel_lb_basic_sku": "High availability",
    "rel_sql_no_zone": "High availability",
    "rel_appplan_single_instance": "High availability",
    "rel_appservice_no_zone": "High availability",
    "rel_disk_lrs": "Disaster recovery",
    "rel_cosmos_single_region": "Disaster recovery",
    "rel_aks_single_nodepool": "High availability",
    "rel_aks_free_sla": "High availability",
    "rel_appgw_no_zone": "High availability",
}
for _c in ALL_CHECKS:
    if not _c.get("sub_category"):
        sub = _SUB_CATEGORY_MAP.get(_c["id"])
        if sub:
            _c["sub_category"] = sub

# CIS Azure Foundations v5.0.0 numbers for shipped security checks that already cover a CIS
# automated recommendation (so we don't duplicate the control). Overwrites the older v2.1.0
# id so the whole catalog speaks v5 numbering consistently.
_CIS_V5_ALIAS: dict[str, list[str]] = {
    "sec_storage_public_blob": ["CIS Azure 9.3.8"],
    "sec_storage_https_only": ["CIS Azure 9.3.4"],
    "sec_storage_min_tls": ["CIS Azure 9.3.6"],
    "sec_storage_net_default_allow": ["CIS Azure 9.3.2.3"],
    "sec_storage_shared_key": ["CIS Azure 9.3.1.3"],
    "sec_nsg_mgmt_open": ["CIS Azure 7.1", "CIS Azure 7.2"],
    "sec_kv_purge_protection": ["CIS Azure 8.3.5"],
    "sec_kv_rbac": ["CIS Azure 8.3.6"],
    "sec_kv_public_network": ["CIS Azure 8.3.7"],
}
for _c in ALL_CHECKS:
    _alias = _CIS_V5_ALIAS.get(_c["id"])
    if _alias:
        _c["frameworks"]["cis"] = _alias

_BY_ID = {c["id"]: c for c in ALL_CHECKS}

# Frameworks the compliance coverage view aggregates, in display order.
FRAMEWORKS = ("cis", "nist", "iso", "mcsb", "pci")

# Framework display metadata for the compliance coverage view.
FRAMEWORK_META: dict[str, dict[str, str]] = {
    "cis": {"label": f"CIS Azure Foundations {CIS_VERSION}", "icon": "🛡️"},
    "nist": {"label": "NIST 800-53 Rev.5", "icon": "🏛️"},
    "iso": {"label": "ISO/IEC 27001:2022", "icon": "📜"},
    "mcsb": {"label": "Microsoft Cloud Security Benchmark", "icon": "☁️"},
    "pci": {"label": "PCI DSS v4.0", "icon": "💳"},
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

    For each framework (see ``FRAMEWORKS``: CIS/NIST/ISO/MCSB/PCI), maps each referenced
    control id to the worst status across the checks that cite it (fail > error >
    not_applicable > pass), so the same assessment run yields a compliance coverage view
    alongside the WAF score."""
    rank = {"fail": 3, "error": 2, "not_applicable": 1, "pass": 0}
    out: dict[str, Any] = {}
    for fw in FRAMEWORKS:
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
        "metric_backed": bool(c.get("metric")),
        "kind": c.get("kind") or ("metric" if c.get("metric") else "graph"),
        "impact": c.get("impact", ""),
        "effort": c.get("effort", ""),
        "sub_category": c.get("sub_category", ""),
        "source": c.get("source", "built-in"),
        "learn_more": c.get("learn_more", []),
        "profile": c.get("profile", ""),
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
        "packs": [
            {"id": pid, **{k: v for k, v in meta.items()}}
            for pid, meta in PACKS.items()
        ],
        "sub_categories": list(SUB_CATEGORIES),
        "frameworks": FRAMEWORK_META,
        "checks": by_pillar,
        "score_bands": bands,
    }


def catalog_markdown() -> str:
    """Render the shipped catalog as a Markdown reference, auto-generated from ``ALL_CHECKS``.

    One table per pillar (id/title, severity, control kind, mapped framework controls) so a
    compliance reference can be produced on demand without hand-maintaining a separate doc.
    Run ``python -m app.assessments.catalog`` to print it."""
    lines: list[str] = ["# Assessment check catalog", ""]
    lines.append(
        f"_{len(ALL_CHECKS)} shipped controls across {len(PILLARS)} Well-Architected pillars. "
        f"CIS control ids pinned to {CIS_VERSION}._"
    )
    lines.append("")
    by_pillar: dict[str, list[dict[str, Any]]] = {p: [] for p in PILLARS}
    for c in ALL_CHECKS:
        by_pillar.setdefault(c["pillar"], []).append(c)
    for p in PILLARS:
        meta = PILLAR_META.get(p, {"label": p, "icon": ""})
        rows = by_pillar.get(p, [])
        lines.append(f"## {meta.get('icon', '')} {meta.get('label', p)} ({len(rows)})".strip())
        lines.append("")
        lines.append("| Check | Severity | Kind | Frameworks |")
        lines.append("| --- | --- | --- | --- |")
        for c in rows:
            fws = c.get("frameworks") or {}
            fw_str = (
                "; ".join(
                    f"{FRAMEWORK_META.get(k, {}).get('label', k)}: {', '.join(v)}"
                    for k, v in fws.items()
                    if v
                )
                or "—"
            )
            kind = c.get("kind") or ("metric" if c.get("metric") else "graph")
            lines.append(f"| `{c['id']}` — {c['title']} | {c['severity']} | {kind} | {fw_str} |")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


if __name__ == "__main__":  # pragma: no cover - manual doc generation helper
    print(catalog_markdown())



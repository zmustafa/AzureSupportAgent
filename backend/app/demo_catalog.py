"""Central catalog of demo workloads and their resources.

Single source of truth shared by every demo scan lens (Monitoring/AMBA, Telemetry and
Backup&DR coverage, Performance Profiler, Retirement Radar) and by the workload registry
seed. Each demo workload owns a *distinct, realistic* resource set with its own names,
types, regions and tags — so the three demo workloads no longer look identical.

Each resource carries a coarse health ``tier``:
    "green"  well-managed     → alerts present, diag compliant, backups offsite, perf healthy
    "amber"  partially managed → some gaps in each lens
    "red"    neglected/legacy  → no alerts, no diag, not backed up, perf breaching

A single tier per resource keeps the three demo workloads coherent across all five lenses
(a neglected resource is bad everywhere; a well-run one is green everywhere) while giving
each workload a believably different red/amber/green spread.
"""
from __future__ import annotations

import hashlib
from typing import Any

DEMO_SUB = "00000000-0000-0000-0000-0000000d3340"

CONTOSO_ID = "demo-amba-coverage"
ZAVA_WEB_ID = "demo-zava-shoes-website"
ZAVA_CRM_ID = "demo-zava-shoes-crm"

# Every workload id treated as a demo scope: scans serve synthetic data instead of Azure.
DEMO_WORKLOAD_IDS = {CONTOSO_ID, ZAVA_WEB_ID, ZAVA_CRM_ID}

GREEN = "green"
AMBER = "amber"
RED = "red"


# --------------------------------------------------------------------------- catalog
# Each resource: (resource_type, short_name, tier, region). The resource group and the
# subscription come from the workload definition.
_WORKLOADS: dict[str, dict[str, Any]] = {
    CONTOSO_ID: {
        "name": "Contoso Hotels",
        "description": "Hotel booking & property-management platform: Front Door → App Gateway → "
        "booking web + property API, rate-sync Function, reservations SQL, guest Cosmos DB, "
        "media storage, Redis session cache, AKS microservices and a legacy PMS VM.",
        "rg": "rg-contoso-hotels",
        "primary_region": "eastus",
        "tags": ["contoso", "demo", "hospitality"],
        "approved_workspace": "contoso-hotels-law",
        "resources": [
            ("microsoft.cdn/profiles", "contoso-afd", GREEN, "global"),
            ("microsoft.network/applicationgateways", "contoso-appgw", GREEN, "eastus"),
            ("microsoft.web/serverfarms", "contoso-plan", GREEN, "eastus"),
            ("microsoft.web/sites", "contoso-booking-web", GREEN, "eastus"),
            ("microsoft.web/sites", "contoso-property-api", AMBER, "eastus"),
            ("microsoft.web/sites", "contoso-ratesync-func", GREEN, "eastus"),
            ("microsoft.sql/servers/databases", "contoso-sql/reservations", AMBER, "eastus"),
            ("microsoft.documentdb/databaseaccounts", "contoso-guests-cosmos", GREEN, "eastus"),
            ("microsoft.storage/storageaccounts", "contosohotelsmedia", GREEN, "eastus"),
            ("microsoft.keyvault/vaults", "contoso-kv", GREEN, "eastus"),
            ("microsoft.cache/redis", "contoso-redis", RED, "eastus"),
            ("microsoft.containerservice/managedclusters", "contoso-aks", AMBER, "eastus"),
            ("microsoft.compute/virtualmachines", "contoso-pms-vm", RED, "westeurope"),
            ("microsoft.compute/disks", "contoso-pms-vm-datadisk", RED, "westeurope"),
        ],
    },
    ZAVA_WEB_ID: {
        "name": "Zava Shoes Website",
        "description": "Public e-commerce storefront: Traffic Manager → App Gateway → storefront App "
        "Service + checkout Function, catalog SQL, product-image storage, Redis cart cache, "
        "Cognitive Search and Front Door CDN.",
        "rg": "rg-zava-web",
        "primary_region": "eastus2",
        "tags": ["zava", "demo", "ecommerce"],
        "approved_workspace": "zava-web-law",
        "resources": [
            ("microsoft.network/trafficmanagerprofiles", "zava-web-tm", GREEN, "global"),
            ("microsoft.cdn/profiles", "zava-web-cdn", GREEN, "global"),
            ("microsoft.network/applicationgateways", "zava-web-appgw", GREEN, "eastus2"),
            ("microsoft.web/serverfarms", "zava-web-plan", GREEN, "eastus2"),
            ("microsoft.web/sites", "zava-web-storefront", AMBER, "eastus2"),
            ("microsoft.web/sites", "zava-web-checkout-func", RED, "eastus2"),
            ("microsoft.sql/servers/databases", "zava-web-sql/catalog", AMBER, "eastus2"),
            ("microsoft.storage/storageaccounts", "zavawebmedia", GREEN, "eastus2"),
            ("microsoft.cache/redis", "zava-web-redis", AMBER, "eastus2"),
            ("microsoft.keyvault/vaults", "zava-web-kv", GREEN, "eastus2"),
            ("microsoft.search/searchservices", "zava-web-search", GREEN, "eastus2"),
        ],
    },
    ZAVA_CRM_ID: {
        "name": "Zava Shoes CRM",
        "description": "Internal CRM: App Gateway → portal App Service, lead-sync Logic App + "
        "integration Function, VM-hosted services, accounts SQL, analytics PostgreSQL, Redis "
        "cache, document storage and Key Vault.",
        "rg": "rg-zava-crm",
        "primary_region": "centralus",
        "tags": ["zava", "demo", "crm"],
        "approved_workspace": "zava-crm-law",
        "resources": [
            ("microsoft.network/applicationgateways", "zava-crm-appgw", GREEN, "centralus"),
            ("microsoft.web/serverfarms", "zava-crm-plan", AMBER, "centralus"),
            ("microsoft.web/sites", "zava-crm-portal", GREEN, "centralus"),
            ("microsoft.web/sites", "zava-crm-integration-func", AMBER, "centralus"),
            ("microsoft.logic/workflows", "zava-crm-lead-sync", GREEN, "centralus"),
            ("microsoft.compute/virtualmachines", "zava-crm-vm01", AMBER, "centralus"),
            ("microsoft.compute/virtualmachines", "zava-crm-vm02", RED, "centralus"),
            ("microsoft.sql/servers/databases", "zava-crm-sql/accounts", AMBER, "centralus"),
            ("microsoft.dbforpostgresql/flexibleservers", "zava-crm-pg", RED, "centralus"),
            ("microsoft.cache/redis", "zava-crm-redis", GREEN, "centralus"),
            ("microsoft.keyvault/vaults", "zava-crm-kv", AMBER, "centralus"),
            ("microsoft.storage/storageaccounts", "zavacrmdocs", GREEN, "centralus"),
        ],
    },
}


def is_demo_workload(scope_id: str) -> bool:
    return scope_id in DEMO_WORKLOAD_IDS


def all_demo_ids() -> list[str]:
    return list(_WORKLOADS.keys())


def workload_meta(scope_id: str) -> dict[str, Any]:
    return _WORKLOADS.get(scope_id, _WORKLOADS[CONTOSO_ID])


def name_for(scope_id: str) -> str:
    return workload_meta(scope_id)["name"]


def rg_for(scope_id: str) -> str:
    return workload_meta(scope_id)["rg"]


def approved_workspace_id(scope_id: str) -> str:
    meta = workload_meta(scope_id)
    return (
        f"/subscriptions/{DEMO_SUB}/resourceGroups/{meta['rg']}/providers/"
        f"microsoft.operationalinsights/workspaces/{meta['approved_workspace']}"
    )


def _rid(rg: str, ptype: str, name: str) -> str:
    return f"/subscriptions/{DEMO_SUB}/resourceGroups/{rg}/providers/{ptype}/{name}"


# --------------------------------------------------------------------------- demo tag profiles
# Realistic, intentionally-MESSY Azure tags per demo workload so Tag Intelligence has something
# meaningful to discover: inconsistent casing (CostCenter vs costcenter vs Cost Center), value
# variants (Production / Prod / PRD), partially-applied required tags (Owner missing on some),
# high-cardinality keys (CreatedBy), a few fully-untagged resources, and per-workload billing
# codes + business units. Everything is derived deterministically from the resource id so it's
# stable across runs (and across drift snapshots).
_TAG_PROFILE: dict[str, dict[str, Any]] = {
    CONTOSO_ID: {
        "app": "contoso-hotels", "bu": "Hospitality",
        "billing_codes": ["FIN-204", "FIN-204", "FIN-311"],   # FIN-204 dominant
        "owners": ["hotels-platform@contoso.com", "booking-team@contoso.com", "data-team@contoso.com"],
        "domain": "contoso.com",
    },
    ZAVA_WEB_ID: {
        "app": "zava-web", "bu": "Ecommerce",
        "billing_codes": ["ZAVA-1001", "ZAVA-1001", "ZAVA-1007"],
        "owners": ["web-platform@zava.com", "storefront-team@zava.com"],
        "domain": "zava.com",
    },
    ZAVA_CRM_ID: {
        "app": "zava-crm", "bu": "Sales",
        "billing_codes": ["ZAVA-2002", "ZAVA-2002", "ZAVA-2050"],
        "owners": ["crm-platform@zava.com", "sales-ops@zava.com"],
        "domain": "zava.com",
    },
}

# Spelling variants for the billing key, distributed across resources so Hygiene's near-duplicate
# key detection has real signal. ``CostCenter`` is intentionally the most common (canonical).
_COSTCENTER_KEYS = ["CostCenter", "CostCenter", "costcenter", "Cost Center"]
# Environment value variants (same meaning, different spelling) — Hygiene normalizes these.
_ENV_VARIANTS = ["Production", "Production", "Prod", "PRD"]
_CRIT = {GREEN: "high", AMBER: "medium", RED: "low"}


def _demo_tags(scope_id: str, rid: str, name: str, tier: str, idx: int) -> dict[str, str]:
    """Deterministic, realistic-but-messy Azure tags for one demo resource."""
    prof = _TAG_PROFILE.get(scope_id)
    if not prof:
        return {"environment": "prod", "criticality": _CRIT.get(tier, "medium"), "owner": "platform-team"}

    # Neglected (RED-tier) resources are the ones most likely to be untagged; well-run ones
    # rarely are. Gives every workload a believable "untagged" slice for the census + coverage.
    if (tier == RED and bucket(rid + "untag", 2) == 0) or bucket(rid + "untag", 12) == 0:
        return {}

    tags: dict[str, str] = {}
    # Environment — value variants (Production / Prod / PRD); a couple of non-prod for variety.
    if bucket(rid + "envkind", 8) == 0:
        tags["Environment"] = "Staging" if tier == AMBER else "Development"
    else:
        tags["Environment"] = _ENV_VARIANTS[bucket(rid + "env", len(_ENV_VARIANTS))]

    # Billing — near-duplicate KEY spellings + per-workload code values.
    cc_key = _COSTCENTER_KEYS[bucket(rid + "cck", len(_COSTCENTER_KEYS))]
    tags[cc_key] = prof["billing_codes"][bucket(rid + "ccv", len(prof["billing_codes"]))]

    # Application — mostly present (the workload's app), missing on ~1 in 6.
    if bucket(rid + "app", 6) != 0:
        tags["Application"] = prof["app"]

    # Owner — required-ish, present on ~70%; a few use lowercase "owner" (another near-dup key).
    ob = bucket(rid + "own", 10)
    if ob < 7:
        tags["Owner"] = prof["owners"][bucket(rid + "ownv", len(prof["owners"]))]
    elif ob == 7:
        tags["owner"] = prof["owners"][bucket(rid + "ownv", len(prof["owners"]))]
    # ob in (8, 9) -> Owner missing (coverage gap / "missing only one tag").

    # BusinessUnit — present on ~80%.
    if bucket(rid + "bu", 5) != 0:
        tags["BusinessUnit"] = prof["bu"]

    # DataClassification — present on ~60%.
    dcb = bucket(rid + "dc", 5)
    if dcb < 3:
        tags["DataClassification"] = ["Confidential", "Internal", "Public"][dcb]

    # ManagedBy — IaC provenance.
    tags["ManagedBy"] = ["terraform", "bicep", "manual"][bucket(rid + "mb", 3)]

    # Criticality — from the resource's health tier.
    tags["Criticality"] = _CRIT.get(tier, "medium")

    # CreatedBy — HIGH-CARDINALITY (unique-ish per resource), present on ~half.
    if bucket(rid + "cb", 2) == 0:
        tags["CreatedBy"] = f"user{(idx * 7 + bucket(rid, 97)) % 53:02d}@{prof['domain']}"

    return tags


def resources_for(scope_id: str) -> list[dict[str, Any]]:
    """Resources in the collector shape: {id,name,type,resourceGroup,subscriptionId,location,tags}."""
    meta = workload_meta(scope_id)
    rg = meta["rg"]
    out: list[dict[str, Any]] = []
    for idx, (ptype, name, tier, region) in enumerate(meta["resources"]):
        rid = _rid(rg, ptype, name)
        out.append(
            {
                "id": rid,
                "name": name,
                "type": ptype,
                "resourceGroup": rg,
                "subscriptionId": DEMO_SUB,
                "location": region,
                "tier": tier,
                "tags": _demo_tags(scope_id, rid, name, tier, idx),
            }
        )
    return out



def nodes_for(scope_id: str) -> list[dict[str, Any]]:
    """Resources in the workload-registry node shape (for the picker / inventory / All Resources)."""
    return [
        {
            "kind": "resource",
            "id": r["id"],
            "name": r["name"],
            "subscription_id": r["subscriptionId"],
            "resource_group": r["resourceGroup"],
            "resource_type": r["type"],
            "location": r["location"],
        }
        for r in resources_for(scope_id)
    ]


def tier_index(scope_id: str) -> dict[str, str]:
    """Map of lowercased resource id → tier, for synthesizers."""
    return {r["id"].lower(): r["tier"] for r in resources_for(scope_id)}


def bucket(rid: str, n: int) -> int:
    """Deterministic 0..n-1 from a resource id, for stable per-resource variation."""
    h = hashlib.sha1(rid.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % max(1, n)

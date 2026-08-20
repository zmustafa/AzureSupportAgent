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
    h = hashlib.sha1(rid.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(h[:8], 16) % max(1, n)


# ====================================================================== recovery readiness
# A SECOND axis, deliberately ORTHOGONAL to the health `tier` above.
#
# `tier` means "well-managed -> neglected". Deriving recoverability from it would make the
# demo one-dimensional — everything green resilient, everything red not — and would fail to
# show the one thing this product exists to say:
#
#     A resource can be flawlessly redundant, reported "resilient" by every other tool, and
#     still carry a 24-hour RPO and NO recovery path at all for ransomware.
#
# For that, redundancy and recoverability have to be able to DISAGREE. So a resource's
# resiliency profile is assigned independently of its tier.

PROFILE_ZONE_REDUNDANT_NO_PITR = "zone_redundant_no_pitr"
PROFILE_BACKED_UP_SINGLE_ZONE = "backed_up_single_zone"
PROFILE_GEO_PAIRED_HOURLY = "geo_paired_hourly"
PROFILE_SINGLE_INSTANCE_DAILY = "single_instance_daily"
PROFILE_ORPHAN_NO_RECOVERY = "orphan_no_recovery"
PROFILE_CONTINUOUS_REPLICATED = "continuous_replicated"
PROFILE_LRS_VAULT_BACKUP = "lrs_vault_backup"
PROFILE_UNMAPPABLE = "unmappable"

#: What each profile demonstrates. Surfaced in the dev script so the narrative is checkable.
PROFILE_STORY: dict[str, str] = {
    PROFILE_ZONE_REDUNDANT_NO_PITR:
        "Zone-perfect and reported resilient everywhere else; 24h RPO and no path for corruption.",
    PROFILE_BACKED_UP_SINGLE_ZONE:
        "Fails a zone-redundancy check, but survives ransomware.",
    PROFILE_GEO_PAIRED_HOURLY: "The target state: geo-paired and frequently recoverable.",
    PROFILE_SINGLE_INSTANCE_DAILY: "The ordinary majority.",
    PROFILE_ORPHAN_NO_RECOVERY: "No recovery path at all. Not slow — none.",
    PROFILE_CONTINUOUS_REPLICATED: "Measured 30-second RPO from replication.",
    PROFILE_LRS_VAULT_BACKUP: "Backed up, into a vault that dies with the region.",
    PROFILE_UNMAPPABLE: "No datasource mapping — unknown, which is not 'unprotected'.",
}

# Backup policy shapes, in the RAW schedule form the parser consumes, so the demo exercises
# the real parser rather than a shortcut.
_SCHED_DAILY_0200 = {"scheduleRunFrequency": "Daily", "scheduleRunTimes": ["2026-01-01T02:00:00Z"]}
_SCHED_HOURLY_4H = {"scheduleRunFrequency": "Hourly", "hourlySchedule": {"interval": 4}}
_SCHED_WINDOWED = {
    "scheduleRunFrequency": "Hourly",
    "hourlySchedule": {"interval": 4, "scheduleWindowStartTime": "2026-01-01T08:00:00Z",
                       "scheduleWindowDuration": "PT10H"},
}

_PROFILES: dict[str, dict[str, Any]] = {
    PROFILE_ZONE_REDUNDANT_NO_PITR: {
        "zone_redundant": True, "zones": ["1", "2", "3"], "replication": "multi-region-write",
        "native_backup": {"kind": "cosmos_periodic", "interval_minutes": 1440,
                          "retention_days": 7, "geo_redundant": False},
        "protected": False, "vault_redundancy": "", "soft_delete": False,
    },
    PROFILE_BACKED_UP_SINGLE_ZONE: {
        "zone_redundant": False, "zones": [], "replication": "LRS",
        "native_backup": {"kind": "none"},
        "protected": True, "vault_redundancy": "GeoRedundant", "soft_delete": True,
        "schedule": _SCHED_HOURLY_4H, "retention_days": 30, "recovery_point_age_hours": 2.0,
        "policy_name": "hourly-30d",
    },
    PROFILE_GEO_PAIRED_HOURLY: {
        "zone_redundant": True, "zones": ["1", "2", "3"], "replication": "GZRS",
        "native_backup": {"kind": "storage_pitr", "interval_minutes": 5, "retention_days": 30,
                          "geo_redundant": True},
        "protected": True, "vault_redundancy": "GeoRedundant", "soft_delete": True,
        "schedule": _SCHED_HOURLY_4H, "retention_days": 90, "recovery_point_age_hours": 1.5,
        "policy_name": "hourly-90d-geo",
    },
    PROFILE_SINGLE_INSTANCE_DAILY: {
        "zone_redundant": False, "zones": [], "replication": "LRS",
        "native_backup": {"kind": "none"},
        "protected": True, "vault_redundancy": "GeoRedundant", "soft_delete": True,
        "schedule": _SCHED_DAILY_0200, "retention_days": 30, "recovery_point_age_hours": 9.0,
        "policy_name": "DefaultPolicy",
    },
    PROFILE_ORPHAN_NO_RECOVERY: {
        "zone_redundant": False, "zones": [], "replication": "LRS",
        "native_backup": {"kind": "none"},
        "protected": False, "vault_redundancy": "", "soft_delete": False,
    },
    PROFILE_CONTINUOUS_REPLICATED: {
        "zone_redundant": False, "zones": ["1"], "replication": "LRS",
        "native_backup": {"kind": "none"},
        "protected": True, "vault_redundancy": "GeoRedundant", "soft_delete": True,
        "schedule": _SCHED_DAILY_0200, "retention_days": 30, "recovery_point_age_hours": 5.0,
        "policy_name": "DefaultPolicy",
        "asr": {"rpo_seconds": 30, "replication_health": "Normal",
                "protection_state": "Protected", "last_test_failover_age_days": 45},
    },
    PROFILE_LRS_VAULT_BACKUP: {
        "zone_redundant": True, "zones": [], "replication": "LRS",
        "native_backup": {"kind": "sql_pitr", "interval_minutes": 10, "retention_days": 7,
                          "geo_redundant": False},
        # The point of this row: protected, and the backups die with the region.
        "protected": True, "vault_redundancy": "LocallyRedundant", "soft_delete": True,
        "schedule": _SCHED_WINDOWED, "retention_days": 30, "recovery_point_age_hours": 3.0,
        "policy_name": "biz-hours-lrs",
    },
    PROFILE_UNMAPPABLE: {
        "zone_redundant": None, "zones": [], "replication": "",
        "native_backup": {"kind": "unknown"},
        "protected": None, "vault_redundancy": "", "soft_delete": None,
    },
}

# Named assignments so the demo has rehearsed moments rather than whatever the hash gives.
# Keyed by the catalog's short name.
_PROFILE_BY_NAME: dict[str, str] = {
    # --- Contoso Hotels -------------------------------------------------------------
    "contoso-guests-cosmos": PROFILE_ZONE_REDUNDANT_NO_PITR,   # the money row
    "contoso-pms-vm": PROFILE_ORPHAN_NO_RECOVERY,              # no recovery path
    "contoso-pms-vm-datadisk": PROFILE_ORPHAN_NO_RECOVERY,
    "contosohotelsmedia": PROFILE_GEO_PAIRED_HOURLY,
    "contoso-sql/reservations": PROFILE_BACKED_UP_SINGLE_ZONE,
    "contoso-redis": PROFILE_UNMAPPABLE,
    "contoso-aks": PROFILE_SINGLE_INSTANCE_DAILY,
    # --- Zava Shoes Website ---------------------------------------------------------
    "zava-web-sql/catalog": PROFILE_LRS_VAULT_BACKUP,          # protected, region-fatal
    "zavawebmedia": PROFILE_GEO_PAIRED_HOURLY,
    "zava-web-redis": PROFILE_UNMAPPABLE,
    "zava-web-search": PROFILE_UNMAPPABLE,
    # --- Zava Shoes CRM -------------------------------------------------------------
    "zava-crm-vm01": PROFILE_CONTINUOUS_REPLICATED,            # 30s RPO...
    "zava-crm-vm02": PROFILE_SINGLE_INSTANCE_DAILY,            # ...beside 24h, same app
    "zava-crm-pg": PROFILE_ORPHAN_NO_RECOVERY,
    "zavacrmdocs": PROFILE_SINGLE_INSTANCE_DAILY,
    "zava-crm-redis": PROFILE_UNMAPPABLE,
}

# Fallback for anything unnamed, so every resource still gets a coherent story.
_PROFILE_FALLBACK = (
    PROFILE_SINGLE_INSTANCE_DAILY, PROFILE_BACKED_UP_SINGLE_ZONE, PROFILE_GEO_PAIRED_HOURLY,
)

#: Recovery criticality per demo workload, so breach lists differ rather than looking
#: decorative. Maps onto the Backup Manager tier registry ids.
DEMO_CRITICALITY: dict[str, str] = {
    CONTOSO_ID: "mission_critical",
    ZAVA_WEB_ID: "business_critical",
    ZAVA_CRM_ID: "standard",
}

# Approximate data volumes, needed for restore-time bands. Absent means "size unknown",
# which must widen the band rather than assume a default.
_SIZE_GB: dict[str, int] = {
    "contoso-pms-vm": 512, "contoso-pms-vm-datadisk": 2048, "contosohotelsmedia": 1200,
    "contoso-sql/reservations": 240, "contoso-guests-cosmos": 80, "contoso-aks": 64,
    "zava-web-sql/catalog": 180, "zavawebmedia": 900,
    "zava-crm-vm01": 256, "zava-crm-vm02": 256, "zavacrmdocs": 300,
}


def profile_for(scope_id: str, name: str, rid: str) -> str:
    """Which resiliency profile a demo resource plays."""
    named = _PROFILE_BY_NAME.get(name)
    if named:
        return named
    return _PROFILE_FALLBACK[bucket(rid + "resil", len(_PROFILE_FALLBACK))]


def criticality_for(scope_id: str) -> str:
    return DEMO_CRITICALITY.get(scope_id, "standard")


def resiliency_for(scope_id: str) -> list[dict[str, Any]]:
    """Redundancy + native-backup configuration, in the shape the resiliency collector emits.

    Deliberately NOT a pre-computed verdict: these rows go through the same derivation the
    live collector feeds, so demo mode exercises the real pipeline. A demo that diverged
    from production would hide exactly the bugs it should catch.
    """
    out: list[dict[str, Any]] = []
    for res in resources_for(scope_id):
        name, rid = res["name"], res["id"]
        profile = profile_for(scope_id, name, rid)
        spec = _PROFILES[profile]
        out.append({
            "id": rid.lower(),
            "name": name,
            "type": res["type"],
            "location": res["location"],
            "zones": list(spec.get("zones") or []),
            "zone_redundant": spec.get("zone_redundant"),
            "replication": spec.get("replication", ""),
            "native_backup": dict(spec.get("native_backup") or {"kind": "unknown"}),
            "soft_delete": spec.get("soft_delete"),
            "size_gb": _SIZE_GB.get(name),
            "demo_profile": profile,
        })
    return out


def resiliency_backup_for(scope_id: str) -> list[dict[str, Any]]:
    """Vault-backed protection facts, in the shape Backup Manager's instances carry.

    Kept separate from :func:`resiliency_for` on purpose: they are two different sources in
    production, and the join has to be exercised joining two things.
    """
    out: list[dict[str, Any]] = []
    for res in resources_for(scope_id):
        name, rid = res["name"], res["id"]
        spec = _PROFILES[profile_for(scope_id, name, rid)]
        protected = spec.get("protected")
        if protected is None:
            continue  # unmappable: absent from the backup estate, which is UNKNOWN not "no"
        if not protected:
            continue  # eligible and genuinely unprotected — the join infers this
        out.append({
            "datasource_id": rid.lower(),
            "friendly_name": name,
            "policy_name": spec.get("policy_name", ""),
            "schedule_raw": spec.get("schedule"),
            "retention_days": spec.get("retention_days"),
            "recovery_point_age_hours": spec.get("recovery_point_age_hours"),
            "vault_redundancy": spec.get("vault_redundancy", ""),
            "soft_delete": spec.get("soft_delete"),
            "protection_stopped": False,
        })
    return out


def resiliency_asr_for(scope_id: str) -> list[dict[str, Any]]:
    """Site Recovery replication facts for the demo estate."""
    out: list[dict[str, Any]] = []
    for res in resources_for(scope_id):
        name, rid = res["name"], res["id"]
        spec = _PROFILES[profile_for(scope_id, name, rid)]
        asr = spec.get("asr")
        if asr:
            out.append({"source_id": rid.lower(), "friendly_name": name, **asr})
    return out


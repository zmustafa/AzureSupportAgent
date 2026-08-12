"""Authoritative Azure Retail Prices projection for architecture resources.

ARM resource types and retail meters are different taxonomies.  This module therefore
uses an explicit, reviewable mapping for resource families where a defensible match is
possible and returns a truthful terminal state for everything else.  It never substitutes
a seeded or fabricated amount when Azure has no matching row.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.core.retail_prices import RetailFetchResult, fetch_retail_prices, normalize_currency

MONTHLY_HOURS = 730.0
CACHE_TTL_SECONDS = 7 * 24 * 3600
MAX_CACHE_ENTRIES = 96
MAX_COMPONENTS = 12
MAX_CANDIDATES = 8
_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "architecture_prices.json"

PricingMode = Literal["fixed", "mixed", "usage"]


@dataclass(frozen=True)
class PricingRule:
    service_name: str
    mode: PricingMode
    requires_sku: bool = False
    query_without_sku: bool = True
    exact_sku_filter: bool = False
    default_sku: str = ""
    quantity_from_capacity: bool = False
    requires_billing_zone: bool = False
    note: str = ""


def _rule(
    service: str,
    mode: PricingMode,
    **kwargs: Any,
) -> PricingRule:
    return PricingRule(service, mode, **kwargs)


# Exact ARM type -> retail-service matching policy.  A policy authorizes a query; it does
# not itself assert a price.  SKU/meter matching below must still select an API row.
RESOURCE_RULES: dict[str, PricingRule] = {
    "microsoft.compute/virtualmachines": _rule(
        "Virtual Machines", "fixed", requires_sku=True, exact_sku_filter=True,
        note="The operating-system/license variant must also match.",
    ),
    "microsoft.compute/virtualmachinescalesets": _rule(
        "Virtual Machines", "fixed", requires_sku=True, exact_sku_filter=True,
        quantity_from_capacity=True, note="Scale-set capacity is required for a monthly baseline.",
    ),
    "microsoft.compute/disks": _rule(
        "Storage", "mixed", requires_sku=True, query_without_sku=False,
        note="Managed-disk tier and size determine the applicable meter.",
    ),
    "microsoft.web/serverfarms": _rule("Azure App Service", "fixed", requires_sku=True),
    "microsoft.web/staticsites": _rule("Azure App Service", "mixed", requires_sku=True),
    "microsoft.sql/servers/databases": _rule("SQL Database", "mixed", requires_sku=True),
    "microsoft.sql/managedinstances": _rule("SQL Managed Instance", "mixed", requires_sku=True),
    "microsoft.dbforpostgresql/servers": _rule("Azure Database for PostgreSQL", "mixed", requires_sku=True),
    "microsoft.dbforpostgresql/flexibleservers": _rule("Azure Database for PostgreSQL", "mixed", requires_sku=True),
    "microsoft.dbformysql/servers": _rule("Azure Database for MySQL", "mixed", requires_sku=True),
    "microsoft.dbformysql/flexibleservers": _rule("Azure Database for MySQL", "mixed", requires_sku=True),
    "microsoft.documentdb/databaseaccounts": _rule(
        "Azure Cosmos DB", "usage", query_without_sku=False,
        note="Throughput/serverless operations and storage quantities are required.",
    ),
    "microsoft.cache/redis": _rule("Redis Cache", "fixed", requires_sku=True),
    "microsoft.storage/storageaccounts": _rule(
        "Storage", "usage", query_without_sku=False,
        note="Access tier, redundancy, stored data, operations, and transfer are required.",
    ),
    "microsoft.storage/storageaccounts/blobservices": _rule("Storage", "usage", query_without_sku=False),
    "microsoft.storage/storageaccounts/fileservices": _rule("Storage", "usage", query_without_sku=False),
    "microsoft.apimanagement/service": _rule("API Management", "mixed", requires_sku=True),
    "microsoft.servicebus/namespaces": _rule("Service Bus", "mixed", requires_sku=True),
    "microsoft.eventhub/namespaces": _rule("Event Hubs", "mixed", requires_sku=True),
    "microsoft.eventgrid/topics": _rule("Event Grid", "usage", query_without_sku=False),
    "microsoft.eventgrid/systemtopics": _rule("Event Grid", "usage", query_without_sku=False),
    "microsoft.logic/workflows": _rule("Logic Apps", "usage", query_without_sku=False),
    "microsoft.network/applicationgateways": _rule("Application Gateway", "mixed", requires_sku=True),
    "microsoft.network/loadbalancers": _rule("Load Balancer", "mixed", default_sku="Standard"),
    "microsoft.network/natgateways": _rule("NAT Gateway", "mixed", default_sku="Standard"),
    "microsoft.network/azurefirewalls": _rule("Azure Firewall", "mixed", requires_sku=True),
    "microsoft.network/bastionhosts": _rule("Azure Bastion", "mixed", requires_sku=True),
    "microsoft.network/virtualnetworkgateways": _rule("VPN Gateway", "fixed", requires_sku=True),
    "microsoft.network/publicipaddresses": _rule("Virtual Network", "mixed", default_sku="Standard"),
    "microsoft.network/privateendpoints": _rule(
        "Virtual Network", "usage", query_without_sku=False,
        note="Private Link processing depends on transferred data and service topology.",
    ),
    "microsoft.network/frontdoors": _rule(
        "Azure Front Door Service", "usage", requires_billing_zone=True,
        note="Front Door rates use billing zones rather than the resource location.",
    ),
    "microsoft.cdn/profiles": _rule(
        "Azure Front Door Service", "usage", requires_billing_zone=True,
        note="CDN/Front Door transfer rates require the traffic billing zone.",
    ),
    "microsoft.containerservice/managedclusters": _rule("Azure Kubernetes Service", "mixed", requires_sku=True),
    "microsoft.containerregistry/registries": _rule("Container Registry", "mixed", requires_sku=True),
    "microsoft.app/containerapps": _rule(
        "Azure Container Apps", "usage", query_without_sku=False,
        note="vCPU, memory, requests, and execution duration are required.",
    ),
    "microsoft.containerinstance/containergroups": _rule(
        "Container Instances", "usage", query_without_sku=False,
    ),
    "microsoft.search/searchservices": _rule("Azure Cognitive Search", "mixed", requires_sku=True),
    "microsoft.cognitiveservices/accounts": _rule(
        "Foundry Models", "usage", query_without_sku=False,
        note="A deployed model/version and token or provisioned-throughput usage are required.",
    ),
    "microsoft.datafactory/factories": _rule(
        "Azure Data Factory v2", "usage", query_without_sku=False,
        note="Pipeline activities, integration runtime, and data movement are required.",
    ),
    "microsoft.synapse/workspaces": _rule("Azure Synapse Analytics", "mixed", requires_sku=True),
    "microsoft.powerbidedicated/capacities": _rule("Power BI Embedded", "fixed", requires_sku=True),
    "microsoft.purview/accounts": _rule("Azure Purview", "mixed", requires_sku=True),
    "microsoft.databricks/workspaces": _rule(
        "Azure Databricks", "usage", query_without_sku=False,
        note="Workspace control plane is not the billed cluster configuration.",
    ),
    "microsoft.kusto/clusters": _rule("Azure Data Explorer", "mixed", requires_sku=True),
    "microsoft.insights/components": _rule(
        "Application Insights", "usage", query_without_sku=False,
        note="Ingestion, retention, tests, and enabled features are required.",
    ),
    "microsoft.operationalinsights/workspaces": _rule(
        "Log Analytics", "usage", query_without_sku=False,
        note="Ingestion, commitment tier, retention, and query usage are required.",
    ),
    "microsoft.keyvault/vaults": _rule(
        "Key Vault", "usage", query_without_sku=False,
        note="Operations, key type, certificates, and HSM usage are required.",
    ),
}

# These resources are real architecture nodes but are either free control-plane objects or
# accrue their charges on another resource/meter.  "not_applicable" is intentionally not
# the same as a fabricated zero.
NO_DIRECT_METER: dict[str, str] = {
    "microsoft.web/sites": "Dedicated App Service is billed through its App Service plan; consumption hosting needs execution usage.",
    "microsoft.web/sites/slots": "Deployment slots are priced through the hosting plan.",
    "microsoft.web/sites/functions": "Function execution is usage-based or billed through its hosting plan.",
    "microsoft.sql/servers": "The logical SQL server has no direct meter; databases and related services are billed.",
    "microsoft.app/managedenvironments": "The managed environment is not enough to price its apps and workload profiles.",
    "microsoft.compute/availabilitysets": "Availability sets have no direct retail meter.",
    "microsoft.network/virtualnetworks": "Virtual network objects have no base meter; peering and transfer may be billed separately.",
    "microsoft.network/subnets": "Subnets have no direct retail meter.",
    "microsoft.network/networksecuritygroups": "Network security groups have no direct retail meter.",
    "microsoft.network/networkinterfaces": "Network interfaces have no direct retail meter.",
    "microsoft.network/routetables": "Route tables have no direct retail meter.",
    "microsoft.network/connections": "Connection charges are represented by the owning gateway/circuit meters.",
    "microsoft.managedidentity/userassignedidentities": "Managed identity objects have no direct retail meter.",
    "microsoft.insights/actiongroups": "Notification charges require channel and notification volume.",
    "microsoft.recoveryservices/vaults": "The vault object has no base price; protected instances and storage are billed separately.",
    "microsoft.machinelearningservices/workspaces": "The workspace control plane does not identify billed compute or endpoints.",
}

_PREFIX_RULES: tuple[tuple[str, PricingRule], ...] = (
    ("microsoft.storage/storageaccounts/", RESOURCE_RULES["microsoft.storage/storageaccounts"]),
)

_USAGE_METER_TERMS = (
    "data processed", "data transfer", "data stored", "storage", "request", "operation",
    "token", "execution", "duration", "ingress", "egress", "retention", "message",
    "api call", "calls", "throughput", "capacity unit", "vcore", "iops", "backup",
    "read", "write", "retrieval", "bandwidth", "overage",
)
_EXCLUDED_VARIANTS = ("spot", "low priority", "dev/test", "dev test")


@dataclass(frozen=True)
class CatalogQuery:
    service_name: str
    currency: str
    regions: tuple[str, ...]
    sku_names: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return "|".join((self.currency, self.service_name, ",".join(self.regions), ",".join(self.sku_names)))


def _type_token(value: Any) -> str:
    return str(value or "").strip().lower()


def rule_for_type(arm_type: str | None) -> PricingRule | None:
    token = _type_token(arm_type)
    if token in RESOURCE_RULES:
        return RESOURCE_RULES[token]
    for prefix, rule in _PREFIX_RULES:
        if token.startswith(prefix):
            return rule
    return None


def classify_resource_type(arm_type: str | None) -> dict[str, str]:
    """Classify any ARM type without raising; used by the live provider-catalog audit."""
    token = _type_token(arm_type)
    if not token or token == "__note__":
        return {"state": "not_applicable", "service_name": ""}
    if token in NO_DIRECT_METER:
        return {"state": "not_applicable", "service_name": ""}
    rule = rule_for_type(token)
    if rule:
        return {"state": "mapped", "service_name": rule.service_name}
    return {"state": "unmatched", "service_name": ""}


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(data: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


def _hint(node: dict[str, Any], key: str) -> Any:
    hint = node.get("pricing_hint") if isinstance(node.get("pricing_hint"), dict) else {}
    if key in hint:
        return hint.get(key)
    meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
    aliases = {
        "meter_id": ("pricingMeterId", "meterId"),
        "tier": ("pricingTier", "tier"),
        "capacity": ("pricingCapacity", "capacity", "instanceCount", "instance_count", "replicas"),
        "os_type": ("pricingOs", "osType", "os_type", "operatingSystem"),
        "billing_zone": ("pricingBillingZone", "billingZone", "billing_zone"),
        "spot": ("spot", "priority"),
    }
    for alias in aliases.get(key, (key,)):
        if alias in meta:
            return meta.get(alias)
    return None


def _sku_tokens(node: dict[str, Any]) -> list[str]:
    values = [node.get("sku"), _hint(node, "sku"), _hint(node, "tier")]
    return list(dict.fromkeys(str(v).strip() for v in values if str(v or "").strip()))


def _query_for(node: dict[str, Any], rule: PricingRule, currency: str) -> CatalogQuery | None:
    skus = _sku_tokens(node)
    meter_id = str(_hint(node, "meter_id") or "").strip()
    if rule.requires_sku and not skus and not meter_id:
        return None
    if not rule.query_without_sku and not skus and not meter_id:
        return None

    location = str(node.get("location") or "").strip()
    billing_zone = str(_hint(node, "billing_zone") or "").strip()
    if rule.requires_billing_zone:
        if not billing_zone:
            return None
        regions = (billing_zone, "")
    else:
        if not location:
            return None
        regions = tuple(dict.fromkeys((location.lower(), "Global", "")))
    sku_filter = tuple(skus) if rule.exact_sku_filter else ()
    return CatalogQuery(rule.service_name, currency, regions, sku_filter)


def _empty_node(node: dict[str, Any], status: str, reason: str, currency: str) -> dict[str, Any]:
    return {
        "node_id": str(node.get("id") or ""),
        "arm_type": str(node.get("type") or ""),
        "status": status,
        "currency": currency,
        "region": str(node.get("location") or ""),
        "monthly_estimate": None,
        "quantity": None,
        "confidence": "none",
        "components": [],
        "candidates": [],
        "reason": reason,
        "stale": False,
    }


def _preflight_node(node: dict[str, Any], currency: str) -> tuple[dict[str, Any] | None, PricingRule | None]:
    arm_type = _type_token(node.get("type"))
    if not node.get("arm_id"):
        return _empty_node(node, "not_applicable", "Conceptual/manual nodes are not priced without a real ARM resource identity.", currency), None
    if not arm_type or arm_type == "__note__":
        return _empty_node(node, "not_applicable", "This diagram node is not an Azure billable resource.", currency), None
    if arm_type in NO_DIRECT_METER:
        return _empty_node(node, "not_applicable", NO_DIRECT_METER[arm_type], currency), None
    rule = rule_for_type(arm_type)
    if not rule:
        return _empty_node(node, "unmatched", "No verified ARM-type to Retail Prices mapping is available.", currency), None
    query = _query_for(node, rule, currency)
    if query is None:
        if rule.requires_billing_zone and not _hint(node, "billing_zone"):
            reason = rule.note or "A Retail Prices billing zone is required."
            return _empty_node(node, "rate_only", reason, currency), rule
        if rule.requires_sku and not _sku_tokens(node) and not _hint(node, "meter_id"):
            return _empty_node(node, "unmatched", "A concrete Azure SKU is required to select a retail meter.", currency), rule
        if not node.get("location"):
            return _empty_node(node, "unmatched", "The Azure region is required to select a retail meter.", currency), rule
        return _empty_node(node, "rate_only", rule.note or "Usage and meter selection are required for this service.", currency), rule
    return None, rule


def _catalog_entry(result: RetailFetchResult) -> dict[str, Any]:
    return {
        "cached_at": time.time(),
        "fetched_at": result.fetched_at,
        "items": result.items,
        "truncated": result.truncated,
        "invalid_rows": result.invalid_rows,
    }


async def _load_catalogs(queries: dict[str, CatalogQuery], *, force: bool) -> dict[str, dict[str, Any]]:
    cache = _read_cache()
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    now = time.time()
    resolved: dict[str, dict[str, Any]] = {}
    pending: list[CatalogQuery] = []
    for query in queries.values():
        entry = entries.get(query.key) if isinstance(entries.get(query.key), dict) else None
        age = now - float((entry or {}).get("cached_at") or 0)
        if entry and not force and age < CACHE_TTL_SECONDS:
            resolved[query.key] = {**entry, "cache_age_seconds": int(max(age, 0)), "stale": False, "error": ""}
        else:
            pending.append(query)

    semaphore = asyncio.Semaphore(4)

    async def fetch_one(query: CatalogQuery) -> tuple[CatalogQuery, RetailFetchResult]:
        async with semaphore:
            result = await fetch_retail_prices(
                query.service_name,
                currency=query.currency,
                regions=query.regions,
                sku_names=query.sku_names,
            )
            return query, result

    changed = False
    if pending:
        fetched = await asyncio.gather(*(fetch_one(query) for query in pending))
        for query, result in fetched:
            old = entries.get(query.key) if isinstance(entries.get(query.key), dict) else None
            if not result.error:
                entry = _catalog_entry(result)
                entries[query.key] = entry
                resolved[query.key] = {**entry, "cache_age_seconds": 0, "stale": False, "error": ""}
                changed = True
            elif old:
                age = now - float(old.get("cached_at") or 0)
                resolved[query.key] = {
                    **old, "cache_age_seconds": int(max(age, 0)), "stale": True,
                    "error": "Retail Prices refresh failed; cached rates are shown.",
                }
            else:
                resolved[query.key] = {
                    "items": [], "fetched_at": "", "truncated": False,
                    "invalid_rows": 0, "cache_age_seconds": 0, "stale": False,
                    "error": "Retail Prices are temporarily unavailable.",
                }

    if changed:
        if len(entries) > MAX_CACHE_ENTRIES:
            keep = sorted(
                entries.items(), key=lambda pair: float((pair[1] or {}).get("cached_at") or 0), reverse=True,
            )[:MAX_CACHE_ENTRIES]
            entries = dict(keep)
        _write_cache({"entries": entries})
    return resolved


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _latest_rows(items: list[dict[str, Any]], currency: str) -> list[dict[str, Any]]:
    newest: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("type") != "Consumption":
            continue
        row_currency = str(item.get("currencyCode") or currency).upper()
        if row_currency and row_currency != currency:
            continue
        key = str(item.get("meterId") or "|").lower() or (
            f"{item.get('skuId')}|{item.get('meterName')}|{item.get('unitOfMeasure')}"
        )
        if key not in newest or str(item.get("effectiveStartDate") or "") > str(newest[key].get("effectiveStartDate") or ""):
            newest[key] = item
    return list(newest.values())


def _group_rows(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = str(item.get("skuId") or "").strip()
        if not key:
            key = "|".join((str(item.get("productId") or item.get("productName") or ""), str(item.get("skuName") or item.get("armSkuName") or "")))
        groups.setdefault(key, []).append(item)
    return list(groups.values())


def _group_text(group: list[dict[str, Any]]) -> str:
    return " ".join(
        str(group[0].get(field) or "") for field in ("productName", "skuName", "armSkuName")
    ) + " " + " ".join(str(row.get("meterName") or "") for row in group)


def _score_group(group: list[dict[str, Any]], node: dict[str, Any], rule: PricingRule) -> tuple[int, str]:
    raw_tokens = _sku_tokens(node)
    tokens = [_norm(value) for value in raw_tokens if _norm(value)]
    fields = {
        _norm(row.get(field))
        for row in group
        for field in ("armSkuName", "skuName", "meterName")
        if _norm(row.get(field))
    }
    score = 0
    confidence = "low"
    for token in tokens:
        if token in fields:
            score = max(score, 100)
            confidence = "high"
        elif any(token in field or field in token for field in fields if len(field) >= 3):
            score = max(score, 50)
            confidence = "medium"
    if not tokens and rule.default_sku:
        default = _norm(rule.default_sku)
        if default in fields or default in _norm(_group_text(group)):
            score, confidence = 25, "medium"
    return score, confidence


def _filter_variants(groups: list[list[dict[str, Any]]], node: dict[str, Any]) -> list[list[dict[str, Any]]]:
    wants_spot = str(_hint(node, "spot") or "").strip().lower() in ("true", "1", "spot", "low")
    os_type = str(_hint(node, "os_type") or "").strip().lower()
    filtered: list[list[dict[str, Any]]] = []
    for group in groups:
        text = _group_text(group).lower()
        if not wants_spot and any(term in text for term in _EXCLUDED_VARIANTS):
            continue
        if os_type.startswith("linux") and "windows" in text:
            continue
        if os_type.startswith("windows") and "linux" in text:
            continue
        filtered.append(group)
    return filtered or groups


def _fixed_factor(row: dict[str, Any], rule: PricingRule) -> float | None:
    if rule.mode == "usage" or float(row.get("tierMinimumUnits") or 0) > 0:
        return None
    meter = str(row.get("meterName") or "").lower()
    if any(term in meter for term in _USAGE_METER_TERMS):
        return None
    unit = str(row.get("unitOfMeasure") or "").strip().lower().replace(" ", "")
    if unit in ("1hour", "1/hour"):
        return MONTHLY_HOURS
    if unit in ("1/month", "1month"):
        return 1.0
    return None


def _quantity(node: dict[str, Any], rule: PricingRule) -> float | None:
    if not rule.quantity_from_capacity:
        return 1.0
    try:
        value = float(_hint(node, "capacity"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _component(row: dict[str, Any], rule: PricingRule) -> dict[str, Any]:
    return {
        "meter_id": str(row.get("meterId") or ""),
        "service_name": str(row.get("serviceName") or rule.service_name),
        "product_name": str(row.get("productName") or ""),
        "sku_name": str(row.get("skuName") or row.get("armSkuName") or ""),
        "meter_name": str(row.get("meterName") or ""),
        "retail_price": float(row.get("retailPrice") or 0),
        "unit_of_measure": str(row.get("unitOfMeasure") or ""),
        "effective_start_date": str(row.get("effectiveStartDate") or ""),
        "fixed_baseline": _fixed_factor(row, rule) is not None,
    }


def _candidate(group: list[dict[str, Any]], rule: PricingRule) -> dict[str, Any]:
    first = group[0]
    return {
        "meter_id": str(first.get("meterId") or ""),
        "product_name": str(first.get("productName") or ""),
        "sku_name": str(first.get("skuName") or first.get("armSkuName") or ""),
        "component_count": len(group),
        "components": [_component(row, rule) for row in group[:4]],
    }


def _project_node(
    node: dict[str, Any],
    rule: PricingRule,
    catalog: dict[str, Any],
    currency: str,
) -> dict[str, Any]:
    if catalog.get("error") and not catalog.get("items"):
        result = _empty_node(node, "unavailable", str(catalog["error"]), currency)
        result["stale"] = bool(catalog.get("stale"))
        return result

    rows = _latest_rows(list(catalog.get("items") or []), currency)
    meter_id = str(_hint(node, "meter_id") or "").strip().lower()
    groups = _filter_variants(_group_rows(rows), node)
    confidence = "low"
    if meter_id:
        groups = [group for group in groups if any(str(row.get("meterId") or "").lower() == meter_id for row in group)]
        confidence = "high"
    else:
        scored = [(_score_group(group, node, rule), group) for group in groups]
        best = max((score for (score, _), _group in scored), default=0)
        if best > 0:
            groups = [group for (score, _confidence), group in scored if score == best]
            confidence = next((_confidence for (score, _confidence), _group in scored if score == best), "low")
        elif rule.requires_sku or _sku_tokens(node):
            groups = []

    if not groups:
        status = "unavailable" if catalog.get("truncated") else "unmatched"
        reason = (
            "The bounded Retail Prices result was incomplete; no safe meter match was asserted."
            if catalog.get("truncated")
            else "Azure returned no Consumption meter matching this resource's region and SKU."
        )
        result = _empty_node(node, status, reason, currency)
        result["stale"] = bool(catalog.get("stale"))
        return result

    if len(groups) > 1:
        result = _empty_node(
            node, "ambiguous",
            "Multiple retail SKU groups match; select a meter or add OS/tier details.",
            currency,
        )
        result.update({
            "confidence": confidence,
            "candidates": [_candidate(group, rule) for group in groups[:MAX_CANDIDATES]],
            "stale": bool(catalog.get("stale")),
        })
        return result

    group = groups[0]
    components = [_component(row, rule) for row in group[:MAX_COMPONENTS]]
    fixed_rows = [row for row in group if _fixed_factor(row, rule) is not None]
    quantity = _quantity(node, rule)
    monthly: float | None = None
    if len(fixed_rows) == 1 and quantity is not None:
        factor = _fixed_factor(fixed_rows[0], rule)
        monthly = round(float(fixed_rows[0].get("retailPrice") or 0) * float(factor or 0) * quantity, 6)

    if monthly == 0 and len(group) == 1 and float(group[0].get("retailPrice") or 0) == 0:
        status = "free"
        reason = "Azure returned an authoritative zero-priced Consumption meter."
    elif monthly is not None:
        status = "priced_monthly"
        usage_count = sum(1 for component in components if not component["fixed_baseline"])
        reason = (
            f"Fixed retail baseline only; {usage_count} usage-dependent component(s) are excluded."
            if usage_count else "Fixed retail baseline derived from the matched meter."
        )
    else:
        status = "rate_only"
        reason = rule.note or "Retail unit rates are available, but usage or component quantities are required."

    return {
        "node_id": str(node.get("id") or ""),
        "arm_type": str(node.get("type") or ""),
        "status": status,
        "currency": currency,
        "region": str(node.get("location") or ""),
        "monthly_estimate": monthly,
        "quantity": quantity,
        "confidence": confidence,
        "components": components,
        "candidates": [],
        "reason": reason,
        "stale": bool(catalog.get("stale")),
    }


async def price_architecture(
    architecture: dict[str, Any],
    currency: str = "USD",
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Resolve every diagram node to an explicit, non-fabricated pricing state."""
    currency = normalize_currency(currency)
    nodes = [node for node in architecture.get("nodes", []) if isinstance(node, dict)]
    preflight: dict[str, dict[str, Any]] = {}
    rules: dict[str, PricingRule] = {}
    node_queries: dict[str, CatalogQuery] = {}
    queries: dict[str, CatalogQuery] = {}

    for node in nodes:
        node_id = str(node.get("id") or "")
        terminal, rule = _preflight_node(node, currency)
        if terminal is not None:
            preflight[node_id] = terminal
            continue
        assert rule is not None
        query = _query_for(node, rule, currency)
        assert query is not None
        rules[node_id] = rule
        node_queries[node_id] = query
        queries[query.key] = query

    catalogs = await _load_catalogs(queries, force=force) if queries else {}
    priced_nodes: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id in preflight:
            priced_nodes.append(preflight[node_id])
            continue
        query = node_queries[node_id]
        priced_nodes.append(_project_node(node, rules[node_id], catalogs[query.key], currency))

    counts: dict[str, int] = {}
    total = 0.0
    for node in priced_nodes:
        status = str(node.get("status") or "unavailable")
        counts[status] = counts.get(status, 0) + 1
        if node.get("monthly_estimate") is not None:
            total += float(node["monthly_estimate"])
    fetched_times = [str(catalog.get("fetched_at") or "") for catalog in catalogs.values() if catalog.get("fetched_at")]
    stale = any(bool(catalog.get("stale")) for catalog in catalogs.values())
    unavailable = counts.get("unavailable", 0)
    covered = counts.get("priced_monthly", 0) + counts.get("rate_only", 0) + counts.get("free", 0)
    return {
        "architecture_id": str(architecture.get("id") or ""),
        "source": "azure_retail_prices",
        "currency": currency,
        "as_of": max(fetched_times, default=datetime.now(timezone.utc).isoformat()),
        "monthly_hours": MONTHLY_HOURS,
        "stale": stale,
        "partial": covered < len(priced_nodes),
        "error": "Some retail prices are unavailable." if unavailable else "",
        "nodes": priced_nodes,
        "summary": {
            "known_fixed_monthly": round(total, 6),
            "node_count": len(priced_nodes),
            "covered_count": covered,
            "counts": counts,
        },
    }
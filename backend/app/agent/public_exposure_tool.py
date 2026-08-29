"""Deterministic public-endpoint inventory for chat."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.connectors.base import ConnectorTool, err, ok
from app.exec.command_runner import run_kql_collect

TOOL_NAME = "azure_public_exposure_inventory"
_MAX_SUBSCRIPTIONS = 25
_MAX_ROWS = 5000

_NETWORK_TYPES = (
    "microsoft.network/publicipaddresses",
    "microsoft.network/publicipprefixes",
    "microsoft.network/loadbalancers",
    "microsoft.network/applicationgateways",
    "microsoft.network/azurefirewalls",
    "microsoft.network/natgateways",
    "microsoft.network/bastionhosts",
    "microsoft.network/virtualnetworkgateways",
    "microsoft.network/trafficmanagerprofiles",
    "microsoft.network/frontdoors",
    "microsoft.cdn/profiles/endpoints",
    "microsoft.cdn/profiles/afdendpoints",
    "microsoft.apimanagement/service",
)
_PAAS_TYPES = (
    "microsoft.storage/storageaccounts",
    "microsoft.sql/servers",
    "microsoft.sql/managedinstances",
    "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.dbformysql/flexibleservers",
    "microsoft.documentdb/databaseaccounts",
    "microsoft.keyvault/vaults",
    "microsoft.keyvault/managedhsms",
    "microsoft.containerregistry/registries",
    "microsoft.servicebus/namespaces",
    "microsoft.eventhub/namespaces",
    "microsoft.cache/redis",
    "microsoft.cache/redisenterprise",
    "microsoft.search/searchservices",
    "microsoft.cognitiveservices/accounts",
    "microsoft.signalrservice/signalr",
    "microsoft.signalrservice/webpubsub",
    "microsoft.insights/components",
)
_WEB_TYPES = (
    "microsoft.web/sites",
    "microsoft.web/sites/slots",
    "microsoft.app/containerapps",
    "microsoft.containerservice/managedclusters",
    "microsoft.logic/workflows",
)


def _quoted(values: list[str]) -> str:
    return ", ".join(json.dumps(value) for value in values)


def _types(values: tuple[str, ...]) -> str:
    return ", ".join(json.dumps(value) for value in values)


def _queries(subscription_ids: list[str]) -> list[tuple[str, str]]:
    scope = _quoted(subscription_ids)
    return [
        (
            "network_edge",
            f"""Resources
| where subscriptionId in~ ({scope})
| where type in~ ({_types(_NETWORK_TYPES)})
| project subscriptionId, resourceGroup, name, type=tolower(type), location, id,
    ipAddress=tostring(properties.ipAddress), fqdn=tostring(properties.dnsSettings.fqdn),
    provisioningState=tostring(properties.provisioningState),
    publicIpAllocation=tostring(properties.publicIPAllocationMethod),
    frontendIpConfigurations=properties.frontendIPConfigurations,
    ipConfigurations=properties.ipConfigurations, sku=tostring(sku.name)
| order by type asc, resourceGroup asc, name asc""",
        ),
        (
            "paas",
            f"""Resources
| where subscriptionId in~ ({scope})
| where type in~ ({_types(_PAAS_TYPES)})
| project subscriptionId, resourceGroup, name, type=tolower(type), location, id,
    publicNetworkAccess=tostring(properties.publicNetworkAccess),
    defaultAction=tostring(coalesce(properties.networkAcls.defaultAction, properties.networkRuleSet.defaultAction)),
    allowBlobPublicAccess=tostring(properties.allowBlobPublicAccess),
    hostName=coalesce(tostring(properties.fullyQualifiedDomainName), tostring(properties.hostName), tostring(properties.endpoint)),
    ipRules=properties.ipRules, virtualNetworkRules=properties.virtualNetworkRules,
    minimumTlsVersion=coalesce(tostring(properties.minimumTlsVersion), tostring(properties.minimalTlsVersion)),
    sku=tostring(sku.name)
| order by type asc, resourceGroup asc, name asc""",
        ),
        (
            "web_and_workflows",
            f"""Resources
| where subscriptionId in~ ({scope})
| where type in~ ({_types(_WEB_TYPES)})
| project subscriptionId, resourceGroup, name, type=tolower(type), location, id,
    publicNetworkAccess=tostring(properties.publicNetworkAccess),
    defaultHostName=tostring(properties.defaultHostName),
    fqdn=coalesce(tostring(properties.configuration.ingress.fqdn), tostring(properties.fqdn)),
    externalIngress=tostring(properties.configuration.ingress.external),
    privateCluster=tostring(properties.apiServerAccessProfile.enablePrivateCluster),
    httpsOnly=tostring(properties.httpsOnly), state=tostring(properties.state),
    ipSecurityRestrictions=properties.siteConfig.ipSecurityRestrictions,
    accessEndpoint=tostring(properties.accessEndpoint), triggers=properties.definition.triggers,
    sku=tostring(sku.name)
| order by type asc, resourceGroup asc, name asc""",
        ),
    ]


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "enabled", "allow"}


def _trigger_summary(triggers: Any) -> dict[str, Any]:
    if not isinstance(triggers, dict):
        return {"trigger_count": 0, "http_trigger_count": 0}
    http_count = 0
    for trigger in triggers.values():
        if not isinstance(trigger, dict):
            continue
        kind = f"{trigger.get('type', '')} {trigger.get('kind', '')}".lower()
        if "request" in kind or "http" in kind:
            http_count += 1
    return {"trigger_count": len(triggers), "http_trigger_count": http_count}


def _classify(row: dict[str, Any], category: str) -> dict[str, Any]:
    resource_type = str(row.get("type") or "").lower()
    public_network = str(row.get("publicNetworkAccess") or "").lower()
    default_action = str(row.get("defaultAction") or "").lower()
    if category == "network_edge":
        status = "public_endpoint_resource"
    elif resource_type == "microsoft.app/containerapps":
        status = "public" if _truthy(row.get("externalIngress")) else "not_public"
    elif resource_type == "microsoft.containerservice/managedclusters":
        status = "not_public" if _truthy(row.get("privateCluster")) else "public_api_possible"
    elif resource_type == "microsoft.logic/workflows":
        summary = _trigger_summary(row.get("triggers"))
        status = "public_http_trigger" if summary["http_trigger_count"] else "no_http_trigger_found"
        row = {**row, **summary}
    elif public_network == "disabled" or default_action == "deny":
        status = "restricted"
    elif public_network in {"enabled", "true"} or default_action in {"allow", ""}:
        status = "public_or_firewall_controlled"
    else:
        status = "undetermined"
    return {**row, "category": category, "exposure_status": status}


async def _discover_visible(connection: dict[str, Any] | None) -> tuple[list[str], str]:
    from app.azure.arm import list_subscriptions
    from app.azure.credentials import get_arm_token

    token, token_error = await get_arm_token(connection or {})
    if not token:
        return [], (token_error or "No Azure token is available for this connection.")[:300]
    subscriptions, error = await list_subscriptions(token)
    if error:
        return [], error[:300]
    return [
        str(subscription["id"]) for subscription in subscriptions
        if subscription.get("id") and (
            not subscription.get("state") or subscription.get("state") == "Enabled"
        )
    ], ""


def make_public_exposure_query(
    connection: dict[str, Any] | None,
    *,
    allowed_subscription_ids: list[str] | None = None,
):
    async def _handler(_config: dict[str, Any], _args: dict[str, Any]) -> dict[str, Any]:
        subscription_ids = list(dict.fromkeys(allowed_subscription_ids or []))
        if not subscription_ids:
            subscription_ids, error = await _discover_visible(connection)
            if error:
                return err(error)
        omitted = subscription_ids[_MAX_SUBSCRIPTIONS:]
        subscription_ids = subscription_ids[:_MAX_SUBSCRIPTIONS]
        if not subscription_ids:
            return err("No visible subscription is in the selected chat scope.")

        async def run_one(category: str, query: str):
            result = await run_kql_collect(query, connection, max_rows=_MAX_ROWS)
            return category, result

        results = await asyncio.gather(*(run_one(category, query) for category, query in _queries(subscription_ids)))
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        source_counts: dict[str, int] = {}
        source_totals: dict[str, int | None] = {}
        incomplete_sources: list[dict[str, Any]] = []
        for category, result in results:
            if not result.ok:
                errors.append({"source": category, "error": (result.error or "Query failed.")[:400]})
                continue
            classified = [_classify(dict(row), category) for row in result.rows]
            rows.extend(classified)
            source_counts[category] = len(classified)
            source_totals[category] = result.total
            if not result.complete:
                incomplete_sources.append({
                    "source": category,
                    "rows_collected": len(classified),
                    "total_rows": result.total,
                    "reason": f"Collection reached the {_MAX_ROWS}-row safety limit.",
                })

        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for row in rows:
            status = str(row["exposure_status"])
            resource_type = str(row.get("type") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            type_counts[resource_type] = type_counts.get(resource_type, 0) + 1
        inventory_complete = not errors and not incomplete_sources and not omitted
        payload = {
            "status": "complete" if inventory_complete else "partial",
            "inventory_complete": inventory_complete,
            "subscriptions_queried": len(subscription_ids),
            "subscriptions_omitted": len(omitted),
            "rows": rows,
            "row_count": len(rows),
            "counts_by_exposure_status": status_counts,
            "counts_by_resource_type": dict(sorted(type_counts.items())),
            "source_counts": source_counts,
            "source_totals": source_totals,
            "incomplete_sources": incomplete_sources,
            "errors": errors,
            "scope_note": "Only subscriptions in the selected chat scope were queried.",
            "interpretation_note": (
                "Public or firewall-controlled means the service exposes a public endpoint; "
                "network rules may still restrict which clients can connect."
            ),
        }
        return ok(
            json.dumps(payload, ensure_ascii=False, default=str),
            f"Inventoried {len(rows)} potential public endpoint resource(s)",
        )

    return _handler


def register_public_exposure_tool(
    toolset: Any,
    *,
    connection: dict[str, Any] | None,
    allowed_subscription_ids: list[str] | None = None,
) -> None:
    toolset.add_connector({}, [ConnectorTool(
        name=TOOL_NAME,
        description=(
            "Deterministically inventory public endpoints and public-network controls across "
            "network edge resources, PaaS services, apps, clusters, and Logic App HTTP triggers. "
            "Use this ONE tool for complete public exposure inventory; do not decompose the same "
            "question into Resource Graph, Storage data-plane, or deployment-tool calls. Read-only."
        ),
        parameters={"type": "object", "properties": {}},
        kind="read",
        handler=make_public_exposure_query(
            connection, allowed_subscription_ids=allowed_subscription_ids,
        ),
    )])


__all__ = ["TOOL_NAME", "make_public_exposure_query", "register_public_exposure_tool"]

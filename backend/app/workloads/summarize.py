"""Friendly resource-type naming and workload summarization (type breakdown)."""
from __future__ import annotations

from typing import Any

# ARM type (lowercase) -> friendly, pluralized category label.
_FRIENDLY: dict[str, str] = {
    "microsoft.compute/virtualmachines": "Virtual Machines",
    "microsoft.compute/virtualmachinescalesets": "VM Scale Sets",
    "microsoft.compute/disks": "Managed Disks",
    "microsoft.compute/availabilitysets": "Availability Sets",
    "microsoft.compute/images": "VM Images",
    "microsoft.storage/storageaccounts": "Storage Accounts",
    "microsoft.sql/servers": "SQL Servers",
    "microsoft.sql/servers/databases": "SQL Databases",
    "microsoft.sql/managedinstances": "SQL Managed Instances",
    "microsoft.dbforpostgresql/servers": "PostgreSQL Servers",
    "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL Servers",
    "microsoft.dbformysql/servers": "MySQL Servers",
    "microsoft.dbformysql/flexibleservers": "MySQL Servers",
    "microsoft.documentdb/databaseaccounts": "Cosmos DB",
    "microsoft.network/applicationgateways": "Application Gateways",
    "microsoft.network/loadbalancers": "Load Balancers",
    "microsoft.network/virtualnetworks": "Virtual Networks",
    "microsoft.network/networksecuritygroups": "Network Security Groups",
    "microsoft.network/publicipaddresses": "Public IP Addresses",
    "microsoft.network/networkinterfaces": "Network Interfaces",
    "microsoft.network/privateendpoints": "Private Endpoints",
    "microsoft.network/azurefirewalls": "Azure Firewalls",
    "microsoft.network/bastionhosts": "Bastion Hosts",
    "microsoft.network/natgateways": "NAT Gateways",
    "microsoft.network/dnszones": "DNS Zones",
    "microsoft.network/privatednszones": "Private DNS Zones",
    "microsoft.network/frontdoors": "Front Doors",
    "microsoft.network/trafficmanagerprofiles": "Traffic Manager Profiles",
    "microsoft.cdn/profiles": "CDN / Front Door",
    "microsoft.keyvault/vaults": "Key Vaults",
    "microsoft.web/sites": "App Services",
    "microsoft.web/serverfarms": "App Service Plans",
    "microsoft.web/staticsites": "Static Web Apps",
    "microsoft.containerservice/managedclusters": "AKS Clusters",
    "microsoft.containerregistry/registries": "Container Registries",
    "microsoft.app/containerapps": "Container Apps",
    "microsoft.app/managedenvironments": "Container Apps Environments",
    "microsoft.cache/redis": "Redis Caches",
    "microsoft.servicebus/namespaces": "Service Bus Namespaces",
    "microsoft.eventhub/namespaces": "Event Hubs Namespaces",
    "microsoft.eventgrid/topics": "Event Grid Topics",
    "microsoft.insights/components": "Application Insights",
    "microsoft.insights/actiongroups": "Action Groups",
    "microsoft.operationalinsights/workspaces": "Log Analytics Workspaces",
    "microsoft.recoveryservices/vaults": "Recovery Services Vaults",
    "microsoft.apimanagement/service": "API Management",
    "microsoft.logic/workflows": "Logic Apps",
    "microsoft.datafactory/factories": "Data Factories",
    "microsoft.cognitiveservices/accounts": "Cognitive Services",
    "microsoft.machinelearningservices/workspaces": "ML Workspaces",
    "microsoft.search/searchservices": "Cognitive Search",
    "microsoft.signalrservice/signalr": "SignalR",
    "microsoft.managedidentity/userassignedidentities": "Managed Identities",
}


def friendly_type(arm_type: str | None) -> str:
    """Map an ARM resource type to a friendly, pluralized category label."""
    t = (arm_type or "").lower().strip()
    if not t:
        return "Other"
    if t in _FRIENDLY:
        return _FRIENDLY[t]
    # Fallback: clean the last path segment.
    seg = t.split("/")[-1].replace("-", " ").replace("_", " ")
    label = seg.title() or "Other"
    if not label.endswith("s"):
        label += "s"
    return label


def type_breakdown(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count resources by friendly type. Returns [{label, count}] sorted desc."""
    counts: dict[str, int] = {}
    for r in resources:
        label = friendly_type(r.get("resource_type") or r.get("type"))
        counts[label] = counts.get(label, 0) + 1
    out = [{"label": k, "count": v} for k, v in counts.items()]
    out.sort(key=lambda x: (-x["count"], x["label"]))
    return out


def summarize_nodes(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a summary from a workload's node list (no Azure calls).

    For resource nodes the type breakdown is exact; scope nodes (mg/sub/rg) are counted
    separately since their full contents aren't known without enumeration."""
    scope_counts = {"mg": 0, "subscription": 0, "resource_group": 0, "resource": 0}
    resources: list[dict[str, Any]] = []
    for n in nodes or []:
        kind = n.get("kind", "resource")
        scope_counts[kind] = scope_counts.get(kind, 0) + 1
        if kind == "resource":
            resources.append(n)
    return {
        "types": type_breakdown(resources),
        "total_resources": len(resources),
        "scope_counts": scope_counts,
    }

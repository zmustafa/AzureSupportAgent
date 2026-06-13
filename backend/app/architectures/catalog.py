"""Architecture catalog: resource-type taxonomy (category/layer) + the drag-drop palette.

Deterministic, no Azure calls. ``categorize`` maps an ARM type to a visual category that
drives node color/icon; ``layer_for`` maps a category to an architectural tier used by the
auto-layout. ``PALETTE`` is a curated list of common Azure resource types the manual
builder can drag onto the canvas.
"""
from __future__ import annotations

from typing import Any

# Category id -> display label + a hex color used for node accents in the UI.
CATEGORY_META: dict[str, dict[str, str]] = {
    "web": {"label": "Web & API", "color": "#2563eb"},
    "compute": {"label": "Compute", "color": "#7c3aed"},
    "containers": {"label": "Containers", "color": "#0891b2"},
    "data": {"label": "Databases", "color": "#dc2626"},
    "storage": {"label": "Storage", "color": "#ea580c"},
    "integration": {"label": "Integration", "color": "#16a34a"},
    "networking": {"label": "Networking", "color": "#0d9488"},
    "security": {"label": "Security & Identity", "color": "#b91c1c"},
    "ai": {"label": "AI & ML", "color": "#9333ea"},
    "monitoring": {"label": "Monitoring", "color": "#ca8a04"},
    "analytics": {"label": "Analytics", "color": "#0284c7"},
    "other": {"label": "Other", "color": "#6b7280"},
}

# Architectural tier each category sits in (drives the layered auto-layout, top→bottom).
LAYER_ORDER = [
    "edge",
    "presentation",
    "application",
    "integration",
    "data",
    "networking",
    "security",
    "monitoring",
    "shared",
]
_CATEGORY_LAYER: dict[str, str] = {
    "web": "presentation",
    "compute": "application",
    "containers": "application",
    "ai": "application",
    "integration": "integration",
    "data": "data",
    "storage": "data",
    "analytics": "data",
    "networking": "networking",
    "security": "security",
    "monitoring": "monitoring",
    "other": "shared",
}

# ARM type (lowercase) -> category. Checked exact-first, then by prefix.
_TYPE_CATEGORY: dict[str, str] = {
    "microsoft.web/sites": "web",
    "microsoft.web/sites/slots": "web",
    "microsoft.web/staticsites": "web",
    "microsoft.web/serverfarms": "compute",
    "microsoft.apimanagement/service": "integration",
    "microsoft.cdn/profiles": "edge",
    "microsoft.network/frontdoors": "edge",
    "microsoft.network/applicationgateways": "networking",
    "microsoft.network/loadbalancers": "networking",
    "microsoft.network/trafficmanagerprofiles": "edge",
    "microsoft.compute/virtualmachines": "compute",
    "microsoft.compute/virtualmachinescalesets": "compute",
    "microsoft.compute/disks": "storage",
    "microsoft.compute/availabilitysets": "compute",
    "microsoft.containerservice/managedclusters": "containers",
    "microsoft.containerregistry/registries": "containers",
    "microsoft.app/containerapps": "containers",
    "microsoft.app/managedenvironments": "containers",
    "microsoft.web/sites/functions": "compute",
    "microsoft.sql/servers": "data",
    "microsoft.sql/servers/databases": "data",
    "microsoft.sql/managedinstances": "data",
    "microsoft.dbforpostgresql/servers": "data",
    "microsoft.dbforpostgresql/flexibleservers": "data",
    "microsoft.dbformysql/servers": "data",
    "microsoft.dbformysql/flexibleservers": "data",
    "microsoft.documentdb/databaseaccounts": "data",
    "microsoft.cache/redis": "data",
    "microsoft.storage/storageaccounts": "storage",
    "microsoft.servicebus/namespaces": "integration",
    "microsoft.eventhub/namespaces": "integration",
    "microsoft.eventgrid/topics": "integration",
    "microsoft.eventgrid/systemtopics": "integration",
    "microsoft.logic/workflows": "integration",
    "microsoft.relay/namespaces": "integration",
    "microsoft.network/virtualnetworks": "networking",
    "microsoft.network/networksecuritygroups": "networking",
    "microsoft.network/publicipaddresses": "networking",
    "microsoft.network/networkinterfaces": "networking",
    "microsoft.network/privateendpoints": "networking",
    "microsoft.network/privatednszones": "networking",
    "microsoft.network/dnszones": "networking",
    "microsoft.network/natgateways": "networking",
    "microsoft.network/azurefirewalls": "networking",
    "microsoft.network/bastionhosts": "networking",
    "microsoft.network/virtualnetworkgateways": "networking",
    "microsoft.network/connections": "networking",
    "microsoft.keyvault/vaults": "security",
    "microsoft.managedidentity/userassignedidentities": "security",
    "microsoft.insights/components": "monitoring",
    "microsoft.insights/actiongroups": "monitoring",
    "microsoft.operationalinsights/workspaces": "monitoring",
    "microsoft.recoveryservices/vaults": "storage",
    "microsoft.cognitiveservices/accounts": "ai",
    "microsoft.machinelearningservices/workspaces": "ai",
    "microsoft.search/searchservices": "ai",
    "microsoft.datafactory/factories": "analytics",
    "microsoft.synapse/workspaces": "analytics",
    "microsoft.databricks/workspaces": "analytics",
    "microsoft.kusto/clusters": "analytics",
}

# Prefix → category fallback (by provider/namespace) when the exact type isn't mapped.
_PREFIX_CATEGORY: list[tuple[str, str]] = [
    ("microsoft.web/", "web"),
    ("microsoft.compute/", "compute"),
    ("microsoft.containerservice/", "containers"),
    ("microsoft.containerregistry/", "containers"),
    ("microsoft.app/", "containers"),
    ("microsoft.sql/", "data"),
    ("microsoft.dbfor", "data"),
    ("microsoft.documentdb/", "data"),
    ("microsoft.cache/", "data"),
    ("microsoft.storage/", "storage"),
    ("microsoft.servicebus/", "integration"),
    ("microsoft.eventhub/", "integration"),
    ("microsoft.eventgrid/", "integration"),
    ("microsoft.logic/", "integration"),
    ("microsoft.apimanagement/", "integration"),
    ("microsoft.network/", "networking"),
    ("microsoft.cdn/", "edge"),
    ("microsoft.keyvault/", "security"),
    ("microsoft.managedidentity/", "security"),
    ("microsoft.aad", "security"),
    ("microsoft.insights/", "monitoring"),
    ("microsoft.operationalinsights/", "monitoring"),
    ("microsoft.recoveryservices/", "storage"),
    ("microsoft.cognitiveservices/", "ai"),
    ("microsoft.machinelearningservices/", "ai"),
    ("microsoft.search/", "ai"),
    ("microsoft.datafactory/", "analytics"),
    ("microsoft.synapse/", "analytics"),
    ("microsoft.databricks/", "analytics"),
    ("microsoft.kusto/", "analytics"),
]


def categorize(arm_type: str | None) -> str:
    """Map an ARM resource type to a visual category id (defaults to 'other')."""
    t = (arm_type or "").lower().strip()
    if not t:
        return "other"
    if t in _TYPE_CATEGORY:
        return _TYPE_CATEGORY[t]
    for prefix, cat in _PREFIX_CATEGORY:
        if t.startswith(prefix):
            return cat
    return "other"


def layer_for(category: str) -> str:
    """The architectural tier a category sits in (for the layered auto-layout)."""
    return _CATEGORY_LAYER.get(category, "shared")


# Curated palette: common Azure types the manual builder can drag onto the canvas,
# grouped by category. Each item: {type, label}.
PALETTE: list[dict[str, Any]] = [
    {"type": "microsoft.web/sites", "label": "App Service"},
    {"type": "microsoft.web/staticsites", "label": "Static Web App"},
    {"type": "microsoft.web/serverfarms", "label": "App Service Plan"},
    {"type": "microsoft.web/sites/functions", "label": "Function App"},
    {"type": "microsoft.apimanagement/service", "label": "API Management"},
    {"type": "microsoft.cdn/profiles", "label": "Front Door / CDN"},
    {"type": "microsoft.compute/virtualmachines", "label": "Virtual Machine"},
    {"type": "microsoft.compute/virtualmachinescalesets", "label": "VM Scale Set"},
    {"type": "microsoft.containerservice/managedclusters", "label": "AKS Cluster"},
    {"type": "microsoft.app/containerapps", "label": "Container App"},
    {"type": "microsoft.containerregistry/registries", "label": "Container Registry"},
    {"type": "microsoft.sql/servers", "label": "SQL Server"},
    {"type": "microsoft.sql/servers/databases", "label": "SQL Database"},
    {"type": "microsoft.dbforpostgresql/flexibleservers", "label": "PostgreSQL"},
    {"type": "microsoft.dbformysql/flexibleservers", "label": "MySQL"},
    {"type": "microsoft.documentdb/databaseaccounts", "label": "Cosmos DB"},
    {"type": "microsoft.cache/redis", "label": "Redis Cache"},
    {"type": "microsoft.storage/storageaccounts", "label": "Storage Account"},
    {"type": "microsoft.servicebus/namespaces", "label": "Service Bus"},
    {"type": "microsoft.eventhub/namespaces", "label": "Event Hubs"},
    {"type": "microsoft.eventgrid/topics", "label": "Event Grid"},
    {"type": "microsoft.logic/workflows", "label": "Logic App"},
    {"type": "microsoft.network/virtualnetworks", "label": "Virtual Network"},
    {"type": "microsoft.network/applicationgateways", "label": "Application Gateway"},
    {"type": "microsoft.network/loadbalancers", "label": "Load Balancer"},
    {"type": "microsoft.network/azurefirewalls", "label": "Azure Firewall"},
    {"type": "microsoft.network/privateendpoints", "label": "Private Endpoint"},
    {"type": "microsoft.network/publicipaddresses", "label": "Public IP"},
    {"type": "microsoft.keyvault/vaults", "label": "Key Vault"},
    {"type": "microsoft.managedidentity/userassignedidentities", "label": "Managed Identity"},
    {"type": "microsoft.cognitiveservices/accounts", "label": "Azure AI / OpenAI"},
    {"type": "microsoft.search/searchservices", "label": "AI Search"},
    {"type": "microsoft.machinelearningservices/workspaces", "label": "ML Workspace"},
    {"type": "microsoft.datafactory/factories", "label": "Data Factory"},
    {"type": "microsoft.synapse/workspaces", "label": "Synapse"},
    {"type": "microsoft.insights/components", "label": "App Insights"},
    {"type": "microsoft.operationalinsights/workspaces", "label": "Log Analytics"},
]


def public_catalog() -> dict[str, Any]:
    """Catalog payload for the manual builder: categories + palette (decorated)."""
    overrides: dict[str, str] = {}
    try:
        from app.core.app_settings import architecture_category_colors

        overrides = architecture_category_colors()
    except Exception:  # noqa: BLE001
        overrides = {}
    palette = [
        {**item, "category": categorize(item["type"])}
        for item in PALETTE
    ]
    return {
        "categories": [
            {"id": cid, **meta, "color": overrides.get(cid, meta["color"])}
            for cid, meta in CATEGORY_META.items()
        ],
        "layers": LAYER_ORDER,
        "palette": palette,
    }

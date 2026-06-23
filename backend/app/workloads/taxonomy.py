"""Resource-type taxonomy: map an ARM resource type to a high-level CATEGORY.

The category powers the composition visualizations on the Workloads command center (how many
Compute / Networking / Data / Security / … resources a workload has) and the per-type
breakdown rings. It complements :mod:`app.workloads.summarize` which produces the friendly,
pluralized *type* label; here we roll those types up into a small, stable set of categories.

Pure / offline — no Azure calls. Keep the category set SMALL and stable; the frontend mirrors
these keys for colors + icons.
"""
from __future__ import annotations

from typing import Any

# The canonical, ordered category set. Order = display order in composition charts.
CATEGORIES: tuple[str, ...] = (
    "Compute",
    "Web",
    "Containers",
    "Data",
    "Storage",
    "Networking",
    "Security",
    "Integration",
    "AI / ML",
    "Analytics",
    "Monitoring",
    "Management",
    "Other",
)

# Exact ARM type (lowercase) -> category. Only the cases the prefix rules below get wrong, or
# that deserve a more specific bucket, are listed explicitly.
_EXACT: dict[str, str] = {
    "microsoft.network/networksecuritygroups": "Security",
    "microsoft.network/azurefirewalls": "Security",
    "microsoft.network/bastionhosts": "Security",
    "microsoft.network/firewallpolicies": "Security",
    "microsoft.network/ddosprotectionplans": "Security",
    "microsoft.keyvault/vaults": "Security",
    "microsoft.managedidentity/userassignedidentities": "Security",
    "microsoft.web/serverfarms": "Web",
    "microsoft.web/sites": "Web",
    "microsoft.web/staticsites": "Web",
    "microsoft.cdn/profiles": "Networking",
    "microsoft.insights/components": "Monitoring",
    "microsoft.insights/actiongroups": "Monitoring",
    "microsoft.insights/activitylogalerts": "Monitoring",
    "microsoft.insights/metricalerts": "Monitoring",
    "microsoft.insights/scheduledqueryrules": "Monitoring",
    "microsoft.insights/datacollectionrules": "Monitoring",
    "microsoft.operationalinsights/workspaces": "Monitoring",
    "microsoft.alertsmanagement/smartdetectoralertrules": "Monitoring",
    "microsoft.dashboard/grafana": "Monitoring",
    "microsoft.recoveryservices/vaults": "Management",
    "microsoft.dataprotection/backupvaults": "Management",
    "microsoft.automation/automationaccounts": "Management",
    "microsoft.resources/templatespecs": "Management",
    "microsoft.portal/dashboards": "Management",
}

# Prefix rules (checked in order) on the ARM provider/type. First match wins.
_PREFIX: tuple[tuple[str, str], ...] = (
    ("microsoft.compute/", "Compute"),
    ("microsoft.containerservice/", "Containers"),
    ("microsoft.containerregistry/", "Containers"),
    ("microsoft.containerinstance/", "Containers"),
    ("microsoft.app/", "Containers"),
    ("microsoft.kubernetes/", "Containers"),
    ("microsoft.redhatopenshift/", "Containers"),
    ("microsoft.web/", "Web"),
    ("microsoft.sql/", "Data"),
    ("microsoft.dbforpostgresql/", "Data"),
    ("microsoft.dbformysql/", "Data"),
    ("microsoft.dbformariadb/", "Data"),
    ("microsoft.documentdb/", "Data"),
    ("microsoft.cache/", "Data"),
    ("microsoft.sqlvirtualmachine/", "Data"),
    ("microsoft.storage/", "Storage"),
    ("microsoft.netapp/", "Storage"),
    ("microsoft.storagesync/", "Storage"),
    ("microsoft.classicstorage/", "Storage"),
    ("microsoft.network/", "Networking"),
    ("microsoft.cdn/", "Networking"),
    ("microsoft.keyvault/", "Security"),
    ("microsoft.security/", "Security"),
    ("microsoft.aad/", "Security"),
    ("microsoft.managedidentity/", "Security"),
    ("microsoft.servicebus/", "Integration"),
    ("microsoft.eventhub/", "Integration"),
    ("microsoft.eventgrid/", "Integration"),
    ("microsoft.relay/", "Integration"),
    ("microsoft.logic/", "Integration"),
    ("microsoft.apimanagement/", "Integration"),
    ("microsoft.cognitiveservices/", "AI / ML"),
    ("microsoft.machinelearningservices/", "AI / ML"),
    ("microsoft.search/", "AI / ML"),
    ("microsoft.botservice/", "AI / ML"),
    ("microsoft.datafactory/", "Analytics"),
    ("microsoft.synapse/", "Analytics"),
    ("microsoft.databricks/", "Analytics"),
    ("microsoft.kusto/", "Analytics"),
    ("microsoft.streamanalytics/", "Analytics"),
    ("microsoft.hdinsight/", "Analytics"),
    ("microsoft.powerbidedicated/", "Analytics"),
    ("microsoft.insights/", "Monitoring"),
    ("microsoft.operationalinsights/", "Monitoring"),
    ("microsoft.operationsmanagement/", "Monitoring"),
    ("microsoft.alertsmanagement/", "Monitoring"),
    ("microsoft.recoveryservices/", "Management"),
    ("microsoft.dataprotection/", "Management"),
    ("microsoft.automation/", "Management"),
    ("microsoft.resources/", "Management"),
    ("microsoft.management/", "Management"),
    ("microsoft.portal/", "Management"),
    ("microsoft.signalrservice/", "Integration"),
)


def category_for(arm_type: str | None) -> str:
    """Roll an ARM resource type up to one of :data:`CATEGORIES`."""
    t = (arm_type or "").lower().strip()
    if not t:
        return "Other"
    if t in _EXACT:
        return _EXACT[t]
    for prefix, cat in _PREFIX:
        if t.startswith(prefix):
            return cat
    return "Other"


def category_breakdown(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count resources by category. Returns ``[{category, count}]`` in CATEGORIES order
    (only non-empty categories included)."""
    counts: dict[str, int] = {}
    for r in resources:
        cat = category_for(r.get("resource_type") or r.get("type"))
        counts[cat] = counts.get(cat, 0) + 1
    return [{"category": c, "count": counts[c]} for c in CATEGORIES if counts.get(c)]

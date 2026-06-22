"""Self-contained demo scenario for the Change Explorer: Contoso Azure tenant, Contoso Website
Prod workload, and the seven sample changes from the spec. Returns RAW change rows so the real
pipeline (normalize -> classify -> risk -> explain -> insights) produces the results — which is
also the primary test fixture for the engine.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DEMO_WORKLOAD_ID = "demo-contoso-website-prod"
DEMO_WORKLOAD_NAME = "Contoso Website Prod"
DEMO_TENANT = "Contoso Azure"
DEMO_SUB = "00000000-0000-0000-0000-00000c0a7e50"
DEMO_RG = "rg-contoso-web-prod"
DEMO_ACTOR = "pipeline-prod-spn"

# The 12 workload resources (name -> ARM type).
RESOURCES: list[tuple[str, str]] = [
    ("agw-contoso-prod", "microsoft.network/applicationgateways"),
    ("app-contoso-prod", "microsoft.web/sites"),
    ("kv-contoso-prod", "microsoft.keyvault/vaults"),
    ("mi-contoso-prod", "microsoft.managedidentity/userassignedidentities"),
    ("stcontosoprod", "microsoft.storage/storageaccounts"),
    ("sql-contoso-prod", "microsoft.sql/servers"),
    ("pe-sql-contoso-prod", "microsoft.network/privateendpoints"),
    ("privatelink.database.windows.net", "microsoft.network/privatednszones"),
    ("vnet-contoso-prod", "microsoft.network/virtualnetworks"),
    ("snet-app-prod", "microsoft.network/virtualnetworks/subnets"),
    ("nsg-app-prod", "microsoft.network/networksecuritygroups"),
    ("ai-contoso-prod", "microsoft.insights/components"),
]


def _rid(name: str, rtype: str) -> str:
    # subnet ids nest under the vnet; keep it simple/representative for the demo.
    return f"/subscriptions/{DEMO_SUB}/resourceGroups/{DEMO_RG}/providers/{rtype}/{name}"


def _t(hour: int, minute: int) -> str:
    return datetime(2026, 6, 20, hour, minute, 0, tzinfo=timezone.utc).isoformat()


def is_demo(workload_id: str) -> bool:
    return workload_id == DEMO_WORKLOAD_ID


def demo_workload() -> dict[str, Any]:
    """A registry-shaped workload dict for the dropdown + scope resolution."""
    return {
        "id": DEMO_WORKLOAD_ID,
        "name": DEMO_WORKLOAD_NAME,
        "demo": True,
        "tenant": DEMO_TENANT,
        "nodes": [{"kind": "resource", "id": _rid(n, t), "name": n, "subscription_id": DEMO_SUB} for n, t in RESOURCES],
    }


def demo_resources() -> list[dict[str, Any]]:
    return [{"id": _rid(n, t), "name": n, "type": t, "resourceGroup": DEMO_RG,
             "subscriptionId": DEMO_SUB, "location": "eastus",
             "tags": {"environment": "prod", "workload": "contoso-website"}} for n, t in RESOURCES]


def raw_changes() -> list[dict[str, Any]]:
    """The seven sample changes as raw collector rows (chronological)."""
    a = DEMO_ACTOR
    return [
        {  # 1:05 PM — Tags updated on resource group, Low
            "source": "ResourceGraph", "resourceId": f"/subscriptions/{DEMO_SUB}/resourceGroups/{DEMO_RG}",
            "resourceName": DEMO_RG, "resourceType": "microsoft.resources/subscriptions/resourcegroups",
            "resourceGroup": DEMO_RG, "subscriptionId": DEMO_SUB, "location": "eastus",
            "eventTime": _t(13, 5), "operation": "Microsoft.Resources/tags/write", "changeType": "Update",
            "actor": a, "actorType": "ServicePrincipal", "correlationId": "demo-corr-0105",
            "changes": [{"propertyPath": "tags.costCenter", "before": "CC-1001", "after": "CC-1002"}],
            "raw": {"operationName": "Microsoft.Resources/tags/write", "caller": a},
        },
        {  # 1:18 PM — App Service app setting changed, Medium
            "source": "ActivityLog", "resourceId": _rid("app-contoso-prod", "microsoft.web/sites"),
            "resourceName": "app-contoso-prod", "resourceType": "microsoft.web/sites/config",
            "resourceGroup": DEMO_RG, "subscriptionId": DEMO_SUB, "location": "eastus",
            "eventTime": _t(13, 18), "operation": "Microsoft.Web/sites/config/write", "changeType": "Update",
            "actor": a, "actorType": "ServicePrincipal", "correlationId": "demo-corr-0118",
            "changes": [{"propertyPath": "appSettings.ApiBaseUrl", "before": "https://api-v1.contoso.com",
                         "after": "https://api-v2.contoso.com"}],
            "raw": {"operationName": "Microsoft.Web/sites/config/write", "caller": a},
        },
        {  # 1:42 PM — NSG rule added to backend subnet, High
            "source": "ActivityLog", "resourceId": _rid("nsg-app-prod", "microsoft.network/networksecuritygroups"),
            "resourceName": "nsg-app-prod", "resourceType": "microsoft.network/networksecuritygroups",
            "resourceGroup": DEMO_RG, "subscriptionId": DEMO_SUB, "location": "eastus",
            "eventTime": _t(13, 42), "operation": "Microsoft.Network/networkSecurityGroups/securityRules/write",
            "changeType": "Update", "actor": a, "actorType": "ServicePrincipal", "correlationId": "demo-corr-0142",
            "changes": [{"propertyPath": "securityRules/allow-3389", "before": None,
                         "after": "Allow TCP 3389 from 0.0.0.0/0", "changeType": "Create"}],
            "raw": {"operationName": "Microsoft.Network/networkSecurityGroups/securityRules/write", "caller": a},
        },
        {  # 2:06 PM — Key Vault certificate version changed, High
            "source": "ActivityLog", "resourceId": _rid("kv-contoso-prod", "microsoft.keyvault/vaults") + "/certificates/tls-contoso",
            "resourceName": "kv-contoso-prod", "resourceType": "microsoft.keyvault/vaults/certificates",
            "resourceGroup": DEMO_RG, "subscriptionId": DEMO_SUB, "location": "eastus",
            "eventTime": _t(14, 6), "operation": "Microsoft.KeyVault/vaults/certificates/write", "changeType": "Update",
            "actor": a, "actorType": "ServicePrincipal", "correlationId": "demo-corr-0206",
            "changes": [{"propertyPath": "certificates/tls-contoso/version", "before": "v17", "after": "v18"}],
            "raw": {"operationName": "Microsoft.KeyVault/vaults/certificates/write", "caller": a},
        },
        {  # 2:08 PM — App Gateway listener/certificate configuration updated, Critical
            "source": "ActivityLog", "resourceId": _rid("agw-contoso-prod", "microsoft.network/applicationgateways"),
            "resourceName": "agw-contoso-prod", "resourceType": "microsoft.network/applicationgateways",
            "resourceGroup": DEMO_RG, "subscriptionId": DEMO_SUB, "location": "eastus",
            "eventTime": _t(14, 8), "operation": "Microsoft.Network/applicationGateways/write", "changeType": "Update",
            "actor": a, "actorType": "ServicePrincipal", "correlationId": "demo-corr-0208",
            "changes": [
                {"propertyPath": "httpsListeners/https/sslCertificate", "before": "tls-contoso-v17", "after": "tls-contoso-v18"},
                {"propertyPath": "httpsListeners/https/hostName", "before": "www.contoso.com", "after": "www.contoso.com"},
            ],
            "raw": {"operationName": "Microsoft.Network/applicationGateways/write", "caller": a},
        },
        {  # 2:12 PM — Private DNS A record changed, High
            "source": "ActivityLog", "resourceId": _rid("privatelink.database.windows.net", "microsoft.network/privatednszones") + "/A/sql-contoso-prod",
            "resourceName": "privatelink.database.windows.net", "resourceType": "microsoft.network/privatednszones/a",
            "resourceGroup": DEMO_RG, "subscriptionId": DEMO_SUB, "location": "global",
            "eventTime": _t(14, 12), "operation": "Microsoft.Network/privateDnsZones/A/write", "changeType": "Update",
            "actor": a, "actorType": "ServicePrincipal", "correlationId": "demo-corr-0212",
            "changes": [{"propertyPath": "A/sql-contoso-prod/ipv4Address", "before": "10.20.1.4", "after": "10.20.1.9"}],
            "raw": {"operationName": "Microsoft.Network/privateDnsZones/A/write", "caller": a},
        },
        {  # 2:30 PM — Diagnostic setting updated, Low
            "source": "ActivityLog", "resourceId": _rid("app-contoso-prod", "microsoft.web/sites") + "/providers/microsoft.insights/diagnosticSettings/diag",
            "resourceName": "app-contoso-prod", "resourceType": "microsoft.insights/diagnosticsettings",
            "resourceGroup": DEMO_RG, "subscriptionId": DEMO_SUB, "location": "eastus",
            "eventTime": _t(14, 30), "operation": "Microsoft.Insights/diagnosticSettings/write", "changeType": "Update",
            "actor": a, "actorType": "ServicePrincipal", "correlationId": "demo-corr-0230",
            "changes": [{"propertyPath": "logs.AppServiceHTTPLogs.enabled", "before": "false", "after": "true"}],
            "raw": {"operationName": "Microsoft.Insights/diagnosticSettings/write", "caller": a},
        },
    ]


# --------------------------------------------------------------------------- catalog demo workloads
# The shared demo workloads (Contoso Hotels, Zava Shoes Website / CRM) also produce synthetic
# changes so Change Explorer is fully explorable for them offline. Changes are derived
# deterministically from each workload's resources (type-aware operations, varied risk, actors,
# before/after diffs), spread across the trailing 24h.

_DEMO_ACTORS = [
    ("pipeline-prod-spn", "ServicePrincipal"),
    ("ops-admin@demo.local", "User"),
    ("policy-remediation", "AzurePolicy"),
    ("mi-deploy", "ManagedIdentity"),
]


def _bucket(s: str, n: int) -> int:
    import hashlib
    return int(hashlib.sha1(s.encode()).hexdigest()[:8], 16) % max(1, n)


def _op_for_type(rtype: str, name: str) -> dict[str, Any] | None:
    """A representative change operation + before/after for a resource type (None = skip)."""
    t = rtype.lower()
    if "applicationgateways" in t:
        return {"operation": "Microsoft.Network/applicationGateways/write", "rtype": rtype,
                "changes": [{"propertyPath": "httpsListeners/https/sslCertificate", "before": "tls-v7", "after": "tls-v8"}]}
    if "networksecuritygroups" in t:
        return {"operation": "Microsoft.Network/networkSecurityGroups/securityRules/write", "rtype": rtype,
                "changes": [{"propertyPath": "securityRules/allow-inbound", "before": None, "after": "Allow TCP 443 from Internet", "changeType": "Create"}]}
    if "keyvault" in t:
        return {"operation": "Microsoft.KeyVault/vaults/certificates/write", "rtype": rtype + "/certificates",
                "changes": [{"propertyPath": "certificates/app-tls/version", "before": "v3", "after": "v4"}]}
    if "privatednszones" in t or "dnszones" in t:
        return {"operation": "Microsoft.Network/privateDnsZones/A/write", "rtype": rtype + "/a",
                "changes": [{"propertyPath": "A/api/ipv4Address", "before": "10.0.1.4", "after": "10.0.1.9"}]}
    if "sql/servers" in t or "databases" in t or "postgresql" in t:
        return {"operation": "Microsoft.Sql/servers/firewallRules/write", "rtype": rtype,
                "changes": [{"propertyPath": "firewallRules/allow-azure", "before": None, "after": "0.0.0.0-255.255.255.255", "changeType": "Create"}]}
    if "/sites" in t or "serverfarms" in t:
        return {"operation": "Microsoft.Web/sites/config/write", "rtype": rtype + "/config",
                "changes": [{"propertyPath": "appSettings.ApiBaseUrl", "before": "https://api-v1", "after": "https://api-v2"}]}
    if "storageaccounts" in t:
        return {"operation": "Microsoft.Storage/storageAccounts/write", "rtype": rtype,
                "changes": [{"propertyPath": "networkAcls.defaultAction", "before": "Deny", "after": "Allow"}]}
    if "redis" in t or "cache" in t:
        return {"operation": "Microsoft.Cache/redis/write", "rtype": rtype,
                "changes": [{"propertyPath": "properties.minimumTlsVersion", "before": "1.2", "after": "1.0"}]}
    if "virtualmachines" in t or "disks" in t or "managedclusters" in t:
        return {"operation": f"{rtype}/write", "rtype": rtype,
                "changes": [{"propertyPath": "tags.patchGroup", "before": "ring1", "after": "ring2"}]}
    if "insights" in t or "operationalinsights" in t:
        return {"operation": "Microsoft.Insights/diagnosticSettings/write", "rtype": "microsoft.insights/diagnosticsettings",
                "changes": [{"propertyPath": "logs.enabled", "before": "false", "after": "true"}]}
    # Everything else: a tag write (low risk).
    return {"operation": "Microsoft.Resources/tags/write", "rtype": "microsoft.resources/tags",
            "changes": [{"propertyPath": "tags.costCenter", "before": "CC-100", "after": "CC-200"}]}


def is_catalog_demo(workload_id: str) -> bool:
    from app.demo_catalog import is_demo_workload
    return is_demo_workload(workload_id)


def catalog_changes(workload_id: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Synthetic raw changes for a shared catalog demo workload, derived from its resources."""
    from datetime import datetime, timedelta, timezone

    from app.demo_catalog import resources_for

    try:
        end = datetime.fromisoformat((end_iso or "").replace("Z", "+00:00")) if end_iso else datetime.now(timezone.utc)
    except ValueError:
        end = datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    resources = resources_for(workload_id)
    out: list[dict[str, Any]] = []
    for i, r in enumerate(resources):
        # Not every resource changed — ~70% did, deterministically.
        if _bucket(r["id"] + "chg", 10) >= 7:
            continue
        op = _op_for_type(r["type"], r["name"])
        if not op:
            continue
        actor, atype = _DEMO_ACTORS[_bucket(r["id"] + "act", len(_DEMO_ACTORS))]
        # Spread events across the trailing 18 hours.
        mins = 30 + _bucket(r["id"] + "t", 18 * 60)
        ev_time = (end - timedelta(minutes=mins)).isoformat()
        out.append({
            "source": "ActivityLog" if i % 2 else "ResourceGraph",
            "resourceId": r["id"], "resourceName": r["name"], "resourceType": op["rtype"],
            "resourceGroup": r["resourceGroup"], "subscriptionId": r["subscriptionId"], "location": r.get("location", ""),
            "eventTime": ev_time, "operation": op["operation"], "changeType": "Update",
            "actor": actor, "actorType": atype, "correlationId": f"demo-{workload_id[:8]}-{i:02d}",
            "changes": op["changes"], "raw": {"operationName": op["operation"], "caller": actor},
        })
    out.sort(key=lambda e: e["eventTime"])
    return out


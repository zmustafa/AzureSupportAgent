"""Dependency-role inference + blast-radius hints (powers the Dependency Impact tab and feeds
the risk engine). Pure functions over a resource's type/name. Roles are *inferred* — callers
present them with confidence and "could impact" language.
"""
from __future__ import annotations

# Canonical dependency roles a changed resource can play in a workload.
ROLE_PUBLIC_INGRESS = "Public ingress"
ROLE_BACKEND = "Backend API / compute"
ROLE_IDENTITY = "Identity dependency"
ROLE_SECRET = "Secret / certificate dependency"
ROLE_DATABASE = "Database dependency"
ROLE_PRIVATE_NET = "Private networking dependency"
ROLE_MONITORING = "Monitoring dependency"
ROLE_SHARED = "Shared platform dependency"
ROLE_STORAGE = "Storage dependency"
ROLE_OTHER = "Workload resource"

# resource-type substring -> role (first match wins).
_ROLE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("applicationgateways", "frontdoors", "cdn/profiles", "trafficmanager", "loadbalancers", "publicip", "frontdoor"), ROLE_PUBLIC_INGRESS),
    (("apimanagement",), ROLE_PUBLIC_INGRESS),
    (("privatednszones", "dnszones", "privateendpoint", "virtualnetworks", "subnets", "networksecuritygroups",
      "routetables", "azurefirewalls", "networkinterfaces", "natgateways", "virtualnetworkgateways"), ROLE_PRIVATE_NET),
    (("vaults/certificates", "vaults/secrets", "vaults/keys", "keyvault", "vaults"), ROLE_SECRET),
    (("managedidentity", "userassignedidentities", "serviceprincipals", "applications"), ROLE_IDENTITY),
    (("sql/servers", "databases", "dbforpostgresql", "dbformysql", "documentdb", "cache/redis"), ROLE_DATABASE),
    (("storageaccounts",), ROLE_STORAGE),
    (("insights/components", "operationalinsights", "insights/diagnosticsettings", "insights/metricalerts",
      "insights/actiongroups", "insights/scheduledqueryrules"), ROLE_MONITORING),
    (("serverfarms", "sites", "virtualmachines", "virtualmachinescalesets", "containerservice", "disks"), ROLE_BACKEND),
]

# role -> plain-English blast-radius hint.
_BLAST: dict[str, str] = {
    ROLE_PUBLIC_INGRESS: "This resource sits on the public ingress path. Listener, certificate, backend-pool, "
                         "routing or health-probe changes could affect user traffic and may cause 502, TLS or routing failures.",
    ROLE_BACKEND: "This resource runs workload code. Configuration or scale changes could affect application "
                  "availability, latency or behavior.",
    ROLE_IDENTITY: "This is an identity the workload authenticates with. Changes could break sign-in, token "
                   "acquisition or downstream resource access.",
    ROLE_SECRET: "This holds secrets or certificates the workload depends on. Changes could cause auth, TLS or "
                 "connection failures if a consumer references a rotated value.",
    ROLE_DATABASE: "This is a data dependency. Firewall, networking or configuration changes could affect "
                   "connectivity or data access for the workload.",
    ROLE_PRIVATE_NET: "This is part of the private networking path. NSG, route, DNS or private-endpoint changes "
                      "could affect connectivity between workload components.",
    ROLE_MONITORING: "This is a monitoring dependency. Changes affect observability (diagnostics, alerts) rather "
                     "than the running workload directly.",
    ROLE_STORAGE: "This is a storage dependency. Networking or access changes could affect data availability.",
    ROLE_SHARED: "This is shared infrastructure. Changes have a larger blast radius and could affect multiple workloads.",
    ROLE_OTHER: "Role in the workload could not be inferred from the resource type.",
}


def role_for(resource_type: str, name: str = "") -> str:
    rt = (resource_type or "").lower()
    nm = (name or "").lower()
    for needles, role in _ROLE_RULES:
        if any(n in rt for n in needles):
            # DNS zones are private-net but call out DNS explicitly via name when relevant.
            return role
    if "shared" in nm or "hub" in nm:
        return ROLE_SHARED
    return ROLE_OTHER


def blast_radius(role: str) -> str:
    return _BLAST.get(role, _BLAST[ROLE_OTHER])

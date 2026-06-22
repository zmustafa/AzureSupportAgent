"""Change classifier: map (resourceType, operation) -> one of CATEGORIES. Pure + deterministic."""
from __future__ import annotations


def _op_kind(operation: str) -> str:
    """Coarse operation kind from an ARM operationName or ARG changeType."""
    op = (operation or "").lower()
    if op in ("delete", "deleted") or op.endswith("/delete") or "delete" in op:
        return "delete"
    if op in ("create", "created"):
        return "create"
    if op.endswith("/action") or "/action/" in op:
        return "action"
    if op in ("update", "updated") or op.endswith("/write") or "write" in op:
        return "write"
    if op.endswith("/read") or op in ("read",):
        return "read"
    return "write"


# (substrings in the lowercased resource type, operation, OR a changed property path) -> category.
# Order matters. Property-path tokens (e.g. ``securityrules``, ``subnets``, ``appsettings``) are
# included so a change still classifies even when the resource type/id came back empty from the
# change feed — the signal then lives in WHAT property changed.
_RULES: list[tuple[tuple[str, ...], str]] = [
    (("/tags", "microsoft.resources/tags", "tags["), "TagsMetadata"),
    (("roleassignments", "roledefinitions", "microsoft.authorization/roleassignment", "roleassignment"), "RBAC"),
    (("privilegedaccess", "roleeligibility", "roleassignmentschedule", "/pim"), "PIM"),
    (("microsoft.graph/applications", "/applications", "appregistration"), "AppRegistration"),
    (("serviceprincipals",), "ServicePrincipal"),
    (("managedidentity", "userassignedidentities"), "ManagedIdentity"),
    (("vaults/certificates", "/certificates"), "Certificate"),
    (("vaults/secrets", "/secrets"), "Secret"),
    (("vaults/keys", "keyvault/vaults", "microsoft.keyvault", "accesspolicies"), "KeyVault"),
    (("privatednszones", "dnszones", "/dnszones", "/a/", "/cname", "/soa", "recordsets"), "DNS"),
    (("policyassignments", "policydefinitions", "policysetdefinitions", "microsoft.authorization/policy"), "Policy"),
    (("microsoft.security", "securitycontacts", "defender"), "Security"),
    (("sites/config", "sites/appsettings", "connectionstrings", "/config/", "appsettings", "siteconfig"), "AppConfiguration"),
    (("microsoft.resources/deployments", "/deployments"), "Deployment"),
    (("networksecuritygroups", "applicationgateways", "frontdoors", "azurefirewalls", "routetables",
      "virtualnetworks", "subnets", "loadbalancers", "publicipaddresses", "networkinterfaces",
      "privateendpoints", "trafficmanager", "cdn/profiles", "natgateways", "frontdoor",
      "virtualnetworkgateways", "apimanagement",
      # property-path tokens for networking sub-resource changes:
      "securityrules", "securityrule", "subnet", "routes", "ipconfigurations", "frontendipconfigurations",
      "backendaddresspools", "httplisteners", "requestroutingrules", "sslcertificates", "addressprefix",
      "destinationaddressprefix", "sourceaddressprefix"), "Network"),
    (("sql/servers", "/databases", "dbforpostgresql", "dbformysql", "documentdb", "cache/redis",
      "firewallrules", "virtualnetworkrules"), "Database"),
    (("storageaccounts", "networkacls", "blobservices"), "Storage"),
    (("insights/components", "operationalinsights", "insights/diagnosticsettings", "insights/metricalerts",
      "insights/actiongroups", "insights/scheduledqueryrules", "insights/activitylogalerts",
      "insights/autoscalesettings", "diagnosticsettings"), "Monitoring"),
    (("virtualmachines", "virtualmachinescalesets", "disks", "availabilitysets", "containerservice"), "Compute"),
    (("serverfarms",), "CostScale"),
    (("sites", "functions"), "AppConfiguration"),
]


def classify(resource_type: str, operation: str = "", paths: list[str] | None = None) -> str:
    """Best-guess change category from the resource type, the operation, AND the set of changed
    property paths. The property paths let a change classify even when the resource type/id is
    missing from the feed (the signal is then carried by WHAT changed)."""
    parts = [(resource_type or "").lower(), (operation or "").lower()]
    if paths:
        parts.extend(str(p or "").lower() for p in paths)
    blob = " ".join(parts)
    # Autoscale / SKU operations are CostScale even on compute/web.
    if "autoscalesettings" in blob or "/sku" in blob or "capacity" in blob:
        return "CostScale"
    for needles, category in _RULES:
        if any(n in blob for n in needles):
            return category
    return "Unknown"


def op_kind(operation: str) -> str:
    return _op_kind(operation)

"""Normalized access-row schema for the RBAC (access review) feature.

Ports the wide schema produced by the standalone *all-azure-access* scanner
(github.com/zmustafa/AzureEntraIDIAMScanner) into the app so every access surface — Azure
RBAC (control & data plane), Entra directory roles, group-derived access, service-principal
ownership, PIM/eligible — can be compared in ONE grid. The exact column names are preserved
so a row here is interchangeable with the scanner's ``allAzureAccess.json`` (import parity).

A *row* is a flat dict with the 46 ``COLUMNS`` keys. :func:`make_row` fills every key with a
sane default so partial collectors never emit ragged rows. Role-privilege classification
(:func:`role_is_privileged`, :func:`role_category`) mirrors the scanner's heuristics."""
from __future__ import annotations

from typing import Any

# The 46 normalized columns, in the scanner's canonical order. Kept verbatim for import/export
# parity with allAzureAccess.csv/json.
COLUMNS: tuple[str, ...] = (
    "surface",
    "accessModel",
    "collector",
    "assignmentState",
    "assignmentType",
    "principalId",
    "principalType",
    "principalDisplayName",
    "principalUserPrincipalName",
    "principalAppId",
    "effectivePrincipalId",
    "effectivePrincipalType",
    "effectivePrincipalName",
    "effectivePrincipalUserPrincipalName",
    "accessPath",
    "groupChain",
    "sourceGroupId",
    "sourceGroupName",
    "roleName",
    "roleDefinitionId",
    "roleCategory",
    "roleIsPrivileged",
    "roleHasDataActions",
    "scope",
    "scopeType",
    "scopeDisplayName",
    "tenantId",
    "managementGroupId",
    "managementGroupName",
    "subscriptionId",
    "subscriptionName",
    "resourceGroup",
    "resourceType",
    "resourceName",
    "childResourceType",
    "childResourceName",
    "assignmentId",
    "assignmentCreatedOn",
    "assignmentUpdatedOn",
    "condition",
    "conditionVersion",
    "isInherited",
    "sourceApi",
    "collectionStatus",
    "errorCode",
    "errorMessage",
)

# Surfaces (the "what kind of access" axis the Insights pivots group by).
SURFACE_AZURE_RBAC = "Azure RBAC"
SURFACE_ENTRA = "Entra ID RBAC"
SURFACE_KEY_VAULT = "Key Vault Access Policy"
SURFACE_CLASSIC = "Classic Admin"

# Access models (finer-grained than surface; used for the data-plane split).
ACCESS_CONTROL_PLANE = "AzureRBAC"
ACCESS_DATA_PLANE = "AzureDataPlaneRBAC"
ACCESS_ENTRA = "EntraDirectoryRole"
ACCESS_KV_POLICY = "KeyVaultAccessPolicy"
ACCESS_CLASSIC = "ClassicAzureAdmin"

# Assignment states (PIM distinguishes Active from Eligible/JIT).
STATE_ACTIVE = "Active"
STATE_ELIGIBLE = "Eligible"

# Access paths (how the principal effectively receives the access).
PATH_DIRECT = "Direct"
PATH_GROUP = "GroupTransitive"
PATH_OWNER = "Owner"

# Scope types (the resource-hierarchy level an assignment lands on).
SCOPE_TENANT = "tenantRoot"
SCOPE_MANAGEMENT_GROUP = "managementGroup"
SCOPE_SUBSCRIPTION = "subscription"
SCOPE_RESOURCE_GROUP = "resourceGroup"
SCOPE_RESOURCE = "resource"
SCOPE_DIRECTORY = "directory"

# Collector statuses (every collector reports one; the run continues on any non-fatal value).
STATUS_SUCCEEDED = "Succeeded"
STATUS_SUCCEEDED_WARN = "SucceededWithWarnings"
STATUS_PARTIAL = "PartiallyCollected"
STATUS_SKIPPED = "Skipped"
STATUS_UNAUTHORIZED = "Unauthorized"
STATUS_THROTTLED = "Throttled"
STATUS_FAILED = "Failed"

# A status is "needs attention" (surfaced in Diagnostics) when it isn't a clean success/skip.
ATTENTION_STATUSES = frozenset(
    {STATUS_PARTIAL, STATUS_UNAUTHORIZED, STATUS_THROTTLED, STATUS_FAILED}
)

# Azure RBAC roles that grant privileged (write/assign/delete) control-plane access by name.
PRIVILEGED_AZURE_ROLES = frozenset(
    {
        "owner",
        "contributor",
        "user access administrator",
        "role based access control administrator",
        "co-administrator",
        "account administrator",
        "service administrator",
    }
)

# Entra directory roles considered privileged (tenant-wide blast radius).
PRIVILEGED_ENTRA_ROLES = frozenset(
    {
        "global administrator",
        "company administrator",
        "privileged role administrator",
        "privileged authentication administrator",
        "user administrator",
        "application administrator",
        "cloud application administrator",
        "authentication administrator",
        "groups administrator",
        "security administrator",
        "conditional access administrator",
        "exchange administrator",
        "sharepoint administrator",
        "teams administrator",
        "intune administrator",
        "global reader",
    }
)

# Severity tiers (reused by the UI badges; mirrors the identity dashboard's vocabulary).
SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3, "ok": 4}


def make_row(**values: Any) -> dict[str, Any]:
    """Build a normalized access row: every one of the 46 ``COLUMNS`` present with a default.

    String columns default to ``""``; the two boolean flags to ``False``. Unknown keys are
    ignored so a collector can pass a superset without leaking non-schema fields."""
    row: dict[str, Any] = {}
    for col in COLUMNS:
        if col in ("roleIsPrivileged", "roleHasDataActions", "isInherited"):
            row[col] = bool(values.get(col, False))
        else:
            val = values.get(col, "")
            row[col] = "" if val is None else val
    return row


def role_is_privileged(role_name: str, *, surface: str = SURFACE_AZURE_RBAC, has_data_actions: bool = False) -> bool:
    """Heuristic: does this role grant privileged access?

    Azure RBAC: the canonical write/assign roles by name, OR any role carrying dataActions
    (data-plane read/write is sensitive). Entra: the tenant-admin role set."""
    name = (role_name or "").strip().lower()
    if surface == SURFACE_ENTRA:
        return name in PRIVILEGED_ENTRA_ROLES
    if name in PRIVILEGED_AZURE_ROLES:
        return True
    # Data-plane "Data Owner"/"Data Contributor" style roles are privileged on the data path.
    if has_data_actions and ("owner" in name or "contributor" in name):
        return True
    return False


def role_has_data_actions(actions: list[str] | None, data_actions: list[str] | None) -> bool:
    """True when the role definition declares any dataActions (data-plane reach)."""
    return bool(data_actions)


def role_category(has_data_actions: bool, *, surface: str = SURFACE_AZURE_RBAC) -> str:
    """ControlPlane / DataPlane / Mixed classification for a role definition."""
    if surface == SURFACE_ENTRA:
        return "Directory"
    return "DataPlane" if has_data_actions else "ControlPlane"


def parse_scope(scope: str) -> dict[str, str]:
    """Decompose an ARM scope id into its hierarchy parts.

    Returns a dict with ``scopeType`` and any of ``managementGroupId`` / ``subscriptionId`` /
    ``resourceGroup`` / ``resourceType`` / ``resourceName`` that the scope encodes. Robust to
    the tenant-root ("/") and management-group scopes."""
    s = (scope or "").strip()
    out: dict[str, str] = {}
    if not s or s == "/":
        out["scopeType"] = SCOPE_TENANT
        return out
    low = s.lower()
    if "/providers/microsoft.management/managementgroups/" in low:
        out["scopeType"] = SCOPE_MANAGEMENT_GROUP
        out["managementGroupId"] = s.rstrip("/").split("/")[-1]
        return out
    parts = [p for p in s.split("/") if p]
    # parts like: subscriptions, <sub>, resourceGroups, <rg>, providers, <ns>, <type>, <name>...
    lparts = [p.lower() for p in parts]
    if "subscriptions" in lparts:
        i = lparts.index("subscriptions")
        if i + 1 < len(parts):
            out["subscriptionId"] = parts[i + 1]
    if "resourcegroups" in lparts:
        i = lparts.index("resourcegroups")
        if i + 1 < len(parts):
            out["resourceGroup"] = parts[i + 1]
    if "providers" in lparts:
        i = lparts.index("providers")
        # provider/type/name (possibly with child types)
        if i + 3 < len(parts):
            out["resourceType"] = f"{parts[i + 1]}/{parts[i + 2]}"
            out["resourceName"] = parts[i + 3]
    # Decide the scope type from the deepest part present.
    if out.get("resourceName"):
        out["scopeType"] = SCOPE_RESOURCE
    elif out.get("resourceGroup"):
        out["scopeType"] = SCOPE_RESOURCE_GROUP
    elif out.get("subscriptionId"):
        out["scopeType"] = SCOPE_SUBSCRIPTION
    else:
        out["scopeType"] = SCOPE_TENANT
    return out

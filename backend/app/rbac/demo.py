"""Synthetic RBAC dataset — the local reviewable/testable path.

A live access scan needs a real Azure connection with broad reader + Microsoft Graph
permissions, so (exactly like the other dashboards in this app) the demo dataset is the path
that's exercised end-to-end locally. It deliberately covers every surface and tab: privileged
Azure RBAC, data-plane roles, a management-group inherited assignment, two groups with
transitive expansion, a service-principal owner, Entra directory roles, a PIM-eligible
assignment, a Key Vault access policy and a classic co-administrator — plus one Unauthorized
collector so the Diagnostics tab has something to show.

All identities are fake (``contoso.example`` UPNs, fixed GUID-shaped ids) — no real tenant
data. ``seed_demo`` writes per-scope slices + the directory layer into the cache so the page
renders instantly and per-scope refresh has something to re-stamp."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.rbac import cache, schema

# Fixed demo identifiers (GUID-shaped but obviously synthetic).
TENANT = "demo-tenant-0000-0000-000000000000"
SUB_PROD = "11111111-1111-1111-1111-111111111111"
SUB_DEV = "22222222-2222-2222-2222-222222222222"
MG_ID = "mg-contoso"

SCOPE_MG = f"/providers/Microsoft.Management/managementGroups/{MG_ID}"
SCOPE_PROD = f"/subscriptions/{SUB_PROD}"
SCOPE_DEV = f"/subscriptions/{SUB_DEV}"

# A marker so cache.is_demo / admin demo purge can recognize demo snapshots regardless of tenant.
DEMO_FLAG = True


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _principal(pid: str, ptype: str, name: str, upn: str = "", app_id: str = "") -> dict[str, str]:
    return {
        "principalId": pid,
        "principalType": ptype,
        "principalDisplayName": name,
        "principalUserPrincipalName": upn,
        "principalAppId": app_id,
    }


# --- the cast of principals -----------------------------------------------------------
P = {
    "alice": _principal("u-alice", "User", "Alice Admin", "alice@contoso.example"),
    "bob": _principal("u-bob", "User", "Bob Builder", "bob@contoso.example"),
    "carol": _principal("u-carol", "User", "Carol Reader", "carol@contoso.example"),
    "dave": _principal("u-dave", "User", "Dave Data", "dave@contoso.example"),
    "eve": _principal("u-eve", "User", "Eve Engineer", "eve@contoso.example"),
    "frank": _principal("u-frank", "User", "Frank Finance", "frank@contoso.example"),
    "henry": _principal("u-henry", "User", "Henry Helpdesk", "henry@contoso.example"),
    "ivan": _principal("u-ivan", "User", "Ivan Incident", "ivan@contoso.example"),
    "julia": _principal("u-julia", "User", "Julia Keys", "julia@contoso.example"),
    "ken": _principal("u-ken", "User", "Ken Classic", "ken@contoso.example"),
    "gary": _principal("u-gary", "User", "Gary Owner", "gary@contoso.example"),
    "grp_admins": _principal("g-platform-admins", "Group", "Platform Admins"),
    "grp_readers": _principal("g-data-readers", "Group", "Data Readers"),
    "sp_deploy": _principal("sp-deploy", "ServicePrincipal", "deploy-pipeline", app_id="app-deploy-123"),
}


def _az_row(
    *,
    scope: str,
    principal: dict[str, str],
    role: str,
    privileged: bool,
    data_actions: bool = False,
    state: str = schema.STATE_ACTIVE,
    inherited: bool = False,
    sub_name: str = "",
    collector: str = "AzureSubscriptionRbac",
) -> dict[str, Any]:
    """One Azure RBAC assignment row, scope fields auto-derived from the scope id."""
    parts = schema.parse_scope(scope)
    return schema.make_row(
        surface=schema.SURFACE_AZURE_RBAC,
        accessModel=schema.ACCESS_DATA_PLANE if data_actions else schema.ACCESS_CONTROL_PLANE,
        collector=collector,
        assignmentState=state,
        assignmentType="RoleAssignment",
        accessPath=schema.PATH_DIRECT,
        roleName=role,
        roleCategory=schema.role_category(data_actions),
        roleIsPrivileged=privileged,
        roleHasDataActions=data_actions,
        scope=scope,
        scopeType=parts.get("scopeType", ""),
        scopeDisplayName=sub_name or scope,
        tenantId=TENANT,
        managementGroupId=parts.get("managementGroupId", ""),
        subscriptionId=parts.get("subscriptionId", ""),
        subscriptionName=sub_name,
        resourceGroup=parts.get("resourceGroup", ""),
        resourceType=parts.get("resourceType", ""),
        resourceName=parts.get("resourceName", ""),
        assignmentId=f"{scope}/providers/Microsoft.Authorization/roleAssignments/ra-{principal['principalId']}-{role}".replace(" ", ""),
        assignmentCreatedOn=_iso(120),
        isInherited=inherited,
        sourceApi="az role assignment list",
        collectionStatus=schema.STATUS_SUCCEEDED,
        # effective == principal for direct rows
        effectivePrincipalId=principal["principalId"],
        effectivePrincipalType=principal["principalType"],
        effectivePrincipalName=principal["principalDisplayName"],
        effectivePrincipalUserPrincipalName=principal.get("principalUserPrincipalName", ""),
        **principal,
    )


def _entra_row(*, principal: dict[str, str], role: str, state: str = schema.STATE_ACTIVE) -> dict[str, Any]:
    return schema.make_row(
        surface=schema.SURFACE_ENTRA,
        accessModel=schema.ACCESS_ENTRA,
        collector="EntraRoleAssignments" if state == schema.STATE_ACTIVE else "PimDirectoryAssignments",
        assignmentState=state,
        assignmentType="DirectoryRoleAssignment",
        accessPath=schema.PATH_DIRECT,
        roleName=role,
        roleCategory="Directory",
        roleIsPrivileged=schema.role_is_privileged(role, surface=schema.SURFACE_ENTRA),
        scope="/",
        scopeType=schema.SCOPE_DIRECTORY,
        scopeDisplayName="Directory",
        tenantId=TENANT,
        assignmentId=f"dra-{principal['principalId']}-{role}".replace(" ", ""),
        assignmentCreatedOn=_iso(90),
        sourceApi="Microsoft Graph roleManagement",
        collectionStatus=schema.STATUS_SUCCEEDED,
        effectivePrincipalId=principal["principalId"],
        effectivePrincipalType=principal["principalType"],
        effectivePrincipalName=principal["principalDisplayName"],
        effectivePrincipalUserPrincipalName=principal.get("principalUserPrincipalName", ""),
        **principal,
    )


def _owner_row(*, sp: dict[str, str], owner: dict[str, str]) -> dict[str, Any]:
    """A service-principal ownership row (owner can control the SP's credentials → effective access)."""
    return schema.make_row(
        surface=schema.SURFACE_ENTRA,
        accessModel=schema.ACCESS_ENTRA,
        collector="ServicePrincipalOwners",
        assignmentState=schema.STATE_ACTIVE,
        assignmentType="Owner",
        accessPath=schema.PATH_OWNER,
        roleName="Service Principal Owner",
        roleCategory="Directory",
        roleIsPrivileged=True,
        scope="/",
        scopeType=schema.SCOPE_DIRECTORY,
        scopeDisplayName=sp["principalDisplayName"],
        tenantId=TENANT,
        assignmentId=f"spo-{sp['principalId']}-{owner['principalId']}",
        sourceApi="Microsoft Graph servicePrincipals/owners",
        collectionStatus=schema.STATUS_SUCCEEDED,
        # the SP is the "principal"; the owner is the EFFECTIVE principal
        principalId=sp["principalId"],
        principalType=sp["principalType"],
        principalDisplayName=sp["principalDisplayName"],
        principalAppId=sp.get("principalAppId", ""),
        effectivePrincipalId=owner["principalId"],
        effectivePrincipalType=owner["principalType"],
        effectivePrincipalName=owner["principalDisplayName"],
        effectivePrincipalUserPrincipalName=owner.get("principalUserPrincipalName", ""),
    )


def _kv_row(*, scope: str, principal: dict[str, str], sub_name: str) -> dict[str, Any]:
    parts = schema.parse_scope(scope)
    return schema.make_row(
        surface=schema.SURFACE_KEY_VAULT,
        accessModel=schema.ACCESS_KV_POLICY,
        collector="KeyVaultAccessPolicies",
        assignmentState=schema.STATE_ACTIVE,
        assignmentType="AccessPolicy",
        accessPath=schema.PATH_DIRECT,
        roleName="Key Vault Access Policy (get,list secrets)",
        roleCategory="DataPlane",
        roleIsPrivileged=True,
        roleHasDataActions=True,
        scope=scope,
        scopeType=parts.get("scopeType", ""),
        scopeDisplayName="kv-contoso-prod",
        tenantId=TENANT,
        subscriptionId=parts.get("subscriptionId", ""),
        subscriptionName=sub_name,
        resourceGroup=parts.get("resourceGroup", ""),
        resourceType="Microsoft.KeyVault/vaults",
        resourceName="kv-contoso-prod",
        sourceApi="az keyvault show",
        collectionStatus=schema.STATUS_SUCCEEDED,
        effectivePrincipalId=principal["principalId"],
        effectivePrincipalType=principal["principalType"],
        effectivePrincipalName=principal["principalDisplayName"],
        effectivePrincipalUserPrincipalName=principal.get("principalUserPrincipalName", ""),
        **principal,
    )


def _classic_row(*, scope: str, principal: dict[str, str], sub_name: str) -> dict[str, Any]:
    parts = schema.parse_scope(scope)
    return schema.make_row(
        surface=schema.SURFACE_CLASSIC,
        accessModel=schema.ACCESS_CLASSIC,
        collector="ClassicAdmins",
        assignmentState=schema.STATE_ACTIVE,
        assignmentType="ClassicAdministrator",
        accessPath=schema.PATH_DIRECT,
        roleName="Co-Administrator",
        roleCategory="ControlPlane",
        roleIsPrivileged=True,
        scope=scope,
        scopeType=parts.get("scopeType", ""),
        scopeDisplayName=sub_name,
        tenantId=TENANT,
        subscriptionId=parts.get("subscriptionId", ""),
        subscriptionName=sub_name,
        sourceApi="az role assignment list --include-classic-administrators",
        collectionStatus=schema.STATUS_SUCCEEDED,
        effectivePrincipalId=principal["principalId"],
        effectivePrincipalType=principal["principalType"],
        effectivePrincipalName=principal["principalDisplayName"],
        effectivePrincipalUserPrincipalName=principal.get("principalUserPrincipalName", ""),
        **principal,
    )


# --- role definitions + principal directory (reference sets) --------------------------
ROLE_DEFS = [
    {"roleName": "Owner", "roleCategory": "ControlPlane", "roleIsPrivileged": True, "roleHasDataActions": False, "actionsCount": 1, "dataActionsCount": 0, "description": "Full access including the right to assign roles."},
    {"roleName": "Contributor", "roleCategory": "ControlPlane", "roleIsPrivileged": True, "roleHasDataActions": False, "actionsCount": 1, "dataActionsCount": 0, "description": "Manage everything except access."},
    {"roleName": "Reader", "roleCategory": "ControlPlane", "roleIsPrivileged": False, "roleHasDataActions": False, "actionsCount": 1, "dataActionsCount": 0, "description": "View only."},
    {"roleName": "User Access Administrator", "roleCategory": "ControlPlane", "roleIsPrivileged": True, "roleHasDataActions": False, "actionsCount": 3, "dataActionsCount": 0, "description": "Manage user access to Azure resources."},
    {"roleName": "Storage Blob Data Contributor", "roleCategory": "DataPlane", "roleIsPrivileged": True, "roleHasDataActions": True, "actionsCount": 4, "dataActionsCount": 3, "description": "Read/write/delete blob data."},
    {"roleName": "Storage Blob Data Reader", "roleCategory": "DataPlane", "roleIsPrivileged": False, "roleHasDataActions": True, "actionsCount": 2, "dataActionsCount": 1, "description": "Read blob data."},
]


def _principal_dir() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in P.values():
        out.append(
            {
                "principalId": p["principalId"],
                "principalType": p["principalType"],
                "displayName": p["principalDisplayName"],
                "userPrincipalName": p.get("principalUserPrincipalName", ""),
                "appId": p.get("principalAppId", ""),
                "source": "Demo",
            }
        )
    return out


# --- group-expansion graph ------------------------------------------------------------
def _groups() -> dict[str, Any]:
    return {
        P["grp_admins"]["principalId"]: {
            "name": P["grp_admins"]["principalDisplayName"],
            "members": [P["alice"], P["eve"]],
        },
        P["grp_readers"]["principalId"]: {
            "name": P["grp_readers"]["principalDisplayName"],
            "members": [P["carol"], P["frank"]],
        },
    }


# --- per-scope slices -----------------------------------------------------------------
def _scope_slices() -> list[dict[str, Any]]:
    """Each entry: scope meta + the Azure-RBAC/KV/classic rows that land on that scope."""
    prod = "Contoso Production"
    dev = "Contoso Development"
    rg_data = f"{SCOPE_PROD}/resourceGroups/rg-data"
    kv_scope = f"{SCOPE_PROD}/resourceGroups/rg-data/providers/Microsoft.KeyVault/vaults/kv-contoso-prod"

    mg_rows = [
        _az_row(scope=SCOPE_MG, principal=P["alice"], role="Owner", privileged=True, inherited=True, collector="ManagementGroupRbac"),
    ]
    prod_rows = [
        _az_row(scope=SCOPE_PROD, principal=P["bob"], role="Contributor", privileged=True, sub_name=prod),
        _az_row(scope=SCOPE_PROD, principal=P["carol"], role="Reader", privileged=False, sub_name=prod),
        _az_row(scope=SCOPE_PROD, principal=P["grp_admins"], role="Owner", privileged=True, sub_name=prod),
        _az_row(scope=SCOPE_PROD, principal=P["sp_deploy"], role="Contributor", privileged=True, sub_name=prod),
        _az_row(scope=SCOPE_PROD, principal=P["henry"], role="User Access Administrator", privileged=True, sub_name=prod),
        _classic_row(scope=SCOPE_PROD, principal=P["ken"], sub_name=prod),
    ]
    data_rows = [
        _az_row(scope=rg_data, principal=P["dave"], role="Storage Blob Data Contributor", privileged=True, data_actions=True, sub_name=prod, collector="AzureResourceGroupRbac"),
        _az_row(scope=rg_data, principal=P["grp_readers"], role="Storage Blob Data Reader", privileged=False, data_actions=True, sub_name=prod, collector="AzureResourceGroupRbac"),
        _kv_row(scope=kv_scope, principal=P["julia"], sub_name=prod),
    ]
    dev_rows = [
        _az_row(scope=SCOPE_DEV, principal=P["bob"], role="Owner", privileged=True, sub_name=dev),
        _az_row(scope=SCOPE_DEV, principal=P["eve"], role="Contributor", privileged=True, sub_name=dev),
        _az_row(scope=SCOPE_DEV, principal=P["carol"], role="Reader", privileged=False, sub_name=dev),
    ]

    def _collectors(names: list[tuple[str, str, int]]) -> list[dict[str, Any]]:
        return [{"collector": n, "status": st, "rowsAdded": c, "durationSeconds": 1.0, "message": ""} for n, st, c in names]

    return [
        {
            "scope": SCOPE_MG, "scopeType": schema.SCOPE_MANAGEMENT_GROUP, "displayName": "Contoso (root MG)",
            "managementGroupId": MG_ID, "rows": mg_rows, "demo": DEMO_FLAG,
            "collectors": _collectors([("ManagementGroupRbac", schema.STATUS_SUCCEEDED, 1)]),
            "coverage": {"roleAssignments": len(mg_rows)},
        },
        {
            "scope": SCOPE_PROD, "scopeType": schema.SCOPE_SUBSCRIPTION, "displayName": prod,
            "subscriptionId": SUB_PROD, "rows": prod_rows + data_rows, "demo": DEMO_FLAG,
            "collectors": _collectors([
                ("AzureSubscriptionRbac", schema.STATUS_SUCCEEDED, len(prod_rows)),
                ("AzureResourceGroupRbac", schema.STATUS_SUCCEEDED, len(data_rows)),
                ("KeyVaultAccessPolicies", schema.STATUS_SUCCEEDED, 1),
                ("ClassicAdmins", schema.STATUS_SUCCEEDED, 1),
                ("ReservationAccess", schema.STATUS_UNAUTHORIZED, 0),
            ]),
            "coverage": {"roleAssignments": len(prod_rows + data_rows), "resourceGroups": 2, "resources": 2},
        },
        {
            "scope": SCOPE_DEV, "scopeType": schema.SCOPE_SUBSCRIPTION, "displayName": dev,
            "subscriptionId": SUB_DEV, "rows": dev_rows, "demo": DEMO_FLAG,
            "collectors": _collectors([("AzureSubscriptionRbac", schema.STATUS_SUCCEEDED, len(dev_rows))]),
            "coverage": {"roleAssignments": len(dev_rows)},
        },
    ]


def _directory_rows() -> list[dict[str, Any]]:
    return [
        _entra_row(principal=P["alice"], role="Global Administrator"),
        _entra_row(principal=P["henry"], role="User Administrator"),
        _entra_row(principal=P["ivan"], role="Security Administrator", state=schema.STATE_ELIGIBLE),
        _owner_row(sp=P["sp_deploy"], owner=P["gary"]),
    ]


def seed_demo(tenant_id: str) -> dict[str, Any]:
    """Write the demo per-scope slices + directory layer into the cache for ``tenant_id``.

    Idempotent: overwrites any existing demo snapshot. Returns a small summary."""
    slices = _scope_slices()
    for sl in slices:
        meta = {
            "scopeType": sl["scopeType"],
            "displayName": sl["displayName"],
            "subscriptionId": sl.get("subscriptionId", ""),
            "managementGroupId": sl.get("managementGroupId", ""),
            "collectors": sl["collectors"],
            "coverage": sl["coverage"],
            "status": schema.STATUS_SUCCEEDED,
            "demo": DEMO_FLAG,
        }
        cache.write_scope(tenant_id, sl["scope"], meta=meta, rows=sl["rows"])

    dir_rows = _directory_rows()
    cache.write_directory(
        tenant_id,
        meta={
            "status": schema.STATUS_SUCCEEDED,
            "demo": DEMO_FLAG,
            "collectors": [
                {"collector": "EntraRoleAssignments", "status": schema.STATUS_SUCCEEDED, "rowsAdded": 2, "durationSeconds": 1.0, "message": ""},
                {"collector": "PimDirectoryAssignments", "status": schema.STATUS_SUCCEEDED, "rowsAdded": 1, "durationSeconds": 1.0, "message": ""},
                {"collector": "ServicePrincipalOwners", "status": schema.STATUS_SUCCEEDED, "rowsAdded": 1, "durationSeconds": 1.0, "message": ""},
                {"collector": "GroupExpansion", "status": schema.STATUS_SUCCEEDED, "rowsAdded": 4, "durationSeconds": 1.0, "message": ""},
            ],
        },
        rows=dir_rows,
        role_defs=ROLE_DEFS,
        principals=_principal_dir(),
        groups=_groups(),
    )
    return {"scopes": len(slices), "directory_rows": len(dir_rows)}


def is_demo_tenant(tenant_id: str) -> bool:
    return cache.is_demo(tenant_id)

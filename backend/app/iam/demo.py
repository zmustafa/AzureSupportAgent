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

from app.iam import cache, schema

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
    # Managed identities. They appear in the grid as ordinary service principals — which is
    # exactly the problem the identity inventory solves: without it these read as unexplained
    # GUIDs and nobody can say what they are attached to or who can run code as them.
    "mi_vm": _principal("mi-vm-prod-01", "ServicePrincipal", "vm-prod-01"),
    "mi_shared": _principal("mi-shared-deploy", "ServicePrincipal", "id-shared-deploy"),
}

# --- managed identity inventory (P5) --------------------------------------------------
# `vm-prod-01` carries a system-assigned identity that is a Contributor: anyone who can run a
# command on that VM inherits Contributor. That is the single most common real escalation path
# in any tenant, and it is invisible in every Azure-native view.
_RG_PROD = f"/subscriptions/{SUB_PROD}/resourceGroups/rg-prod"
_RG_DEV = f"/subscriptions/{SUB_DEV}/resourceGroups/rg-dev"
VM_PROD_ID = f"{_RG_PROD}/providers/Microsoft.Compute/virtualMachines/vm-prod-01"
UAMI_SHARED_ID = f"{_RG_PROD}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-shared-deploy"

IDENTITIES: dict[str, dict[str, Any]] = {
    "mi-vm-prod-01": {
        "principalId": "mi-vm-prod-01",
        "identityKind": "SystemAssigned",
        "identityResourceId": VM_PROD_ID,
        "identityName": "vm-prod-01",
        "clientId": "",
        "subscriptionId": SUB_PROD,
        "resourceGroup": "rg-prod",
        "attachedResourceIds": [VM_PROD_ID],
        "attachedResourceType": "Microsoft.Compute/virtualMachines",
        "attachedResourceCount": 1,
    },
    "mi-shared-deploy": {
        "principalId": "mi-shared-deploy",
        "identityKind": "UserAssigned",
        "identityResourceId": UAMI_SHARED_ID,
        "identityName": "id-shared-deploy",
        "clientId": "cid-shared-deploy",
        "subscriptionId": SUB_PROD,
        "resourceGroup": "rg-prod",
        # Attached in BOTH prod and dev — a compromise in dev reaches prod.
        "attachedResourceIds": [
            f"{_RG_PROD}/providers/Microsoft.Web/sites/app-prod",
            f"{_RG_DEV}/providers/Microsoft.Web/sites/app-dev",
        ],
        "attachedResourceCount": 2,
    },
}

# A federated credential with a wildcard subject: any repository in the org, any branch, can
# mint a token for this identity. No secret, no expiry, nothing unusual in the sign-in logs.
FEDERATED: list[dict[str, Any]] = [
    {
        "identityResourceId": UAMI_SHARED_ID,
        "identityName": "id-shared-deploy",
        "credentialId": f"{UAMI_SHARED_ID}/federatedIdentityCredentials/gh-any",
        "name": "gh-any",
        "issuer": "https://token.actions.githubusercontent.com",
        "subject": "repo:contoso/*:ref:refs/heads/*",
        "audiences": ["api://AzureADTokenExchange"],
    },
    {
        "identityResourceId": UAMI_SHARED_ID,
        "identityName": "id-shared-deploy",
        "credentialId": f"{UAMI_SHARED_ID}/federatedIdentityCredentials/gh-main",
        "name": "gh-main",
        "issuer": "https://token.actions.githubusercontent.com",
        "subject": "repo:contoso/platform:ref:refs/heads/main",
        "audiences": ["api://AzureADTokenExchange"],
    },
]


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
    activation_hours_left: float | None = None,
) -> dict[str, Any]:
    """One Azure RBAC assignment row, scope fields auto-derived from the scope id.

    ``activation_hours_left`` marks the row as a PIM elevation currently in force — the same
    shape the orchestrator stamps on via ``_annotate_pim``. Such a row is active *now* but is
    not standing privilege, because it expires."""
    parts = schema.parse_scope(scope)
    elevated = activation_hours_left is not None
    return schema.make_row(
        surface=schema.SURFACE_AZURE_RBAC,
        accessModel=schema.ACCESS_DATA_PLANE if data_actions else schema.ACCESS_CONTROL_PLANE,
        collector=collector,
        assignmentState=state,
        assignmentType="ActivatedRoleAssignment" if elevated else "RoleAssignment",
        accessPath=schema.PATH_DIRECT,
        pimManaged=elevated,
        activationExpiresOn=(
            (datetime.now(timezone.utc) + timedelta(hours=activation_hours_left)).isoformat() if elevated else ""
        ),
        memberType="Direct" if elevated else "",
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


def _eligible_row(
    *,
    scope: str,
    principal: dict[str, str],
    role: str,
    sub_name: str,
    permanent: bool = False,
    requires_approval: bool = True,
    requires_mfa: bool = True,
    max_hours: str = "8",
) -> dict[str, Any]:
    """An Azure role the principal can ACTIVATE but does not currently hold.

    Eligible access does not appear in ``roleAssignments`` at all, so these are new rows rather
    than duplicates of the active grants."""
    parts = schema.parse_scope(scope)
    start = _iso(60)
    end = "" if permanent else (datetime.now(timezone.utc) + timedelta(days=120)).isoformat()
    return schema.make_row(
        surface=schema.SURFACE_AZURE_RBAC,
        accessModel=schema.ACCESS_CONTROL_PLANE,
        collector="AzurePimEligibility",
        assignmentState=schema.STATE_ELIGIBLE,
        assignmentType="RoleEligibility",
        accessPath=schema.PATH_DIRECT,
        roleName=role,
        roleCategory="ControlPlane",
        roleIsPrivileged=schema.role_is_privileged(role),
        scope=scope,
        scopeType=parts.get("scopeType", ""),
        scopeDisplayName=sub_name or scope,
        tenantId=TENANT,
        managementGroupId=parts.get("managementGroupId", ""),
        subscriptionId=parts.get("subscriptionId", ""),
        subscriptionName=sub_name,
        assignmentId=f"{scope}/providers/Microsoft.Authorization/roleEligibilityScheduleInstances/es-{principal['principalId']}-{role}".replace(" ", ""),
        assignmentCreatedOn=start,
        sourceApi="ARM roleEligibilityScheduleInstances",
        collectionStatus=schema.STATUS_SUCCEEDED,
        pimManaged=True,
        eligibilityStartDateTime=start,
        eligibilityEndDateTime=end,
        isPermanentEligible=permanent,
        memberType="Direct",
        requiresApproval=requires_approval,
        requiresMfa=requires_mfa,
        requiresJustification=True,
        activationMaxHours=max_hours,
        effectivePrincipalId=principal["principalId"],
        effectivePrincipalType=principal["principalType"],
        effectivePrincipalName=principal["principalDisplayName"],
        effectivePrincipalUserPrincipalName=principal.get("principalUserPrincipalName", ""),
        **principal,
    )


def _deny_row(*, scope: str, sub_name: str, name: str, all_principals: bool = False, principal: dict[str, str] | None = None) -> dict[str, Any]:
    """A deny assignment — evaluated before role assignments and not overridable, even by Owner."""
    parts = schema.parse_scope(scope)
    who = principal or _principal("sys-all-principals", "SystemDefined", "All principals")
    return schema.make_row(
        surface=schema.SURFACE_DENY,
        accessModel=schema.ACCESS_DENY,
        collector="AzureDenyAssignments",
        effect=schema.EFFECT_DENY,
        assignmentState=schema.STATE_ACTIVE,
        assignmentType="DenyAssignment",
        accessPath=schema.PATH_DIRECT,
        roleName=name,
        roleCategory="ControlPlane",
        roleIsPrivileged=False,
        scope=scope,
        scopeType=parts.get("scopeType", ""),
        scopeDisplayName=sub_name or scope,
        tenantId=TENANT,
        subscriptionId=parts.get("subscriptionId", ""),
        subscriptionName=sub_name,
        resourceGroup=parts.get("resourceGroup", ""),
        assignmentId=f"{scope}/providers/Microsoft.Authorization/denyAssignments/da-{name}".replace(" ", ""),
        assignmentCreatedOn=_iso(45),
        sourceApi="ARM denyAssignments",
        collectionStatus=schema.STATUS_SUCCEEDED,
        principalId=who["principalId"],
        principalType=who["principalType"],
        principalDisplayName=who["principalDisplayName"],
        effectivePrincipalId=who["principalId"],
        effectivePrincipalType=who["principalType"],
        effectivePrincipalName=who["principalDisplayName"],
    )


# --- role definitions + principal directory (reference sets) --------------------------
# The four action lists are real: they are what `app.iam.effective` resolves "can P do A on R"
# against, and without them the demo tenant answers "indeterminate" to every question about the
# product's flagship capability. Trimmed to the actions the demo actually exercises, but the
# SHAPE is exact — notably Contributor's notActions and the control/data-plane split, which are
# the two rules the engine most needs to demonstrate.
_BLOB = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs"

ROLE_DEFS = [
    {
        "roleName": "Owner", "roleCategory": "ControlPlane", "roleIsPrivileged": True,
        "roleHasDataActions": False, "actionsCount": 1, "dataActionsCount": 0,
        "description": "Full access including the right to assign roles.",
        "actions": ["*"], "notActions": [], "dataActions": [], "notDataActions": [],
    },
    {
        "roleName": "Contributor", "roleCategory": "ControlPlane", "roleIsPrivileged": True,
        "roleHasDataActions": False, "actionsCount": 1, "dataActionsCount": 0,
        "description": "Manage everything except access.",
        "actions": ["*"],
        # The reason Contributor cannot hand out access — and the engine's notActions test case.
        "notActions": [
            "Microsoft.Authorization/*/Delete",
            "Microsoft.Authorization/*/Write",
            "Microsoft.Authorization/elevateAccess/Action",
        ],
        "dataActions": [], "notDataActions": [],
    },
    {
        "roleName": "Reader", "roleCategory": "ControlPlane", "roleIsPrivileged": False,
        "roleHasDataActions": False, "actionsCount": 1, "dataActionsCount": 0,
        "description": "View only.",
        "actions": ["*/read"], "notActions": [], "dataActions": [], "notDataActions": [],
    },
    {
        "roleName": "User Access Administrator", "roleCategory": "ControlPlane",
        "roleIsPrivileged": True, "roleHasDataActions": False, "actionsCount": 3,
        "dataActionsCount": 0, "description": "Manage user access to Azure resources.",
        "actions": ["*/read", "Microsoft.Authorization/*", "Microsoft.Support/*"],
        "notActions": [], "dataActions": [], "notDataActions": [],
    },
    {
        "roleName": "Storage Blob Data Contributor", "roleCategory": "DataPlane",
        "roleIsPrivileged": True, "roleHasDataActions": True, "actionsCount": 4,
        "dataActionsCount": 3, "description": "Read/write/delete blob data.",
        "actions": [
            "Microsoft.Storage/storageAccounts/blobServices/containers/delete",
            "Microsoft.Storage/storageAccounts/blobServices/containers/read",
            "Microsoft.Storage/storageAccounts/blobServices/containers/write",
        ],
        "notActions": [],
        "dataActions": [f"{_BLOB}/delete", f"{_BLOB}/read", f"{_BLOB}/write"],
        "notDataActions": [],
    },
    {
        "roleName": "Storage Blob Data Reader", "roleCategory": "DataPlane",
        "roleIsPrivileged": False, "roleHasDataActions": True, "actionsCount": 2,
        "dataActionsCount": 1, "description": "Read blob data.",
        "actions": ["Microsoft.Storage/storageAccounts/blobServices/containers/read"],
        "notActions": [], "dataActions": [f"{_BLOB}/read"], "notDataActions": [],
    },
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
        # Assigned ONCE at the management group. The two subscription slices below each carry an
        # inherited copy with the same assignmentId, exactly as ARM returns them — compose's
        # dedupe must collapse all three back to this one authoritative row.
        _az_row(scope=SCOPE_MG, principal=P["alice"], role="Owner", privileged=True, collector="ManagementGroupRbac"),
    ]
    # The inherited copies ARM reports from each child subscription. Same assignmentId, same
    # principal, but attributed to whichever subscription answered.
    mg_inherited_prod = _az_row(
        scope=SCOPE_MG, principal=P["alice"], role="Owner", privileged=True, inherited=True,
        sub_name="Contoso Production",
    )
    mg_inherited_dev = _az_row(
        scope=SCOPE_MG, principal=P["alice"], role="Owner", privileged=True, inherited=True,
        sub_name="Contoso Development",
    )
    prod_rows = [
        _az_row(scope=SCOPE_PROD, principal=P["bob"], role="Contributor", privileged=True, sub_name=prod),
        _az_row(scope=SCOPE_PROD, principal=P["carol"], role="Reader", privileged=False, sub_name=prod),
        _az_row(scope=SCOPE_PROD, principal=P["grp_admins"], role="Owner", privileged=True, sub_name=prod),
        _az_row(scope=SCOPE_PROD, principal=P["sp_deploy"], role="Contributor", privileged=True, sub_name=prod),
        _az_row(scope=SCOPE_PROD, principal=P["henry"], role="User Access Administrator", privileged=True, sub_name=prod),
        _classic_row(scope=SCOPE_PROD, principal=P["ken"], sub_name=prod),
        mg_inherited_prod,
        # A Blueprint-style deny wall over the locked resource group.
        _deny_row(scope=f"{SCOPE_PROD}/resourceGroups/rg-locked", sub_name=prod, name="Blueprint lock: rg-locked"),
        # PIM: Ivan can request Owner (approval + MFA required, 8h max) but does not hold it.
        _eligible_row(scope=SCOPE_PROD, principal=P["ivan"], role="Owner", sub_name=prod),
        # PIM configured badly: permanently eligible for User Access Administrator with no
        # approval and no MFA — technically JIT, practically standing privilege.
        _eligible_row(
            scope=SCOPE_PROD, principal=P["henry"], role="User Access Administrator", sub_name=prod,
            permanent=True, requires_approval=False, requires_mfa=False, max_hours="24",
        ),
    ]
    data_rows = [
        _az_row(scope=rg_data, principal=P["dave"], role="Storage Blob Data Contributor", privileged=True, data_actions=True, sub_name=prod, collector="AzureResourceGroupRbac"),
        _az_row(scope=rg_data, principal=P["grp_readers"], role="Storage Blob Data Reader", privileged=False, data_actions=True, sub_name=prod, collector="AzureResourceGroupRbac"),
        _kv_row(scope=kv_scope, principal=P["julia"], sub_name=prod),
    ]
    # --- escalation fixtures (P5) -----------------------------------------------------
    # Frank is only a Contributor on rg-prod, which is the point: he holds no privileged ROLE,
    # but Contributor carries virtualMachines/runCommand/action, so he can run code on
    # vm-prod-01 and inherit its identity — and that identity is a Contributor at subscription
    # scope. A two-hop path from "not privileged" to "controls production".
    escalation_rows = [
        _az_row(scope=_RG_PROD, principal=P["frank"], role="Contributor", privileged=True,
                sub_name=prod, collector="AzureResourceGroupRbac"),
        # The VM's system-assigned identity, holding more than the person who can hijack it.
        _az_row(scope=SCOPE_PROD, principal=P["mi_vm"], role="Contributor", privileged=True, sub_name=prod),
        # The shared user-assigned identity, reachable through its federated credentials.
        _az_row(scope=SCOPE_PROD, principal=P["mi_shared"], role="User Access Administrator",
                privileged=True, sub_name=prod),
    ]
    dev_rows = [
        _az_row(scope=SCOPE_DEV, principal=P["bob"], role="Owner", privileged=True, sub_name=dev),
        # Eve is elevated right now via PIM — active, but it expires, so it is NOT standing
        # privilege and must not be counted as such.
        _az_row(scope=SCOPE_DEV, principal=P["eve"], role="Contributor", privileged=True, sub_name=dev, activation_hours_left=3.5),
        _az_row(scope=SCOPE_DEV, principal=P["carol"], role="Reader", privileged=False, sub_name=dev),
        mg_inherited_dev,
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
            "subscriptionId": SUB_PROD, "rows": prod_rows + data_rows + escalation_rows, "demo": DEMO_FLAG,
            "collectors": _collectors([
                ("AzureSubscriptionRbac", schema.STATUS_SUCCEEDED, len(prod_rows)),
                ("AzureResourceGroupRbac", schema.STATUS_SUCCEEDED, len(data_rows)),
                ("AzurePimEligibility", schema.STATUS_SUCCEEDED, 2),
                ("AzurePimPolicies", schema.STATUS_SUCCEEDED, 2),
                ("AzureDenyAssignments", schema.STATUS_SUCCEEDED, 1),
                ("KeyVaultAccessPolicies", schema.STATUS_SUCCEEDED, 1),
                ("ClassicAdministrators", schema.STATUS_SUCCEEDED, 1),
                ("ArgManagedIdentities", schema.STATUS_SUCCEEDED, len(IDENTITIES)),
                ("FederatedIdentityCredentials", schema.STATUS_SUCCEEDED, len(FEDERATED)),
                ("ReservationAccess", schema.STATUS_UNAUTHORIZED, 0),
            ]),
            "coverage": {"roleAssignments": len(prod_rows + data_rows + escalation_rows), "resourceGroups": 2, "resources": 2},
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


# --- RBAC-bypass surface (P6) ----------------------------------------------------------
# Deliberately mixed: some resources where RBAC really is the only door, and some where it is
# not. A demo estate where everything is broken teaches nothing, and one where nothing is broken
# demonstrates nothing.
def _bypass_resources() -> list[dict[str, Any]]:
    def _res(name: str, rtype: str, rg: str, sub: str, **props: str) -> dict[str, Any]:
        base = {
            "id": f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{rtype}/{name}",
            "name": name,
            "type": rtype.lower(),
            "subscriptionId": sub,
            "resourceGroup": rg,
            "tags": {},
        }
        base.update(props)
        return base

    return [
        # Wide open: shared keys, no expiry policy, anonymous blobs. The one a reader should act on.
        _res("stprodpayments", "Microsoft.Storage/storageAccounts", "rg-data", SUB_PROD,
             allowSharedKeyAccess="true", allowBlobPublicAccess="true",
             allowCrossTenantReplication="true"),
        # Locked down: RBAC really is the only door here.
        _res("stprodlogs", "Microsoft.Storage/storageAccounts", "rg-data", SUB_PROD,
             allowSharedKeyAccess="false", allowBlobPublicAccess="false",
             allowCrossTenantReplication="false", keyExpirationPeriodInDays="90"),
        # Dev sandbox with the same settings as the payments account — same configuration, and
        # NOT the same finding. This is what the environment modifier exists to demonstrate.
        _res("stdevscratch", "Microsoft.Storage/storageAccounts", "rg-dev", SUB_DEV,
             allowSharedKeyAccess="true", allowBlobPublicAccess="false"),
        _res("cosmos-orders", "Microsoft.DocumentDB/databaseAccounts", "rg-data", SUB_PROD,
             disableLocalAuth="false"),
        _res("sb-events", "Microsoft.ServiceBus/namespaces", "rg-data", SUB_PROD),
        # AKS: local accounts on AND no Azure RBAC — cluster-admin without Entra.
        _res("aks-prod", "Microsoft.ContainerService/managedClusters", "rg-prod", SUB_PROD,
             disableLocalAccounts="false", enableAzureRBAC="false", aadProfileManaged="true"),
        _res("acr-contoso", "Microsoft.ContainerRegistry/registries", "rg-prod", SUB_PROD,
             adminUserEnabled="true"),
        _res("sql-erp", "Microsoft.Sql/servers", "rg-data", SUB_PROD,
             azureADOnlyAuthentication="false", adminLogin="dba@contoso.example"),
        # The legacy vault from the access rows, seen from the other side: access policies, not RBAC.
        _res("kv-contoso-prod", "Microsoft.KeyVault/vaults", "rg-data", SUB_PROD,
             enableRbacAuthorization="false"),
    ]


def _bypass_environment() -> dict[str, str]:
    """Resource id -> environment. Production is where a shared key matters."""
    out: dict[str, str] = {}
    for r in _bypass_resources():
        out[str(r["id"]).lower()] = "dev" if r["subscriptionId"] == SUB_DEV else "prod"
    return out


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
        identities=IDENTITIES,
        federated=FEDERATED,
    )

    # The bypass sweep, assessed through the real code path so the demo exercises the same
    # detectors and the same severity modulation a live tenant does.
    from app.iam import bypass

    resources = _bypass_resources()
    rows = bypass.assess(resources, reachability={}, reachability_available=False,
                         workload_env=_bypass_environment())
    fam_status = {
        f: collector_status(f, resources) for f in bypass.FAMILIES
    }
    cache.write_bypass(
        tenant_id,
        meta={
            "status": schema.STATUS_SUCCEEDED,
            "demo": DEMO_FLAG,
            "collectors": [s.public() for s in fam_status.values()],
            "reachability_available": False,
        },
        resources=resources,
        rows=rows,
        summary=bypass.summarize(resources, rows, fam_status),
    )
    return {"scopes": len(slices), "directory_rows": len(dir_rows), "bypass_rows": len(rows)}


def collector_status(family: str, resources: list[dict[str, Any]]):
    """A per-family status for the demo sweep, mirroring what the live collector produces."""
    from app.iam.bypass import specs as bypass_specs
    from app.iam.collectors import CollectorStatus

    count = sum(1 for r in resources if str(r.get("type", "")).lower() in bypass_specs.TYPES_BY_FAMILY[family])
    st = CollectorStatus(f"Bypass{family.title()}", schema.STATUS_SUCCEEDED, count, 0.5, "")
    if count == 0:
        st.status = schema.STATUS_SKIPPED
        st.message = "No resources of this type were returned."
    return st


def is_demo_tenant(tenant_id: str) -> bool:
    return cache.is_demo(tenant_id)

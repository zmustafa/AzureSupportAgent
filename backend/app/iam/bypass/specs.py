"""The bypass check table — one declarative spec per way RBAC can be sidestepped.

Declarative on purpose. Each spec names the resource type it applies to, the projected field it
reads, how to decide whether the bypass is *enabled*, the control-plane action that yields the
credential, and — critically — what breaks if you turn it off.

That last field is not decoration. A ``--allow-shared-key-access false`` that silently breaks
every connection-string client in production is worse than the finding it closes, so remediation
is never published without it.

**Defaults matter more than they look.** Almost every one of these properties is absent on older
resources and means *enabled*. Reading a missing field as "off" would report an estate wide open
as an estate that is locked down, so every detector below states its default explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Bypass kinds — the vocabulary the UI groups and filters by.
KIND_SHARED_KEY = "SharedKey"
KIND_LOCAL_AUTH = "LocalAuth"
KIND_SAS_RULE = "SasRule"
KIND_ADMIN_USER = "AdminUser"
KIND_CLUSTER_ADMIN = "ClusterAdminCredential"
KIND_SQL_AUTH = "SqlAuth"
KIND_BASIC_PUBLISHING = "BasicPublishing"
KIND_RUNAS = "RunAs"
KIND_PUBLIC_ACCESS = "PublicAccess"
KIND_KEY_LIFECYCLE = "KeyLifecycle"


def _is_false(value: Any) -> bool:
    """Explicitly false. Anything else — including absent — is NOT false."""
    return str(value).strip().lower() == "false"


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _absent(value: Any) -> bool:
    return str(value).strip().lower() in ("", "none", "null")


@dataclass(frozen=True)
class BypassSpec:
    """One way to reach a resource without an Azure role assignment."""

    key: str                      # "storage.shared_key"
    family: str                   # groups into an independently-skippable collector
    resource_type: str            # lower-case ARM type
    bypass_kind: str
    title: str
    # Given the projected ARG row, is this bypass ENABLED on this resource?
    detect: Callable[[dict[str, Any]], bool]
    detail: str
    remediation: str
    # What stops working if the remediation is applied. Never publish one without the other.
    breaks_if: str
    # The control-plane action that yields the credential. Feeds `reachableBy` via the
    # effective-permission engine — an empty string means the credential is not reachable
    # through an Azure action (e.g. anonymous public access needs no credential at all).
    credential_action: str = ""
    severity: str = "warning"
    # True when the service CAN be operated with RBAC only. False means disabling the bypass is
    # not an option today, and reporting it as remediable would be wrong.
    rbac_only_possible: bool = True
    frameworks: tuple[str, ...] = ()
    # Extra ARG fields this spec needs, beyond the shared projection.
    fields: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- the table
BYPASS_SPECS: list[BypassSpec] = [
    # ---- storage ---------------------------------------------------------------------
    BypassSpec(
        key="storage.shared_key",
        family="storage",
        resource_type="microsoft.storage/storageaccounts",
        bypass_kind=KIND_SHARED_KEY,
        title="Shared key authentication is enabled",
        # ABSENT MEANS ENABLED. `allowSharedKeyAccess` was added years after storage accounts
        # existed; treating a missing value as disabled reports every legacy account as safe.
        detect=lambda r: not _is_false(r.get("allowSharedKeyAccess")),
        detail=(
            "The account keys grant full data-plane access to every container, share, queue and "
            "table, with no principal, no conditional access and no role assignment involved."
        ),
        remediation="az storage account update -n {name} -g {rg} --allow-shared-key-access false",
        breaks_if=(
            "any client using a connection string or account key — SDK defaults, AzCopy without "
            "--auth-mode login, Terraform backends, and most CI pipelines"
        ),
        credential_action="Microsoft.Storage/storageAccounts/listKeys/action",
        severity="error",
        frameworks=("CIS-Azure:3.8", "MCSB:IM-1", "NIST:AC-3"),
    ),
    BypassSpec(
        key="storage.key_never_expires",
        family="storage",
        resource_type="microsoft.storage/storageaccounts",
        bypass_kind=KIND_KEY_LIFECYCLE,
        title="Account keys have no expiry policy",
        detect=lambda r: (
            not _is_false(r.get("allowSharedKeyAccess")) and _absent(r.get("keyExpirationPeriodInDays"))
        ),
        detail=(
            "Shared key access is enabled and no key expiration policy is set, so a leaked key "
            "stays valid until somebody notices and rotates it manually."
        ),
        remediation="az storage account update -n {name} -g {rg} --key-exp-days 90",
        breaks_if="nothing immediately; clients must handle rotation before the period elapses",
        credential_action="Microsoft.Storage/storageAccounts/listKeys/action",
        severity="warning",
        frameworks=("CIS-Azure:3.9",),
    ),
    BypassSpec(
        key="storage.public_blob",
        family="storage",
        resource_type="microsoft.storage/storageaccounts",
        bypass_kind=KIND_PUBLIC_ACCESS,
        title="Anonymous blob access is permitted",
        detect=lambda r: _is_true(r.get("allowBlobPublicAccess")),
        detail=(
            "Containers in this account may be configured for anonymous read. That is access "
            "with no credential at all — not even a key."
        ),
        remediation="az storage account update -n {name} -g {rg} --allow-blob-public-access false",
        breaks_if="any container serving public static content or public downloads",
        credential_action="",  # anonymous: no credential, so nothing to be "reachable by"
        severity="error",
        frameworks=("CIS-Azure:3.7", "NIST:AC-3"),
    ),
    BypassSpec(
        key="storage.cross_tenant_replication",
        family="storage",
        resource_type="microsoft.storage/storageaccounts",
        bypass_kind=KIND_PUBLIC_ACCESS,
        title="Cross-tenant replication is allowed",
        detect=lambda r: not _is_false(r.get("allowCrossTenantReplication")),
        detail="Object replication can copy this account's data into another Entra tenant.",
        remediation="az storage account update -n {name} -g {rg} --allow-cross-tenant-replication false",
        breaks_if="any configured cross-tenant object replication policy",
        credential_action="Microsoft.Storage/storageAccounts/write",
        severity="warning",
        frameworks=("MCSB:DP-3",),
    ),
    # ---- local-auth family (one property, many services) -------------------------------
    BypassSpec(
        key="cosmos.local_auth",
        family="cosmos",
        resource_type="microsoft.documentdb/databaseaccounts",
        bypass_kind=KIND_LOCAL_AUTH,
        title="Cosmos DB key-based authentication is enabled",
        detect=lambda r: not _is_true(r.get("disableLocalAuth")),
        detail=(
            "The primary and secondary keys grant full data-plane access and bypass Cosmos DB's "
            "own RBAC entirely."
        ),
        remediation=(
            "az resource update --ids {id} --set properties.disableLocalAuth=true"
        ),
        breaks_if="every SDK client using an account key or connection string",
        credential_action="Microsoft.DocumentDB/databaseAccounts/listKeys/action",
        severity="error",
        frameworks=("MCSB:IM-1", "NIST:AC-3"),
    ),
    BypassSpec(
        key="servicebus.local_auth",
        family="servicebus",
        resource_type="microsoft.servicebus/namespaces",
        bypass_kind=KIND_LOCAL_AUTH,
        title="Service Bus SAS authentication is enabled",
        detect=lambda r: not _is_true(r.get("disableLocalAuth")),
        detail="Shared access signatures grant send/listen with a connection string and no principal.",
        remediation="az servicebus namespace update -n {name} -g {rg} --disable-local-auth true",
        breaks_if="every publisher or subscriber using a connection string",
        credential_action="Microsoft.ServiceBus/namespaces/authorizationRules/listKeys/action",
        severity="error",
        frameworks=("MCSB:IM-1",),
    ),
    BypassSpec(
        key="eventhub.local_auth",
        family="eventhub",
        resource_type="microsoft.eventhub/namespaces",
        bypass_kind=KIND_LOCAL_AUTH,
        title="Event Hubs SAS authentication is enabled",
        detect=lambda r: not _is_true(r.get("disableLocalAuth")),
        detail="Shared access signatures grant send/listen with a connection string and no principal.",
        remediation="az eventhubs namespace update -n {name} -g {rg} --disable-local-auth true",
        breaks_if="every producer or consumer using a connection string",
        credential_action="Microsoft.EventHub/namespaces/authorizationRules/listKeys/action",
        severity="error",
        frameworks=("MCSB:IM-1",),
    ),
    BypassSpec(
        key="appconfig.local_auth",
        family="appconfig",
        resource_type="microsoft.appconfiguration/configurationstores",
        bypass_kind=KIND_LOCAL_AUTH,
        title="App Configuration access keys are enabled",
        detect=lambda r: not _is_true(r.get("disableLocalAuth")),
        detail="The access keys read every setting in the store, including anything not marked secret.",
        remediation="az appconfig update -n {name} -g {rg} --disable-local-auth true",
        breaks_if="clients using a connection string rather than a managed identity",
        credential_action="Microsoft.AppConfiguration/configurationStores/ListKeys/action",
        severity="warning",
        frameworks=("MCSB:IM-1",),
    ),
    BypassSpec(
        key="eventgrid.local_auth",
        family="eventgrid",
        resource_type="microsoft.eventgrid/topics",
        bypass_kind=KIND_LOCAL_AUTH,
        title="Event Grid access keys are enabled",
        detect=lambda r: not _is_true(r.get("disableLocalAuth")),
        detail="The topic keys allow publishing events with no principal.",
        remediation="az eventgrid topic update -n {name} -g {rg} --disable-local-auth true",
        breaks_if="publishers using a topic key",
        credential_action="Microsoft.EventGrid/topics/listKeys/action",
        severity="warning",
        frameworks=("MCSB:IM-1",),
    ),
    BypassSpec(
        key="search.local_auth",
        family="search",
        resource_type="microsoft.search/searchservices",
        bypass_kind=KIND_LOCAL_AUTH,
        title="Cognitive Search API keys are enabled",
        detect=lambda r: not _is_true(r.get("disableLocalAuth")),
        detail="Admin API keys grant full control of every index.",
        remediation="az search service update -n {name} -g {rg} --auth-options aadOrApiKey --disable-local-auth true",
        breaks_if="clients using an admin or query key",
        credential_action="Microsoft.Search/searchServices/listAdminKeys/action",
        severity="warning",
        frameworks=("MCSB:IM-1",),
    ),
    BypassSpec(
        key="redis.access_keys",
        family="redis",
        resource_type="microsoft.cache/redis",
        bypass_kind=KIND_SHARED_KEY,
        title="Redis access key authentication is enabled",
        detect=lambda r: not _is_true(r.get("disableAccessKeyAuthentication")),
        detail="The access keys grant full access to the cache with no principal.",
        remediation="az redis update -n {name} -g {rg} --set redisConfiguration.aad-enabled=true",
        breaks_if="every client using an access key or connection string",
        credential_action="Microsoft.Cache/redis/listKeys/action",
        severity="warning",
        frameworks=("MCSB:IM-1",),
    ),
    # ---- AKS -------------------------------------------------------------------------
    BypassSpec(
        key="aks.local_accounts",
        family="aks",
        resource_type="microsoft.containerservice/managedclusters",
        bypass_kind=KIND_CLUSTER_ADMIN,
        title="AKS local accounts are enabled",
        detect=lambda r: not _is_true(r.get("disableLocalAccounts")),
        detail=(
            "`listClusterAdminCredential` returns a certificate-based kubeconfig that is "
            "cluster-admin and bypasses Entra entirely — no sign-in, no conditional access, no MFA."
        ),
        remediation="az aks update -n {name} -g {rg} --disable-local-accounts",
        breaks_if="any pipeline or operator using the admin kubeconfig",
        credential_action="Microsoft.ContainerService/managedClusters/listClusterAdminCredential/action",
        severity="error",
        frameworks=("CIS-Azure:8.6", "MCSB:IM-1"),
    ),
    BypassSpec(
        key="aks.no_azure_rbac",
        family="aks",
        resource_type="microsoft.containerservice/managedclusters",
        bypass_kind=KIND_CLUSTER_ADMIN,
        title="Azure RBAC for Kubernetes authorization is off",
        detect=lambda r: not _is_true(r.get("enableAzureRBAC")),
        detail=(
            "Authorization inside the cluster is managed by Kubernetes RBAC objects, which this "
            "product does not read. Azure role assignments do not describe who can do what here."
        ),
        remediation="az aks update -n {name} -g {rg} --enable-azure-rbac",
        breaks_if="existing ClusterRoleBindings that grant access to Kubernetes-native identities",
        credential_action="Microsoft.ContainerService/managedClusters/listClusterUserCredential/action",
        severity="warning",
        frameworks=("CIS-Azure:8.5",),
    ),
    BypassSpec(
        key="aks.no_aad",
        family="aks",
        resource_type="microsoft.containerservice/managedclusters",
        bypass_kind=KIND_CLUSTER_ADMIN,
        title="AKS is not integrated with Entra ID at all",
        detect=lambda r: _absent(r.get("aadProfileManaged")) and _absent(r.get("enableAzureRBAC")),
        detail="Cluster access is entirely outside the directory. No Entra identity is involved.",
        remediation="az aks update -n {name} -g {rg} --enable-aad --enable-azure-rbac",
        breaks_if="every existing kubeconfig; all cluster users must re-authenticate",
        credential_action="Microsoft.ContainerService/managedClusters/listClusterAdminCredential/action",
        severity="error",
        frameworks=("CIS-Azure:8.5",),
    ),
    # ---- SQL / Synapse ---------------------------------------------------------------
    BypassSpec(
        key="sql.entra_only_off",
        family="sql",
        resource_type="microsoft.sql/servers",
        bypass_kind=KIND_SQL_AUTH,
        title="SQL authentication is permitted",
        detect=lambda r: not _is_true(r.get("azureADOnlyAuthentication")),
        detail=(
            "SQL logins exist outside the directory: no conditional access, no MFA, no joiner-"
            "mover-leaver process, and no trace of them in any access review."
        ),
        remediation="az sql server ad-only-auth enable -n {name} -g {rg}",
        breaks_if="every application or job connecting with a SQL username and password",
        credential_action="Microsoft.Sql/servers/write",
        severity="error",
        frameworks=("CIS-Azure:4.1.3", "MCSB:IM-1"),
    ),
    BypassSpec(
        key="sql.no_entra_admin",
        family="sql",
        resource_type="microsoft.sql/servers",
        bypass_kind=KIND_SQL_AUTH,
        title="No Entra administrator is configured",
        detect=lambda r: _absent(r.get("administratorLogin")) is False and _absent(r.get("adminLogin")),
        detail="Without an Entra admin, directory identities cannot be used to administer this server.",
        remediation="az sql server ad-admin create -s {name} -g {rg} --display-name <group> --object-id <id>",
        breaks_if="nothing; this only adds a directory administrator",
        credential_action="Microsoft.Sql/servers/administrators/write",
        severity="warning",
        frameworks=("CIS-Azure:4.1.4",),
    ),
    BypassSpec(
        key="synapse.sql_auth",
        family="synapse",
        resource_type="microsoft.synapse/workspaces",
        bypass_kind=KIND_SQL_AUTH,
        title="Synapse SQL authentication is configured",
        detect=lambda r: not _absent(r.get("sqlAdministratorLogin")),
        detail="A SQL administrator login exists outside the directory.",
        remediation="Configure Entra-only authentication on the workspace.",
        breaks_if="jobs connecting with the SQL administrator login",
        credential_action="Microsoft.Synapse/workspaces/write",
        severity="warning",
        frameworks=("MCSB:IM-1",),
    ),
    # ---- registries and web ----------------------------------------------------------
    BypassSpec(
        key="acr.admin_user",
        family="acr",
        resource_type="microsoft.containerregistry/registries",
        bypass_kind=KIND_ADMIN_USER,
        title="Container registry admin user is enabled",
        detect=lambda r: _is_true(r.get("adminUserEnabled")),
        detail=(
            "A single shared username and password with push rights. It belongs to no one, "
            "rotates never, and anyone who can read the registry's credentials can use it."
        ),
        remediation="az acr update -n {name} --admin-enabled false",
        breaks_if="any pipeline or Kubernetes imagePullSecret using the admin credentials",
        credential_action="Microsoft.ContainerRegistry/registries/listCredentials/action",
        severity="error",
        frameworks=("CIS-Azure:9.4", "MCSB:IM-1"),
    ),
    BypassSpec(
        key="keyvault.rbac_off",
        family="keyvault",
        resource_type="microsoft.keyvault/vaults",
        bypass_kind=KIND_LOCAL_AUTH,
        title="Key Vault uses access policies, not RBAC",
        detect=lambda r: not _is_true(r.get("enableRbacAuthorization")),
        detail=(
            "Data access is granted by the vault's own access-policy list, which does not appear "
            "in any role assignment. Anyone with control-plane write on the vault can add "
            "themselves to it."
        ),
        remediation="az keyvault update -n {name} -g {rg} --enable-rbac-authorization true",
        breaks_if="every principal currently listed in the vault's access policies",
        credential_action="Microsoft.KeyVault/vaults/write",
        severity="warning",
        frameworks=("CIS-Azure:8.4", "MCSB:PA-8"),
    ),
    BypassSpec(
        key="batch.shared_key",
        family="batch",
        resource_type="microsoft.batch/batchaccounts",
        bypass_kind=KIND_SHARED_KEY,
        title="Batch account allows shared-key authentication",
        detect=lambda r: "sharedkey" in str(r.get("allowedAuthenticationModes", "")).lower(),
        detail="Account keys allow submitting jobs with no principal.",
        remediation="az batch account set -n {name} -g {rg} --allowed-auth-modes AAD",
        breaks_if="clients submitting jobs with an account key",
        credential_action="Microsoft.Batch/batchAccounts/listKeys/action",
        severity="warning",
        frameworks=("MCSB:IM-1",),
    ),
]

# Families, in the order the UI lists them. Each is independently skippable and carries its own
# collector status, so one blind service never blanks the tab.
FAMILIES: list[str] = []
for _spec in BYPASS_SPECS:
    if _spec.family not in FAMILIES:
        FAMILIES.append(_spec.family)

SPECS_BY_FAMILY: dict[str, list[BypassSpec]] = {}
for _spec in BYPASS_SPECS:
    SPECS_BY_FAMILY.setdefault(_spec.family, []).append(_spec)

RESOURCE_TYPES: list[str] = []
for _spec in BYPASS_SPECS:
    if _spec.resource_type not in RESOURCE_TYPES:
        RESOURCE_TYPES.append(_spec.resource_type)

TYPES_BY_FAMILY: dict[str, set[str]] = {}
for _spec in BYPASS_SPECS:
    TYPES_BY_FAMILY.setdefault(_spec.family, set()).add(_spec.resource_type)

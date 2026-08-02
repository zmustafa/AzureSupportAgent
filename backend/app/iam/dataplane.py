"""The data-plane access catalogue: what holds data, which roles reach it, and which doors are
not role assignments at all.

Azure RBAC is only one of the authorization systems in a tenant, and for most services it is not
the one that decides who can read the data. This module encodes three separate facts per service:

1. **Which built-in roles reach the data, and how badly.** Not by name-matching. A role's risk
   comes from its ``dataActions``, and the tier that matters most is not "write" — it is
   ``TIER_CREDENTIAL``: roles whose data IS a credential. Reading a Key Vault secret does not
   read data, it makes you the identity that secret belongs to, and every downstream permission
   comes with it. That is a different class of finding from reading a blob.

2. **Which doors bypass RBAC entirely.** Account keys, SAS, connection strings, admin users,
   local SQL logins, device symmetric keys. A tenant can have a spotless role assignment list
   and a shared account key in a wiki.

3. **Which authorization models this product cannot read at all** — SQL GRANTs, Kubernetes
   RBAC objects, Cosmos native role assignments, Databricks/Unity Catalog, Fabric, Azure DevOps,
   PostgreSQL/MySQL roles, Managed HSM local RBAC, Kusto database principals. These need a
   data-plane credential an ARM/Graph connection does not have. They are recorded here so the
   product can NAME the blind spot. A service whose authorization we cannot enumerate must never
   contribute to a clean verdict, and the only thing worse than not checking is not saying so.

Why a catalogue and not pure derivation: derivation from ``dataActions`` handles custom roles and
roles Microsoft adds tomorrow, so it is the primary mechanism. The catalogue exists for the facts
derivation cannot know — which service a resource type belongs to, which doors exist beside RBAC,
and which authorization systems are invisible from here.

Every role name below was taken from a live tenant's role catalogue (981 definitions, 324 with
dataActions), not from documentation. Several roles that are commonly assumed to be data-plane
roles are deliberately ABSENT because they are not: ``AcrPull``/``AcrPush``/``AcrDelete`` are
control-plane actions, ``Storage Blob Delegator`` grants a control-plane key-generation action,
and Cosmos DB / Redis / Data Explorer / Managed HSM do not publish ARM data roles at all — their
grants live in service-native systems listed under :data:`UNREADABLE`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- risk tiers
#: The data this role reaches IS a credential. Reading it is an identity takeover, not a read:
#: whoever holds the secret becomes the principal it authenticates, with everything that
#: principal can reach. Ranked above write deliberately.
TIER_CREDENTIAL = "credential"
#: Can modify or destroy the data.
TIER_WRITE = "write"
#: Can read the data.
TIER_READ = "read"
#: Reaches configuration or metadata, not the data itself. `Key Vault Reader` lives here: it
#: lists secret NAMES and properties but cannot read a secret VALUE, and conflating the two
#: produces a critical finding for a role that cannot open anything.
TIER_META = "meta"
#: Not a data-plane role.
TIER_NONE = "none"

TIER_RANK = {TIER_CREDENTIAL: 0, TIER_WRITE: 1, TIER_READ: 2, TIER_META: 3, TIER_NONE: 4}
#: Tiers that justify a finding when held broadly. `meta` does not.
TIER_SENSITIVE = (TIER_CREDENTIAL, TIER_WRITE, TIER_READ)


def tier_rank(tier: str) -> int:
    return TIER_RANK.get(tier, TIER_RANK[TIER_NONE])


def at_least(tier: str, floor: str) -> bool:
    """True when ``tier`` is at least as severe as ``floor``."""
    return tier_rank(tier) <= tier_rank(floor)


# --------------------------------------------------------------------------- credential stores
#: Providers whose data-plane content is credentials. Any grant here beyond metadata-read is
#: TIER_CREDENTIAL regardless of the verb, because the verb is not what makes it dangerous.
CREDENTIAL_NAMESPACES = frozenset({
    "microsoft.keyvault",
    "microsoft.managedhsm",
})

#: Verb segments that mutate. Matched as a whole PATH SEGMENT, never as a substring: the first
#: version tested `"/delete" in action`, which classified
#: `Microsoft.DigitalTwins/jobs/deletions/read` as a write because "deletions" contains "delete",
#: and `"/manage" in action` matched every single
#: `Microsoft.ContainerService/managedClusters/...` action — turning `Azure Kubernetes Service
#: RBAC Reader` into a write-tier role.
_WRITE_VERBS = frozenset({"write", "delete"})

#: Operations expressed as `.../<operation>/action` that MUTATE. Azure models plenty of read
#: operations as `/action` too (`receive`, `query`, `filter`), so the verb `action` alone says
#: nothing — the operation name is what carries the meaning.
_MUTATING_OPERATIONS = frozenset({
    "send", "process", "add", "create", "update", "delete", "purge", "clear",
    "manage", "setacl", "modifypermissions", "takeownership", "restore", "import",
    "runasowner", "runassuperuser", "write", "deletemessage", "publish",
})


@dataclass(frozen=True)
class ServiceSpec:
    """One service that holds data, and everything that decides who can reach it."""

    key: str
    label: str
    #: Rough order of "if exactly one of these is wrong, which hurts most".
    priority: int
    #: Lowercased ARM resource types this service owns.
    resource_types: tuple[str, ...] = ()
    #: Lowercased ARM provider namespaces its dataActions live under.
    namespaces: tuple[str, ...] = ()
    #: Access paths that are NOT role assignments. Named so a report can list them even when
    #: the product cannot yet test each one.
    doors: tuple[str, ...] = ()
    #: True when Azure RBAC role assignments are a COMPLETE picture of data access. False when a
    #: service-native authorization system decides, and an ARM/Graph connection cannot read it.
    rbac_is_complete: bool = True
    #: Why the picture is incomplete. Required whenever ``rbac_is_complete`` is False — a blind
    #: spot without a stated reason is indistinguishable from a pass.
    blind_reason: str = ""
    #: Role-name overrides, lowercased, for roles whose tier derivation would be wrong or whose
    #: definition carries no dataActions at all.
    role_tiers: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "priority": self.priority,
            "resource_types": list(self.resource_types),
            "doors": list(self.doors),
            "rbac_is_complete": self.rbac_is_complete,
            "blind_reason": self.blind_reason,
        }


SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        key="blob",
        label="Blob Storage / ADLS Gen2",
        priority=1,
        resource_types=("microsoft.storage/storageaccounts",),
        namespaces=("microsoft.storage",),
        doors=(
            "Account keys (full control, no role assignment, no per-user attribution)",
            "Service SAS, account SAS and user-delegation SAS",
            "Anonymous container/blob public access",
            "Shared-key authorization when allowSharedKeyAccess is not disabled",
            "ADLS Gen2 POSIX ACLs at filesystem, directory and file level",
        ),
        rbac_is_complete=False,
        blind_reason=(
            "ADLS Gen2 POSIX ACLs and issued SAS tokens are not visible from the control plane. "
            "A recursive rwx ACL for a large group grants file access that no role assignment "
            "records, and an issued SAS cannot be enumerated at all — only revoked by rotating "
            "the key or the stored access policy it was signed with."
        ),
    ),
    ServiceSpec(
        key="keyvault",
        label="Key Vault",
        priority=2,
        resource_types=("microsoft.keyvault/vaults",),
        namespaces=("microsoft.keyvault",),
        doors=(
            "Legacy access policies on vaults with enableRbacAuthorization=false",
            "Public network access and private-endpoint configuration",
        ),
        # Access policies ARE collected (SURFACE_KEY_VAULT), so RBAC plus policies is complete.
        rbac_is_complete=True,
        role_tiers={
            # Reads names and properties, never a secret VALUE. Must not be a credential finding.
            "key vault reader": TIER_META,
        },
    ),
    ServiceSpec(
        key="managedhsm",
        label="Managed HSM",
        priority=3,
        resource_types=("microsoft.keyvault/managedhsms",),
        namespaces=("microsoft.managedhsm",),
        doors=("Local HSM security-domain holders", "Backup/restore roles"),
        rbac_is_complete=False,
        blind_reason=(
            "Managed HSM authorization is LOCAL RBAC held inside the HSM, not ARM role "
            "assignments. Managed HSM Administrator, Crypto User and Crypto Officer are granted "
            "through the HSM's own data plane and do not appear in any role-assignment listing."
        ),
    ),
    ServiceSpec(
        key="files",
        label="Azure Files",
        priority=4,
        resource_types=("microsoft.storage/storageaccounts",),
        namespaces=("microsoft.storage",),
        doors=("NTFS/SMB share and file ACLs", "Storage account keys", "SAS"),
        rbac_is_complete=False,
        blind_reason=(
            "SMB share contents are governed by NTFS ACLs evaluated by the file system, not by "
            "Azure RBAC. The share-level roles are only the outer gate."
        ),
    ),
    ServiceSpec(
        key="acr",
        label="Container Registry",
        priority=5,
        resource_types=("microsoft.containerregistry/registries",),
        namespaces=("microsoft.containerregistry",),
        doors=(
            "Admin account (adminUserEnabled)",
            "Scoped tokens and scope maps",
            "Connected registries",
            "Anonymous pull",
        ),
        role_tiers={
            # Control-plane actions, not dataActions — but they are how images move, so they are
            # tiered here rather than being invisible.
            "acrpull": TIER_READ,
            "acrpush": TIER_WRITE,
            "acrdelete": TIER_WRITE,
        },
    ),
    ServiceSpec(
        key="servicebus",
        label="Service Bus",
        priority=6,
        resource_types=("microsoft.servicebus/namespaces",),
        namespaces=("microsoft.servicebus",),
        doors=("Namespace/queue/topic shared access authorization rules (SAS)", "Connection strings"),
    ),
    ServiceSpec(
        key="eventhub",
        label="Event Hubs",
        priority=7,
        resource_types=("microsoft.eventhub/namespaces",),
        namespaces=("microsoft.eventhub",),
        doors=("Namespace/entity shared access policies", "Connection strings", "Kafka credentials"),
    ),
    ServiceSpec(
        key="cosmos",
        label="Cosmos DB for NoSQL",
        priority=8,
        resource_types=("microsoft.documentdb/databaseaccounts",),
        namespaces=("microsoft.documentdb",),
        doors=("Account keys", "Resource tokens", "Connection strings"),
        rbac_is_complete=False,
        blind_reason=(
            "Cosmos DB data access is granted by NATIVE role definitions and assignments "
            "(sqlRoleDefinitions / sqlRoleAssignments) held on the account, not by ARM role "
            "assignments. Cosmos DB Built-in Data Reader and Data Contributor never appear in a "
            "role-assignment listing, so an empty result here says nothing about who can read "
            "the documents."
        ),
    ),
    ServiceSpec(
        key="sql",
        label="Azure SQL Database / SQL Managed Instance",
        priority=9,
        resource_types=("microsoft.sql/servers", "microsoft.sql/managedinstances"),
        doors=("SQL logins and contained database users", "Entra admin", "Linked servers"),
        rbac_is_complete=False,
        blind_reason=(
            "SQL data access is decided by database-native authorization — db_owner, "
            "db_datareader, db_datawriter, contained users and object-level GRANT/DENY. Reading "
            "it requires a connection to each database with permission to query the catalogue "
            "views; an ARM connection cannot see any of it."
        ),
    ),
    ServiceSpec(
        key="aks",
        label="AKS / Kubernetes",
        priority=10,
        resource_types=("microsoft.containerservice/managedclusters",),
        namespaces=("microsoft.containerservice",),
        doors=("Local cluster-admin kubeconfig", "Kubernetes service accounts", "Workload identity federation"),
        rbac_is_complete=False,
        blind_reason=(
            "Authorization inside the cluster is Kubernetes RBAC — Role, ClusterRole, RoleBinding "
            "and ClusterRoleBinding objects, plus service accounts — which lives in the cluster's "
            "API server, not in Azure. A cluster with Azure RBAC disabled grants everything "
            "through bindings this product cannot read, including who can read Secrets."
        ),
    ),
    ServiceSpec(
        key="appconfig",
        label="App Configuration",
        priority=11,
        resource_types=("microsoft.appconfiguration/configurationstores",),
        namespaces=("microsoft.appconfiguration",),
        doors=("Access keys", "Key Vault references"),
    ),
    ServiceSpec(
        key="search",
        label="Azure AI Search",
        priority=12,
        resource_types=("microsoft.search/searchservices",),
        namespaces=("microsoft.search",),
        doors=("Admin keys", "Query keys", "Data-source credentials"),
    ),
    ServiceSpec(
        key="openai",
        label="Azure OpenAI / AI Services",
        priority=13,
        resource_types=("microsoft.cognitiveservices/accounts",),
        namespaces=("microsoft.cognitiveservices",),
        doors=("API keys when local auth is enabled", "Connected data sources", "RAG identities"),
    ),
    ServiceSpec(
        key="queue",
        label="Queue Storage",
        priority=14,
        resource_types=("microsoft.storage/storageaccounts",),
        doors=("SAS", "Account keys"),
    ),
    ServiceSpec(
        key="table",
        label="Table Storage",
        priority=15,
        resource_types=("microsoft.storage/storageaccounts",),
        doors=("SAS", "Account keys"),
    ),
    ServiceSpec(
        key="redis",
        label="Azure Cache for Redis / Managed Redis",
        priority=16,
        resource_types=("microsoft.cache/redis", "microsoft.cache/redisenterprise"),
        doors=("Redis access keys", "Redis ACL users and access policies", "Connection strings"),
        rbac_is_complete=False,
        blind_reason=(
            "Redis command and key access is governed by Redis access policies and ACL users on "
            "the cache itself. There are no ARM data-plane roles for Redis, so role assignments "
            "say nothing about who can read cached session or application data."
        ),
    ),
    ServiceSpec(
        key="kusto",
        label="Azure Data Explorer",
        priority=17,
        resource_types=("microsoft.kusto/clusters",),
        doors=("Database principal assignments", "Ingestion identities"),
        rbac_is_complete=False,
        blind_reason=(
            "Data Explorer authorization is held as cluster/database PRINCIPAL assignments "
            "(admins, users, viewers, ingestors) managed through Kusto control commands, not ARM "
            "role assignments. AllDatabasesAdmin and Database Viewer are invisible from here."
        ),
    ),
    ServiceSpec(
        key="databricks",
        label="Azure Databricks / Unity Catalog",
        priority=18,
        resource_types=("microsoft.databricks/workspaces",),
        doors=("Personal access tokens", "Service principals", "Secret scopes", "Cluster policies"),
        rbac_is_complete=False,
        blind_reason=(
            "Databricks authorization is workspace and Unity Catalog native — account/workspace "
            "admins, metastore admins, and catalog/schema/table grants such as SELECT, MODIFY and "
            "OWN. It is reachable only through the Databricks APIs with a workspace credential."
        ),
    ),
    ServiceSpec(
        key="fabric",
        label="Microsoft Fabric / Power BI",
        priority=19,
        resource_types=(),
        doors=("Share links", "App audiences", "Tenant settings", "Service-principal access"),
        rbac_is_complete=False,
        blind_reason=(
            "Fabric and Power BI use their own workspace and item permissions (Admin, Member, "
            "Contributor, Viewer, semantic-model Build). They are not Azure resources and hold no "
            "ARM role assignments."
        ),
    ),
    ServiceSpec(
        key="synapse",
        label="Azure Synapse Analytics",
        priority=20,
        resource_types=("microsoft.synapse/workspaces",),
        doors=("Linked-service credentials", "Workspace managed identity", "ADLS ACLs", "SQL logins"),
        rbac_is_complete=False,
        blind_reason=(
            "Synapse workspace RBAC is a separate system from ARM role assignments, and neither "
            "describes the SQL permissions or the ADLS Gen2 ACLs that actually gate the data."
        ),
    ),
    ServiceSpec(
        key="postgres",
        label="Azure Database for PostgreSQL",
        priority=21,
        resource_types=("microsoft.dbforpostgresql/flexibleservers", "microsoft.dbforpostgresql/servers"),
        doors=("Local PostgreSQL users and passwords", "Row-level security policies"),
        rbac_is_complete=False,
        blind_reason=(
            "PostgreSQL access is native roles and GRANTs on databases, schemas, tables and "
            "functions, including pg_read_all_data. Azure RBAC does not show table access."
        ),
    ),
    ServiceSpec(
        key="mysql",
        label="Azure Database for MySQL",
        priority=22,
        resource_types=("microsoft.dbformysql/flexibleservers", "microsoft.dbformysql/servers"),
        doors=("Local MySQL users and passwords", "Definer privileges on stored procedures"),
        rbac_is_complete=False,
        blind_reason=(
            "MySQL access is native users and grants, including global privileges and "
            "GRANT OPTION. None of it appears in Azure role assignments."
        ),
    ),
    ServiceSpec(
        key="iothub",
        label="IoT Hub",
        priority=23,
        resource_types=("microsoft.devices/iothubs",),
        namespaces=("microsoft.devices",),
        doors=("Shared access policies", "Device symmetric keys", "X.509 certificates", "DPS enrolments"),
        rbac_is_complete=False,
        blind_reason=(
            "Device credentials — symmetric keys, X.509 certificates and DPS enrolment groups — "
            "authenticate to the hub without any role assignment."
        ),
    ),
    ServiceSpec(
        key="eventgrid",
        label="Event Grid",
        priority=24,
        resource_types=("microsoft.eventgrid/topics", "microsoft.eventgrid/domains", "microsoft.eventgrid/namespaces"),
        namespaces=("microsoft.eventgrid",),
        doors=("Topic access keys", "Webhook destination credentials"),
    ),
    ServiceSpec(
        key="signalr",
        label="Azure SignalR Service",
        priority=25,
        resource_types=("microsoft.signalrservice/signalr",),
        namespaces=("microsoft.signalrservice",),
        doors=("Access keys", "Connection strings"),
    ),
    ServiceSpec(
        key="webpubsub",
        label="Azure Web PubSub",
        priority=26,
        resource_types=("microsoft.signalrservice/webpubsub",),
        doors=("Access keys", "Connection strings", "Generated client tokens"),
    ),
    ServiceSpec(
        key="digitaltwins",
        label="Azure Digital Twins",
        priority=27,
        resource_types=("microsoft.digitaltwins/digitaltwinsinstances",),
        namespaces=("microsoft.digitaltwins",),
        doors=("Event-route identities",),
    ),
    ServiceSpec(
        key="maps",
        label="Azure Maps",
        priority=28,
        resource_types=("microsoft.maps/accounts",),
        namespaces=("microsoft.maps",),
        doors=("Shared keys", "SAS-style tokens"),
    ),
    ServiceSpec(
        key="health",
        label="Azure Health Data Services",
        priority=29,
        resource_types=("microsoft.healthcareapis/services", "microsoft.healthcareapis/workspaces"),
        namespaces=("microsoft.healthcareapis",),
        doors=("SMART on FHIR / OAuth application permissions",),
    ),
    ServiceSpec(
        key="ledger",
        label="Azure Confidential Ledger",
        priority=30,
        resource_types=("microsoft.confidentialledger/ledgers",),
        namespaces=("microsoft.confidentialledger",),
        doors=("Ledger-specific certificates and identities",),
    ),
    ServiceSpec(
        key="batch",
        label="Azure Batch",
        priority=31,
        resource_types=("microsoft.batch/batchaccounts",),
        namespaces=("microsoft.batch",),
        doors=("Batch account keys", "Pool node credentials", "Auto-user elevation"),
    ),
    ServiceSpec(
        key="purview",
        label="Microsoft Purview",
        priority=32,
        resource_types=("microsoft.purview/accounts",),
        doors=("Collection administrators", "Data curators", "Data source administrators"),
        rbac_is_complete=False,
        blind_reason=(
            "Purview collection and governance roles are assigned inside the Purview account's "
            "own permission model and are reachable only through the Purview APIs."
        ),
    ),
    ServiceSpec(
        key="apim",
        label="API Management",
        priority=33,
        resource_types=("microsoft.apimanagement/service",),
        doors=("Subscription keys", "OAuth/JWT gateway policy", "Backend credentials in named values"),
        rbac_is_complete=False,
        blind_reason=(
            "API consumer access is granted by APIM products and subscription keys, and enforced "
            "by gateway policy. None of it is an Azure role assignment."
        ),
    ),
    ServiceSpec(
        key="devops",
        label="Azure DevOps",
        priority=34,
        resource_types=(),
        doors=("PATs", "SSH keys", "Service connections", "Variable groups", "Secure files"),
        rbac_is_complete=False,
        blind_reason=(
            "Azure DevOps uses its own ACLs for repositories, pipelines, environments and feeds. "
            "A service connection can hold subscription Owner while no Azure role assignment "
            "names the person who can run the pipeline that uses it."
        ),
    ),
    ServiceSpec(
        key="netapp",
        label="Azure NetApp Files / Managed Lustre / HPC Cache",
        priority=35,
        resource_types=("microsoft.netapp/netappaccounts",),
        doors=("NFS export policies and client IP rules", "Active Directory permissions", "Filesystem ACLs"),
        rbac_is_complete=False,
        blind_reason=(
            "NFS and SMB filesystem access is decided by export policies and directory ACLs. "
            "There are no Azure data-plane roles for these volumes."
        ),
    ),
    ServiceSpec(
        key="appservice",
        label="App Service / Functions / Container Apps",
        priority=36,
        resource_types=(
            "microsoft.web/sites",
            "microsoft.app/containerapps",
        ),
        doors=("Function host and master keys", "Deployment credentials", "Connection strings", "Anonymous endpoints"),
        rbac_is_complete=False,
        blind_reason=(
            "Application endpoints are authorized by the application — app roles, OAuth scopes, "
            "Easy Auth configuration or nothing at all — and by function keys that are not role "
            "assignments."
        ),
    ),
)

SERVICE_BY_KEY: dict[str, ServiceSpec] = {s.key: s for s in SERVICES}

#: Services whose data-plane authorization an ARM/Graph connection cannot enumerate. These are
#: the honest blind spots: their presence in an estate must be REPORTED, never treated as clean.
UNREADABLE: tuple[ServiceSpec, ...] = tuple(s for s in SERVICES if not s.rbac_is_complete)

#: resource type (lowercased) -> the services that own it. Storage accounts host four data
#: services, so this is one-to-many.
_TYPE_INDEX: dict[str, list[ServiceSpec]] = {}
for _s in SERVICES:
    for _t in _s.resource_types:
        _TYPE_INDEX.setdefault(_t.lower(), []).append(_s)

#: Explicit role-name overrides across every service, lowercased.
_ROLE_TIERS: dict[str, str] = {}
for _s in SERVICES:
    for _name, _tier in _s.role_tiers.items():
        _ROLE_TIERS[_name.lower()] = _tier

#: Which service a role belongs to, by role name, for the roles we override.
_ROLE_SERVICE: dict[str, str] = {}
for _s in SERVICES:
    for _name in _s.role_tiers:
        _ROLE_SERVICE[_name.lower()] = _s.key


def services_for_type(resource_type: str) -> list[ServiceSpec]:
    return list(_TYPE_INDEX.get((resource_type or "").strip().lower(), []))


def _namespace_of(action: str) -> str:
    return (action or "").strip().lower().split("/", 1)[0]


def _is_metadata_only(action: str) -> bool:
    """True when the pattern can only list or describe, never open.

    `Microsoft.KeyVault/vaults/*/read` reads secret NAMES and properties; reading a secret VALUE
    is `.../secrets/getSecret/action`. Treating the two alike turns Key Vault Reader — a role
    that cannot open anything — into a critical credential finding, and a report that cries wolf
    on the harmless role gets switched off before it reports the dangerous one."""
    return _segments(action)[-1:] == ["read"]


def _segments(action: str) -> list[str]:
    return [s for s in (action or "").strip().lower().split("/") if s]


def _tier_of_action(action: str) -> str:
    """Tier for a single dataAction pattern."""
    a = (action or "").strip().lower()
    if not a:
        return TIER_NONE
    segs = _segments(a)
    ns = _namespace_of(a)

    if ns in CREDENTIAL_NAMESPACES or ns == "*":
        # Anything beyond a pure listing in a credential store is an identity takeover.
        return TIER_META if _is_metadata_only(a) else TIER_CREDENTIAL

    verb = segs[-1] if segs else ""
    if a == "*" or verb == "*":
        # A wildcard tail covers write and delete whatever else it covers.
        return TIER_WRITE
    if verb in _WRITE_VERBS:
        return TIER_WRITE
    if verb == "action":
        operation = segs[-2] if len(segs) >= 2 else ""
        return TIER_WRITE if operation in _MUTATING_OPERATIONS else TIER_READ
    if verb == "read":
        return TIER_READ
    return TIER_READ


def derive_tier(data_actions: tuple[str, ...] | list[str] | None) -> str:
    """Risk tier from a role's dataActions alone — the worst tier any single pattern reaches.

    Derivation rather than a name list, so a custom role called "Team Helper" that carries
    `Microsoft.KeyVault/vaults/*` is classified on what it can do, not on what it is called."""
    best = TIER_NONE
    for raw in (data_actions or []):
        tier = _tier_of_action(str(raw))
        if tier_rank(tier) < tier_rank(best):
            best = tier
    return best


def role_tier(role_name: str, data_actions: tuple[str, ...] | list[str] | None = None) -> str:
    """The tier for a named role, catalogue override first, then derivation.

    The override exists for two cases derivation cannot get right: roles whose wildcard reads
    more dangerous than it is (`Key Vault Reader`), and roles that carry no dataActions at all
    yet are how data moves (`AcrPull`, `AcrPush`, `AcrDelete` are control-plane actions)."""
    name = (role_name or "").strip().lower()
    if name in _ROLE_TIERS:
        return _ROLE_TIERS[name]
    return derive_tier(data_actions)


def service_for_role(role_name: str, data_actions: tuple[str, ...] | list[str] | None = None) -> str:
    """Best-effort service key for a role, from its dataActions namespace or the override map."""
    name = (role_name or "").strip().lower()
    if name in _ROLE_SERVICE:
        return _ROLE_SERVICE[name]
    for a in (data_actions or []):
        ns = _namespace_of(str(a))
        for s in SERVICES:
            if ns in s.namespaces:
                return s.key
    return ""


def is_privileged_data_role(role_name: str, data_actions: tuple[str, ...] | list[str] | None = None) -> bool:
    """Whether a data-plane role deserves the `roleIsPrivileged` flag.

    Replaces a substring test for "owner"/"contributor" in the role NAME, which was wrong in both
    directions on a real catalogue: it missed `Key Vault Administrator`, `Key Vault Secrets
    Officer`, `Azure Kubernetes Service RBAC Cluster Admin` and `Storage File Data SMB Admin`,
    while flagging `Avere Contributor` and `AgFood Platform Sensor Partner Contributor` purely
    because the word appeared in their names.

    Read-tier data access is NOT privileged here. It is sensitive, and `dp` signals report it —
    but folding every reader into the privileged count makes the number that drives the PIM and
    standing-privilege screens useless."""
    return at_least(role_tier(role_name, data_actions), TIER_WRITE)


def public_catalogue() -> list[dict[str, Any]]:
    """The catalogue as data, for the API and the coverage screen."""
    return [s.public() for s in sorted(SERVICES, key=lambda s: s.priority)]

"""Tenant-wide access collection via Azure Resource Graph.

``orchestrator.refresh_all`` issues one ARM call per scope per collector. On a 26-subscription
tenant that is 26 serial round trips for role assignments, 26 more for role definitions and 26
more for deny assignments. ARG's ``authorizationresources`` table returns all three tenant-wide
in one paged query, so this module collapses 78 calls into 3.

**What ARG cannot see**, and therefore what stays on ARM (see :mod:`app.iam.orchestrator`):

* management-group-scoped and tenant-root assignments — ``authorizationresources`` is indexed
  per subscription, and a grant made at an MG appears only as an inherited copy under each child
  subscription, which is exactly the duplication the MG-first ARM walk exists to fix;
* PIM eligibility, activation schedules and policies;
* classic administrators.

Key Vault access policies ARE reachable here (they are plain resource properties), so they are
included — that removes another per-subscription fan-out.

**Fail-closed.** Every function returns a ``CollectorStatus`` whose status reflects what actually
happened. ``run_kql_collect`` returning ``ok=False`` becomes ``Failed``/``Throttled`` with zero
rows, never a silent empty success — an empty result that looks like a clean one is how a
throttled scan gets persisted as "this tenant has no privileged access".
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.iam import schema
from app.iam.collectors import _KV_PRIVILEGED_PERMS, CollectorStatus, _merge_permissions, _role_def_guid

log = logging.getLogger("app.iam.arg")

# ARG caps a page at 1000 rows regardless of what the query asks for, and each page costs one
# unit of the 15-queries-per-5s-per-principal quota. ``run_kql_collect`` paces and retries.
_PAGE = 1000

# A tenant-wide assignment sweep is the largest query this product makes. 200k rows is far above
# any real tenant (the ARM limit is 4000 per subscription) while still bounding a runaway.
MAX_ASSIGNMENT_ROWS = 200_000
MAX_ROLE_DEF_ROWS = 50_000

# ``resourcechanges`` retains roughly 14 days. Past that, "no changes" is indistinguishable from
# "the change aged out of the window", so a delta refresh must decline and fall back to a full one.
CHANGE_RETENTION_DAYS = 13

# `| order by <col> asc` is MANDATORY: $skipToken paging is only deterministic over an ordered
# result set. Without it pages overlap and drop rows non-reproducibly.
_ASSIGNMENTS_KQL = """
authorizationresources
| where type =~ 'microsoft.authorization/roleassignments'
| extend p = properties
| project id, subscriptionId,
          scope            = tostring(p.scope),
          principalId      = tostring(p.principalId),
          principalType    = tostring(p.principalType),
          roleDefinitionId = tostring(p.roleDefinitionId),
          createdOn        = tostring(p.createdOn),
          updatedOn        = tostring(p.updatedOn),
          condition        = tostring(p.condition),
          conditionVersion = tostring(p.conditionVersion)
| order by id asc
"""

_ROLE_DEFS_KQL = """
authorizationresources
| where type =~ 'microsoft.authorization/roledefinitions'
| extend p = properties
| project id, subscriptionId,
          roleName    = tostring(p.roleName),
          roleType    = tostring(p.type),
          permissions = p.permissions
| order by id asc
"""

# Azure Lighthouse. A registration ASSIGNMENT binds a scope to a registration DEFINITION, and
# the definition is where the managing tenant and the delegated authorizations actually live —
# so both are needed and joined on the definition id. Neither appears in the portal's Access
# control (IAM) blade, which is exactly why an operator can be unaware of them.
_LIGHTHOUSE_ASSIGNMENT_KQL = """
resources
| where type =~ 'microsoft.managedservices/registrationassignments'
| extend p = properties
| project id, subscriptionId, resourceGroup,
          registrationDefinitionId = tostring(p.registrationDefinitionId),
          provisioningState        = tostring(p.provisioningState)
| order by id asc
"""

_LIGHTHOUSE_DEFINITION_KQL = """
resources
| where type =~ 'microsoft.managedservices/registrationdefinitions'
| extend p = properties
| project id, subscriptionId,
          managedByTenantId   = tostring(p.managedByTenantId),
          managedByTenantName = tostring(p.managedByTenantName),
          definitionName      = tostring(p.registrationDefinitionName),
          authorizations      = p.authorizations
| order by id asc
"""

_DENY_KQL = """
authorizationresources
| where type =~ 'microsoft.authorization/denyassignments'
| extend p = properties
| project id, subscriptionId,
          denyName        = tostring(p.denyAssignmentName),
          description     = tostring(p.description),
          scope           = tostring(p.scope),
          principals      = p.principals,
          excludePrincipals = p.excludePrincipals,
          permissions     = p.permissions,
          isSystemProtected = tostring(p.isSystemProtected),
          doNotApplyToChildScopes = tostring(p.doNotApplyToChildScopes)
| order by id asc
"""

# Key Vault access policies live on the vault resource. `enableRbacAuthorization` vaults IGNORE
# their access-policy list entirely, so including them would report access that does not exist.
_KEYVAULT_KQL = """
resources
| where type =~ 'microsoft.keyvault/vaults'
| extend p = properties
| project id, name, subscriptionId, resourceGroup, tenantIdProp = tostring(p.tenantId),
          enableRbacAuthorization = tostring(p.enableRbacAuthorization),
          accessPolicies = p.accessPolicies
| order by id asc
"""


def _status_for_kql(result: Any) -> str:
    """Map a failed ``KqlResult`` onto a collector status.

    Throttling is distinguished from a hard failure because the two mean different things to the
    reader: a throttled scope should be retried, an unauthorized one needs a permission."""
    err = (getattr(result, "error", "") or "").lower()
    if any(t in err for t in ("429", "throttl", "toomanyrequests", "rate limit")):
        return schema.STATUS_THROTTLED
    if any(t in err for t in ("forbidden", "authorizationfailed", "401", "403", "does not have authorization")):
        return schema.STATUS_UNAUTHORIZED
    return schema.STATUS_FAILED


async def _collect(kql: str, connection: dict[str, Any] | None, *, max_rows: int) -> Any:
    from app.exec.command_runner import run_kql_collect

    return await run_kql_collect(kql, connection, max_rows=max_rows, page_size=_PAGE)


# --------------------------------------------------------------------------- role definitions
async def collect_role_definitions_arg(
    connection: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], CollectorStatus]:
    """Every role definition visible in the tenant, keyed by GUID.

    One query replaces the per-scope ``collect_role_definitions`` fan-out. Built-in definitions
    are returned once per subscription that can see them, so the map is keyed on the GUID and
    later copies simply overwrite identical earlier ones."""
    st = CollectorStatus("ArgRoleDefinitions")
    started = time.monotonic()
    res = await _collect(_ROLE_DEFS_KQL, connection, max_rows=MAX_ROLE_DEF_ROWS)
    st.duration_seconds = time.monotonic() - started
    if not res.ok:
        st.status = _status_for_kql(res)
        st.message = (res.error or "")[:300]
        return {}, st

    index: dict[str, dict[str, Any]] = {}
    for rd in res.rows:
        rid = str(rd.get("id", ""))
        name = str(rd.get("roleName", ""))
        perms = rd.get("permissions") or [{}]
        actions, not_actions, data_actions, not_data_actions = _merge_permissions(perms)
        data_flag = bool(data_actions or not_data_actions)
        guid = _role_def_guid(rid)
        if not guid:
            continue
        index[guid] = {
            "roleName": name,
            "roleDefinitionId": rid,
            "roleCategory": schema.role_category(data_flag),
            "roleIsPrivileged": schema.role_is_privileged(
                name, has_data_actions=data_flag, data_actions=data_actions
            ),
            "roleHasDataActions": data_flag,
            "roleType": str(rd.get("roleType", "")),
            "description": "",
            "actions": actions,
            "notActions": not_actions,
            "dataActions": data_actions,
            "notDataActions": not_data_actions,
            "assignableScopes": [],
        }
    if not res.complete:
        st.status = schema.STATUS_PARTIAL
        st.message = f"Capped at {MAX_ROLE_DEF_ROWS} role definitions."
    st.rows_added = len(index)
    return index, st


# --------------------------------------------------------------------------- role assignments
async def collect_assignments_arg(
    connection: dict[str, Any] | None,
    *,
    tenant_id: str,
    role_defs: dict[str, dict[str, Any]],
    subscription_names: dict[str, str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], CollectorStatus]:
    """All subscription-and-below role assignments, bucketed by subscription scope.

    Returns ``{"/subscriptions/<id>": [row, ...]}`` so the caller can write the existing
    per-scope cache slices unchanged — the storage model, the freshness table and every
    downstream consumer keep working.

    Rows land in the bucket of the subscription that *returned* them, which for an
    MG-inherited grant is every child subscription. That is the same shape the ARM collector
    produces, so ``compose.dedupe_assignments`` resolves it identically."""
    st = CollectorStatus("ArgRoleAssignments")
    names = subscription_names or {}
    started = time.monotonic()
    res = await _collect(_ASSIGNMENTS_KQL, connection, max_rows=MAX_ASSIGNMENT_ROWS)
    st.duration_seconds = time.monotonic() - started
    if not res.ok:
        st.status = _status_for_kql(res)
        st.message = (res.error or "")[:300]
        return {}, st

    buckets: dict[str, list[dict[str, Any]]] = {}
    for ra in res.rows:
        sub_id = str(ra.get("subscriptionId", "")).strip()
        if not sub_id:
            # No subscription means an MG- or tenant-root-scoped row. ARG surfaces these
            # inconsistently across tenants, and the ARM management-group walk collects them
            # authoritatively, so dropping them here avoids a half-populated MG scope that
            # would look collected but be incomplete.
            continue
        scope = str(ra.get("scope", "")) or f"/subscriptions/{sub_id}"
        rdef = role_defs.get(_role_def_guid(str(ra.get("roleDefinitionId", "")))) or {}
        parts = schema.parse_scope(scope)
        principal_id = str(ra.get("principalId", ""))
        ptype = str(ra.get("principalType", ""))
        sub_scope = f"/subscriptions/{sub_id}"
        buckets.setdefault(sub_scope, []).append(
            schema.make_row(
                surface=schema.SURFACE_AZURE_RBAC,
                accessModel=(
                    schema.ACCESS_DATA_PLANE if rdef.get("roleHasDataActions") else schema.ACCESS_CONTROL_PLANE
                ),
                collector=st.collector,
                assignmentState=schema.STATE_ACTIVE,
                assignmentType="RoleAssignment",
                accessPath=schema.PATH_DIRECT,
                principalId=principal_id,
                principalType=ptype,
                roleName=rdef.get("roleName", _role_def_guid(str(ra.get("roleDefinitionId", "")))),
                roleDefinitionId=str(ra.get("roleDefinitionId", "")),
                roleCategory=rdef.get("roleCategory", "ControlPlane"),
                roleIsPrivileged=bool(rdef.get("roleIsPrivileged")),
                roleHasDataActions=bool(rdef.get("roleHasDataActions")),
                scope=scope,
                scopeType=parts.get("scopeType", ""),
                scopeDisplayName=scope,
                tenantId=tenant_id,
                managementGroupId=parts.get("managementGroupId", ""),
                subscriptionId=parts.get("subscriptionId", sub_id),
                subscriptionName=names.get(sub_id, sub_id),
                resourceGroup=parts.get("resourceGroup", ""),
                resourceType=parts.get("resourceType", ""),
                resourceName=parts.get("resourceName", ""),
                assignmentId=str(ra.get("id", "")),
                assignmentCreatedOn=str(ra.get("createdOn", "")),
                assignmentUpdatedOn=str(ra.get("updatedOn", "")),
                condition=str(ra.get("condition", "") or ""),
                conditionVersion=str(ra.get("conditionVersion", "") or ""),
                isInherited=parts.get("scopeType") != schema.SCOPE_SUBSCRIPTION
                and bool(parts.get("subscriptionId")),
                sourceApi="ARG authorizationresources",
                collectionStatus=schema.STATUS_SUCCEEDED,
                effectivePrincipalId=principal_id,
                effectivePrincipalType=ptype,
            )
        )
    if not res.complete:
        st.status = schema.STATUS_PARTIAL
        st.message = f"Capped at {MAX_ASSIGNMENT_ROWS} assignments."
    st.rows_added = sum(len(v) for v in buckets.values())
    return buckets, st


# --------------------------------------------------------------------------- deny assignments
async def collect_deny_assignments_arg(
    connection: dict[str, Any] | None,
    *,
    tenant_id: str,
    subscription_names: dict[str, str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], CollectorStatus]:
    """Deny assignments tenant-wide, bucketed by subscription.

    A deny row is emitted with ``effect="Deny"`` and ``roleIsPrivileged=False``: a deny grants
    nothing, and counting one as privileged access would report the control as the risk."""
    st = CollectorStatus("ArgDenyAssignments")
    names = subscription_names or {}
    started = time.monotonic()
    res = await _collect(_DENY_KQL, connection, max_rows=MAX_ROLE_DEF_ROWS)
    st.duration_seconds = time.monotonic() - started
    if not res.ok:
        st.status = _status_for_kql(res)
        st.message = (res.error or "")[:300]
        return {}, st

    buckets: dict[str, list[dict[str, Any]]] = {}
    for da in res.rows:
        sub_id = str(da.get("subscriptionId", "")).strip()
        if not sub_id:
            continue
        scope = str(da.get("scope", "")) or f"/subscriptions/{sub_id}"
        parts = schema.parse_scope(scope)
        principals = da.get("principals") or []
        if isinstance(principals, dict):
            principals = [principals]
        excluded = da.get("excludePrincipals") or []
        if isinstance(excluded, dict):
            excluded = [excluded]
        # Same wording as the ARM collector, deliberately: these rows are deduped against ARM's
        # and two spellings of the same deny would survive as two rows and two findings.
        excl_note = f" (excludes {len(excluded)})" if excluded else ""
        name = str(da.get("denyName", "")) or "Deny assignment"
        sub_scope = f"/subscriptions/{sub_id}"
        for pr in principals:
            if not isinstance(pr, dict):
                continue
            pid = str(pr.get("id", ""))
            ptype = str(pr.get("type", "") or "")
            buckets.setdefault(sub_scope, []).append(
                schema.make_row(
                    surface=schema.SURFACE_DENY,
                    accessModel=schema.ACCESS_DENY,
                    collector=st.collector,
                    assignmentState=schema.STATE_ACTIVE,
                    assignmentType="DenyAssignment",
                    accessPath=schema.PATH_DIRECT,
                    effect=schema.EFFECT_DENY,
                    principalId=pid,
                    principalType=ptype,
                    # A SystemDefined principal is the all-principals wildcard; a bare GUID here
                    # reads as an unresolved user and badly understates the blast radius.
                    principalDisplayName="All principals" if ptype.lower() == "systemdefined" else "",
                    roleName=f"{name}{excl_note}",
                    roleCategory="ControlPlane",
                    # A deny is not a grant. Flagging it privileged would inflate the privileged
                    # KPI with rows that REMOVE access.
                    roleIsPrivileged=False,
                    scope=scope,
                    scopeType=parts.get("scopeType", ""),
                    scopeDisplayName=scope,
                    tenantId=tenant_id,
                    managementGroupId=parts.get("managementGroupId", ""),
                    subscriptionId=parts.get("subscriptionId", sub_id),
                    subscriptionName=names.get(sub_id, sub_id),
                    resourceGroup=parts.get("resourceGroup", ""),
                    resourceType=parts.get("resourceType", ""),
                    resourceName=parts.get("resourceName", ""),
                    assignmentId=str(da.get("id", "")),
                    # Resource Graph projects this as the string "true"/"false"; make_row coerces
                    # it to a real bool for the effective-permission engine.
                    doNotApplyToChildScopes=str(da.get("doNotApplyToChildScopes", "")).lower() == "true",
                    isInherited=str(da.get("doNotApplyToChildScopes", "")).lower() != "true"
                    and scope != sub_scope,
                    sourceApi="ARG authorizationresources",
                    collectionStatus=schema.STATUS_SUCCEEDED,
                    effectivePrincipalId=pid,
                    effectivePrincipalType=ptype,
                )
            )
    st.rows_added = sum(len(v) for v in buckets.values())
    return buckets, st


# --------------------------------------------------------------------------- lighthouse
async def collect_lighthouse_arg(
    connection: dict[str, Any] | None,
    *,
    tenant_id: str,
    subscription_names: dict[str, str] | None = None,
    role_defs: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], CollectorStatus]:
    """Azure Lighthouse delegations — another tenant's principals holding roles in this one.

    These grants are **invisible in the portal's Access control (IAM) blade**, so a tenant can
    carry a managing partner's standing Owner access with nobody in the tenant able to see it
    from the obvious place. That is the whole reason this surface exists.

    Two queries, joined on the definition id: the ASSIGNMENT says which scope is delegated, the
    DEFINITION says to whom and with what roles. Emitting the assignment alone would produce a
    delegation with no counterparty — a row saying access exists without saying whose.

    A failure here is reported as a collector status, never as zero delegations: a tenant with
    delegations it does not know about is the exact case where a confident "none found" does the
    most damage."""
    st = CollectorStatus("AzureLighthouseDelegations")
    names = subscription_names or {}
    defs_index = role_defs or {}
    started = time.monotonic()

    assignments = await _collect(_LIGHTHOUSE_ASSIGNMENT_KQL, connection, max_rows=MAX_ROLE_DEF_ROWS)
    if not assignments.ok:
        st.duration_seconds = time.monotonic() - started
        st.status = _status_for_kql(assignments)
        st.message = (assignments.error or "")[:300]
        return {}, st

    definitions = await _collect(_LIGHTHOUSE_DEFINITION_KQL, connection, max_rows=MAX_ROLE_DEF_ROWS)
    st.duration_seconds = time.monotonic() - started
    if not definitions.ok:
        # The assignments read fine but the definitions did not. Reporting the assignments alone
        # would name a delegated scope with no managing tenant and no roles, which is worse than
        # saying the surface could not be read.
        st.status = _status_for_kql(definitions)
        st.message = (definitions.error or "")[:300]
        return {}, st

    by_def: dict[str, dict[str, Any]] = {
        str(d.get("id", "")).lower(): d for d in definitions.rows if d.get("id")
    }

    buckets: dict[str, list[dict[str, Any]]] = {}
    for ra in assignments.rows:
        sub_id = str(ra.get("subscriptionId", "")).strip()
        definition = by_def.get(str(ra.get("registrationDefinitionId", "")).lower()) or {}
        managing_tenant = str(definition.get("managedByTenantId", "")) or "unknown"
        managing_name = str(definition.get("managedByTenantName", "")) or managing_tenant
        auths = definition.get("authorizations") or []
        if isinstance(auths, dict):
            auths = [auths]
        # A delegation whose scope is a resource group narrows to it; otherwise the whole
        # subscription is delegated.
        scope = str(ra.get("id", "")).split("/providers/Microsoft.ManagedServices/")[0] \
            or f"/subscriptions/{sub_id}"
        parts = schema.parse_scope(scope)

        for auth in auths:
            if not isinstance(auth, dict):
                continue
            rdef_id = str(auth.get("roleDefinitionId", ""))
            rdef = defs_index.get(_role_def_guid(rdef_id)) or {}
            role_name = str(rdef.get("roleName", "")) or rdef_id.rsplit("/", 1)[-1]
            # An UNRESOLVED delegated role counts as privileged. There is no "unknown" state on
            # this flag, and the two available errors are not symmetric: calling a delegated
            # Reader privileged costs a second look, while calling a delegated Owner ordinary
            # hides a foreign tenant's full control over the subscription. Delegations are
            # high-trust by construction, so the conservative reading is the correct default.
            known_role = bool(rdef)
            privileged = (
                bool(rdef.get("isPrivileged"))
                or role_name.strip().lower() in ("owner", "user access administrator", "contributor")
                if known_role
                else True
            )
            buckets.setdefault(scope, []).append(
                schema.make_row(
                    surface=schema.SURFACE_LIGHTHOUSE,
                    accessModel=schema.ACCESS_LIGHTHOUSE,
                    collector=st.collector,
                    assignmentState=schema.STATE_ACTIVE,
                    assignmentType="LighthouseDelegation",
                    accessPath=schema.PATH_DIRECT,
                    effect=schema.EFFECT_ALLOW,
                    principalId=str(auth.get("principalId", "")),
                    principalType="ServicePrincipal",
                    principalDisplayName=str(auth.get("principalIdDisplayName", "")),
                    roleName=role_name,
                    roleDefinitionId=rdef_id,
                    roleCategory="ControlPlane",
                    roleIsPrivileged=privileged,
                    scope=scope,
                    scopeType=parts.get("scopeType", ""),
                    scopeDisplayName=scope,
                    tenantId=tenant_id,
                    # The principal lives in the MANAGING tenant's directory, not this one — so
                    # every directory lookup for it will fail here. Recording which tenant owns
                    # it is what stops the orphan detector calling a partner's identity deleted.
                    managingTenantId=managing_tenant,
                    managingTenantName=managing_name,
                    principalExists=schema.EXISTS_UNKNOWN,
                    managementGroupId=parts.get("managementGroupId", ""),
                    subscriptionId=parts.get("subscriptionId", sub_id),
                    subscriptionName=names.get(sub_id, sub_id),
                    resourceGroup=parts.get("resourceGroup", ""),
                    assignmentId=str(ra.get("id", "")),
                    sourceApi="ARG managedservices",
                    collectionStatus=schema.STATUS_SUCCEEDED,
                    effectivePrincipalId=str(auth.get("principalId", "")),
                    effectivePrincipalName=str(auth.get("principalIdDisplayName", "")),
                    effectivePrincipalType="ServicePrincipal",
                )
            )
    st.rows_added = sum(len(v) for v in buckets.values())
    return buckets, st


# --------------------------------------------------------------------------- key vault policies
async def collect_keyvault_policies_arg(
    connection: dict[str, Any] | None,
    *,
    tenant_id: str,
    subscription_names: dict[str, str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], CollectorStatus]:
    """Legacy Key Vault access policies for every vault in the tenant, bucketed by subscription.

    Vaults are ordinary resources, so one ARG query replaces the per-subscription vault listing.
    Mirrors :func:`app.iam.collectors.collect_keyvault_policies` row-for-row, including the rule
    that matters most: **``enableRbacAuthorization`` vaults are skipped**, because those vaults
    ignore their access-policy list entirely and their real grants are ordinary role assignments
    already collected. Emitting both would double-count every Key Vault grant."""
    st = CollectorStatus("ArgKeyVaultAccessPolicies")
    names = subscription_names or {}
    started = time.monotonic()
    res = await _collect(_KEYVAULT_KQL, connection, max_rows=MAX_ROLE_DEF_ROWS)
    st.duration_seconds = time.monotonic() - started
    if not res.ok:
        st.status = _status_for_kql(res)
        st.message = (res.error or "")[:300]
        return {}, st

    buckets: dict[str, list[dict[str, Any]]] = {}
    for vault in res.rows:
        # ARG renders booleans as JSON true/false, but the tostring() projection makes them
        # "true"/"false"; accept either so a schema change on Azure's side cannot silently
        # start including RBAC vaults.
        rbac_flag = vault.get("enableRbacAuthorization")
        if str(rbac_flag).strip().lower() == "true":
            continue
        sub_id = str(vault.get("subscriptionId", "")).strip()
        if not sub_id:
            continue
        vault_id = str(vault.get("id", ""))
        vault_name = str(vault.get("name", "")) or vault_id.rstrip("/").split("/")[-1]
        parts = schema.parse_scope(vault_id)
        policies = vault.get("accessPolicies") or []
        if isinstance(policies, dict):
            policies = [policies]
        sub_scope = f"/subscriptions/{sub_id}"
        for policy in policies:
            if not isinstance(policy, dict):
                continue
            perms = policy.get("permissions") or {}
            if not isinstance(perms, dict):
                continue
            families: list[str] = []
            privileged = False
            for family in ("keys", "secrets", "certificates", "storage"):
                granted = [str(p).lower() for p in (perms.get(family) or [])]
                if not granted:
                    continue
                families.append(f"{family}({','.join(granted)})")
                if any(p in _KV_PRIVILEGED_PERMS for p in granted):
                    privileged = True
            if not families:
                continue  # an empty policy grants nothing
            object_id = str(policy.get("objectId", ""))
            buckets.setdefault(sub_scope, []).append(
                schema.make_row(
                    surface=schema.SURFACE_KEY_VAULT,
                    accessModel=schema.ACCESS_KV_POLICY,
                    collector=st.collector,
                    assignmentState=schema.STATE_ACTIVE,
                    assignmentType="AccessPolicy",
                    accessPath=schema.PATH_DIRECT,
                    principalId=object_id,
                    roleName=f"Access Policy: {' '.join(families)}",
                    roleCategory="DataPlane",
                    roleIsPrivileged=privileged,
                    roleHasDataActions=True,
                    scope=vault_id,
                    scopeType=parts.get("scopeType", schema.SCOPE_RESOURCE),
                    scopeDisplayName=vault_name,
                    tenantId=tenant_id,
                    subscriptionId=parts.get("subscriptionId", sub_id),
                    subscriptionName=names.get(sub_id, sub_id),
                    resourceGroup=parts.get("resourceGroup", ""),
                    resourceType="Microsoft.KeyVault/vaults",
                    resourceName=vault_name,
                    assignmentId=f"{vault_id}/accessPolicies/{object_id}",
                    sourceApi="ARG keyVault accessPolicies",
                    collectionStatus=schema.STATUS_SUCCEEDED,
                    effectivePrincipalId=object_id,
                )
            )
    st.rows_added = sum(len(v) for v in buckets.values())
    return buckets, st


# --------------------------------------------------------------------------- managed identities
# Every resource that carries an identity block, plus user-assigned identities as resources in
# their own right. The join key is the identity's principalId, which is exactly what the access
# rows already carry — so "which resource IS this mystery service principal?" becomes answerable,
# and that question is the single most common complaint about any RBAC report.
_IDENTITY_KQL = """
resources
| where isnotnull(identity) and isnotempty(tostring(identity.principalId))
| project id, name, type, subscriptionId, resourceGroup,
          identityType = tostring(identity.type),
          principalId  = tostring(identity.principalId),
          userAssigned = identity.userAssignedIdentities
| order by id asc
"""

_UAMI_KQL = """
resources
| where type =~ 'microsoft.managedidentity/userassignedidentities'
| project id, name, subscriptionId, resourceGroup,
          principalId = tostring(properties.principalId),
          clientId    = tostring(properties.clientId)
| order by id asc
"""


async def collect_managed_identities(
    connection: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], CollectorStatus]:
    """principalId (lower-cased) -> identity facts, tenant-wide.

    Two queries: resources carrying a system-assigned identity, and user-assigned identities as
    resources. A user-assigned identity attached to several resources gets them all — a shared
    identity is precisely the interesting case, because a compromise in dev then reaches prod."""
    st = CollectorStatus("ArgManagedIdentities")
    started = time.monotonic()

    res = await _collect(_UAMI_KQL, connection, max_rows=MAX_ROLE_DEF_ROWS)
    if not res.ok:
        st.duration_seconds = time.monotonic() - started
        st.status = _status_for_kql(res)
        st.message = (res.error or "")[:300]
        return {}, st

    out: dict[str, dict[str, Any]] = {}
    for r in res.rows:
        pid = str(r.get("principalId", "")).strip().lower()
        if not pid:
            continue
        out[pid] = {
            "principalId": pid,
            "identityKind": "UserAssigned",
            "identityResourceId": str(r.get("id", "")),
            "identityName": str(r.get("name", "")),
            "clientId": str(r.get("clientId", "")),
            "subscriptionId": str(r.get("subscriptionId", "")),
            "resourceGroup": str(r.get("resourceGroup", "")),
            "attachedResourceIds": [],
        }

    res2 = await _collect(_IDENTITY_KQL, connection, max_rows=MAX_ASSIGNMENT_ROWS)
    st.duration_seconds = time.monotonic() - started
    if not res2.ok:
        # The user-assigned half succeeded; report Partial rather than discarding it, but do
        # NOT claim the inventory is complete.
        st.status = schema.STATUS_PARTIAL
        st.message = f"System-assigned identities unavailable: {(res2.error or '')[:200]}"
        st.rows_added = len(out)
        return out, st

    for r in res2.rows:
        rid = str(r.get("id", ""))
        pid = str(r.get("principalId", "")).strip().lower()
        itype = str(r.get("identityType", ""))
        if pid:
            entry = out.setdefault(
                pid,
                {
                    "principalId": pid,
                    "identityKind": "SystemAssigned",
                    "identityResourceId": rid,
                    "identityName": str(r.get("name", "")),
                    "clientId": "",
                    "subscriptionId": str(r.get("subscriptionId", "")),
                    "resourceGroup": str(r.get("resourceGroup", "")),
                    "attachedResourceIds": [],
                },
            )
            # A system-assigned identity IS its resource.
            if entry["identityKind"] == "SystemAssigned":
                entry["attachedResourceIds"] = [rid]
                entry["attachedResourceType"] = str(r.get("type", ""))
        # The userAssignedIdentities map keys are the UAMI resource ids attached here.
        ua = r.get("userAssigned")
        if isinstance(ua, dict):
            for uami_rid, meta in ua.items():
                upid = ""
                if isinstance(meta, dict):
                    upid = str(meta.get("principalId", "")).strip().lower()
                if not upid:
                    # Match by resource id when the inline block omits the principal id.
                    upid = next(
                        (k for k, v in out.items()
                         if str(v.get("identityResourceId", "")).lower() == str(uami_rid).lower()),
                        "",
                    )
                if upid and upid in out:
                    out[upid]["attachedResourceIds"].append(rid)
        if "identityType" in r and itype and pid in out:
            out[pid].setdefault("identityTypeRaw", itype)

    for entry in out.values():
        entry["attachedResourceCount"] = len(entry.get("attachedResourceIds") or [])
    st.rows_added = len(out)
    return out, st


# --------------------------------------------------------------------------- delta detection
_CHANGES_KQL = """
resourcechanges
| extend ca = properties.changeAttributes
| extend changeTime = todatetime(ca.timestamp),
         targetType = tostring(properties.targetResourceType)
| where changeTime > datetime({since})
| where targetType has 'microsoft.authorization'
| project subscriptionId, changeTime
| order by subscriptionId asc
"""


async def subscriptions_changed_since(
    connection: dict[str, Any] | None, since_iso: str
) -> tuple[set[str] | None, str]:
    """Subscription ids with authorization activity since ``since_iso``.

    Returns ``(None, reason)`` when the answer is not knowable — an ARG failure, or a
    ``since`` older than the ``resourcechanges`` retention window. **``None`` must be treated as
    "refresh everything"**: an empty set and an unanswerable question are the same value in a
    naive implementation, and confusing them means a delta refresh that silently stops
    refreshing anything while still stamping every scope as fresh.
    """
    if not since_iso:
        return None, "no previous run to compare against"
    from datetime import datetime, timedelta, timezone

    try:
        since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    except ValueError:
        return None, f"unparseable last-run timestamp {since_iso!r}"
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - since_dt > timedelta(days=CHANGE_RETENTION_DAYS):
        return None, f"last run is older than the {CHANGE_RETENTION_DAYS}-day change-feed window"

    kql = _CHANGES_KQL.format(since=since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    res = await _collect(kql, connection, max_rows=MAX_ROLE_DEF_ROWS)
    if not res.ok:
        return None, (res.error or "change feed unavailable")[:200]
    if not res.complete:
        return None, "change feed result was capped, so the changed set is not trustworthy"
    return {str(r.get("subscriptionId", "")) for r in res.rows if r.get("subscriptionId")}, ""

"""Live access collectors — port of the scanner's per-surface collectors to async Python.

Each collector hits Azure (ARM REST for Azure RBAC, Microsoft Graph for the directory layer)
through the app's existing connection/token plumbing and returns normalized rows plus a
``CollectorStatus``. They are defensive in the scanner's spirit: a permission/throttle/parse
failure for one collector is recorded (Unauthorized / Throttled / Failed / PartiallyCollected)
and never sinks the others. When no usable token is available they report ``Skipped`` so the UI
can prompt for a connection rather than erroring.

The deterministic demo path (:mod:`app.iam.demo`) is what's exercised locally; these run only
against a real connection with broad reader + Graph permissions."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from app.iam import schema

log = logging.getLogger("app.iam.collectors")

_ARM = "https://management.azure.com"
_GRAPH = "https://graph.microsoft.com/v1.0"
_RA_API = "2022-04-01"  # Authorization roleAssignments / roleDefinitions

# The ONLY hosts this module may send a bearer token to. Every request below attaches the
# connection's ARM/Graph token, so a value that can move the HOST is a token-exfiltration
# primitive rather than merely a wrong URL.
_ALLOWED_HOSTS = frozenset({"management.azure.com", "graph.microsoft.com"})


def _host_error(url: str) -> str | None:
    """Return why ``url`` must not receive a bearer token, or None when it is safe.

    Compares the PARSED host, never a string prefix. Collectors build their URLs as
    ``f"{_ARM}{scope}/..."`` with a caller-supplied ``scope``, which hands the host to anyone
    who can begin that value with:

      * ``@`` -- ``https://management.azure.com@evil.com/...`` parses ``management.azure.com``
        as *userinfo*, so the request (and the token) goes to ``evil.com``;
      * ``.`` -- ``https://management.azure.com.evil.com/...`` is a registrable domain the
        attacker can own.

    A ``startswith("https://management.azure.com")`` test waves both through, which is why
    this parses instead. https is required because the request carries the token.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return "malformed url"
    if parsed.scheme != "https":
        return "url must use https; the request carries a bearer token"
    if (parsed.host or "").lower() not in _ALLOWED_HOSTS:
        return f"refusing to send a bearer token to host {parsed.host or '(none)'!r}"
    return None

# Bounded concurrency for per-principal Graph fan-out (group expansion, SP owners). A directory
# scan can touch hundreds of groups / service principals; issuing one Graph call per id strictly
# sequentially made a refresh take tens of seconds. We fan them out across a small worker pool —
# fast, while staying well under Graph throttling limits.
_GRAPH_FANOUT = 8


@dataclass
class CollectorStatus:
    collector: str
    status: str = schema.STATUS_SUCCEEDED
    rows_added: int = 0
    duration_seconds: float = 0.0
    message: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "collector": self.collector,
            "status": self.status,
            "rowsAdded": self.rows_added,
            "durationSeconds": round(self.duration_seconds, 2),
            "message": self.message,
        }


def _status_for_http(code: int) -> str:
    if code in (401, 403):
        return schema.STATUS_UNAUTHORIZED
    if code == 429:
        return schema.STATUS_THROTTLED
    return schema.STATUS_FAILED


async def _get_all(token: str, url: str, params: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], str | None, int]:
    """GET a paged ARM/Graph collection following nextLink. Returns (value, error, http_code)."""
    # Fail closed BEFORE the token is attached. Callers interpolate caller-supplied scopes and
    # resource ids directly after the host, so the target host is not trustworthy until parsed.
    if bad := _host_error(url):
        return [], f"request refused: {bad}", 0
    headers = {"Authorization": f"Bearer {token}"}
    out: list[dict[str, Any]] = []
    next_url: str | None = url
    next_params = dict(params or {})
    code = 200
    # nextLink is echoed from the response body, and the bearer token is re-sent on every
    # hop, so every hop is re-checked against the SAME allowlist rather than against the
    # first URL's host -- pinning to the origin alone would faithfully follow an attacker
    # who had already moved the host on the initial request.
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            while next_url:
                if bad := _host_error(next_url):
                    return out, f"refusing to follow nextLink: {bad}", code
                resp = await client.get(next_url, headers=headers, params=next_params or None)
                code = resp.status_code
                if code != 200:
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text)
                    except (ValueError, AttributeError):
                        detail = resp.text
                    return out, f"HTTP {code}: {str(detail)[:300]}", code
                body = resp.json()
                out.extend(body.get("value", []) or [])
                next_url = body.get("nextLink") or body.get("@odata.nextLink")
                next_params = {}  # nextLink already encodes paging
    except httpx.HTTPError as exc:
        return out, f"request error: {exc}", 0
    return out, None, code


def _role_def_guid(role_definition_id: str) -> str:
    return (role_definition_id or "").rstrip("/").split("/")[-1]


def _merge_permissions(perms: Any) -> tuple[list[str], list[str], list[str], list[str]]:
    """Union the four action lists across a role's permission blocks.

    ARM allows several blocks per definition and unions them when evaluating, so reading only
    ``permissions[0]`` silently drops grants on any multi-block role."""
    if isinstance(perms, dict):
        perms = [perms]
    actions: list[str] = []
    not_actions: list[str] = []
    data_actions: list[str] = []
    not_data_actions: list[str] = []
    for p in perms or []:
        if not isinstance(p, dict):
            continue
        actions.extend(str(a) for a in (p.get("actions") or []))
        not_actions.extend(str(a) for a in (p.get("notActions") or []))
        data_actions.extend(str(a) for a in (p.get("dataActions") or []))
        not_data_actions.extend(str(a) for a in (p.get("notDataActions") or []))
    return actions, not_actions, data_actions, not_data_actions


# --------------------------------------------------------------------------- Azure RBAC
async def collect_role_definitions(token: str, scope: str) -> tuple[dict[str, dict[str, Any]], CollectorStatus]:
    """Map roleDefinitionGuid -> {name, category, privileged, has_data_actions, action sets}.

    The four action lists are retained verbatim, not reduced to ``roleHasDataActions``. They are
    what :mod:`app.iam.effective` resolves "can P do A on R" against, and re-fetching every role
    definition to answer one question would make the engine unusable."""
    st = CollectorStatus("AzureRoleDefinitions")
    url = f"{_ARM}{scope}/providers/Microsoft.Authorization/roleDefinitions"
    value, err, code = await _get_all(token, url, {"api-version": _RA_API})
    index: dict[str, dict[str, Any]] = {}
    for rd in value:
        props = rd.get("properties", {}) or {}
        name = props.get("roleName", "")
        perms = props.get("permissions", []) or [{}]
        # A role definition may carry several permission blocks; ARM unions them.
        actions, not_actions, data_actions, not_data_actions = _merge_permissions(perms)
        data_flag = bool(data_actions or not_data_actions)
        guid = _role_def_guid(rd.get("id", "") or rd.get("name", ""))
        index[guid] = {
            "roleName": name,
            "roleDefinitionId": rd.get("id", ""),
            "roleCategory": schema.role_category(data_flag),
            "roleIsPrivileged": schema.role_is_privileged(
                name, has_data_actions=data_flag, data_actions=data_actions
            ),
            "roleHasDataActions": data_flag,
            "roleType": props.get("type", ""),
            "description": props.get("description", ""),
            "actions": actions,
            "notActions": not_actions,
            "dataActions": data_actions,
            "notDataActions": not_data_actions,
            "assignableScopes": [str(s) for s in (props.get("assignableScopes") or [])],
        }
    if err:
        st.status = schema.STATUS_PARTIAL if index else _status_for_http(code)
        st.message = err
    st.rows_added = len(index)
    return index, st


async def collect_azure_rbac(
    token: str,
    *,
    scope: str,
    subscription_id: str,
    subscription_name: str,
    tenant_id: str,
    role_defs: dict[str, dict[str, Any]],
    collector: str = "AzureSubscriptionRbac",
) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Role assignments at and below an ARM scope (subscription or management group)."""
    st = CollectorStatus(collector)
    url = f"{_ARM}{scope}/providers/Microsoft.Authorization/roleAssignments"
    value, err, code = await _get_all(token, url, {"api-version": _RA_API})
    rows: list[dict[str, Any]] = []
    for ra in value:
        props = ra.get("properties", {}) or {}
        scope = props.get("scope", f"/subscriptions/{subscription_id}")
        rdef = role_defs.get(_role_def_guid(props.get("roleDefinitionId", "")), {})
        parts = schema.parse_scope(scope)
        principal_id = props.get("principalId", "")
        ptype = props.get("principalType", "")
        rows.append(
            schema.make_row(
                surface=schema.SURFACE_AZURE_RBAC,
                accessModel=schema.ACCESS_DATA_PLANE if rdef.get("roleHasDataActions") else schema.ACCESS_CONTROL_PLANE,
                collector=st.collector,
                assignmentState=schema.STATE_ACTIVE,
                assignmentType="RoleAssignment",
                accessPath=schema.PATH_DIRECT,
                principalId=principal_id,
                principalType=ptype,
                roleName=rdef.get("roleName", _role_def_guid(props.get("roleDefinitionId", ""))),
                roleDefinitionId=props.get("roleDefinitionId", ""),
                roleCategory=rdef.get("roleCategory", "ControlPlane"),
                roleIsPrivileged=bool(rdef.get("roleIsPrivileged")),
                roleHasDataActions=bool(rdef.get("roleHasDataActions")),
                scope=scope,
                scopeType=parts.get("scopeType", ""),
                scopeDisplayName=scope,
                tenantId=tenant_id,
                managementGroupId=parts.get("managementGroupId", ""),
                subscriptionId=parts.get("subscriptionId", subscription_id),
                subscriptionName=subscription_name,
                resourceGroup=parts.get("resourceGroup", ""),
                resourceType=parts.get("resourceType", ""),
                resourceName=parts.get("resourceName", ""),
                assignmentId=ra.get("id", ""),
                assignmentCreatedOn=props.get("createdOn", ""),
                assignmentUpdatedOn=props.get("updatedOn", ""),
                condition=props.get("condition", "") or "",
                conditionVersion=props.get("conditionVersion", "") or "",
                isInherited=parts.get("scopeType") != schema.SCOPE_SUBSCRIPTION and bool(parts.get("subscriptionId")),
                sourceApi="ARM roleAssignments",
                collectionStatus=schema.STATUS_SUCCEEDED,
                effectivePrincipalId=principal_id,
                effectivePrincipalType=ptype,
            )
        )
    if err:
        st.status = schema.STATUS_PARTIAL if rows else _status_for_http(code)
        st.message = err
    st.rows_added = len(rows)
    return rows, st


# --------------------------------------------------------------------------- Directory (Graph)
async def collect_entra_roles(token: str, tenant_id: str) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Active Entra directory-role assignments (roleManagement/directory/roleAssignments)."""
    st = CollectorStatus("EntraRoleAssignments")
    url = f"{_GRAPH}/roleManagement/directory/roleAssignments"
    # Graph allows only ONE $expand per query ("Only one property can be expanded in a single
    # query"), so expand roleDefinition for the role name; the principal GUID is resolved to a
    # name by the shared principal-directory resolver (getByIds) during the directory refresh.
    value, err, code = await _get_all(token, url, {"$expand": "roleDefinition"})
    rows: list[dict[str, Any]] = []
    for ra in value:
        rdef = ra.get("roleDefinition", {}) or {}
        principal = ra.get("principal", {}) or {}
        name = rdef.get("displayName", "")
        rows.append(
            schema.make_row(
                surface=schema.SURFACE_ENTRA,
                accessModel=schema.ACCESS_ENTRA,
                collector=st.collector,
                assignmentState=schema.STATE_ACTIVE,
                assignmentType="DirectoryRoleAssignment",
                accessPath=schema.PATH_DIRECT,
                principalId=principal.get("id", ra.get("principalId", "")),
                principalType=(principal.get("@odata.type", "").split(".")[-1] or "").replace("user", "User").replace("group", "Group").replace("servicePrincipal", "ServicePrincipal"),
                principalDisplayName=principal.get("displayName", ""),
                principalUserPrincipalName=principal.get("userPrincipalName", ""),
                roleName=name,
                roleDefinitionId=rdef.get("id", ""),
                roleCategory="Directory",
                roleIsPrivileged=schema.role_is_privileged(name, surface=schema.SURFACE_ENTRA),
                scope=ra.get("directoryScopeId", "/") or "/",
                scopeType=schema.SCOPE_DIRECTORY,
                scopeDisplayName="Directory",
                tenantId=tenant_id,
                assignmentId=ra.get("id", ""),
                sourceApi="Graph roleManagement",
                collectionStatus=schema.STATUS_SUCCEEDED,
                effectivePrincipalId=principal.get("id", ra.get("principalId", "")),
                effectivePrincipalName=principal.get("displayName", ""),
                effectivePrincipalUserPrincipalName=principal.get("userPrincipalName", ""),
            )
        )
    if err:
        st.status = schema.STATUS_PARTIAL if rows else _status_for_http(code)
        st.message = err
    st.rows_added = len(rows)
    return rows, st


async def collect_group_expansion(token: str, group_ids: list[str]) -> tuple[dict[str, Any], CollectorStatus]:
    """Transitive membership for each group id -> {id: {name, members:[principal dict]}}.

    The per-group Graph calls run concurrently (bounded) since a tenant can have many groups."""
    st = CollectorStatus("GroupExpansion")
    graph: dict[str, Any] = {}
    errors = 0
    sem = asyncio.Semaphore(_GRAPH_FANOUT)

    async def _one(gid: str) -> tuple[str, list[dict[str, Any]] | None]:
        async with sem:
            members, err, _code = await _get_all(token, f"{_GRAPH}/groups/{gid}/transitiveMembers")
        return gid, (None if err else members)

    for gid, members in await asyncio.gather(*[_one(g) for g in group_ids]):
        if members is None:
            errors += 1
            continue
        graph[gid] = {
            "name": "",
            "members": [
                {
                    "principalId": m.get("id", ""),
                    "principalType": (m.get("@odata.type", "").split(".")[-1] or "User").replace("user", "User").replace("servicePrincipal", "ServicePrincipal"),
                    "principalDisplayName": m.get("displayName", ""),
                    "principalUserPrincipalName": m.get("userPrincipalName", ""),
                }
                for m in members
                if "group" not in (m.get("@odata.type", "").lower())
            ],
        }
    if errors:
        st.status = schema.STATUS_PARTIAL if graph else schema.STATUS_UNAUTHORIZED
        st.message = f"{errors} group(s) could not be expanded."
    st.rows_added = sum(len(g["members"]) for g in graph.values())
    return graph, st


async def collect_sp_owners(token: str, sp_ids: list[str], tenant_id: str) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Owner rows for the service principals seen in assignments (owners can control credentials).

    The per-SP Graph owner calls run concurrently (bounded); SP counts can be in the hundreds."""
    st = CollectorStatus("ServicePrincipalOwners")
    rows: list[dict[str, Any]] = []
    errors = 0
    sem = asyncio.Semaphore(_GRAPH_FANOUT)

    async def _one(spid: str) -> tuple[str, list[dict[str, Any]] | None]:
        async with sem:
            owners, err, _code = await _get_all(token, f"{_GRAPH}/servicePrincipals/{spid}/owners")
        return spid, (None if err else owners)

    for spid, owners in await asyncio.gather(*[_one(s) for s in sp_ids]):
        if owners is None:
            errors += 1
            continue
        for o in owners:
            rows.append(
                schema.make_row(
                    surface=schema.SURFACE_ENTRA,
                    accessModel=schema.ACCESS_ENTRA,
                    collector=st.collector,
                    assignmentState=schema.STATE_ACTIVE,
                    assignmentType="Owner",
                    accessPath=schema.PATH_OWNER,
                    roleName="Service Principal Owner",
                    roleCategory="Directory",
                    roleIsPrivileged=True,
                    scope="/",
                    scopeType=schema.SCOPE_DIRECTORY,
                    tenantId=tenant_id,
                    principalId=spid,
                    principalType="ServicePrincipal",
                    sourceApi="Graph servicePrincipals/owners",
                    collectionStatus=schema.STATUS_SUCCEEDED,
                    effectivePrincipalId=o.get("id", ""),
                    effectivePrincipalType=(o.get("@odata.type", "").split(".")[-1] or "User").replace("user", "User"),
                    effectivePrincipalName=o.get("displayName", ""),
                    effectivePrincipalUserPrincipalName=o.get("userPrincipalName", ""),
                )
            )
    if errors:
        st.status = schema.STATUS_PARTIAL if rows else schema.STATUS_UNAUTHORIZED
        st.message = f"{errors} service principal(s) had no readable owners."
    st.rows_added = len(rows)
    return rows, st


def _normalize_principal_type(odata_type: str) -> str:
    """Map a Graph ``@odata.type`` (e.g. ``#microsoft.graph.servicePrincipal``) to our
    canonical principal type label."""
    leaf = (odata_type or "").split(".")[-1].strip().lower()
    return {
        "user": "User",
        "group": "Group",
        "serviceprincipal": "ServicePrincipal",
        "device": "Device",
        "application": "Application",
    }.get(leaf, leaf[:1].upper() + leaf[1:] if leaf else "")


async def _graph_post(token: str, url: str, body: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None, int]:
    """POST a Graph collection request (e.g. directoryObjects/getByIds), following nextLink."""
    # Same fail-closed host check as _get_all: this attaches the same bearer token, and taking
    # a url parameter means a future caller could interpolate into it as the GETs already do.
    if bad := _host_error(url):
        return [], f"request refused: {bad}", 0
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    out: list[dict[str, Any]] = []
    code = 200
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=body)
            code = resp.status_code
            if code != 200:
                try:
                    detail = resp.json().get("error", {}).get("message", resp.text)
                except (ValueError, AttributeError):
                    detail = resp.text
                return out, f"HTTP {code}: {str(detail)[:300]}", code
            out.extend(resp.json().get("value", []) or [])
    except httpx.HTTPError as exc:
        return out, f"request error: {exc}", 0
    return out, None, code


async def collect_principal_directory(token: str, principal_ids: list[str]) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Resolve a set of principal object ids (GUIDs) to their directory display names via
    Microsoft Graph ``directoryObjects/getByIds`` (batched, ≤1000 ids per call). Returns a
    principal directory list ``[{principalId, principalType, displayName, userPrincipalName,
    appId, source}]`` used to overlay friendly names onto the GUID-only Azure-RBAC rows.

    Defensive: a permission/throttle/parse failure is recorded but never raises — any ids that
    couldn't be resolved simply stay as GUIDs in the grid."""
    st = CollectorStatus("PrincipalDirectory")
    ids = [i for i in dict.fromkeys(principal_ids) if i]  # de-dupe, preserve order
    out: list[dict[str, Any]] = []
    if not ids:
        return out, st
    errors = 0
    last_code = 200
    # RP5 — resolve the ≤1000-id chunks concurrently (bounded) instead of sequentially; these are
    # read-only Graph calls and the output is an id-keyed overlay, so ordering is irrelevant.
    chunks = [ids[start : start + 1000] for start in range(0, len(ids), 1000)]
    sem = asyncio.Semaphore(6)

    async def _resolve(chunk: list[str]) -> tuple[list[dict[str, Any]], str | None, int]:
        async with sem:
            return await _graph_post(
                token,
                f"{_GRAPH}/directoryObjects/getByIds",
                {"ids": chunk, "types": ["user", "group", "servicePrincipal"]},
            )

    for value, err, code in await asyncio.gather(*[_resolve(c) for c in chunks]):
        if err:
            errors += 1
            last_code = code
            continue
        for obj in value:
            oid = obj.get("id", "")
            if not oid:
                continue
            out.append(
                {
                    "principalId": oid,
                    "principalType": _normalize_principal_type(obj.get("@odata.type", "")),
                    "displayName": obj.get("displayName", ""),
                    "userPrincipalName": obj.get("userPrincipalName", "") or obj.get("mail", ""),
                    "appId": obj.get("appId", ""),
                    "source": "MicrosoftGraph",
                }
            )
    if errors:
        st.status = schema.STATUS_PARTIAL if out else _status_for_http(last_code)
        st.message = f"{errors} principal batch(es) could not be resolved."
    st.rows_added = len(out)
    return out, st


async def collect_management_groups(token: str) -> tuple[dict[str, str], CollectorStatus]:
    """Resolve management-group ids → display names via ARM ``getEntities`` so the scope tree and
    MG-scoped assignment rows can show a friendly name instead of the bare MG id/GUID.

    Returns ``({mg_id_lower: displayName}, status)``. Defensive: a failure is recorded and an
    empty map returned (the scope tree then falls back to the GUID)."""
    from app.azure.arm import list_all_management_groups

    st = CollectorStatus("ManagementGroups")
    mgs, err = await list_all_management_groups(token)
    name_map: dict[str, str] = {}
    for m in mgs:
        mid = str(m.get("id", "")).lower()
        if mid:
            name_map[mid] = m.get("name", "") or m.get("id", "")
    if err:
        st.status = schema.STATUS_PARTIAL if name_map else _status_for_http(0)
        st.message = err
    st.rows_added = len(name_map)
    return name_map, st


# --------------------------------------------------------------------------- deny assignments
async def collect_deny_assignments(
    token: str,
    *,
    scope: str,
    subscription_id: str,
    subscription_name: str,
    tenant_id: str,
) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Deny assignments at and below an ARM scope.

    Created by Azure Blueprints, Managed Applications and the deny-assignment preview. They are
    evaluated BEFORE role assignments and **cannot be overridden, not even by Owner** — so an
    access report that omits them can state the exact opposite of the truth.

    A deny can target ``AllPrincipals`` (``principalType == "SystemDefined"`` with the well-known
    all-principals id) with an ``excludePrincipals`` carve-out, so one API row can expand to
    several normalized rows."""
    st = CollectorStatus("AzureDenyAssignments")
    url = f"{_ARM}{scope}/providers/Microsoft.Authorization/denyAssignments"
    value, err, code = await _get_all(token, url, {"api-version": _RA_API})
    rows: list[dict[str, Any]] = []
    for da in value:
        props = da.get("properties", {}) or {}
        da_scope = props.get("scope") or scope
        parts = schema.parse_scope(da_scope)
        name = props.get("denyAssignmentName") or da.get("name", "") or "Deny assignment"
        # A deny with no principals grants nothing and denies nothing; skip rather than emit noise.
        principals = props.get("principals") or []
        excluded = props.get("excludePrincipals") or []
        excl_note = f" (excludes {len(excluded)})" if excluded else ""
        for p in principals:
            pid = p.get("id", "")
            ptype = p.get("type", "") or ""
            rows.append(
                schema.make_row(
                    surface=schema.SURFACE_DENY,
                    accessModel=schema.ACCESS_DENY,
                    collector=st.collector,
                    effect=schema.EFFECT_DENY,
                    assignmentState=schema.STATE_ACTIVE,
                    assignmentType="DenyAssignment",
                    accessPath=schema.PATH_DIRECT,
                    principalId=pid,
                    principalType=ptype,
                    # A SystemDefined principal is the "all principals" wildcard — name it, because
                    # a bare GUID here reads as an unresolved user and badly understates the blast radius.
                    principalDisplayName="All principals" if ptype.lower() == "systemdefined" else "",
                    roleName=f"{name}{excl_note}",
                    roleCategory="ControlPlane",
                    # A deny is not a *grant*; flagging it privileged would inflate the privileged
                    # KPI with rows that remove access. It gets its own surface + effect instead.
                    roleIsPrivileged=False,
                    scope=da_scope,
                    scopeType=parts.get("scopeType", ""),
                    scopeDisplayName=da_scope,
                    tenantId=tenant_id,
                    managementGroupId=parts.get("managementGroupId", ""),
                    subscriptionId=parts.get("subscriptionId", subscription_id),
                    subscriptionName=subscription_name,
                    resourceGroup=parts.get("resourceGroup", ""),
                    resourceType=parts.get("resourceType", ""),
                    resourceName=parts.get("resourceName", ""),
                    assignmentId=da.get("id", ""),
                    assignmentCreatedOn=props.get("createdOn", ""),
                    assignmentUpdatedOn=props.get("updatedOn", ""),
                    # Confines the deny to its own scope. The effective-permission engine reads
                    # this; without it every deny is assumed to cascade to child resources.
                    doNotApplyToChildScopes=bool(props.get("doNotApplyToChildScopes")),
                    isInherited=bool(props.get("doNotApplyToChildScopes")) is False and da_scope != scope,
                    sourceApi="ARM denyAssignments",
                    collectionStatus=schema.STATUS_SUCCEEDED,
                    effectivePrincipalId=pid,
                    effectivePrincipalType=ptype,
                )
            )
    if err:
        st.status = schema.STATUS_PARTIAL if rows else _status_for_http(code)
        st.message = err
    st.rows_added = len(rows)
    return rows, st


# --------------------------------------------------------------------------- Key Vault policies
# Permissions that make an access-policy holder privileged on the vault's data.
_KV_PRIVILEGED_PERMS = frozenset({
    "all", "purge", "delete", "set", "import", "create", "update", "recover", "restore",
    "setsas", "regeneratekey", "wrapkey", "unwrapkey", "decrypt", "sign",
})


async def collect_keyvault_policies(
    token: str,
    *,
    subscription_id: str,
    subscription_name: str,
    tenant_id: str,
) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Legacy Key Vault **access policies** — the pre-RBAC data-plane grant model.

    Vaults with ``enableRbacAuthorization: false`` grant data access through access policies that
    do not appear anywhere in ``roleAssignments``. A tenant with legacy vaults therefore reads as
    having no Key Vault data access at all, which is a false negative, not a gap.

    One row per (objectId, vault). ``roleName`` serialises the granted permission families so the
    grid is readable without a drawer."""
    st = CollectorStatus("KeyVaultAccessPolicies")
    url = f"{_ARM}/subscriptions/{subscription_id}/providers/Microsoft.KeyVault/vaults"
    value, err, code = await _get_all(token, url, {"api-version": "2023-07-01"})
    rows: list[dict[str, Any]] = []
    for vault in value:
        props = vault.get("properties", {}) or {}
        # RBAC-authorization vaults have no access policies; their grants are ordinary role
        # assignments already collected by collect_azure_rbac.
        if props.get("enableRbacAuthorization"):
            continue
        vault_id = vault.get("id", "")
        vault_name = vault.get("name", "") or vault_id.rstrip("/").split("/")[-1]
        parts = schema.parse_scope(vault_id)
        for policy in props.get("accessPolicies", []) or []:
            perms = policy.get("permissions", {}) or {}
            families = []
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
            rows.append(
                schema.make_row(
                    surface=schema.SURFACE_KEY_VAULT,
                    accessModel=schema.ACCESS_KV_POLICY,
                    collector=st.collector,
                    assignmentState=schema.STATE_ACTIVE,
                    assignmentType="AccessPolicy",
                    accessPath=schema.PATH_DIRECT,
                    principalId=policy.get("objectId", ""),
                    roleName=f"Access Policy: {' '.join(families)}",
                    roleCategory="DataPlane",
                    roleIsPrivileged=privileged,
                    # Access policies are a data-plane grant model by definition.
                    roleHasDataActions=True,
                    scope=vault_id,
                    scopeType=parts.get("scopeType", schema.SCOPE_RESOURCE),
                    scopeDisplayName=vault_name,
                    tenantId=tenant_id,
                    subscriptionId=parts.get("subscriptionId", subscription_id),
                    subscriptionName=subscription_name,
                    resourceGroup=parts.get("resourceGroup", ""),
                    resourceType="Microsoft.KeyVault/vaults",
                    resourceName=vault_name,
                    assignmentId=f"{vault_id}/accessPolicies/{policy.get('objectId', '')}",
                    sourceApi="ARM keyVault accessPolicies",
                    collectionStatus=schema.STATUS_SUCCEEDED,
                    effectivePrincipalId=policy.get("objectId", ""),
                )
            )
    if err:
        st.status = schema.STATUS_PARTIAL if rows else _status_for_http(code)
        st.message = err
    st.rows_added = len(rows)
    return rows, st


# --------------------------------------------------------------------------- classic admins
async def collect_classic_admins(
    token: str,
    *,
    subscription_id: str,
    subscription_name: str,
    tenant_id: str,
) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Classic subscription administrators (Account / Service / Co-Administrator).

    Co-Administrator is effectively Owner and **does not appear in the portal's Access control
    (IAM) blade by default**, which is exactly why these survive for years. Subscriptions created
    after the classic model was retired return nothing — that is ``Skipped``, not a failure."""
    st = CollectorStatus("ClassicAdministrators")
    url = f"{_ARM}/subscriptions/{subscription_id}/providers/Microsoft.Authorization/classicAdministrators"
    value, err, code = await _get_all(token, url, {"api-version": "2015-07-01"})
    scope = f"/subscriptions/{subscription_id}"
    rows: list[dict[str, Any]] = []
    for admin in value:
        props = admin.get("properties", {}) or {}
        email = props.get("emailAddress", "") or ""
        # `role` is a comma-separated list, e.g. "ServiceAdministrator;AccountAdministrator".
        raw_roles = str(props.get("role", "") or "").replace(";", ",")
        for role in [r.strip() for r in raw_roles.split(",") if r.strip()]:
            rows.append(
                schema.make_row(
                    surface=schema.SURFACE_CLASSIC,
                    accessModel=schema.ACCESS_CLASSIC,
                    collector=st.collector,
                    assignmentState=schema.STATE_ACTIVE,
                    assignmentType="ClassicAdministrator",
                    accessPath=schema.PATH_DIRECT,
                    # Classic admins are identified by e-mail, not object id — there is no GUID to
                    # resolve, so carry the address in both the id and the name rather than
                    # leaving a blank principal that reads as an orphan.
                    principalId=email,
                    principalType="User",
                    principalDisplayName=email,
                    principalUserPrincipalName=email,
                    roleName=role,
                    roleCategory="ControlPlane",
                    roleIsPrivileged=True,
                    scope=scope,
                    scopeType=schema.SCOPE_SUBSCRIPTION,
                    scopeDisplayName=subscription_name or subscription_id,
                    tenantId=tenant_id,
                    subscriptionId=subscription_id,
                    subscriptionName=subscription_name,
                    assignmentId=admin.get("id", ""),
                    sourceApi="ARM classicAdministrators",
                    collectionStatus=schema.STATUS_SUCCEEDED,
                    effectivePrincipalId=email,
                    effectivePrincipalType="User",
                    effectivePrincipalName=email,
                    effectivePrincipalUserPrincipalName=email,
                )
            )
    if err:
        # 404/400 here means the classic model is not available on this subscription, which is a
        # legitimate "nothing to collect", not a failure the operator should chase.
        if code in (400, 404):
            st.status = schema.STATUS_SKIPPED
            st.message = "Classic administrators are not available on this subscription."
        else:
            st.status = schema.STATUS_PARTIAL if rows else _status_for_http(code)
            st.message = err
    st.rows_added = len(rows)
    return rows, st


# --------------------------------------------------------------------------- PIM / JIT
_PIM_API = "2020-10-01"  # Microsoft.Authorization PIM schedule + policy APIs

# --------------------------------------------------------------------------- federated creds
_FIC_API = "2023-01-31"

# Issuers we recognise. Anything else is not necessarily malicious, but it IS unreviewed, and an
# unreviewed OIDC issuer can mint tokens for an Azure identity with no secret and no expiry.
KNOWN_FIC_ISSUERS = (
    "https://token.actions.githubusercontent.com",
    "https://vstoken.dev.azure.com",
    "https://gitlab.com",
    "https://app.terraform.io",
    "https://oidc.prod-aks.azure.com",
    "https://kubernetes.default.svc",
    "https://accounts.google.com",
    "https://login.microsoftonline.com",
    "https://sts.windows.net",
)


async def collect_federated_credentials(
    token: str, identity_resource_ids: list[str]
) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Federated identity credentials on user-assigned managed identities.

    A federated credential turns an external OIDC identity into an Azure principal with **no
    secret, no expiry, and no unusual sign-in log entry**. A loose ``subject`` on the GitHub
    issuer — ``repo:org/*:*``, or a ``pull_request`` subject — means any fork, or any contributor
    who can open a pull request, can assume that identity. Detection is a string comparison; the
    impact is total. That asymmetry is why this collector exists."""
    st = CollectorStatus("FederatedIdentityCredentials")
    rows: list[dict[str, Any]] = []
    if not identity_resource_ids:
        st.status = schema.STATUS_SKIPPED
        st.message = "No user-assigned managed identities to inspect."
        return rows, st

    started = time.monotonic()
    errors: list[str] = []
    codes: list[int] = []

    async def _one(rid: str) -> None:
        url = f"{_ARM}{rid}/federatedIdentityCredentials"
        value, err, code = await _get_all(token, url, {"api-version": _FIC_API})
        if err:
            errors.append(err)
            codes.append(code)
            return
        for fic in value:
            props = fic.get("properties", {}) or {}
            rows.append(
                {
                    "identityResourceId": rid,
                    "identityName": rid.rstrip("/").split("/")[-1],
                    "credentialId": str(fic.get("id", "")),
                    "name": str(fic.get("name", "")),
                    "issuer": str(props.get("issuer", "")),
                    "subject": str(props.get("subject", "")),
                    "audiences": [str(a) for a in (props.get("audiences") or [])],
                }
            )

    sem = asyncio.Semaphore(_GRAPH_FANOUT)

    async def _guarded(rid: str) -> None:
        async with sem:
            await _one(rid)

    await asyncio.gather(*(_guarded(r) for r in identity_resource_ids))

    st.duration_seconds = time.monotonic() - started
    if errors:
        # Partial when some identities answered: a federated credential we could not read is not
        # the same as one that does not exist, and the escalation map must be able to say so.
        st.status = schema.STATUS_PARTIAL if rows else _status_for_http(codes[0] if codes else 0)
        st.message = f"{len(errors)} identity/identities could not be read: {errors[0][:160]}"
    st.rows_added = len(rows)
    return rows, st


# A schedule instance whose status is not one of these granted nothing. Counting a Denied or
# Failed request as access accuses someone of holding privilege they were refused.
_PIM_GRANTED = frozenset({"provisioned", "granted", "accepted", "succeeded", "active", ""})


def _parse_time(value: str):
    """ARM emits up to 7 fractional-second digits, which ``datetime.fromisoformat`` rejects.

    Reuses the Entra collector's parser rather than re-deriving it — that one already handles
    every ISO-8601 spelling Graph and ARM mix inside a single payload."""
    from app.entra.collectors.activations import parse_time

    return parse_time(value)


def _duration_hours(value: str) -> float | None:
    from app.entra.collectors.pim import parse_duration_hours

    return parse_duration_hours(value)


def _derive_end(props: dict[str, Any]) -> str:
    """End of a PIM window.

    **Azure PIM states a DURATION, not an end**: ``expiration: {type: "AfterDuration",
    duration: "PT8H"}`` with ``endDateTime`` absent. Reading only ``endDateTime`` leaves every
    Azure window blank, which reads as "never expires" — the opposite of the truth."""
    end = str(props.get("endDateTime") or "")
    if end:
        return end
    expiration = props.get("expiration") or {}
    if str(expiration.get("type", "")).lower() != "afterduration":
        return ""
    hours = _duration_hours(str(expiration.get("duration") or ""))
    start = _parse_time(str(props.get("startDateTime") or ""))
    if hours is None or start is None:
        return ""
    return (start + timedelta(hours=hours)).isoformat()


def _is_permanent(props: dict[str, Any], derived_end: str) -> bool:
    expiration = props.get("expiration") or {}
    if str(expiration.get("type", "")).lower() == "noexpiration":
        return True
    return not derived_end


def _pim_is_licence_error(message: str) -> bool:
    """PIM reports a missing Entra ID P2 / Governance licence as a **400 with a message**, not a
    403. Classifying it as Failed sends the operator chasing a permission problem that does not
    exist; classifying it as Unauthorized blames their consent."""
    text = (message or "").lower()
    if not any(w in text for w in ("licen", "subscription")):
        return False
    return any(w in text for w in ("p2", "premium", "governance", "aadp2", "not eligible", "insufficient"))


async def collect_pim_eligibility(
    token: str,
    *,
    scope: str,
    subscription_id: str,
    subscription_name: str,
    tenant_id: str,
    role_defs: dict[str, dict[str, Any]],
    policies: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], CollectorStatus]:
    """Azure role **eligibility** — access a principal can activate but does not currently hold.

    Without this every live row is ``Active`` by construction, the PIM-eligible KPI is always 0,
    and standing privilege cannot be told apart from JIT privilege. An eligible assignment does
    NOT appear in ``roleAssignments``, so these rows are new information, not duplicates."""
    st = CollectorStatus("AzurePimEligibility")
    url = f"{_ARM}{scope}/providers/Microsoft.Authorization/roleEligibilityScheduleInstances"
    value, err, code = await _get_all(token, url, {"api-version": _PIM_API})
    rows: list[dict[str, Any]] = []
    policies = policies or {}
    for inst in value:
        props = inst.get("properties", {}) or {}
        if str(props.get("status", "")).strip().lower() not in _PIM_GRANTED:
            continue
        expanded = props.get("expandedProperties", {}) or {}
        principal = expanded.get("principal", {}) or {}
        role_ref = expanded.get("roleDefinition", {}) or {}
        inst_scope = props.get("scope") or (expanded.get("scope", {}) or {}).get("id") or scope
        parts = schema.parse_scope(inst_scope)
        guid = _role_def_guid(props.get("roleDefinitionId", ""))
        rdef = role_defs.get(guid, {})
        # expandedProperties carries the role name inline; fall back to the definition index, then
        # to the GUID. A bare GUID in the Role column is a product bug, not a data limitation.
        role_name = role_ref.get("displayName") or rdef.get("roleName") or guid
        end = _derive_end(props)
        policy = policies.get(guid, {})
        rows.append(
            schema.make_row(
                surface=schema.SURFACE_AZURE_RBAC,
                accessModel=schema.ACCESS_DATA_PLANE if rdef.get("roleHasDataActions") else schema.ACCESS_CONTROL_PLANE,
                collector=st.collector,
                assignmentState=schema.STATE_ELIGIBLE,
                assignmentType="RoleEligibility",
                accessPath=schema.PATH_DIRECT,
                principalId=props.get("principalId", "") or principal.get("id", ""),
                principalType=props.get("principalType", "") or principal.get("type", ""),
                principalDisplayName=principal.get("displayName", ""),
                principalUserPrincipalName=principal.get("email", ""),
                roleName=role_name,
                roleDefinitionId=props.get("roleDefinitionId", ""),
                roleCategory=rdef.get("roleCategory", "ControlPlane"),
                roleIsPrivileged=bool(rdef.get("roleIsPrivileged")) or schema.role_is_privileged(role_name),
                roleHasDataActions=bool(rdef.get("roleHasDataActions")),
                scope=inst_scope,
                scopeType=parts.get("scopeType", ""),
                scopeDisplayName=(expanded.get("scope", {}) or {}).get("displayName", "") or inst_scope,
                tenantId=tenant_id,
                managementGroupId=parts.get("managementGroupId", ""),
                subscriptionId=parts.get("subscriptionId", subscription_id),
                subscriptionName=subscription_name,
                resourceGroup=parts.get("resourceGroup", ""),
                resourceType=parts.get("resourceType", ""),
                resourceName=parts.get("resourceName", ""),
                assignmentId=inst.get("id", ""),
                assignmentCreatedOn=props.get("createdOn", "") or props.get("startDateTime", ""),
                sourceApi="ARM roleEligibilityScheduleInstances",
                collectionStatus=schema.STATUS_SUCCEEDED,
                effectivePrincipalId=props.get("principalId", "") or principal.get("id", ""),
                effectivePrincipalType=props.get("principalType", "") or principal.get("type", ""),
                effectivePrincipalName=principal.get("displayName", ""),
                effectivePrincipalUserPrincipalName=principal.get("email", ""),
                # PIM fields
                pimManaged=True,
                eligibilityStartDateTime=props.get("startDateTime", ""),
                eligibilityEndDateTime=end,
                isPermanentEligible=_is_permanent(props, end),
                memberType=props.get("memberType", ""),
                requiresApproval=bool(policy.get("requiresApproval")),
                requiresMfa=bool(policy.get("requiresMfa")),
                requiresJustification=bool(policy.get("requiresJustification")),
                activationMaxHours=str(policy.get("activationMaxHours") or ""),
            )
        )
    if err:
        if _pim_is_licence_error(err):
            st.status = schema.STATUS_SKIPPED
            st.message = "PIM is not licensed on this tenant (needs Entra ID P2 / Governance)."
        else:
            st.status = schema.STATUS_PARTIAL if rows else _status_for_http(code)
            st.message = err
    st.rows_added = len(rows)
    return rows, st


async def collect_pim_active_schedules(token: str, *, scope: str) -> tuple[dict[str, dict[str, Any]], CollectorStatus]:
    """Map ``originRoleAssignmentId`` → the PIM facts about a currently-active assignment.

    ``roleAssignmentScheduleInstances`` MIRRORS rows that ``roleAssignments`` already returns —
    an activation creates a real role assignment — so emitting them as rows would double-count
    every JIT elevation. Instead this returns an annotation map keyed on the ARM role-assignment
    id, and the orchestrator stamps the PIM fields onto the existing row.

    That is what lets the product say "this Owner is a time-boxed elevation that expires at
    17:04" rather than reporting it as standing privilege."""
    st = CollectorStatus("AzurePimActiveSchedules")
    url = f"{_ARM}{scope}/providers/Microsoft.Authorization/roleAssignmentScheduleInstances"
    value, err, code = await _get_all(token, url, {"api-version": _PIM_API})
    out: dict[str, dict[str, Any]] = {}
    for inst in value:
        props = inst.get("properties", {}) or {}
        if str(props.get("status", "")).strip().lower() not in _PIM_GRANTED:
            continue
        origin = str(props.get("originRoleAssignmentId", "")).strip().lower()
        if not origin:
            continue
        assignment_type = str(props.get("assignmentType", ""))
        end = _derive_end(props)
        out[origin] = {
            # "Activated" = a JIT elevation in force. "Assigned" = a permanent grant that PIM
            # merely knows about — still standing privilege, so it gets no expiry.
            "activated": assignment_type.lower() == "activated",
            "assignmentType": assignment_type,
            "activationExpiresOn": end if assignment_type.lower() == "activated" else "",
            "memberType": props.get("memberType", ""),
            "startDateTime": props.get("startDateTime", ""),
        }
    if err:
        if _pim_is_licence_error(err):
            st.status = schema.STATUS_SKIPPED
            st.message = "PIM is not licensed on this tenant (needs Entra ID P2 / Governance)."
        else:
            st.status = schema.STATUS_PARTIAL if out else _status_for_http(code)
            st.message = err
    st.rows_added = len(out)
    return out, st


async def collect_pim_policies(token: str, *, scope: str) -> tuple[dict[str, dict[str, Any]], CollectorStatus]:
    """Activation controls per role: approval, MFA, justification and the maximum duration.

    A tenant that bought PIM but requires neither approval nor MFA to activate Owner has bought
    very little, and nothing else in the product would show that. Returns
    ``{roleDefinitionGuid: {...}}``."""
    st = CollectorStatus("AzurePimPolicies")
    url = f"{_ARM}{scope}/providers/Microsoft.Authorization/roleManagementPolicyAssignments"
    value, err, code = await _get_all(token, url, {"api-version": _PIM_API, "$filter": "atScope()"})
    out: dict[str, dict[str, Any]] = {}
    for pa in value:
        props = pa.get("properties", {}) or {}
        guid = _role_def_guid(props.get("roleDefinitionId", ""))
        if not guid:
            continue
        entry = {"requiresApproval": False, "requiresMfa": False, "requiresJustification": False, "activationMaxHours": None}
        for rule in props.get("effectiveRules", []) or []:
            rid = str(rule.get("id", ""))
            rtype = str(rule.get("ruleType", ""))
            # Only the END-USER ASSIGNMENT rules describe what activation demands; the Admin_*
            # rules describe what an administrator must do to grant eligibility, which is a
            # different question. Conflating them reports approval as required when it is not.
            if not rid.startswith("Approval_EndUser_Assignment") and "EndUser_Assignment" not in rid:
                continue
            if rtype == "RoleManagementPolicyApprovalRule":
                entry["requiresApproval"] = bool((rule.get("setting") or {}).get("isApprovalRequired"))
            elif rtype == "RoleManagementPolicyEnablementRule":
                enabled = [str(x).lower() for x in (rule.get("enabledRules") or [])]
                entry["requiresMfa"] = "multifactorauthentication" in enabled
                entry["requiresJustification"] = "justification" in enabled
            elif rtype == "RoleManagementPolicyExpirationRule":
                entry["activationMaxHours"] = _duration_hours(str(rule.get("maximumDuration") or ""))
        out[guid] = entry
    if err:
        if _pim_is_licence_error(err):
            st.status = schema.STATUS_SKIPPED
            st.message = "PIM is not licensed on this tenant (needs Entra ID P2 / Governance)."
        else:
            st.status = schema.STATUS_PARTIAL if out else _status_for_http(code)
            st.message = err
    st.rows_added = len(out)
    return out, st





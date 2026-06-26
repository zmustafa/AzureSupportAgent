"""Live access collectors — port of the scanner's per-surface collectors to async Python.

Each collector hits Azure (ARM REST for Azure RBAC, Microsoft Graph for the directory layer)
through the app's existing connection/token plumbing and returns normalized rows plus a
``CollectorStatus``. They are defensive in the scanner's spirit: a permission/throttle/parse
failure for one collector is recorded (Unauthorized / Throttled / Failed / PartiallyCollected)
and never sinks the others. When no usable token is available they report ``Skipped`` so the UI
can prompt for a connection rather than erroring.

The deterministic demo path (:mod:`app.rbac.demo`) is what's exercised locally; these run only
against a real connection with broad reader + Graph permissions."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.rbac import schema

log = logging.getLogger("app.rbac.collectors")

_ARM = "https://management.azure.com"
_GRAPH = "https://graph.microsoft.com/v1.0"
_RA_API = "2022-04-01"  # Authorization roleAssignments / roleDefinitions

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
    headers = {"Authorization": f"Bearer {token}"}
    out: list[dict[str, Any]] = []
    next_url: str | None = url
    next_params = dict(params or {})
    code = 200
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            while next_url:
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


# --------------------------------------------------------------------------- Azure RBAC
async def collect_role_definitions(token: str, scope: str) -> tuple[dict[str, dict[str, Any]], CollectorStatus]:
    """Map roleDefinitionGuid -> {name, category, privileged, has_data_actions} for a scope."""
    st = CollectorStatus("AzureRoleDefinitions")
    url = f"{_ARM}{scope}/providers/Microsoft.Authorization/roleDefinitions"
    value, err, code = await _get_all(token, url, {"api-version": _RA_API})
    index: dict[str, dict[str, Any]] = {}
    for rd in value:
        props = rd.get("properties", {}) or {}
        name = props.get("roleName", "")
        perms = props.get("permissions", []) or [{}]
        data_actions = any((p.get("dataActions") or p.get("notDataActions")) for p in perms)
        guid = _role_def_guid(rd.get("id", "") or rd.get("name", ""))
        index[guid] = {
            "roleName": name,
            "roleDefinitionId": rd.get("id", ""),
            "roleCategory": schema.role_category(bool(data_actions)),
            "roleIsPrivileged": schema.role_is_privileged(name, has_data_actions=bool(data_actions)),
            "roleHasDataActions": bool(data_actions),
            "roleType": props.get("type", ""),
            "description": props.get("description", ""),
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



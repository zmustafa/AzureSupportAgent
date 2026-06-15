"""Drive a per-scope (or directory) RBAC refresh against an Azure connection.

The orchestrator is the write path behind ``POST /rbac/refresh``: it acquires the connection's
ARM / Graph tokens, runs the relevant collectors continue-on-error (the scanner model), and
writes the result into the per-scope cache. Each step emits a progress line so the SSE endpoint
can stream live status while neighbours stay served from cache.

It never raises on a collector failure — failures are captured as collector statuses and the
scope's slice is still written (stale-while-error per scope)."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from app.rbac import cache, collectors, schema

log = logging.getLogger("app.rbac.orchestrator")

ProgressFn = Callable[[str, str], Awaitable[None]]


async def _noop(_level: str, _message: str) -> None:
    return None


def _scope_label(scope: str, scope_type: str) -> str:
    if scope_type == schema.SCOPE_SUBSCRIPTION:
        return scope.rstrip("/").split("/")[-1]
    if scope_type == schema.SCOPE_MANAGEMENT_GROUP:
        return scope.rstrip("/").split("/")[-1]
    return scope


async def refresh_scope(
    tenant_id: str,
    connection: dict[str, Any] | None,
    scope: str,
    *,
    display_name: str = "",
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Refresh a single subscription/management-group scope and write its cache slice."""
    progress = progress or _noop
    started = time.monotonic()
    parts = schema.parse_scope(scope)
    scope_type = parts.get("scopeType", schema.SCOPE_SUBSCRIPTION)
    subscription_id = parts.get("subscriptionId", "")
    label = display_name or _scope_label(scope, scope_type)
    await progress("info", f"Refreshing access for {label}…")

    from app.azure.credentials import get_arm_token

    token, terr = (None, "no connection") if not connection else await get_arm_token(connection)
    if not token:
        await progress("warning", f"No Azure token for this connection — skipped ({terr}).")
        meta = {
            "scopeType": scope_type,
            "displayName": label,
            "subscriptionId": subscription_id,
            "managementGroupId": parts.get("managementGroupId", ""),
            "status": schema.STATUS_SKIPPED,
            "collectors": [collectors.CollectorStatus("AzureSubscriptionRbac", schema.STATUS_SKIPPED, 0, 0.0, terr or "").public()],
            "coverage": {},
            "demo": False,
        }
        return cache.write_scope(tenant_id, scope, meta=meta, rows=[])

    await progress("info", "Collecting role definitions…")
    role_defs, rd_status = await collectors.collect_role_definitions(token, scope)
    await progress("info", f"{rd_status.rows_added} role definition(s) [{rd_status.status}].")

    collector_name = "AzureSubscriptionRbac" if scope_type == schema.SCOPE_SUBSCRIPTION else "ManagementGroupRbac"
    await progress("info", "Collecting role assignments…")
    rows, ra_status = await collectors.collect_azure_rbac(
        token,
        scope=scope,
        subscription_id=subscription_id,
        subscription_name=label,
        tenant_id=tenant_id,
        role_defs=role_defs,
        collector=collector_name,
    )
    await progress("ok", f"{ra_status.rows_added} role assignment(s) [{ra_status.status}].")

    statuses = [rd_status, ra_status]
    overall = schema.STATUS_SUCCEEDED
    for s in statuses:
        if s.status in schema.ATTENTION_STATUSES:
            overall = schema.STATUS_PARTIAL
    meta = {
        "scopeType": scope_type,
        "displayName": label,
        "subscriptionId": subscription_id,
        "managementGroupId": parts.get("managementGroupId", ""),
        "status": overall,
        "collectors": [s.public() for s in statuses],
        "coverage": {"roleAssignments": len(rows), "roleDefinitions": len(role_defs)},
        "demo": False,
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    written = cache.write_scope(tenant_id, scope, meta=meta, rows=rows)
    await progress("ok", f"Cached {len(rows)} assignment(s) for {label}.")
    return written


async def refresh_directory(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Refresh the tenant directory layer: Entra roles, group expansion, SP owners.

    Group ids and service-principal ids are derived from the assignments already cached in the
    scope slices, so the directory layer enriches whatever Azure RBAC has discovered."""
    progress = progress or _noop
    await progress("info", "Refreshing Entra directory layer…")

    from app.azure.credentials import get_arm_token, get_graph_token

    # Resolve management-group names via ARM first — this works even when the connection lacks
    # Microsoft Graph directory permissions, so the scope tree shows MG names regardless.
    mg_names: dict[str, str] = {}
    mg_status: collectors.CollectorStatus | None = None
    if connection:
        arm_token, _aerr = await get_arm_token(connection)
        if arm_token:
            await progress("info", "Resolving management-group names…")
            mg_names, mg_status = await collectors.collect_management_groups(arm_token)
            await progress("info", f"Resolved {len(mg_names)} management group name(s) [{mg_status.status}].")

    token, terr = (None, "no connection") if not connection else await get_graph_token(connection)
    if not token:
        await progress("warning", f"No Microsoft Graph token — directory skipped ({terr}).")
        collector_list = [collectors.CollectorStatus("EntraRoleAssignments", schema.STATUS_SKIPPED, 0, 0.0, terr or "").public()]
        if mg_status is not None:
            collector_list.append(mg_status.public())
        meta = {
            "status": schema.STATUS_SKIPPED,
            "collectors": collector_list,
            "demo": False,
        }
        return cache.write_directory(tenant_id, meta=meta, rows=[], role_defs=[], principals=[], groups={}, management_groups=mg_names)

    statuses: list[collectors.CollectorStatus] = []
    if mg_status is not None:
        statuses.append(mg_status)
    await progress("info", "Collecting Entra directory roles…")
    entra_rows, entra_status = await collectors.collect_entra_roles(token, tenant_id)
    statuses.append(entra_status)
    await progress("info", f"{entra_status.rows_added} directory role assignment(s) [{entra_status.status}].")

    # Derive the group + SP ids that actually appear in cached assignments (only expand what's used).
    scope_rows = cache.all_scope_rows(tenant_id)
    group_ids = sorted({r.get("principalId", "") for r in scope_rows if r.get("principalType") == "Group" and r.get("principalId")})
    sp_ids = sorted({r.get("principalId", "") for r in scope_rows if r.get("principalType") == "ServicePrincipal" and r.get("principalId")})

    await progress("info", f"Expanding {len(group_ids)} group(s)…")
    groups, grp_status = await collectors.collect_group_expansion(token, group_ids)
    statuses.append(grp_status)

    await progress("info", f"Resolving owners for {len(sp_ids)} service principal(s)…")
    owner_rows, owner_status = await collectors.collect_sp_owners(token, sp_ids, tenant_id)
    statuses.append(owner_status)

    # Resolve every distinct principal GUID seen in the cached Azure-RBAC/KV/classic assignments
    # AND the Entra directory-role rows to a friendly name (ARM only returns the object id, and
    # the Entra query expands roleDefinition not principal). This populates the principal directory
    # used by compose to backfill names across every tab + export.
    principal_ids = sorted(
        {r.get("principalId", "") for r in scope_rows if r.get("principalId")}
        | {r.get("effectivePrincipalId", "") for r in scope_rows if r.get("effectivePrincipalId")}
        | {r.get("principalId", "") for r in entra_rows if r.get("principalId")}
        | {r.get("effectivePrincipalId", "") for r in owner_rows if r.get("effectivePrincipalId")}
    )
    await progress("info", f"Resolving {len(principal_ids)} principal name(s)…")
    principals, prin_status = await collectors.collect_principal_directory(token, principal_ids)
    statuses.append(prin_status)
    await progress("info", f"Resolved {prin_status.rows_added} principal name(s) [{prin_status.status}].")

    # Backfill group display names (the expansion graph stores members but not the group's own
    # name) from the resolved principal directory, so group rows read with names too.
    _pmap = {p["principalId"].lower(): p.get("displayName", "") for p in principals if p.get("principalId")}
    for gid, grp in groups.items():
        if not grp.get("name"):
            grp["name"] = _pmap.get(str(gid).lower(), "")

    overall = schema.STATUS_SUCCEEDED
    for s in statuses:
        if s.status in schema.ATTENTION_STATUSES:
            overall = schema.STATUS_PARTIAL
    meta = {
        "status": overall,
        "collectors": [s.public() for s in statuses],
        "demo": False,
    }
    written = cache.write_directory(
        tenant_id,
        meta=meta,
        rows=[*entra_rows, *owner_rows],
        role_defs=[],
        principals=principals,
        groups=groups,
        management_groups=mg_names,
    )
    await progress("ok", "Directory layer cached.")
    return written


async def refresh_all(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Enumerate the connection's subscriptions, refresh each scope, then the directory layer."""
    progress = progress or _noop
    from app.azure.arm import list_subscriptions
    from app.azure.credentials import get_arm_token

    token, terr = (None, "no connection") if not connection else await get_arm_token(connection)
    if not token:
        await progress("warning", f"No Azure token — nothing to scan ({terr}).")
        return {"scopes": 0, "skipped": True, "error": terr}

    await progress("info", "Listing subscriptions…")
    subs, serr = await list_subscriptions(token)
    if serr:
        await progress("warning", f"Subscription listing failed: {serr}")
    await progress("info", f"{len(subs)} subscription(s) visible.")

    refreshed = 0
    for sub in subs:
        scope = f"/subscriptions/{sub['id']}"
        await refresh_scope(tenant_id, connection, scope, display_name=sub.get("name", sub["id"]), progress=progress)
        refreshed += 1

    await refresh_directory(tenant_id, connection, progress=progress)
    await progress("ok", f"Refreshed {refreshed} scope(s) + directory.")
    return {"scopes": refreshed, "skipped": False}

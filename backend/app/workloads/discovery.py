"""Azure discovery for the resource picker: tree expansion, search, and facets.

All resource-group / resource / type / location data comes from Azure Resource Graph
(`az graph query`, via the command runner), bound to a connection's identity. Management
groups and subscriptions come from ARM REST. Everything is read-only.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.azure.arm import (
    get_management_group_children,
    list_management_groups,
    list_root_management_groups,
    list_subscriptions,
)
from app.azure.credentials import get_arm_token
from app.exec.command_runner import run_kql_capture

logger = logging.getLogger("app.workloads.discovery")

_PAGE = 200


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _esc(val: str) -> str:
    """Escape a value for safe single-quoted embedding in a KQL string."""
    return (val or "").replace("'", "''")


async def list_top_level(connection: dict | None, group_by: str) -> list[dict[str, Any]]:
    """Top-level tree nodes: management groups (group_by='mg') or subscriptions."""
    token, err = await get_arm_token(connection) if connection else (None, "no connection")
    if group_by == "mg":
        if token and not err:
            # Prefer the ROOT management groups (real hierarchy) so nested MGs are revealed by
            # expansion rather than listed flat alongside their parent.
            roots, root_err = await list_root_management_groups(token)
            if not root_err and roots:
                return [
                    {"kind": "mg", "id": g["id"], "name": g["name"], "has_children": True}
                    for g in roots
                ]
            # Fallback (getEntities unavailable/forbidden): the flat list, so the picker still
            # works — degraded to showing every visible MG at the top level.
            mgs, mg_err = await list_management_groups(token)
            if not mg_err and mgs:
                return [
                    {"kind": "mg", "id": g["id"], "name": g["name"], "has_children": True}
                    for g in mgs
                ]
        # fall through to subscriptions if MG discovery failed
    subs = await _subscriptions(connection, token, err)
    return [
        {"kind": "subscription", "id": s["id"], "name": s["name"], "has_children": True}
        for s in subs
    ]


async def _subscriptions(connection: dict | None, token: str | None, err: str | None) -> list[dict]:
    if token and not err:
        subs, sub_err = await list_subscriptions(token)
        if not sub_err:
            return subs
    return []


async def expand_node(
    connection: dict | None, kind: str, node_id: str, *, session_config_dir: str | None = None
) -> list[dict[str, Any]]:
    """Lazily list the direct children of a tree node.

    ``session_config_dir`` (optional) reuses a pre-authenticated SP login across many
    queries (used by cache prefetch) — see command_runner.open_sp_session."""
    if kind == "mg":
        token, err = await get_arm_token(connection) if connection else (None, "no connection")
        if not token or err:
            return []
        children, _ = await get_management_group_children(token, node_id)
        return [
            {"kind": c["kind"], "id": c["id"], "name": c["name"], "has_children": True}
            for c in children
        ]
    if kind == "subscription":
        # Resource groups in the subscription, via Resource Graph.
        kql = (
            "ResourceContainers "
            "| where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
            f"| where subscriptionId =~ '{_esc(node_id)}' "
            "| project name, location, id "
            "| order by name asc"
        )
        cap = await run_kql_capture(kql, connection, output="json", session_config_dir=session_config_dir)
        rows = _parse_rows(cap.stdout) if cap.ok else []
        return [
            {
                "kind": "resource_group",
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "location": r.get("location", ""),
                "subscription_id": node_id,
                "has_children": True,
            }
            for r in rows
        ]
    if kind == "resource_group":
        # Resources directly in the RG. node_id is the RG's full ARM id.
        rg_name = node_id.rstrip("/").split("/")[-1]
        sub_id = ""
        parts = node_id.strip("/").split("/")
        if "subscriptions" in parts:
            sub_id = parts[parts.index("subscriptions") + 1]
        kql = (
            "Resources "
            f"| where subscriptionId =~ '{_esc(sub_id)}' and resourceGroup =~ '{_esc(rg_name)}' "
            "| project name, type, location, id, resourceGroup, subscriptionId "
            "| order by name asc"
        )
        cap = await run_kql_capture(kql, connection, output="json", session_config_dir=session_config_dir)
        rows = _parse_rows(cap.stdout) if cap.ok else []
        return [
            {
                "kind": "resource",
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "resource_type": r.get("type", ""),
                "location": r.get("location", ""),
                "resource_group": r.get("resourceGroup", ""),
                "subscription_id": r.get("subscriptionId", ""),
                "has_children": False,
            }
            for r in rows
        ]
    return []


async def search_resources(
    connection: dict | None,
    *,
    query: str = "",
    subscription_id: str = "",
    types: list[str] | None = None,
    locations: list[str] | None = None,
    skip: int = 0,
    top: int = _PAGE,
) -> dict[str, Any]:
    """Flat resource search across the tenant, filtered by query/type/location/sub."""
    _ = skip  # reserved for future skip-token paging (Resource Graph has no `skip`)
    clauses: list[str] = []
    if subscription_id:
        clauses.append(f"subscriptionId =~ '{_esc(subscription_id)}'")
    if types:
        joined = ", ".join(f"'{_esc(t)}'" for t in types)
        clauses.append(f"type in~ ({joined})")
    if locations:
        joined = ", ".join(f"'{_esc(loc)}'" for loc in locations)
        clauses.append(f"location in~ ({joined})")
    if query:
        q = _esc(query)
        clauses.append(
            f"(name contains '{q}' or type contains '{q}' or resourceGroup contains '{q}')"
        )
    where = (" | where " + " and ".join(clauses)) if clauses else ""
    # Note: Azure Resource Graph's KQL subset does NOT support the `skip` operator
    # (server-side paging uses a skip-token instead). We cap with `take`; deep paging is
    # left to a future skip-token implementation.
    kql = (
        "Resources"
        f"{where} "
        "| project name, type, location, id, resourceGroup, subscriptionId "
        "| order by name asc "
        f"| take {min(max(top, 1), 1000)}"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    if not cap.ok:
        return {"rows": [], "error": cap.error or "Query failed."}
    rows = _parse_rows(cap.stdout)
    return {
        "rows": [
            {
                "kind": "resource",
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "resource_type": r.get("type", ""),
                "location": r.get("location", ""),
                "resource_group": r.get("resourceGroup", ""),
                "subscription_id": r.get("subscriptionId", ""),
            }
            for r in rows
        ],
        "error": "",
    }


async def facets(
    connection: dict | None, subscription_id: str = "", *, session_config_dir: str | None = None
) -> dict[str, Any]:
    """Distinct resource types + locations for the filter dropdowns (one query)."""
    sub = f"| where subscriptionId =~ '{_esc(subscription_id)}' " if subscription_id else ""
    # Single query: summarize distinct types and locations together, then split.
    kql = (
        f"Resources {sub}"
        "| summarize types = make_set(type, 2000), locations = make_set(location, 2000)"
    )
    cap = await run_kql_capture(kql, connection, output="json", session_config_dir=session_config_dir)
    types: list[str] = []
    locations: list[str] = []
    rows = _parse_rows(cap.stdout) if cap.ok else []
    if rows:
        row = rows[0]
        types = sorted({t for t in (row.get("types") or []) if t})
        locations = sorted({loc for loc in (row.get("locations") or []) if loc})
    return {"types": types, "locations": locations}


def _norm_resource(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "resource",
        "id": r.get("id", ""),
        "name": r.get("name", ""),
        "resource_type": r.get("type", ""),
        "location": r.get("location", ""),
        "resource_group": r.get("resourceGroup", ""),
        "subscription_id": r.get("subscriptionId", ""),
        "tags": r.get("tags") or {},
    }


async def all_resource_groups(
    connection: dict | None, *, session_config_dir: str | None = None
) -> list[dict[str, Any]]:
    """Every resource group across the whole tenant, in ONE Resource Graph query.

    Each item carries its parent ``subscription_id`` so the caller can group RGs per
    subscription. Used by cache prefetch to avoid one query per subscription."""
    kql = (
        "ResourceContainers "
        "| where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
        "| project name, location, id, subscriptionId "
        "| order by name asc"
    )
    cap = await run_kql_capture(kql, connection, output="json", session_config_dir=session_config_dir)
    rows = _parse_rows(cap.stdout) if cap.ok else []
    return [
        {
            "kind": "resource_group",
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "location": r.get("location", ""),
            "subscription_id": r.get("subscriptionId", ""),
            "has_children": True,
        }
        for r in rows
    ]


async def all_resources(
    connection: dict | None, *, cap: int = 1000, session_config_dir: str | None = None
) -> list[dict[str, Any]]:
    """Every resource across the whole tenant (with tags), in ONE query, capped."""
    kql = (
        "Resources "
        "| project name, type, location, id, resourceGroup, subscriptionId, tags "
        "| order by subscriptionId asc, resourceGroup asc, name asc "
        f"| take {min(max(cap, 1), 1000)}"
    )
    capres = await run_kql_capture(kql, connection, output="json", session_config_dir=session_config_dir)
    if not capres.ok:
        return []
    return [_norm_resource(r) for r in _parse_rows(capres.stdout)]


async def subscriptions_under_mg(connection: dict | None, mg_id: str) -> list[str]:
    """Recursively collect subscription ids beneath a management group (cycle-safe)."""
    token, err = await get_arm_token(connection) if connection else (None, "no connection")
    if not token or err:
        return []
    subs: list[str] = []
    seen_mgs: set[str] = set()
    stack = [mg_id]
    while stack:
        cur = stack.pop()
        if cur in seen_mgs:
            continue
        seen_mgs.add(cur)
        children, _ = await get_management_group_children(token, cur)
        for c in children:
            if c["kind"] == "mg":
                stack.append(c["id"])
            elif c["kind"] == "subscription" and c["id"] not in subs:
                subs.append(c["id"])
    return subs


async def resources_in_subscriptions(
    connection: dict | None,
    subscription_ids: list[str],
    *,
    cap: int = 1000,
    session_config_dir: str | None = None,
) -> list[dict[str, Any]]:
    """All resources across the given subscriptions (with tags), capped.

    ``session_config_dir`` reuses a pre-authenticated SP login (cache prefetch)."""
    if not subscription_ids:
        return []
    joined = ", ".join(f"'{_esc(s)}'" for s in subscription_ids)
    kql = (
        "Resources "
        f"| where subscriptionId in~ ({joined}) "
        "| project name, type, location, id, resourceGroup, subscriptionId, tags "
        "| order by resourceGroup asc, name asc "
        f"| take {min(max(cap, 1), 1000)}"
    )
    capres = await run_kql_capture(kql, connection, output="json", session_config_dir=session_config_dir)
    if not capres.ok:
        logger.warning(
            "Resource Graph query failed (exit=%s) for subs %s: error=%r stderr=%r",
            capres.exit_code,
            subscription_ids,
            (capres.error or "")[:500],
            (capres.stderr or "")[:500],
        )
        return []
    return [_norm_resource(r) for r in _parse_rows(capres.stdout)]


async def resources_in_resource_groups(
    connection: dict | None, pairs: list[tuple[str, str]], *, cap: int = 1000
) -> list[dict[str, Any]]:
    """Resources within the given (subscription_id, resource_group) pairs."""
    if not pairs:
        return []
    clauses = [
        f"(subscriptionId =~ '{_esc(sub)}' and resourceGroup =~ '{_esc(rg)}')"
        for sub, rg in pairs
        if rg
    ]
    if not clauses:
        return []
    kql = (
        "Resources "
        f"| where {' or '.join(clauses)} "
        "| project name, type, location, id, resourceGroup, subscriptionId, tags "
        "| order by name asc "
        f"| take {min(max(cap, 1), 1000)}"
    )
    capres = await run_kql_capture(kql, connection, output="json")
    if not capres.ok:
        return []
    return [_norm_resource(r) for r in _parse_rows(capres.stdout)]


async def resources_exist(connection: dict | None, ids: list[str]) -> set[str]:
    """Return the subset of resource ids that still exist in Azure."""
    if not ids:
        return set()
    found: set[str] = set()
    # Chunk to keep each KQL bounded.
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        joined = ", ".join(f"'{_esc(x)}'" for x in chunk)
        kql = f"Resources | where id in~ ({joined}) | project id"
        capres = await run_kql_capture(kql, connection, output="json")
        if capres.ok:
            for r in _parse_rows(capres.stdout):
                if r.get("id"):
                    found.add(r["id"].lower())
    return found


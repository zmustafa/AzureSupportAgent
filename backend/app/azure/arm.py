"""Minimal Azure Resource Manager (ARM) REST helpers used for multi-tenant discovery
and connection health, independent of the MCP server.

Given an ARM access token (see app.azure.credentials.get_arm_token) these list the
subscriptions and management groups visible to a connection's identity. This powers the
tenant/subscription/management-group pickers without requiring the MCP server, so the
selector works even for pasted-token connections.
"""
from __future__ import annotations

from typing import Any

import httpx

_ARM = "https://management.azure.com"


async def _get(token: str, path: str, params: dict[str, str]) -> tuple[Any, str | None]:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30, base_url=_ARM) as client:
            resp = await client.get(path, headers=headers, params=params)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            return None, f"ARM {resp.status_code}: {str(detail)[:300]}"
        return resp.json(), None
    except httpx.HTTPError as e:  # noqa: BLE001
        return None, f"ARM request error: {e}"


async def list_subscriptions(token: str) -> tuple[list[dict[str, str]], str | None]:
    data, err = await _get(token, "/subscriptions", {"api-version": "2022-12-01"})
    if err:
        return [], err
    subs: list[dict[str, str]] = []
    for s in (data or {}).get("value", []):
        subs.append(
            {
                "id": s.get("subscriptionId", ""),
                "name": s.get("displayName", s.get("subscriptionId", "")),
                "state": s.get("state", ""),
                "is_default": False,
            }
        )
    return subs, None


async def list_management_groups(token: str) -> tuple[list[dict[str, str]], str | None]:
    data, err = await _get(
        token,
        "/providers/Microsoft.Management/managementGroups",
        {"api-version": "2020-05-01"},
    )
    if err:
        return [], err
    groups: list[dict[str, str]] = []
    for g in (data or {}).get("value", []):
        gid = g.get("name", "")
        props = g.get("properties", {}) or {}
        if not gid:
            continue
        groups.append({"id": gid, "name": props.get("displayName", gid)})
    return groups, None


async def get_management_group_children(
    token: str, group_id: str
) -> tuple[list[dict[str, str]], str | None]:
    """Direct children (child management groups + subscriptions) of a management group.

    Used to lazily expand an MG node in the resource picker's MG ▸ Sub ▸ RG ▸ Resource
    tree. Returns nodes of kind 'mg' or 'subscription'."""
    data, err = await _get(
        token,
        f"/providers/Microsoft.Management/managementGroups/{group_id}",
        {"api-version": "2020-05-01", "$expand": "children"},
    )
    if err:
        return [], err
    props = (data or {}).get("properties", {}) or {}
    children: list[dict[str, str]] = []
    for c in props.get("children", []) or []:
        ctype = (c.get("type", "") or "").lower()
        cid = c.get("name", "")
        if not cid:
            continue
        if "managementgroups" in ctype and "subscriptions" not in ctype:
            children.append({"kind": "mg", "id": cid, "name": c.get("displayName", cid)})
        elif "subscription" in ctype:
            children.append(
                {"kind": "subscription", "id": cid, "name": c.get("displayName", cid)}
            )
    return children, None


async def list_tenants(token: str) -> tuple[list[dict[str, str]], str | None]:
    data, err = await _get(token, "/tenants", {"api-version": "2022-12-01"})
    if err:
        return [], err
    tenants: list[dict[str, str]] = []
    for t in (data or {}).get("value", []):
        tenants.append(
            {
                "id": t.get("tenantId", ""),
                "name": t.get("displayName", t.get("tenantId", "")),
                "domain": t.get("defaultDomain", ""),
            }
        )
    return tenants, None


async def query_resource_graph(
    token: str,
    query: str,
    subscriptions: list[str] | None = None,
    top: int = 1000,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run a Resource Graph (KQL) query via ARM REST. Returns (rows, error).

    This is the credential-independent discovery path used when there is no ambient
    Azure CLI login (e.g. a Container App authenticating with a managed identity, or a
    pasted-token connection). Omitting ``subscriptions`` queries across every subscription
    the token's identity can access in the tenant — matching ``az graph query`` default
    scoping.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    options: dict[str, Any] = {"resultFormat": "objectArray"}
    if top:
        options["$top"] = int(top)
    body: dict[str, Any] = {"query": query, "options": options}
    if subscriptions:
        body["subscriptions"] = subscriptions
    try:
        async with httpx.AsyncClient(timeout=60, base_url=_ARM) as client:
            resp = await client.post(
                "/providers/Microsoft.ResourceGraph/resources",
                params={"api-version": "2022-10-01"},
                headers=headers,
                json=body,
            )
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            return [], f"Resource Graph {resp.status_code}: {str(detail)[:300]}"
        data = resp.json()
        rows = data.get("data", [])
        return (rows if isinstance(rows, list) else []), None
    except httpx.HTTPError as e:  # noqa: BLE001
        return [], f"Resource Graph request error: {e}"

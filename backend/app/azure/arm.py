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


async def _post(token: str, path: str, params: dict[str, str], body: dict[str, Any] | None = None) -> tuple[Any, str | None]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30, base_url=_ARM) as client:
            resp = await client.post(path, headers=headers, params=params, json=body or {})
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


def _roots_from_entities(entities: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Pick the ROOT management groups out of a flat ``getEntities`` result.

    A management group is a root of the *visible* forest when it has no parent, or its parent
    isn't itself visible to the identity (e.g. the user has access to a child MG but not the
    Tenant Root Group above it). Nested MGs are intentionally excluded here — they're revealed
    by lazily expanding their parent — so the tree mirrors the real hierarchy instead of
    listing every MG flat alongside its parent."""
    mgs = [e for e in entities if "managementgroups" in (e.get("type", "") or "").lower()]
    visible = {e.get("name", "") for e in mgs if e.get("name")}
    roots: list[dict[str, str]] = []
    for e in mgs:
        name = e.get("name", "")
        if not name:
            continue
        props = e.get("properties", {}) or {}
        parent_id = ((props.get("parent") or {}).get("id") or "")
        parent_name = parent_id.rstrip("/").split("/")[-1] if parent_id else ""
        if not parent_name or parent_name not in visible:
            roots.append({"id": name, "name": props.get("displayName", name)})
    return roots


def _skiptoken(next_link: str) -> str:
    """Extract the ``$skiptoken`` from a paged ARM ``@nextLink`` URL (empty if none)."""
    if not next_link:
        return ""
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(next_link).query)
    return (q.get("$skiptoken") or q.get("%24skiptoken") or [""])[0]


async def list_root_management_groups(token: str) -> tuple[list[dict[str, str]], str | None]:
    """Root management groups visible to the identity — the top of the MG hierarchy.

    Uses ``getEntities`` (one call returns every visible MG with its parent id) and keeps only
    the roots, so nested MGs are surfaced by expansion rather than shown flat next to their
    parent. Pages through ``@nextLink`` for very large hierarchies. Returns ``([], error)`` on
    failure so callers can fall back to the flat list."""
    entities: list[dict[str, Any]] = []
    skiptoken = ""
    for _ in range(20):  # bound paging for very large MG forests
        params = {"api-version": "2020-05-01"}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        data, err = await _post(token, "/providers/Microsoft.Management/getEntities", params)
        if err:
            return [], err
        entities.extend((data or {}).get("value", []))
        skiptoken = _skiptoken((data or {}).get("@nextLink") or (data or {}).get("@odata.nextLink") or "")
        if not skiptoken:
            break
    return _roots_from_entities(entities), None


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


# Status codes that warrant a bounded retry with backoff (throttling / transient server).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def query_resource_graph_paged(
    token: str,
    query: str,
    subscriptions: list[str] | None = None,
    *,
    page_size: int = 1000,
    max_rows: int = 5000,
    max_retries: int = 4,
) -> tuple[list[dict[str, Any]], str | None, bool, int | None]:
    """Run a Resource Graph (KQL) query via ARM REST, paging through ``$skipToken`` until
    the result set is exhausted or ``max_rows`` is reached.

    Returns ``(rows, error, complete, total)``:
    - ``rows``     all rows gathered across pages (capped at ``max_rows``).
    - ``error``    a non-empty string ONLY on a hard failure (auth/throttle-exhausted/parse)
                   — callers MUST treat a non-None error as "could not evaluate", never as
                   an empty (passing) result. Fail-closed.
    - ``complete`` True when every matching row was returned; False when ``max_rows`` capped
                   the set (more violating resources exist than were fetched).
    - ``total``    the full ``totalRecords`` ARG reports for the query (accurate even when
                   ``rows`` was capped), or None if ARG didn't report it.

    Throttling (429) and transient 5xx responses are retried with exponential backoff +
    jitter, honoring a ``Retry-After`` header when present.
    """
    import asyncio
    import random

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    rows: list[dict[str, Any]] = []
    skip_token = ""
    complete = True
    total: int | None = None
    page_size = max(1, min(1000, int(page_size)))

    try:
        async with httpx.AsyncClient(timeout=90, base_url=_ARM) as client:
            for _page in range(200):  # hard bound on pages (200 * 1000 = 200k rows)
                options: dict[str, Any] = {"resultFormat": "objectArray", "$top": page_size}
                if skip_token:
                    options["$skipToken"] = skip_token
                body: dict[str, Any] = {"query": query, "options": options}
                if subscriptions:
                    body["subscriptions"] = subscriptions

                # --- request with bounded retry on throttle / transient errors ----------
                resp = None
                last_err = ""
                for attempt in range(max_retries + 1):
                    try:
                        resp = await client.post(
                            "/providers/Microsoft.ResourceGraph/resources",
                            params={"api-version": "2022-10-01"},
                            headers=headers,
                            json=body,
                        )
                    except httpx.HTTPError as e:  # noqa: BLE001 - transient network
                        last_err = f"Resource Graph request error: {e}"
                        resp = None
                        if attempt >= max_retries:
                            return rows, last_err, False, total
                        await asyncio.sleep(min(30.0, (2 ** attempt) + random.uniform(0, 0.5)))
                        continue
                    if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
                        retry_after = _retry_after_seconds(resp)
                        delay = retry_after if retry_after is not None else (2 ** attempt) + random.uniform(0, 0.5)
                        await asyncio.sleep(min(60.0, delay))
                        continue
                    break

                if resp is None:
                    return rows, last_err or "Resource Graph request failed.", False, total
                if resp.status_code != 200:
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text)
                    except (ValueError, AttributeError):
                        detail = resp.text
                    return rows, f"Resource Graph {resp.status_code}: {str(detail)[:300]}", False, total
                try:
                    data = resp.json()
                except (ValueError, AttributeError) as e:
                    return rows, f"Resource Graph response parse error: {e}", False, total

                if total is None:
                    tr = data.get("totalRecords")
                    if isinstance(tr, (int, float)):
                        total = int(tr)
                page_rows = data.get("data", [])
                if isinstance(page_rows, list):
                    rows.extend(page_rows)
                skip_token = data.get("$skipToken") or data.get("skipToken") or ""
                if len(rows) >= max_rows:
                    rows = rows[:max_rows]
                    complete = not bool(skip_token)
                    break
                if not skip_token:
                    break
            else:
                # Loop exhausted its page bound with a skip token still pending.
                complete = not bool(skip_token)
        return rows, None, complete, total
    except httpx.HTTPError as e:  # noqa: BLE001
        return rows, f"Resource Graph request error: {e}", False, total, False


def _retry_after_seconds(resp: "httpx.Response") -> float | None:
    """Parse a ``Retry-After`` header (seconds) from a throttled response, if present."""
    val = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return None

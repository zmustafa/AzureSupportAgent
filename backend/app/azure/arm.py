"""Minimal Azure Resource Manager (ARM) REST helpers used for multi-tenant discovery
and connection health, independent of the MCP server.

Given an ARM access token (see app.azure.credentials.get_arm_token) these list the
subscriptions and management groups visible to a connection's identity. This powers the
tenant/subscription/management-group pickers without requiring the MCP server, so the
selector works even for pasted-token connections.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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


def _flatten_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten ALL visible management groups from a ``getEntities`` result into a single
    depth-ordered list (parent before its children), each carrying a ``depth`` for indentation.

    Used by the flat MG picker (Workload Autopilot's ``<select>``) so a nested MG is selectable
    directly — unlike the lazy-expansion tree, a flat dropdown can't reveal children, so it must
    list every MG up front. Cycles / orphaned parents are handled defensively."""
    mgs = [e for e in entities if "managementgroups" in (e.get("type", "") or "").lower()]
    by_name: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = {}
    for e in mgs:
        name = e.get("name", "")
        if not name:
            continue
        props = e.get("properties", {}) or {}
        parent_id = ((props.get("parent") or {}).get("id") or "")
        parent_name = parent_id.rstrip("/").split("/")[-1] if parent_id else ""
        by_name[name] = {"id": name, "name": props.get("displayName", name), "parent": parent_name}
    # A node is a visible root when it has no parent, or its parent isn't visible to us.
    roots = sorted(
        (n for n, e in by_name.items() if not e["parent"] or e["parent"] not in by_name),
        key=lambda n: by_name[n]["name"].lower(),
    )
    for n, e in by_name.items():
        p = e["parent"]
        if p and p in by_name:
            children.setdefault(p, []).append(n)
    for p in children:
        children[p].sort(key=lambda n: by_name[n]["name"].lower())

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _walk(name: str, depth: int) -> None:
        if name in seen:  # defend against cycles
            return
        seen.add(name)
        e = by_name[name]
        out.append({"id": e["id"], "name": e["name"], "depth": depth})
        for c in children.get(name, []):
            _walk(c, depth + 1)

    for r in roots:
        _walk(r, 0)
    # Include any MG not reachable from a root (cycle/orphan) so nothing is silently dropped.
    for n in sorted(by_name, key=lambda x: by_name[x]["name"].lower()):
        if n not in seen:
            _walk(n, 0)
    return out



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
    entities, err = await _get_entities(token)
    if err:
        return [], err
    return _roots_from_entities(entities), None


async def list_all_management_groups(token: str) -> tuple[list[dict[str, Any]], str | None]:
    """EVERY visible management group, flattened into a depth-ordered list (each with a
    ``depth`` for indentation). For the flat MG picker where children can't be lazily expanded,
    so nested MGs must be listed up front. Returns ``([], error)`` on failure."""
    entities, err = await _get_entities(token)
    if err:
        return [], err
    return _flatten_entities(entities), None


async def _get_entities(token: str) -> tuple[list[dict[str, Any]], str | None]:
    """Page through ``Microsoft.Management/getEntities`` (every visible MG with its parent id),
    bounding paging for very large MG forests. Returns ``(entities, error)``."""
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
    return entities, None


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


async def list_activity_log_events(
    token: str,
    subscription_id: str,
    start_iso: str,
    end_iso: str,
    *,
    max_events: int = 1000,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read the Azure Monitor Activity Log (management events) for a subscription window via
    ARM REST. Returns ``(events, error)``.

    This is the credential-independent path used when there is NO ambient Azure CLI login for
    the subscription's tenant — e.g. a pasted-token (``az_cli_token``) or managed-identity
    connection. ``az monitor activity-log list`` can only read whatever tenant the host's ``az``
    is signed into, so for those connections it fails with "subscription not recognized"; this
    uses the connection's own ARM token instead (mirroring how Resource Graph already falls back
    to REST). Each returned event matches the shape ``az monitor activity-log list`` emits, so
    the same row parser handles both paths.

    Pages through ``nextLink`` until ``max_events`` rows or no more pages.
    """
    flt = f"eventTimestamp ge '{start_iso}' and eventTimestamp le '{end_iso}'"
    url = (
        f"{_ARM}/subscriptions/{subscription_id}"
        "/providers/microsoft.insights/eventtypes/management/values"
    )
    params: dict[str, str] | None = {"api-version": "2015-04-01", "$filter": flt}
    headers = {"Authorization": f"Bearer {token}"}
    events: list[dict[str, Any]] = []
    # Page ceiling is derived from the requested max_events (the API returns ~event-dense
    # pages; cap pages so a huge window can't loop unbounded, while still letting a large
    # max_events read deeper). Tunable via the AZURE_ACTIVITY_LOG_MAX_PAGES env/setting.
    try:
        from app.core.config import get_settings

        max_pages = max(1, int(get_settings().azure_activity_log_max_pages))
    except Exception:  # noqa: BLE001
        max_pages = 50
    truncated = False
    # The Activity Log ``eventtypes/management/values`` REST API is notoriously slow for wide
    # windows — a single page on a busy subscription over 30 days can take ~30s. A flat 30s
    # client timeout therefore fails legitimate large queries with a bare "ARM request error".
    # Use a generous read timeout (with a short connect timeout) so big windows complete, and
    # treat a timeout as a soft, partial result rather than a hard failure.
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for _page in range(max_pages):  # safety ceiling — max_events normally stops sooner
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code != 200:
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text)
                    except (ValueError, AttributeError):
                        detail = resp.text
                    return events, f"ARM {resp.status_code}: {str(detail)[:300]}"
                data = resp.json()
                page = data.get("value", []) or []
                events.extend(page)
                if len(events) >= max_events:
                    return events[:max_events], None
                # ``nextLink`` is a full URL already carrying the filter + $skipToken.
                next_link = data.get("nextLink") or ""
                if not next_link:
                    break
                url, params = next_link, None
                if _page == max_pages - 1:
                    truncated = True
        note = (
            f"Activity Log truncated at the {max_pages}-page ceiling "
            f"({len(events)} events); narrow the window for full fidelity."
            if truncated else None
        )
        return events, note
    except httpx.TimeoutException:
        # A timeout on this slow API for a wide window is expected — keep whatever pages we got
        # and tell the user to narrow the range, instead of failing the whole subscription.
        if events:
            return events, (
                f"Activity Log query was slow and timed out for this window — showing the "
                f"{len(events)} event(s) collected so far. Narrow the time range for full coverage."
            )
        return events, (
            "Activity Log query timed out — this window is too large for the subscription's "
            "volume. Narrow the time range (e.g. last 24 hours / 7 days) and try again."
        )
    except httpx.HTTPError as e:  # noqa: BLE001
        return events, f"ARM request error: {e}"


# ---------------------------------------------------------------- ARM data-plane REST helpers
# These mirror the `az monitor …` CLI reads but go straight to ARM REST with the connection's
# own token. They exist so NON-service-principal connections (pasted ARM token / managed
# identity) — which have NO ambient `az login` for their tenant — can still collect the same
# data a service-principal connection gets via the CLI. The returned JSON text is shaped to
# match the corresponding `az` command's stdout so existing parsers consume it unchanged.
_METRICS_API = "2024-02-01"
_METRIC_DEFS_API = "2024-02-01"
_DIAG_SETTINGS_API = "2021-05-01-preview"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def get_metrics(
    token: str,
    resource_id: str,
    *,
    metricnames: list[str],
    aggregations: list[str],
    interval: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    dimension_filter: str | None = None,
) -> tuple[str, str | None]:
    """Azure Monitor metrics for a resource via ARM REST. Returns ``(json_text, error)`` where
    ``json_text`` matches ``az monitor metrics list`` stdout — the full ``{value:[…]}`` object
    whose ``value[].timeseries[].data[]`` the metric parsers read."""
    if not resource_id:
        return "", "No resource id provided."
    if not metricnames:
        return "", "No metric name provided."
    params: dict[str, str] = {
        "api-version": _METRICS_API,
        "metricnames": ",".join(metricnames),
        "aggregation": ",".join(aggregations or ["Average"]),
    }
    if interval:
        params["interval"] = interval
    if start_time:
        # CLI ``--start-time`` is a start datetime to "now"; REST uses a start/end timespan.
        params["timespan"] = f"{start_time}/{end_time or _utc_now_iso()}"
    if dimension_filter:
        params["$filter"] = dimension_filter
    data, err = await _get(token, f"{resource_id}/providers/microsoft.insights/metrics", params)
    if err:
        return "", err
    return json.dumps(data or {}), None


async def get_metric_definitions(token: str, resource_id: str) -> tuple[str, str | None]:
    """Metric definitions catalog for a resource via ARM REST. Returns ``(json_text, error)``
    where ``json_text`` is the BARE LIST ``az monitor metrics list-definitions`` emits (the REST
    body is ``{value:[…]}`` — we unwrap ``value`` so the definitions parser, which requires a
    list, consumes it unchanged)."""
    if not resource_id:
        return "", "No resource id provided."
    data, err = await _get(
        token, f"{resource_id}/providers/microsoft.insights/metricDefinitions",
        {"api-version": _METRIC_DEFS_API},
    )
    if err:
        return "", err
    return json.dumps((data or {}).get("value", []) if isinstance(data, dict) else []), None


async def get_diagnostic_settings(token: str, resource_id: str) -> tuple[str, str | None]:
    """Diagnostic settings for a resource via ARM REST. Returns ``(json_text, error)`` shaped
    like ``az monitor diagnostic-settings list`` (the full ``{value:[…]}`` object)."""
    if not resource_id:
        return "", "No resource id provided."
    data, err = await _get(
        token, f"{resource_id}/providers/microsoft.insights/diagnosticSettings",
        {"api-version": _DIAG_SETTINGS_API},
    )
    if err:
        return "", err
    return json.dumps(data or {}), None


async def arm_rest(
    token: str, method: str, url: str, body: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Generic ARM REST call (the direct equivalent of ``az rest --method … --url … [--body]``
    when the url targets ``management.azure.com``). Returns ``(json_text, error)`` so callers
    that previously parsed ``az rest`` stdout consume it unchanged. Accepts 200/201/202 (LRO
    submit) and returns whatever body came back."""
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(method.upper(), url, headers=headers, json=body)
        if resp.status_code not in (200, 201, 202):
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            return "", f"ARM {resp.status_code}: {str(detail)[:300]}"
        try:
            return json.dumps(resp.json()), None
        except (ValueError, AttributeError):
            return resp.text or "{}", None
    except httpx.HTTPError as e:  # noqa: BLE001
        return "", f"ARM request error: {e}"


def is_arm_url(url: str) -> bool:
    """True when a url targets the Azure Resource Manager plane (so an ``az rest`` call against
    it can be served by the connection's ARM token)."""
    u = (url or "").lower()
    return u.startswith("https://management.azure.com/") or u.startswith("http://management.azure.com/")


async def arm_write(
    token: str, method: str, path: str, *, body: dict[str, Any] | None = None,
    api_version: str = "", query: dict[str, str] | None = None,
) -> tuple[Any, str | None, int]:
    """ARM REST mutation (PUT/PATCH/DELETE). Returns ``(json_or_none, error, status_code)``.

    Accepts the success range 200/201/202 (LRO submit) and **204** (no-content, common for
    DELETE). ``path`` is a management.azure.com-relative path; ``api_version`` is appended as a
    query param when provided. Surfaces the ARM error body so the UI can show why a write failed.
    Never raises."""
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    params = dict(query or {})
    if api_version:
        params["api-version"] = api_version
    try:
        async with httpx.AsyncClient(timeout=60, base_url=_ARM) as client:
            resp = await client.request(method.upper(), path, headers=headers, params=params or None, json=body)
        if resp.status_code not in (200, 201, 202, 204):
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            return None, f"ARM {resp.status_code}: {str(detail)[:400]}", resp.status_code
        if resp.status_code == 204 or not resp.content:
            return {}, None, resp.status_code
        try:
            return resp.json(), None, resp.status_code
        except (ValueError, AttributeError):
            return {}, None, resp.status_code
    except httpx.HTTPError as e:  # noqa: BLE001
        return None, f"ARM request error: {e}", 0

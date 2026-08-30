"""Best-effort Azure cost overlay for the inventory (FinOps lens). Queries Azure Cost
Management via the REST API (``az rest`` against Microsoft.CostManagement/query) for the
trailing-30-days actual cost grouped by resource, per subscription, and returns a resource-id
→ cost map. Uses ``az rest`` (built into az core) rather than the ``costmanagement``
extension, which isn't always installable. Degrades gracefully (empty result + reason) when
Cost Management isn't available or the connection lacks Cost Management Reader.

Read-only. Results are cached per tenant + connection because the query is slow.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

logger = logging.getLogger("app.inventory.cost")

_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "inventory_cost_cache.json"
_mem: dict[str, Any] | None = None

# Cost Management query: actual cost over the trailing 30 days, summed and grouped per
# ResourceId. The timeframe is a rolling window ending now, so it must be built per-call
# (see ``_query_body``) rather than a static constant.
_WINDOW_DAYS = 30
_API_VERSION = "2023-11-01"
# IP7 — query subscriptions with bounded concurrency instead of strictly sequential. Small
# enough to stay under Cost Management's aggressive throttling (each call also retries 429s
# with backoff), but parallel enough that wide tenants no longer load one-subscription-at-a-time.
_COST_CONCURRENCY = 4
# Hard cap on how many subscriptions we'll query for cost in one pass (each is a slow call).
_COST_MAX_SUBSCRIPTIONS = 25

# Interactive cost queries are cached separately from the Inventory Cost snapshot. The key
# includes the resolved period, cost type, grouping, filters, scope, and connection.
_QUERY_CACHE_TTL_SECONDS = 6 * 3600
_query_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_QUERY_GROUPS: dict[str, str] = {
    "resource": "ResourceId",
    "resource_group": "ResourceGroupName",
    "service": "ServiceName",
    "meter_category": "MeterCategory",
}
_QUERY_FILTERS: dict[str, str] = {
    "resource_id": "ResourceId",
    "resource_group": "ResourceGroupName",
    "service_name": "ServiceName",
}
_QUERY_COST_TYPES = frozenset({"ActualCost", "AmortizedCost"})
_QUERY_TIMEFRAMES = frozenset({"last_7_days", "last_30_days", "current_month", "previous_month", "custom"})


def _window() -> tuple[datetime, datetime]:
    """The rolling cost window: (from, to) = (now - 30 days, now), UTC."""
    now = datetime.now(timezone.utc)
    return now - timedelta(days=_WINDOW_DAYS), now


def _query_body() -> dict[str, Any]:
    start, end = _window()
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start.strftime("%Y-%m-%dT00:00:00+00:00"),
            "to": end.strftime("%Y-%m-%dT23:59:59+00:00"),
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceId"}],
        },
    }


def _period_label() -> str:
    start, end = _window()
    return f"{start.strftime('%b %d')} – {end.strftime('%b %d')}"


def resolve_query_period(
    timeframe: str,
    start_date: str = "",
    end_date: str = "",
    *,
    today: date | None = None,
) -> dict[str, str]:
    """Resolve an interactive query period to inclusive UTC calendar dates."""
    current = today or datetime.now(timezone.utc).date()
    if timeframe not in _QUERY_TIMEFRAMES:
        raise ValueError("Unsupported cost timeframe.")
    selected = timeframe
    if selected == "last_7_days":
        start, end = current - timedelta(days=6), current
    elif selected == "last_30_days":
        start, end = current - timedelta(days=29), current
    elif selected == "current_month":
        start, end = current.replace(day=1), current
    elif selected == "previous_month":
        end = current.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    else:
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError("Custom cost queries require ISO dates (YYYY-MM-DD).") from exc
        if start > end:
            raise ValueError("The cost query start date must not be after the end date.")
        if end > current:
            raise ValueError("The cost query end date must not be in the future.")
        if (end - start).days > 365:
            raise ValueError("Custom cost queries are limited to 366 inclusive days.")
    return {
        "timeframe": selected,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "from": f"{start.isoformat()}T00:00:00+00:00",
        "to": f"{end.isoformat()}T23:59:59+00:00",
        "label": f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}",
    }


def build_cost_query_body(
    period: dict[str, str],
    *,
    cost_type: str = "ActualCost",
    group_by: str = "resource",
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a bounded Cost Management Query request from validated options."""
    if cost_type not in _QUERY_COST_TYPES:
        raise ValueError("cost_type must be ActualCost or AmortizedCost.")
    if group_by not in {*_QUERY_GROUPS, "subscription", "day"}:
        raise ValueError("Unsupported cost grouping.")
    dataset: dict[str, Any] = {
        "granularity": "Daily" if group_by == "day" else "None",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
    }
    if group_by in _QUERY_GROUPS:
        dataset["grouping"] = [{"type": "Dimension", "name": _QUERY_GROUPS[group_by]}]
    clauses = [
        {"dimensions": {"name": _QUERY_FILTERS[key], "operator": "In", "values": [value]}}
        for key, value in sorted((filters or {}).items())
        if key in _QUERY_FILTERS and value
    ]
    if len(clauses) == 1:
        dataset["filter"] = clauses[0]
    elif clauses:
        dataset["filter"] = {"and": clauses}
    return {
        "type": cost_type,
        "timeframe": "Custom",
        "timePeriod": {"from": period["from"], "to": period["to"]},
        "dataset": dataset,
    }


def _query_cache_key(
    tenant_id: str,
    connection_id: str,
    subscriptions: list[str],
    period: dict[str, str],
    cost_type: str,
    group_by: str,
    filters: dict[str, str],
) -> str:
    return json.dumps(
        {
            "tenant": tenant_id,
            "connection": connection_id,
            "subscriptions": sorted({s.lower() for s in subscriptions}),
            "start": period["start_date"],
            "end": period["end_date"],
            "cost_type": cost_type,
            "group_by": group_by,
            "filters": {k: filters[k] for k in sorted(filters)},
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def _subscription_breakdown(
    token: str,
    subscription_id: str,
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    """Run one interactive Cost Management query with bounded 429/5xx retry."""
    from app.azure.arm import arm_rest

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query?api-version={_API_VERSION}"
    )
    text, error = "", ""
    for attempt in range(4):
        text, error = await arm_rest(token, "POST", url, body)
        if not error:
            break
        retryable = any(code in error for code in ("429", "500", "502", "503", "504"))
        if not retryable or attempt == 3:
            break
        await asyncio.sleep(2 + attempt * 4)
    if error:
        if "429" in error:
            error = "Azure Cost Management remained rate-limited after four attempts."
        return [], error[:300]
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return [], "Azure Cost Management returned an unreadable response."
    properties = data.get("properties", data)
    columns = properties.get("columns") or []
    rows = properties.get("rows") or []
    return [
        {str(column.get("name") or index): row[index] if index < len(row) else None
         for index, column in enumerate(columns)}
        for row in rows if isinstance(row, list)
    ], ""


def _day_value(value: Any) -> str:
    raw = str(value or "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw[:10] if len(raw) >= 10 else raw


async def query_cost_breakdown(
    connection: dict[str, Any] | None,
    subscriptions: list[str],
    tenant_id: str,
    connection_id: str,
    *,
    timeframe: str = "last_7_days",
    start_date: str = "",
    end_date: str = "",
    cost_type: str = "ActualCost",
    group_by: str = "resource",
    filters: dict[str, str] | None = None,
    top: int = 20,
    force: bool = False,
) -> dict[str, Any]:
    """Query actual Azure spend for chat, with currency-safe aggregation and bounded output."""
    from app.azure.credentials import get_arm_token

    period = resolve_query_period(timeframe, start_date, end_date)
    normalized_filters = {
        key: str(value).strip()
        for key, value in (filters or {}).items()
        if key in _QUERY_FILTERS and str(value).strip()
    }
    body = build_cost_query_body(
        period, cost_type=cost_type, group_by=group_by, filters=normalized_filters,
    )
    targets = list(dict.fromkeys(str(s).strip() for s in subscriptions if str(s).strip()))
    omitted = targets[_COST_MAX_SUBSCRIPTIONS:]
    targets = targets[:_COST_MAX_SUBSCRIPTIONS]
    if not targets:
        return {
            "available": False, "reason": "No visible subscription is in the selected scope.",
            "rows": [], "period": period, "cost_type": cost_type, "group_by": group_by,
        }
    cache_key = _query_cache_key(
        tenant_id, connection_id, targets, period, cost_type, group_by, normalized_filters,
    )
    hit = _query_cache.get(cache_key)
    if hit and not force and time.time() - hit[0] < _QUERY_CACHE_TTL_SECONDS:
        return {**hit[1], "cached": True}

    token, token_error = await get_arm_token(connection or {})
    if not token:
        return {
            "available": False,
            "reason": (token_error or "No Azure token is available for this connection.")[:300],
            "rows": [], "period": period, "cost_type": cost_type, "group_by": group_by,
        }

    semaphore = asyncio.Semaphore(_COST_CONCURRENCY)

    async def run_one(subscription_id: str) -> tuple[str, list[dict[str, Any]], str]:
        async with semaphore:
            rows, error = await _subscription_breakdown(token, subscription_id, body)
            return subscription_id, rows, error

    results = await asyncio.gather(*(run_one(s) for s in targets))
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    succeeded = 0
    dimension = _QUERY_GROUPS.get(group_by, "")
    for subscription_id, raw_rows, error in results:
        if error:
            errors.append({"subscription_id": subscription_id, "error": error})
            continue
        succeeded += 1
        for raw in raw_rows:
            by_lower = {str(k).lower(): value for k, value in raw.items()}
            amount_value = next(
                (by_lower.get(name) for name in ("cost", "pretaxcost", "costusd", "pretaxcostusd")
                 if by_lower.get(name) is not None),
                None,
            )
            try:
                amount = float(amount_value)
            except (TypeError, ValueError):
                continue
            currency = str(by_lower.get("currency") or "UNKNOWN").upper()
            if group_by == "subscription":
                bucket = subscription_id
            elif group_by == "day":
                bucket = _day_value(by_lower.get("usagedate") or by_lower.get("date")) or "unknown"
            else:
                bucket = str(by_lower.get(dimension.lower()) or "unattributed")
            key = (currency, bucket.lower())
            current = grouped.setdefault(
                key, {"dimension": bucket, "cost": 0.0, "currency": currency},
            )
            current["cost"] += amount

    all_rows = [
        {**row, "cost": round(float(row["cost"]), 2)} for row in grouped.values()
    ]
    if group_by == "day":
        all_rows.sort(key=lambda row: str(row["dimension"]))
    else:
        all_rows.sort(key=lambda row: (-float(row["cost"]), str(row["dimension"]).lower()))
    totals: dict[str, float] = {}
    for row in all_rows:
        currency = str(row["currency"])
        totals[currency] = round(totals.get(currency, 0.0) + float(row["cost"]), 2)
    currencies = sorted(totals)
    limit = max(1, min(int(top or 20), 100))
    payload = {
        "available": succeeded > 0,
        "data_kind": "actual_azure_cost",
        "cost_type": cost_type,
        "period": period,
        "group_by": group_by,
        "filters": normalized_filters,
        "rows": all_rows[:limit],
        "returned_rows": min(len(all_rows), limit),
        "total_rows": len(all_rows),
        "truncated": len(all_rows) > limit,
        "totals_by_currency": totals,
        "total": next(iter(totals.values())) if len(totals) == 1 else None,
        "currency": currencies[0] if len(currencies) == 1 else "",
        "currency_note": (
            "Costs use multiple currencies and are not combined into one total."
            if len(currencies) > 1 else ""
        ),
        "subscriptions_queried": len(targets),
        "subscriptions_succeeded": succeeded,
        "subscriptions_failed": len(errors),
        "subscriptions_omitted": len(omitted),
        "errors": errors,
        "cached": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "freshness_note": "Cost Management usage commonly lags actual activity by 8–24 hours.",
    }
    if succeeded and not errors:
        _query_cache[cache_key] = (time.time(), payload)
    return payload


def _load() -> dict[str, Any]:
    global _mem
    loaded = jsonstore.read_json(_CACHE_PATH, {})
    _mem = loaded if isinstance(loaded, dict) else {}
    return _mem or {}


def _key(tenant_id: str, connection_id: str, scope: str = "") -> str:
    # Permanent cache keyed by tenant + connection + scope (NOT month): cost data is captured
    # once and persists indefinitely until the user clicks Refresh. The capture month lives
    # inside the payload so the UI can show which month the figures cover. An empty scope
    # reuses the legacy tenant|connection key so pre-scope cost stays cached; multi-token
    # scopes are canonicalized (sorted/deduped) so order never splits the cache.
    base = f"{tenant_id or ''}|{connection_id or ''}"
    norm = ",".join(sorted({t.strip() for t in (scope or "").split(",") if t.strip()}))
    return f"{base}|{norm}" if norm else base


def _col_index(columns: list[dict[str, Any]], *names: str) -> int:
    lowered = [str(c.get("name", "")).lower() for c in columns]
    for n in names:
        if n.lower() in lowered:
            return lowered.index(n.lower())
    return -1


async def _subscription_cost(
    connection: dict[str, Any] | None,
    sub_id: str,
    body: dict[str, Any],
    *,
    progress=None,
) -> tuple[dict[str, float], str, str]:
    """Trailing-30-days actual cost per resource for one subscription, via the Cost Management
    REST API. Returns (cost_by_resource_id_lower, currency, error).

    Uses ARM REST with the connection's own token (``get_arm_token``) so it works for EVERY
    connection type — service principal, pasted ARM token, and managed identity — not just
    those with an ambient ``az`` login."""
    from app.azure.arm import arm_rest
    from app.azure.credentials import get_arm_token

    token, terr = await get_arm_token(connection or {})
    if not token:
        return {}, "", (terr or "No Azure token for this connection.")[:200]
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/providers/Microsoft.CostManagement/query?api-version={_API_VERSION}"
    )
    # The Cost Management query API is aggressively throttled (429); retry with backoff.
    text, err = "", ""
    for attempt in range(4):
        text, err = await arm_rest(token, "POST", url, body)
        if not err:
            break
        if "429" in err or "Too Many Requests" in err or "throttl" in err.lower():
            delay = 2 + attempt * 4  # 2s, 6s, 10s
            await _notify(
                progress,
                {
                    "type": "subscription_retry",
                    "subscription_id": sub_id,
                    "attempt": attempt + 2,
                    "max_attempts": 4,
                    "delay_seconds": delay,
                    "message": "Azure Cost Management rate-limited this subscription; backing off before retry.",
                },
            )
            await asyncio.sleep(delay)
            continue
        break
    if err:
        msg = err.strip()
        if "429" in msg or "Too Many Requests" in msg:
            msg = "Azure Cost Management is rate-limiting requests right now — try again in a minute."
        return {}, "", msg[:200]
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}, "", "Could not parse cost output."
    props = data.get("properties", data)
    columns = props.get("columns") or []
    rows = props.get("rows") or []
    ci_cost = _col_index(columns, "Cost", "PreTaxCost", "CostUSD", "PreTaxCostUSD")
    ci_res = _col_index(columns, "ResourceId")
    ci_cur = _col_index(columns, "Currency")
    if ci_cost < 0 or ci_res < 0:
        return {}, "", "Unexpected cost result shape."
    out: dict[str, float] = {}
    currency = ""
    for row in rows:
        try:
            rid = str(row[ci_res]).lower()
            amount = float(row[ci_cost])
        except (IndexError, ValueError, TypeError):
            continue
        if ci_cur >= 0 and not currency:
            currency = str(row[ci_cur])
        out[rid] = out.get(rid, 0.0) + amount
    return out, currency, ""


async def _notify(progress, event: dict[str, Any]) -> None:
    """Invoke an optional sync/async progress callback without coupling collection to SSE."""
    if progress is None:
        return
    result = progress(event)
    if inspect.isawaitable(result):
        await result


def peek_cost(tenant_id: str, connection_id: str, scope: str = "") -> dict[str, Any] | None:
    """Return the permanently-cached cost payload if one exists, WITHOUT ever running the slow
    Cost Management query. Used to auto-restore cached cost on a fresh page load."""
    hit = _load().get(_key(tenant_id, connection_id, scope))
    return {**hit["payload"], "cached": True} if hit else None


async def get_cost(
    connection: dict[str, Any] | None,
    subscriptions: list[str],
    tenant_id: str,
    connection_id: str,
    *,
    force: bool = False,
    scope: str = "",
    progress=None,
) -> dict[str, Any]:
    """Aggregate trailing-30-days cost across the given subscriptions, attributed per resource.

    Returns {available, currency, period, by_resource: {id: cost}, by_subscription: {...},
    total, errors, cached, fetched_at}.

    ``scope`` keys the permanent cache so each Azure scope (tenant / management group /
    subscription) caches its cost independently. The result is cached PERMANENTLY (no TTL) —
    a cached payload is returned indefinitely and only recomputed when ``force=True`` (the
    explicit Refresh button), so the (slow, throttled) Cost Management queries run only when
    the user asks for fresh numbers."""
    cache = _load()
    ck = _key(tenant_id, connection_id, scope)
    if not force:
        hit = cache.get(ck)
        if hit:
            return {**hit["payload"], "cached": True}

    by_resource: dict[str, float] = {}
    by_subscription: dict[str, float] = {}
    errors: list[str] = []
    currency = ""

    # The Cost Management query body is identical per subscription.
    body = _query_body()
    targets = subscriptions[:_COST_MAX_SUBSCRIPTIONS]
    await _notify(
        progress,
        {
            "type": "started",
            "subscriptions_total": len(targets),
            "subscriptions_visible": len(subscriptions),
            "subscriptions_omitted": max(0, len(subscriptions) - len(targets)),
            "concurrency": _COST_CONCURRENCY,
            "period": _period_label(),
            "message": f"Preparing Cost Management queries for {len(targets)} subscription(s).",
        },
    )
    # IP7 — fan the (slow, throttled) per-subscription cost queries out with bounded concurrency.
    # A semaphore caps simultaneous calls and a small per-slot stagger avoids hitting the API in
    # lockstep; the 429 retry/backoff lives inside ``_subscription_cost``.
    sem = asyncio.Semaphore(_COST_CONCURRENCY)
    progress_lock = asyncio.Lock()
    completed = 0

    async def _one(idx: int, sub: str) -> tuple[str, dict[str, float], str, str]:
        nonlocal completed
        async with sem:
            if idx % _COST_CONCURRENCY:
                await asyncio.sleep((idx % _COST_CONCURRENCY) * 0.25)
            started = time.monotonic()
            await _notify(
                progress,
                {
                    "type": "subscription_started",
                    "subscription_id": sub,
                    "index": idx + 1,
                    "subscriptions_total": len(targets),
                    "message": f"Querying subscription {idx + 1} of {len(targets)}.",
                },
            )
            if progress is None:
                costs, cur, err = await _subscription_cost(connection, sub, body)
            else:
                costs, cur, err = await _subscription_cost(
                    connection, sub, body, progress=progress
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            async with progress_lock:
                completed += 1
                done_count = completed
            await _notify(
                progress,
                {
                    "type": "subscription_error" if err else "subscription_done",
                    "subscription_id": sub,
                    "index": idx + 1,
                    "subscriptions_total": len(targets),
                    "subscriptions_done": done_count,
                    "resource_cost_rows": len(costs),
                    "subscription_total": round(sum(costs.values()), 2),
                    "currency": cur,
                    "duration_ms": duration_ms,
                    "error": err,
                    "message": (
                        f"Subscription {idx + 1} failed: {err}"
                        if err
                        else f"Subscription {idx + 1} complete ({len(costs)} cost row(s))."
                    ),
                },
            )
            return sub, costs, cur, err

    results = await asyncio.gather(*[_one(i, sub) for i, sub in enumerate(targets)])
    await _notify(
        progress,
        {
            "type": "aggregating",
            "subscriptions_done": len(results),
            "subscriptions_total": len(targets),
            "message": "Combining subscription results and reconciling resource costs.",
        },
    )
    for sub, costs, cur, err in results:
        if err:
            errors.append(f"{sub[:8]}…: {err}")
            continue
        if cur and not currency:
            currency = cur
        sub_total = 0.0
        for rid, amount in costs.items():
            by_resource[rid] = by_resource.get(rid, 0.0) + amount
            sub_total += amount
        by_subscription[sub] = round(sub_total, 2)

    available = bool(by_resource)
    payload = {
        "available": available,
        "currency": currency or "USD",
        "period": _period_label(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "by_resource": {k: round(v, 2) for k, v in by_resource.items()},
        "by_subscription": by_subscription,
        "total": round(sum(by_resource.values()), 2),
        "errors": errors,
    }
    # Only cache a COMPLETE result (every subscription queried) — a partial result (some subs
    # throttled with 429) is returned to the user but not persisted, so the next Refresh
    # retries the missing subscriptions instead of persisting stale partial data. Once cached,
    # the payload is kept indefinitely (no TTL) until the user force-refreshes.
    if available and not errors:
        global _mem
        entry = {"payload": payload, "ts": time.time()}

        def _mutate(stored: dict[str, Any]) -> None:
            stored[ck] = entry

        try:
            _mem = jsonstore.mutate_json(_CACHE_PATH, {}, _mutate, indent=None)
        except OSError:
            pass
    result = {**payload, "cached": False}
    await _notify(
        progress,
        {
            "type": "done",
            "subscriptions_done": len(results),
            "subscriptions_total": len(targets),
            "subscriptions_succeeded": len(results) - len(errors),
            "subscriptions_failed": len(errors),
            "resource_cost_rows": len(by_resource),
            "total": payload["total"],
            "currency": payload["currency"],
            "cached": bool(available and not errors),
            "message": (
                f"Cost refresh completed with {len(errors)} subscription error(s)."
                if errors
                else "Cost refresh completed and the shared cache was updated."
            ),
        },
    )
    return result


def build_rollup(cost_payload: dict[str, Any], resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Join a (permanent-cached) cost payload onto the inventory resource list and roll the
    per-resource trailing-30-days cost up by workload, resource type, region, subscription, and
    resource group, plus the most expensive resources. Pure aggregation over cached data —
    no Azure calls.

    Multi-workload attribution: a resource that belongs to N workloads has its cost SPLIT
    EVENLY across them (cost / N), so the per-workload totals reconcile to the grand total
    rather than double-counting shared resources.
    """
    by_resource: dict[str, float] = cost_payload.get("by_resource") or {}
    currency = cost_payload.get("currency") or "USD"

    by_workload: dict[str, float] = {}
    wl_resource_count: dict[str, int] = {}
    wl_name: dict[str, str] = {}
    by_type: dict[str, float] = {}
    by_location: dict[str, float] = {}
    by_subscription: dict[str, float] = {}
    by_resource_group: dict[str, float] = {}
    unassigned_cost = 0.0
    top: list[dict[str, Any]] = []
    attributed_total = 0.0

    for r in resources:
        rid = (r.get("id") or "").lower()
        amount = by_resource.get(rid)
        if amount is None:  # no cost row for this resource (a genuine $0.00 row is kept)
            continue
        attributed_total += amount
        rtype = r.get("type") or "unknown"
        loc = r.get("location") or "unknown"
        sub = r.get("subscription_id") or "unknown"
        rg = r.get("resource_group") or "unknown"
        by_type[rtype] = by_type.get(rtype, 0.0) + amount
        by_location[loc] = by_location.get(loc, 0.0) + amount
        by_subscription[sub] = by_subscription.get(sub, 0.0) + amount
        by_resource_group[rg] = by_resource_group.get(rg, 0.0) + amount

        wls = r.get("workloads") or []
        if wls:
            share = amount / len(wls)
            for w in wls:
                wid = w.get("id", "")
                wl_name[wid] = w.get("name", wid)
                by_workload[wid] = by_workload.get(wid, 0.0) + share
                wl_resource_count[wid] = wl_resource_count.get(wid, 0) + 1
        else:
            unassigned_cost += amount

        top.append({
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "type": rtype,
            "location": loc,
            "subscription_id": sub,
            "resource_group": rg,
            "workloads": [w.get("name", "") for w in wls],
            "cost": round(amount, 2),
        })

    total = round(attributed_total, 2) or round(cost_payload.get("total", 0.0), 2)

    def _pct(v: float) -> float:
        return round((v / total) * 100, 1) if total else 0.0

    def _rank(d: dict[str, float]) -> list[dict[str, Any]]:
        return [
            {"key": k, "cost": round(v, 2), "pct": _pct(v)}
            for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    workloads = [
        {
            "id": wid,
            "name": wl_name.get(wid, wid),
            "cost": round(c, 2),
            "pct": _pct(c),
            "resource_count": wl_resource_count.get(wid, 0),
        }
        for wid, c in sorted(by_workload.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    top.sort(key=lambda x: x["cost"], reverse=True)

    return {
        "available": bool(by_resource),
        "currency": currency,
        "period": cost_payload.get("period", ""),
        "fetched_at": cost_payload.get("fetched_at", ""),
        "cached": cost_payload.get("cached", False),
        "total": total,
        "attributed_total": round(attributed_total, 2),
        "unattributed_total": round(round(cost_payload.get("total", 0.0), 2) - attributed_total, 2),
        "unassigned_cost": round(unassigned_cost, 2),
        "by_workload": workloads,
        "by_type": _rank(by_type),
        "by_location": _rank(by_location),
        "by_subscription": _rank(by_subscription),
        "by_resource_group": _rank(by_resource_group),
        "top_resources": top[:20],
        "errors": cost_payload.get("errors", []),
    }

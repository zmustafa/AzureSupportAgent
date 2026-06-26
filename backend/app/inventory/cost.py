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
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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


def _load() -> dict[str, Any]:
    global _mem
    if _mem is None:
        if _CACHE_PATH.exists():
            try:
                _mem = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _mem = {}
        else:
            _mem = {}
    return _mem or {}


def _persist() -> None:
    if _mem is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_mem), encoding="utf-8")
    except OSError:
        pass


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
    connection: dict[str, Any] | None, sub_id: str, body: dict[str, Any]
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
            await asyncio.sleep(2 + attempt * 4)  # 2s, 6s, 10s
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
    # IP7 — fan the (slow, throttled) per-subscription cost queries out with bounded concurrency.
    # A semaphore caps simultaneous calls and a small per-slot stagger avoids hitting the API in
    # lockstep; the 429 retry/backoff lives inside ``_subscription_cost``.
    sem = asyncio.Semaphore(_COST_CONCURRENCY)

    async def _one(idx: int, sub: str) -> tuple[str, dict[str, float], str, str]:
        async with sem:
            if idx % _COST_CONCURRENCY:
                await asyncio.sleep((idx % _COST_CONCURRENCY) * 0.25)
            costs, cur, err = await _subscription_cost(connection, sub, body)
            return sub, costs, cur, err

    results = await asyncio.gather(*[_one(i, sub) for i, sub in enumerate(targets)])
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
        cache[ck] = {"payload": payload, "ts": time.time()}
        global _mem
        _mem = cache
        _persist()
    return {**payload, "cached": False}


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

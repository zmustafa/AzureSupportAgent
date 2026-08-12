"""Azure Cost Management — authoritative actual spend for Backup and Site Recovery.

Uses the same ARM token the rest of the module already holds (unlike Log Analytics, which
needs a separate audience), so no new credential type is required.

Three facts, all established by probing the live API rather than assumed:

* **Charges are attributed to the vault, not the protected item.** A subscription with one
  Azure Files backup returned a single row whose ``ResourceId`` was the Recovery Services
  vault; zero rows matched a protected-item id. Per-item cost therefore has to be *allocated*
  from vault totals (see :func:`app.backup_manager.cost.allocate`), and this module never
  pretends otherwise.
* **``timeframe: "TheLastMonth"`` is rejected** at subscription scope ("currently not
  supported"), so every query uses an explicit ``Custom`` period.
* **Currency comes from the response**, not from configuration. The probed tenant bills in
  EUR while the seeded rate table was USD. The returned ``Currency`` column is authoritative
  and is never converted.

Results are cached on disk: Cost Management is heavily throttled and its data only refreshes
once or twice a day, so the module's short-lived in-memory inventory cache is the wrong tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.backup_manager import service

log = logging.getLogger("app.backup_manager.costmgmt")

COST_API = "2024-08-01"
_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "backup_manager_costs.json"
# Usage data lags 8-24h and refreshes at most daily; re-querying more often only burns quota.
CACHE_TTL_SECONDS = 6 * 3600
MAX_RETRIES = 4
# Cost Management is slow (seconds per query); bound the fan-out across subscriptions.
QUERY_CONCURRENCY = 3

# Service names carrying backup/DR spend. Discovered at runtime where possible (`ServiceName`
# tokens vary by offer type); these are the fallback and the discovery seed.
DEFAULT_SERVICE_NAMES = ("Backup", "Azure Site Recovery", "Backup and Site Recovery")
_SERVICE_HINTS = ("backup", "site recovery")


def month_period(months_back: int = 0) -> dict[str, str]:
    """A complete calendar month, or the current month to date when ``months_back`` is 0.

    A month-to-date figure must never be compared against a full-month estimate — the caller
    gets ``partial`` in the result so the UI can say which it is.
    """
    now = datetime.now(timezone.utc)
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if months_back <= 0:
        return {
            "from": first_this_month.strftime("%Y-%m-%dT00:00:00+00:00"),
            "to": now.strftime("%Y-%m-%dT23:59:59+00:00"),
            "partial": "true",
        }
    end = first_this_month
    for _ in range(months_back - 1):
        end = (end - timedelta(days=1)).replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    return {
        "from": start.strftime("%Y-%m-%dT00:00:00+00:00"),
        "to": (end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "partial": "false",
    }


# --------------------------------------------------------------------------- transport
async def _query(
    token: str, scope: str, body: dict[str, Any],
) -> tuple[list[str], list[list[Any]], str]:
    """One Cost Management query with bounded retry. Returns ``(columns, rows, error)``."""
    delay = 2.0
    for attempt in range(MAX_RETRIES + 1):
        submission = await service.arm_submit(
            token, "POST", f"{scope}/providers/Microsoft.CostManagement/query",
            body=body, api_version=COST_API,
        )
        if submission.ok:
            props = service.as_dict((submission.body or {}).get("properties"))
            columns = [str(service.as_dict(c).get("name") or "") for c in service.as_list(props.get("columns"))]
            rows = [r for r in service.as_list(props.get("rows")) if isinstance(r, list)]
            return columns, rows, ""
        # 429 (throttled) and 5xx are worth retrying; 401/403/400 are not.
        if submission.status in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            wait = submission.retry_after or delay
            await asyncio.sleep(min(60.0, wait + random.uniform(0, 0.5)))
            delay *= 2
            continue
        return [], [], submission.error or "Cost Management query failed."
    return [], [], "Cost Management remained throttled after retries."


async def list_service_names(connection: dict[str, Any], subscription_id: str) -> tuple[list[str], str]:
    """Backup/DR service-name tokens that actually carry spend in this subscription.

    Discovered rather than hardcoded because the tokens differ between offer types."""
    try:
        token = await service.token_for(connection)
    except (ValueError, KeyError) as exc:  # noqa: BLE001
        return [], service.safe_error(str(exc))
    period = month_period(1)
    columns, rows, error = await _query(
        token, f"/subscriptions/{subscription_id}",
        {
            "type": "ActualCost", "timeframe": "Custom",
            "timePeriod": {"from": period["from"], "to": period["to"]},
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [{"type": "Dimension", "name": "ServiceName"}],
            },
        },
    )
    if error:
        return [], error
    try:
        index = columns.index("ServiceName")
    except ValueError:
        return [], "Cost Management did not return a ServiceName column."
    found = [
        str(row[index]) for row in rows
        if any(hint in str(row[index]).lower() for hint in _SERVICE_HINTS)
    ]
    return sorted(set(found)), ""


# --------------------------------------------------------------------------- collection
async def backup_actuals(
    connection: dict[str, Any],
    subscriptions: list[str],
    *,
    months_back: int = 1,
    cost_type: str = "AmortizedCost",
    service_names: tuple[str, ...] = DEFAULT_SERVICE_NAMES,
    daily: bool = False,
) -> dict[str, Any]:
    """Actual backup/DR spend for a set of subscriptions, grouped by vault and meter.

    ``AmortizedCost`` is the default because Azure Backup Storage reserved capacity would
    otherwise surface as a lumpy one-off purchase rather than the monthly rate it represents.
    """
    period = month_period(months_back)
    empty = {
        "available": False, "rows": [], "by_vault": {}, "by_meter": {}, "daily": [],
        "currency": "", "total": 0.0, "period": period, "cost_type": cost_type,
        "subscriptions": list(subscriptions), "partial_period": period["partial"] == "true",
        "reason": "", "remedy": "",
    }
    if not subscriptions:
        empty["reason"] = "No subscription is in scope for a cost query."
        return empty
    try:
        token = await service.token_for(connection)
    except (ValueError, KeyError) as exc:  # noqa: BLE001
        empty["reason"] = service.safe_error(str(exc))
        empty["remedy"] = "Use a service-principal or managed-identity connection."
        return empty

    grouping: list[dict[str, str]] = [
        {"type": "Dimension", "name": "ResourceId"},
        {"type": "Dimension", "name": "Meter"},
        {"type": "Dimension", "name": "MeterSubCategory"},
    ]
    body = {
        "type": cost_type,
        "timeframe": "Custom",
        "timePeriod": {"from": period["from"], "to": period["to"]},
        "dataset": {
            "granularity": "Daily" if daily else "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": grouping,
            "filter": {"dimensions": {
                "name": "ServiceName", "operator": "In", "values": list(service_names),
            }},
        },
    }

    semaphore = asyncio.Semaphore(QUERY_CONCURRENCY)

    async def run(subscription_id: str) -> tuple[str, list[str], list[list[Any]], str]:
        async with semaphore:
            columns, rows, error = await _query(token, f"/subscriptions/{subscription_id}", body)
            return subscription_id, columns, rows, error

    results = await asyncio.gather(*(run(s) for s in subscriptions), return_exceptions=True)

    all_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    succeeded_subscriptions = 0
    currencies: set[str] = set()
    for result in results:
        if isinstance(result, BaseException):
            errors.append(service.safe_error(str(result)))
            continue
        subscription_id, columns, rows, error = result
        if error:
            errors.append(f"{subscription_id}: {error}")
            continue
        succeeded_subscriptions += 1
        index = {name: position for position, name in enumerate(columns)}
        for row in rows:
            def cell(name: str) -> Any:
                position = index.get(name)
                return row[position] if position is not None and position < len(row) else None

            try:
                cost = float(cell("Cost") or 0.0)
            except (TypeError, ValueError):
                continue
            row_currency = str(cell("Currency") or "")
            if row_currency:
                currencies.add(row_currency)
            all_rows.append({
                "subscription_id": subscription_id,
                "resource_id": str(cell("ResourceId") or ""),
                "meter": str(cell("Meter") or ""),
                "meter_subcategory": str(cell("MeterSubCategory") or ""),
                "date": str(cell("UsageDate") or ""),
                "cost": cost,
                "currency": row_currency,
            })

    by_vault: dict[str, float] = {}
    by_meter: dict[str, float] = {}
    by_day: dict[str, float] = {}
    totals_by_currency: dict[str, float] = {}
    for row in all_rows:
        key = service.canonical_id(row["resource_id"])
        by_vault[key] = by_vault.get(key, 0.0) + row["cost"]
        by_meter[row["meter"]] = by_meter.get(row["meter"], 0.0) + row["cost"]
        if row["date"]:
            by_day[row["date"]] = by_day.get(row["date"], 0.0) + row["cost"]
        code = str(row.get("currency") or "")
        totals_by_currency[code] = totals_by_currency.get(code, 0.0) + row["cost"]

    available = bool(all_rows) or not errors
    currency = next(iter(currencies)) if len(currencies) == 1 else ""
    mixed_currency = len(currencies) > 1
    return {
        "available": available,
        "rows": all_rows,
        "by_vault": {} if mixed_currency else by_vault,
        "by_meter": {} if mixed_currency else dict(sorted(by_meter.items(), key=lambda kv: -kv[1])),
        "daily": [] if mixed_currency else [{"date": d, "cost": round(c, 4)} for d, c in sorted(by_day.items())],
        "currency": currency,
        "currencies": sorted(currencies),
        "mixed_currency": mixed_currency,
        "totals_by_currency": {code: round(value, 2) for code, value in sorted(totals_by_currency.items())},
        "total": round(sum(by_vault.values()), 2) if not mixed_currency else 0.0,
        "period": period,
        "partial_period": period["partial"] == "true",
        "cost_type": cost_type,
        "subscriptions": list(subscriptions),
        "subscriptions_succeeded": succeeded_subscriptions,
        "subscriptions_failed": len(errors),
        "partial": bool(errors),
        "reason": (
            "Multiple billing currencies are present; totals are reported separately and are not summed."
            if mixed_currency else "; ".join(errors)[:800] if errors
            else ("" if all_rows else "No backup or Site Recovery charges in this period.")
        ),
        "remedy": (
            "Grant the connection Cost Management Reader on the subscription, and confirm the "
            "tenant allows non-billing readers to view charges."
        ) if errors else "",
    }


# --------------------------------------------------------------------------- cache
def _read_cache() -> dict[str, Any]:
    if _CACHE_PATH.exists():
        try:
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_cache(data: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Raw per-row detail is large and not needed once aggregated; keep the cache lean.
        _CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log.debug("backup_manager: could not persist cost cache: %s", exc)


async def cached_actuals(
    connection: dict[str, Any],
    subscriptions: list[str],
    *,
    tenant_id: str = "",
    months_back: int = 1,
    cost_type: str = "AmortizedCost",
    daily: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Disk-cached :func:`backup_actuals`, keyed by tenant/connection/scope/period."""
    key = "|".join([
        tenant_id or "default",
        str(connection.get("id") or "default"),
        ",".join(sorted(subscriptions)),
        str(months_back), cost_type, str(daily),
    ])
    cache = _read_cache()
    entry = cache.get(key) if isinstance(cache.get(key), dict) else None
    if entry and not force:
        age = time.time() - float(entry.get("cached_at") or 0)
        if age < CACHE_TTL_SECONDS and isinstance(entry.get("result"), dict):
            result = dict(entry["result"])
            result["cache_age_seconds"] = int(age)
            return result

    result = await backup_actuals(
        connection, subscriptions, months_back=months_back, cost_type=cost_type, daily=daily,
    )
    if result.get("available"):
        lean = {k: v for k, v in result.items() if k != "rows"}
        cache[key] = {"cached_at": time.time(), "result": lean}
        # Bound the cache so a large multi-scope estate cannot grow it without limit.
        if len(cache) > 64:
            for stale_key in sorted(cache, key=lambda k: cache[k].get("cached_at", 0))[:-64]:
                cache.pop(stale_key, None)
        _write_cache(cache)
    result["cache_age_seconds"] = 0
    return result


def known_currency(connection: dict[str, Any], *, tenant_id: str = "") -> str:
    """The tenant's billing currency if any cached actuals already revealed it.

    Costs nothing: reads the existing disk cache only. Lets callers that must not pay for a
    Cost Management round-trip — the overview scorecard — still quote list prices in the
    currency the customer is actually invoiced in, instead of a seeded default that would
    silently disagree with the Cost tab.
    """
    prefix = f"{tenant_id or 'default'}|{connection.get('id') or 'default'}|"
    newest, currency = 0.0, ""
    for key, entry in _read_cache().items():
        if not key.startswith(prefix) or not isinstance(entry, dict):
            continue
        cached_at = float(entry.get("cached_at") or 0)
        found = str((entry.get("result") or {}).get("currency") or "")
        if found and cached_at > newest:
            newest, currency = cached_at, found
    return currency

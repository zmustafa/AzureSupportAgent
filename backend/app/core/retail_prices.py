"""Bounded client for the public Azure Retail Prices API.

The API is unauthenticated, but response-provided ``NextPageLink`` values are still
host-pinned so pricing cannot become a server-side request primitive.  Raw rows are
normalized at this boundary: callers receive a stable subset of the documented shape,
unknown future fields are ignored, malformed rows are counted, and Reservation rows are
never admitted to a Consumption query.
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import httpx

RETAIL_API = "https://prices.azure.com/api/retail/prices"
RETAIL_API_VERSION = "2023-01-01-preview"
DEFAULT_MAX_PAGES = 8
DEFAULT_MAX_ITEMS = 8_000

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_STRING_FIELDS = (
    "currencyCode",
    "armRegionName",
    "location",
    "effectiveStartDate",
    "meterId",
    "meterName",
    "productId",
    "skuId",
    "productName",
    "skuName",
    "serviceName",
    "serviceId",
    "serviceFamily",
    "unitOfMeasure",
    "type",
    "armSkuName",
)


@dataclass(frozen=True)
class RetailFetchResult:
    items: list[dict[str, Any]]
    fetched_at: str
    pages: int
    invalid_rows: int = 0
    truncated: bool = False
    error: str = ""


def normalize_currency(value: str | None) -> str:
    """Return a validated ISO-style three-letter currency code."""
    currency = str(value or "USD").strip().upper()
    if not _CURRENCY_RE.fullmatch(currency):
        raise ValueError("Currency must be a three-letter code such as USD or EUR.")
    return currency


def odata_literal(value: str, *, max_length: int = 160) -> str:
    """Quote one bounded OData string literal, escaping an embedded apostrophe."""
    text = str(value or "").strip()
    if not text or len(text) > max_length or any(ord(ch) < 32 for ch in text):
        raise ValueError("Retail Prices filter value is empty or invalid.")
    return "'" + text.replace("'", "''") + "'"


def build_filter(
    service_name: str,
    *,
    regions: Sequence[str] = (),
    sku_names: Sequence[str] = (),
) -> str:
    """Build the only filter shape used by the application.

    Values originate in server-side match rules and bounded architecture properties; the
    client never accepts a caller-supplied filter expression.
    """
    clauses = [
        f"serviceName eq {odata_literal(service_name)}",
        "type eq 'Consumption'",
    ]
    region_values = list(dict.fromkeys(str(v) for v in regions))
    if region_values:
        region_clause = " or ".join(
            "armRegionName eq " + (odata_literal(value) if value else "''")
            for value in region_values
        )
        clauses.append(f"({region_clause})")
    sku_values = [v for v in dict.fromkeys(str(v).strip() for v in sku_names) if v]
    if sku_values:
        sku_clause = " or ".join(
            f"armSkuName eq {odata_literal(value)} or skuName eq {odata_literal(value)}"
            for value in sku_values
        )
        clauses.append(f"({sku_clause})")
    return " and ".join(clauses)


def normalize_item(raw: Any) -> dict[str, Any] | None:
    """Normalize one Retail Prices row, or reject it if it cannot be priced safely."""
    if not isinstance(raw, dict) or str(raw.get("type") or "") != "Consumption":
        return None
    try:
        retail_price = float(raw.get("retailPrice"))
        unit_price = float(raw.get("unitPrice", retail_price))
        tier_minimum = float(raw.get("tierMinimumUnits", 0))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and v >= 0 for v in (retail_price, unit_price, tier_minimum)):
        return None
    if not str(raw.get("meterName") or "").strip() or not str(raw.get("unitOfMeasure") or "").strip():
        return None

    item: dict[str, Any] = {}
    for field in _STRING_FIELDS:
        item[field] = str(raw.get(field) or "")[:400]
    item.update({
        "tierMinimumUnits": tier_minimum,
        "retailPrice": retail_price,
        "unitPrice": unit_price,
        "isPrimaryMeterRegion": bool(raw.get("isPrimaryMeterRegion", False)),
    })
    return item


def _safe_page_url(url: str, origin_host: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and (parsed.host or "").lower() == origin_host
        and parsed.port in (None, 443)
    )


async def fetch_retail_prices(
    service_name: str,
    *,
    currency: str,
    regions: Sequence[str] = (),
    sku_names: Sequence[str] = (),
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RetailFetchResult:
    """Fetch normalized Consumption rows for a server-selected service and scope.

    Errors are deliberately returned as bounded codes rather than exception strings; API
    routes can expose availability without reflecting network, proxy, or parser details.
    """
    currency = normalize_currency(currency)
    filter_text = build_filter(service_name, regions=regions, sku_names=sku_names)
    page_limit = max(1, min(int(max_pages), 20))
    item_limit = max(1, min(int(max_items), 20_000))
    origin_host = (httpx.URL(RETAIL_API).host or "").lower()
    fetched_at = datetime.now(timezone.utc).isoformat()
    items: list[dict[str, Any]] = []
    invalid_rows = 0
    pages = 0
    next_url = RETAIL_API
    params: dict[str, str] | None = {
        "api-version": RETAIL_API_VERSION,
        "currencyCode": f"'{currency}'",
        "$filter": filter_text,
    }

    try:
        async with httpx.AsyncClient(timeout=45, transport=transport) as client:
            for _ in range(page_limit):
                if not _safe_page_url(next_url, origin_host):
                    return RetailFetchResult(
                        items, fetched_at, pages, invalid_rows, bool(next_url), "next_page_refused",
                    )
                response: httpx.Response | None = None
                for attempt in range(3):
                    response = await client.get(next_url, params=params)
                    if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                        break
                    try:
                        retry_after = float(response.headers.get("Retry-After") or 0.25 * (2 ** attempt))
                    except ValueError:
                        retry_after = 0.25 * (2 ** attempt)
                    await asyncio.sleep(max(0.05, min(retry_after, 2.0)))
                params = None
                pages += 1
                assert response is not None
                if response.status_code != 200:
                    return RetailFetchResult(
                        items, fetched_at, pages, invalid_rows, False,
                        f"http_{response.status_code}",
                    )
                try:
                    payload = response.json()
                except ValueError:
                    return RetailFetchResult(items, fetched_at, pages, invalid_rows, False, "invalid_json")
                if not isinstance(payload, dict) or not isinstance(payload.get("Items"), list):
                    return RetailFetchResult(items, fetched_at, pages, invalid_rows, False, "invalid_shape")
                for raw in payload["Items"]:
                    normalized = normalize_item(raw)
                    if normalized is None:
                        invalid_rows += 1
                        continue
                    items.append(normalized)
                    if len(items) >= item_limit:
                        return RetailFetchResult(
                            items, fetched_at, pages, invalid_rows, True, "",
                        )
                next_url = str(payload.get("NextPageLink") or "")
                if not next_url:
                    return RetailFetchResult(items, fetched_at, pages, invalid_rows)
    except (httpx.HTTPError, ValueError, TypeError):
        return RetailFetchResult(items, fetched_at, pages, invalid_rows, False, "request_failed")

    return RetailFetchResult(items, fetched_at, pages, invalid_rows, bool(next_url), "")
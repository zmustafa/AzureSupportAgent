"""Azure Reservations collection + aggregation.

Pulls reservation orders from the tenant-level Capacity REST API
(``/providers/Microsoft.Capacity/reservationOrders``) — the same endpoint the original
Logic App used — and, for each order, its child reservations so we can surface the
auto-renew flag and utilization. Reservation orders are billing/tenant-scoped (not under
a subscription), so the scope here is the connection's identity, not a subscription.

``compute_reservations`` is a pure function over already-normalized rows, so it's
unit-testable and powers the demo seed. ``collect_reservations`` resolves the connection,
acquires an ARM token, and gathers the rows from Azure."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("app.reservations.collector")

_ARM = "https://management.azure.com"
_API_VERSION = "2022-11-01"
# Defensive cap on how many orders we expand (one child call each). Real tenants rarely
# have many reservation orders; this just bounds a pathological case.
_ORDER_CAP = 200
# RP3 — bound on concurrent child-reservation expansions during live collection.
_CHILD_CONCURRENCY = 8


# --------------------------------------------------------------------- date helpers
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + (6 if "%H" in fmt else 0)].split(".")[0], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def days_until(target: Any, *, today: date | None = None) -> int | None:
    """Whole days from today (UTC) to the target date. Negative when already past."""
    d = _parse_date(target)
    if d is None:
        return None
    base = today or datetime.now(timezone.utc).date()
    return (d - base).days


def severity_for_days(days: int | None, *, window_days: int = 60) -> str:
    """Countdown color band tuned to the ±``window`` digest:

    - ``red``   — expiring within 30 days, or already expired within the window (urgent).
    - ``amber`` — expiring within the rest of the window (31..window days out).
    - ``grey``  — healthy (>window out), long-expired, or unknown.
    """
    if days is None:
        return "grey"
    if 0 <= days <= 30:
        return "red"
    if -window_days <= days < 0:
        return "red"
    if 30 < days <= window_days:
        return "amber"
    return "grey"


def bucket_for_days(days: int | None, *, window_days: int = 60) -> str:
    """Which lane a reservation falls into relative to the ±``window``."""
    if days is None:
        return "unknown"
    if days < -window_days:
        return "expired"
    if days < 0:
        return "recently_expired"
    if days <= window_days:
        return "expiring_soon"
    return "active"


# --------------------------------------------------------------------- normalization
def _utilization_pct(util: Any) -> float | None:
    """Pull a single utilization percentage from a reservation's ``utilization`` block.

    Prefers the daily (grain 1) aggregate, falling back to the first available; returns
    ``None`` when utilization isn't reported (common for newly-purchased reservations)."""
    if not isinstance(util, dict):
        return None
    aggregates = util.get("aggregates")
    if not isinstance(aggregates, list) or not aggregates:
        return None
    chosen: dict[str, Any] | None = None
    for agg in aggregates:
        if not isinstance(agg, dict):
            continue
        try:
            grain = float(agg.get("grain"))
        except (TypeError, ValueError):
            grain = None
        if grain == 1:
            chosen = agg
            break
        if chosen is None:
            chosen = agg
    if chosen is None:
        return None
    try:
        return round(float(chosen.get("value")), 1)
    except (TypeError, ValueError):
        return None


def normalize_order(order: dict[str, Any], reservations: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a reservation order (+ its child reservations) into one display record.

    Pure given its inputs, so tests can feed synthetic API shapes. The renew flag,
    utilization, SKU and quantity come from the (usually single) child reservation."""
    props = order.get("properties", {}) or {}
    first = (reservations[0].get("properties", {}) or {}) if reservations else {}
    first_sku = (reservations[0].get("sku", {}) or {}) if reservations else {}

    expiry = (
        props.get("expiryDateTime")
        or props.get("expiryDate")
        or first.get("expiryDateTime")
        or first.get("expiryDate")
        or ""
    )
    created = (
        props.get("createdDateTime")
        or props.get("benefitStartTime")
        or props.get("requestDateTime")
        or ""
    )
    renew = first.get("renew") if reservations else props.get("renew")
    util = _utilization_pct(first.get("utilization")) if reservations else None

    quantity: Any = first.get("quantity")
    try:
        quantity = int(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        quantity = None

    return {
        "id": order.get("name", "") or order.get("id", ""),
        "order_id": order.get("id", ""),
        "display_name": props.get("displayName", "") or (first.get("displayName", "") if reservations else ""),
        "term": props.get("term", "") or first.get("term", ""),
        "billing_plan": props.get("billingPlan", "") or first.get("billingPlan", ""),
        "created_date": created,
        "expiry_date": expiry,
        "provisioning_state": props.get("provisioningState", "") or first.get("provisioningState", ""),
        "renew": bool(renew) if isinstance(renew, bool) else None,
        "utilization_pct": util,
        "sku": first_sku.get("name", ""),
        "reserved_resource_type": first.get("reservedResourceType", ""),
        "applied_scope_type": first.get("appliedScopeType", "") or props.get("appliedScopeType", ""),
        "quantity": quantity,
        "reservation_count": len(reservations) if reservations else int(props.get("reservationsCount", 0) or 0),
    }


# --------------------------------------------------------------------- compute (pure)
def compute_reservations(
    records: list[dict[str, Any]],
    *,
    window_days: int = 60,
    today: date | None = None,
) -> dict[str, Any]:
    """Assemble the snapshot (decorated items + counts) from normalized records."""
    items: list[dict[str, Any]] = []
    for r in records:
        rec = dict(r)
        d = days_until(rec.get("expiry_date"), today=today)
        rec["days_until"] = d
        rec["severity"] = severity_for_days(d, window_days=window_days)
        rec["bucket"] = bucket_for_days(d, window_days=window_days)
        rec["expired"] = d is not None and d < 0
        items.append(rec)

    # Soonest-to-expire first; unknown dates sink to the bottom.
    items.sort(key=lambda e: (e["days_until"] is None, e["days_until"] if e["days_until"] is not None else 1 << 30))

    def _count(pred) -> int:
        return sum(1 for e in items if pred(e))

    counts = {
        "total": len(items),
        "expiring_soon": _count(lambda e: e["bucket"] == "expiring_soon"),
        "recently_expired": _count(lambda e: e["bucket"] == "recently_expired"),
        "active": _count(lambda e: e["bucket"] == "active"),
        "expired": _count(lambda e: e["bucket"] == "expired"),
        "in_window": _count(lambda e: e["bucket"] in ("expiring_soon", "recently_expired")),
        "red": _count(lambda e: e["severity"] == "red"),
        "amber": _count(lambda e: e["severity"] == "amber"),
        "grey": _count(lambda e: e["severity"] == "grey"),
        "non_renew": _count(lambda e: e["renew"] is False),
        "low_utilization": _count(lambda e: isinstance(e["utilization_pct"], (int, float)) and e["utilization_pct"] < 25),
    }
    return {
        "generated_at": _now_iso(),
        "window_days": window_days,
        "items": items,
        "counts": counts,
    }


def empty_snapshot(
    *,
    connection_configured: bool,
    window_days: int = 60,
    error: str = "",
    never_loaded: bool = False,
) -> dict[str, Any]:
    snap = compute_reservations([], window_days=window_days)
    snap.update(
        {
            "source": "",
            "demo": False,
            "connection_configured": connection_configured,
            "error": error,
            "never_loaded": never_loaded,
        }
    )
    return snap


# --------------------------------------------------------------------- live queries
async def _arm_get(token: str, path: str, params: dict[str, str]) -> tuple[Any, str | None]:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30, base_url=_ARM) as client:
            resp = await client.get(path, headers=headers, params=params)
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
                detail = err.get("message", resp.text)
                code = err.get("code", "")
            except (ValueError, AttributeError):
                detail, code = resp.text, ""
            if resp.status_code in (401, 403):
                detail = (
                    "The connection's identity is not authorized to read reservations. Grant it the "
                    "'Reservations Reader' role at the reservation order (or tenant) scope. "
                    f"[{code or resp.status_code}]"
                )
            return None, f"{detail}"[:400]
        return resp.json(), None
    except httpx.HTTPError as exc:  # noqa: BLE001
        return None, f"ARM request error: {exc}"


async def collect_reservations(
    connection: dict[str, Any] | None,
    *,
    window_days: int = 60,
) -> dict[str, Any]:
    """Live collection: list reservation orders for the connection's identity and expand
    each with its child reservations (renew + utilization). Never raises — failures are
    folded into the snapshot's ``error`` so the UI can prompt accordingly."""
    if connection is None:
        return empty_snapshot(connection_configured=False, window_days=window_days)

    from app.azure.credentials import get_arm_token

    token, terr = await get_arm_token(connection)
    if terr or not token:
        return empty_snapshot(connection_configured=True, window_days=window_days, error=terr or "No ARM token.")

    data, err = await _arm_get(
        token, "/providers/Microsoft.Capacity/reservationOrders", {"api-version": _API_VERSION}
    )
    if err:
        return empty_snapshot(connection_configured=True, window_days=window_days, error=err)

    orders = (data or {}).get("value", []) or []
    capped = orders[:_ORDER_CAP]

    # RP3 — expand each order's child reservations concurrently (bounded) instead of a
    # sequential 1+N walk, so a large EA tenant's first load isn't N round-trips deep.
    sem = asyncio.Semaphore(_CHILD_CONCURRENCY)

    async def _children(oid: str) -> list[dict[str, Any]]:
        if not oid:
            return []
        async with sem:
            child, cerr = await _arm_get(
                token,
                f"/providers/Microsoft.Capacity/reservationOrders/{oid}/reservations",
                {"api-version": _API_VERSION},
            )
        if cerr:
            return []
        return (child or {}).get("value", []) or []

    child_lists = await asyncio.gather(*[_children(o.get("name", "")) for o in capped])
    records: list[dict[str, Any]] = [
        normalize_order(order, reservations) for order, reservations in zip(capped, child_lists)
    ]

    snap = compute_reservations(records, window_days=window_days)
    snap.update(
        {
            "source": "azure",
            "demo": False,
            "connection_configured": True,
            "error": "",
            "never_loaded": False,
        }
    )
    return snap

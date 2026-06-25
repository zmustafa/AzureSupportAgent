"""Shared helpers for collectors that consume Azure RP *usages* responses.

Two response shapes are handled transparently:
- **Flat** (Compute, Network, Storage, ContainerInstance, CognitiveServices, ML):
  ``{"name": {"value", "localizedValue"}, "currentValue", "limit", "unit"}``.
- **Nested** (SQL): ``{"name": "ServerQuota", "properties": {"displayName", "currentValue",
  "limit", "unit"}}`` — currentValue/limit/unit live under ``properties`` and ``name`` is a string,
  with the human label in ``properties.displayName``.

This maps both to normalized ``QuotaResult`` rows so each collector stays tiny."""
from __future__ import annotations

from typing import Any, Callable

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.model import QuotaResult, redact


def _name_pair(item: dict[str, Any]) -> tuple[str, str]:
    """Return (value, localizedValue) from a usages item's name.

    Handles the flat shape (name is a {value, localizedValue} dict) and the nested SQL shape
    (name is a string + a human ``properties.displayName``)."""
    name = item.get("name")
    if isinstance(name, dict):
        return str(name.get("value", "")), str(name.get("localizedValue", name.get("value", "")))
    s = str(name or "")
    props = item.get("properties")
    if isinstance(props, dict) and props.get("displayName"):
        return s, str(props["displayName"])
    return s, s


def _field(item: dict[str, Any], key: str) -> Any:
    """Read a field from the item, falling back to its nested ``properties`` block (SQL shape)."""
    if key in item and item.get(key) is not None:
        return item.get(key)
    props = item.get("properties")
    if isinstance(props, dict):
        return props.get(key)
    return None


def _as_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def build_usage_results(
    collector: IQuotaCollector,
    ctx: CollectorContext,
    items: list[dict[str, Any]],
    *,
    source_type: str,
    adjustable: str,
    family_fn: Callable[[str], str] | None = None,
    unit_default: str = "Count",
    include_zero_limit: bool = False,
    keep_zero_usage: bool = False,
) -> list[QuotaResult]:
    # When the collector's data is itself the point of the report (e.g. the VM SKU-family quota
    # table), zero-usage rows are kept so the operator sees every family's limit/headroom — like
    # the Azure portal Quotas blade. ``include_unused`` (operator opt-in) forces this everywhere.
    hide_zero = (
        getattr(ctx, "hide_zero_usage", False)
        and not keep_zero_usage
        and not getattr(ctx, "include_unused", False)
    )
    out: list[QuotaResult] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        value, localized = _name_pair(item)
        current = _as_float(_field(item, "currentValue"))
        limit = _as_float(_field(item, "limit"))
        if limit is None and current is None:
            continue
        if not include_zero_limit and limit is not None and limit <= 0 and (current or 0) <= 0:
            # Many usage APIs report rows with limit 0 for SKUs not enabled in the region.
            continue
        # Hide full-headroom rows (current == 0) when requested — the per-model AI / per-family
        # compute tables are otherwise hundreds of zero-usage rows that aren't actionable for a
        # quota monitor. A None current (limit-only row) is NOT zero usage, so it's kept.
        if hide_zero and current == 0:
            continue
        r = collector._base(
            ctx,
            quota_name=localized or value or "Quota",
            sku_family=(family_fn(value) if family_fn else ""),
            current_usage=current,
            limit=limit,
            unit=str(_field(item, "unit") or unit_default),
            source_type=source_type,
            adjustable_status=adjustable,
            raw_provider_response=redact(item),
        )
        r.compute_derived()
        out.append(r)
    return out

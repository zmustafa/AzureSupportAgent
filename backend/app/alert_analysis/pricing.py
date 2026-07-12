"""Pure, transparent Azure Monitor alert-rule cost estimates.

The built-in catalog is a versioned USD reference catalog, not a billing quote.  Callers
may pass catalog overrides to model negotiated/regional prices without changing the
estimator.  Costs outside the direct alert-rule meters (for example log ingestion,
Prometheus ingestion/querying, taxes, free grants, and currency conversion) are never
silently inferred.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

CATALOG_VERSION = "azure-monitor-public-usd-2026-07-11-v1"

DEFAULT_CATALOG: dict[str, Any] = {
    "version": CATALOG_VERSION,
    "effective_date": "2026-07-11",
    "currency": "USD",
    "period": "month",
    "source": "https://azure.microsoft.com/pricing/details/monitor/",
    "scope": "Reference public list prices; actual agreement, region, grants, and taxes may differ.",
    "metric": {
        "static_per_time_series_monthly": 0.10,
        "dynamic_threshold_per_time_series_monthly": 0.10,
        "unknown_dimension_cardinality": {"estimate": 5, "max": 20},
        "max_dimension_multiplier": 10_000,
    },
    "log": {
        # Public pricing is expressed as discrete evaluation-frequency tiers.  The
        # first monitored time series is included in the rule price; additional
        # series cannot be inferred from the normalized rule inventory.
        "frequency_tiers": {
            "PT1M": {"first_time_series_monthly": 3.00, "additional_time_series_monthly": 0.30},
            "PT5M": {"first_time_series_monthly": 1.50, "additional_time_series_monthly": 0.15},
            "PT10M": {"first_time_series_monthly": 1.00, "additional_time_series_monthly": 0.10},
            "PT15M": {"first_time_series_monthly": 0.50, "additional_time_series_monthly": 0.05},
        },
    },
}

_FAMILY_BY_TYPE = {
    "microsoft.insights/metricalerts": "metric",
    "microsoft.insights/scheduledqueryrules": "log",
    "microsoft.insights/activitylogalerts": "activity_log",
    "microsoft.alertsmanagement/smartdetectoralertrules": "smart_detector",
    "microsoft.alertsmanagement/prometheusrules": "prometheus",
}


def pricing_catalog(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return an isolated catalog with optional recursive overrides."""
    result = deepcopy(DEFAULT_CATALOG)

    def merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = deepcopy(value)

    if overrides:
        merge(result, overrides)
    return result


def _money(value: float | int | None) -> float | None:
    return None if value is None else round(float(value) + 0.0, 2)


def _family(rule: Mapping[str, Any]) -> str:
    rule_type = str(rule.get("type") or "").lower()
    if rule_type in _FAMILY_BY_TYPE:
        return _FAMILY_BY_TYPE[rule_type]
    if "prometheus" in rule_type:
        return "prometheus"
    if "smartdetector" in rule_type:
        return "smart_detector"
    if "activitylogalerts" in rule_type:
        return "activity_log"
    if "scheduledqueryrules" in rule_type:
        return "log"
    if "metricalerts" in rule_type:
        return "metric"
    return "unknown"


def _base_result(rule: Mapping[str, Any], family: str, catalog: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "family": family,
        "status": "unknown",
        "confidence": "none",
        "currency": str(catalog.get("currency") or "USD"),
        "period": str(catalog.get("period") or "month"),
        "catalog_version": str(catalog.get("version") or "custom"),
        "enabled": bool(rule.get("enabled", True)),
        "monthly_usd": None,
        "monthly_min_usd": None,
        "monthly_max_usd": None,
        "assumptions": [],
        "components": [],
    }


def _condition_dimensions(condition: Mapping[str, Any]) -> tuple[int, int, list[str]]:
    """Return min/point/max multipliers from normalized dimension filters."""
    explicit_multiplier = 1
    unbounded_count = 0
    notes: list[str] = []
    for raw in condition.get("dimensions") or []:
        name, operator, raw_values = (str(raw).split(":", 2) + ["", ""])[:3]
        values = {value.strip().lower() for value in raw_values.split(",") if value.strip()}
        explicit_include = operator.lower() == "include" and values and "*" not in values
        if explicit_include:
            cardinality = len(values)
            explicit_multiplier *= cardinality
            notes.append(f"Dimension {name or '(unnamed)'} is bounded by {cardinality} explicit Include value(s).")
        else:
            # The caller applies catalog-configured defaults for unbounded dimensions.
            notes.append(
                f"Dimension {name or '(unnamed)'} uses {operator or 'an unspecified operator'} and has unbounded observed cardinality."
            )
            unbounded_count += 1
    return explicit_multiplier, unbounded_count, notes


def _metric_cost(rule: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    result = _base_result(rule, "metric", catalog)
    target_count = int(rule.get("effective_target_count") or 0)
    if target_count <= 0:
        result["assumptions"] = [
            "No effective targets were resolved, so monitored metric time-series cardinality cannot be priced.",
            "No fallback target count was invented from scope strings.",
        ]
        return result

    metric_catalog = catalog.get("metric") or {}
    static_rate = float(metric_catalog.get("static_per_time_series_monthly", 0.0))
    dynamic_rate = float(metric_catalog.get("dynamic_threshold_per_time_series_monthly", 0.0))
    unknown_dim = metric_catalog.get("unknown_dimension_cardinality") or {}
    unknown_point = max(1, int(unknown_dim.get("estimate", 1)))
    unknown_max = max(unknown_point, int(unknown_dim.get("max", unknown_point)))
    multiplier_cap = max(1, int(metric_catalog.get("max_dimension_multiplier", 10_000)))
    conditions = list(rule.get("conditions") or [{}])

    total_point = total_min = total_max = 0.0
    uncertain = False
    assumptions = [
        f"Resolved effective_target_count={target_count} is used as the base monitored time-series count per condition.",
        "Each normalized metric condition is priced independently; shared or platform-side cardinality optimizations are not assumed.",
    ]
    components: list[dict[str, Any]] = []
    for index, raw_condition in enumerate(conditions, start=1):
        condition = raw_condition if isinstance(raw_condition, Mapping) else {}
        explicit_multiplier, unbounded_count, dimension_notes = _condition_dimensions(condition)
        if unbounded_count:
            uncertain = True
            point_multiplier = min(multiplier_cap, explicit_multiplier * (unknown_point ** unbounded_count))
            max_multiplier = min(multiplier_cap, explicit_multiplier * (unknown_max ** unbounded_count))
        else:
            point_multiplier = min(multiplier_cap, explicit_multiplier)
            max_multiplier = point_multiplier
        min_multiplier = min(multiplier_cap, explicit_multiplier)
        min_series = target_count * min_multiplier
        point_series = target_count * point_multiplier
        max_series = target_count * max_multiplier
        dynamic = bool(condition.get("dynamic"))
        per_series = static_rate + (dynamic_rate if dynamic else 0.0)
        point_cost = point_series * per_series
        min_cost = min_series * per_series
        max_cost = max_series * per_series
        total_point += point_cost
        total_min += min_cost
        total_max += max_cost
        components.append(
            {
                "name": f"metric_condition_{index}",
                "status": "range_estimate" if unbounded_count else "estimated",
                "monthly_usd": _money(point_cost),
                "monthly_min_usd": _money(min_cost),
                "monthly_max_usd": _money(max_cost),
                "quantity": point_series,
                "quantity_min": min_series,
                "quantity_max": max_series,
                "unit": "monitored_metric_time_series",
                "unit_price_usd": _money(per_series),
                "notes": dimension_notes,
            }
        )
        assumptions.extend(dimension_notes)

    result.update(
        {
            "status": "range_estimate" if uncertain else "estimated",
            "confidence": "low" if uncertain else "medium",
            "monthly_usd": _money(total_point),
            "monthly_min_usd": _money(total_min),
            "monthly_max_usd": _money(total_max),
            "assumptions": assumptions,
            "components": components,
        }
    )
    return result


def _log_cost(rule: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    result = _base_result(rule, "log", catalog)
    condition = (rule.get("conditions") or [{}])[0]
    frequency = str(condition.get("frequency") or "").upper() if isinstance(condition, Mapping) else ""
    tiers = (catalog.get("log") or {}).get("frequency_tiers") or {}
    tier = tiers.get(frequency)
    if not isinstance(tier, Mapping):
        result["assumptions"] = [
            f"Evaluation frequency {frequency or '(missing)'} has no configured catalog tier.",
            "No interpolation or nearest-tier substitution was performed.",
        ]
        return result

    first_series = float(tier.get("first_time_series_monthly", 0.0))
    additional_series = float(tier.get("additional_time_series_monthly", 0.0))
    result.update(
        {
            "status": "partial_estimate",
            "confidence": "medium",
            "monthly_usd": _money(first_series),
            "monthly_min_usd": _money(first_series),
            "monthly_max_usd": None,
            "assumptions": [
                f"Configured public-style {frequency} tier prices the first monitored time series.",
                "The normalized ARM rule does not reveal the count of additional monitored time series; the point value is therefore a lower-bound direct rule charge.",
                "Log ingestion, query data volume, and other workspace charges are excluded.",
            ],
            "components": [
                {
                    "name": "log_alert_first_time_series",
                    "status": "priced",
                    "monthly_usd": _money(first_series),
                    "monthly_min_usd": _money(first_series),
                    "monthly_max_usd": _money(first_series),
                    "quantity": 1,
                    "unit": "first_monitored_time_series",
                    "unit_price_usd": _money(first_series),
                },
                {
                    "name": "log_alert_additional_time_series",
                    "status": "unknown_quantity",
                    "monthly_usd": None,
                    "monthly_min_usd": 0.0,
                    "monthly_max_usd": None,
                    "quantity": None,
                    "unit": "additional_monitored_time_series",
                    "unit_price_usd": _money(additional_series),
                },
            ],
        }
    )
    return result


def _direct_free(rule: Mapping[str, Any], family: str, catalog: Mapping[str, Any], assumptions: list[str]) -> dict[str, Any]:
    result = _base_result(rule, family, catalog)
    result.update(
        {
            "status": "direct_free",
            "confidence": "high",
            "monthly_usd": 0.0,
            "monthly_min_usd": 0.0,
            "monthly_max_usd": 0.0,
            "assumptions": assumptions,
            "components": [
                {
                    "name": "direct_alert_rule_charge",
                    "status": "direct_free",
                    "monthly_usd": 0.0,
                    "monthly_min_usd": 0.0,
                    "monthly_max_usd": 0.0,
                    "quantity": 1,
                    "unit": "alert_rule",
                    "unit_price_usd": 0.0,
                }
            ],
        }
    )
    return result


def estimate_rule_cost(rule: Mapping[str, Any], catalog_overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Estimate one normalized rule without I/O or Azure mutations."""
    catalog = pricing_catalog(catalog_overrides)
    family = _family(rule)
    if family == "metric":
        return _metric_cost(rule, catalog)
    if family == "log":
        return _log_cost(rule, catalog)
    if family == "activity_log":
        return _direct_free(
            rule,
            family,
            catalog,
            ["Activity Log alert rules have no direct alert-rule charge; downstream routing or data-export charges are excluded."],
        )
    if family == "prometheus":
        return _direct_free(
            rule,
            family,
            catalog,
            ["No separate Prometheus alert-rule charge is assigned.", "Managed Prometheus query, ingestion, storage, and platform charges are excluded and may be material."],
        )
    result = _base_result(rule, family, catalog)
    result["assumptions"] = [
        "A reliable direct rule price cannot be derived from the normalized ARM resource.",
        "No placeholder dollar amount was invented; verify the detector or product pricing separately.",
    ]
    return result


def empty_cost_summary(catalog_overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Stable snapshot summary shape for empty/error snapshots."""
    catalog = pricing_catalog(catalog_overrides)
    return {
        "currency": str(catalog.get("currency") or "USD"),
        "period": str(catalog.get("period") or "month"),
        "catalog_version": str(catalog.get("version") or "custom"),
        "catalog_effective_date": str(catalog.get("effective_date") or ""),
        "catalog_source": str(catalog.get("source") or ""),
        "catalog_scope": str(catalog.get("scope") or ""),
        "monthly_usd": 0.0,
        "monthly_min_usd": 0.0,
        "monthly_max_usd": 0.0,
        "current": {"monthly_usd": 0.0, "monthly_min_usd": 0.0, "monthly_max_usd": 0.0},
        "potential_disabled_monthly": 0.0,
        "potential_disabled_monthly_min": 0.0,
        "potential_disabled_monthly_max": 0.0,
        "disabled": {"monthly_usd": 0.0, "monthly_min_usd": 0.0, "monthly_max_usd": 0.0},
        "priced_count": 0,
        "unknown_count": 0,
        "by_family": {},
        "top_rules": [],
        "assumptions": [
            "Enabled rules contribute to current monthly totals; disabled rules are reported only as potential disabled monthly cost.",
            "A null maximum means at least one priced component has an unbounded quantity; it is not treated as zero.",
        ],
    }


def summarize_rule_costs(rules: list[Mapping[str, Any]], catalog_overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Aggregate rule estimates while preserving unknown/unbounded values."""
    summary = empty_cost_summary(catalog_overrides)
    family_rows: dict[str, list[Mapping[str, Any]]] = {}
    for rule in rules:
        cost = rule.get("cost") if isinstance(rule.get("cost"), Mapping) else estimate_rule_cost(rule, catalog_overrides)
        family_rows.setdefault(str(cost.get("family") or "unknown"), []).append({**rule, "cost": cost})

    def aggregate(items: list[Mapping[str, Any]], enabled: bool) -> dict[str, Any]:
        selected = [item["cost"] for item in items if bool(item.get("enabled", True)) is enabled]
        point = sum(float(cost["monthly_usd"]) for cost in selected if cost.get("monthly_usd") is not None)
        minimum = sum(float(cost["monthly_min_usd"]) for cost in selected if cost.get("monthly_min_usd") is not None)
        max_values = [cost.get("monthly_max_usd") for cost in selected]
        maximum = None if any(value is None for value in max_values) else sum(float(value) for value in max_values)
        return {
            "monthly_usd": _money(point),
            "monthly_min_usd": _money(minimum),
            "monthly_max_usd": _money(maximum),
        }

    current = aggregate(rules, True)
    disabled = aggregate(rules, False)
    summary.update(
        {
            "monthly_usd": current["monthly_usd"],
            "monthly_min_usd": current["monthly_min_usd"],
            "monthly_max_usd": current["monthly_max_usd"],
            "current": current,
            "potential_disabled_monthly": disabled["monthly_usd"],
            "potential_disabled_monthly_min": disabled["monthly_min_usd"],
            "potential_disabled_monthly_max": disabled["monthly_max_usd"],
            "disabled": disabled,
            "priced_count": sum(1 for rule in rules if isinstance(rule.get("cost"), Mapping) and rule["cost"].get("monthly_usd") is not None),
            "unknown_count": sum(1 for rule in rules if isinstance(rule.get("cost"), Mapping) and rule["cost"].get("status") == "unknown"),
        }
    )
    for family, items in sorted(family_rows.items()):
        summary["by_family"][family] = {
            "rule_count": len(items),
            "priced_count": sum(1 for item in items if item["cost"].get("monthly_usd") is not None),
            "unknown_count": sum(1 for item in items if item["cost"].get("status") == "unknown"),
            "current": aggregate(items, True),
            "disabled": aggregate(items, False),
        }
    priced_rules = [rule for rule in rules if isinstance(rule.get("cost"), Mapping) and rule["cost"].get("monthly_usd") is not None]
    summary["top_rules"] = [
        {
            "rule_id": rule.get("id", ""),
            "rule_name": rule.get("name", ""),
            "family": rule["cost"].get("family", "unknown"),
            "enabled": bool(rule.get("enabled", True)),
            "status": rule["cost"].get("status", "unknown"),
            "confidence": rule["cost"].get("confidence", "none"),
            "monthly_usd": rule["cost"].get("monthly_usd"),
            "monthly_min_usd": rule["cost"].get("monthly_min_usd"),
            "monthly_max_usd": rule["cost"].get("monthly_max_usd"),
        }
        for rule in sorted(
            priced_rules,
            key=lambda item: (float(item["cost"].get("monthly_usd") or 0), str(item.get("name") or "")),
            reverse=True,
        )[:10]
    ]
    return summary

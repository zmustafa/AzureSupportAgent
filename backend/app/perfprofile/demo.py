"""Synthetic performance-profile data for review/demo without live Azure metrics.

Drives the demo from the shared per-workload catalog (``app.demo_catalog``) so each demo
workload gets its own resources and metric series derived from each resource's health tier:
    green → every metric healthy (lots of headroom)
    amber → the resource's headline metric is approaching its AMBA threshold
    red   → the headline metric breaches (the binding bottleneck), the rest stay healthy

Marked demo everywhere; the API serves this instead of querying Azure for the demo scope."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.amba.reference import load_reference
from app.demo_catalog import CONTOSO_ID, resources_for
from app.perfprofile.collector import compute_profile

DEMO_WORKLOAD_ID = CONTOSO_ID  # default demo scope used by the API when none is supplied

# Data/cache tiers breach hardest so they rank as the top bottleneck where present.
_DATA_TYPES = {
    "microsoft.cache/redis",
    "microsoft.sql/servers/databases",
    "microsoft.documentdb/databaseaccounts",
    "microsoft.dbforpostgresql/flexibleservers",
}


def _series(base: float, end: float, *, points: int = 24, noise: float = 0.0) -> list[dict[str, Any]]:
    """A rising/falling series from base→end over ``points`` 15-min buckets."""
    start = datetime.now(timezone.utc) - timedelta(minutes=15 * points)
    out = []
    for i in range(points):
        frac = i / max(1, points - 1)
        val = base + (end - base) * frac
        if noise and i % 3 == 0:
            val += noise
        out.append({"timestamp": (start + timedelta(minutes=15 * i)).isoformat(), "value": round(max(0.0, val), 2)})
    return out


def _metric_alerts(rtype: str) -> list[dict[str, Any]]:
    ref = load_reference()
    spec = ref.get("types", {}).get(rtype) or {}
    return [a for a in (spec.get("alerts") or []) if str(a.get("signal", "metric")) == "metric" and a.get("metric")]


def _value_series(state: str, rec: dict[str, Any], rtype: str) -> list[dict[str, Any]]:
    op = rec.get("operator", "GreaterThan")
    thr = rec.get("threshold")
    lower_is_worse = op in ("LessThan", "LessThanOrEqual")
    if thr is None:
        # Informational / count metric: only a red headline shows a nonzero signal.
        return _series(0, 6) if state == "breaching" else _series(0, 0)
    thr = float(thr)
    if thr == 0:
        return _series(0, 5) if state == "breaching" else _series(0, 0)
    if lower_is_worse:  # availability-style (lower observed = worse)
        if thr <= 100:  # a percentage (e.g. Availability 99, HealthCheckStatus 100)
            if state == "breaching":
                return _series(thr + 1, thr - 2)
            if state == "approaching":
                return _series(100, thr + (100 - thr) * 0.3)
            return _series(100, 100)
        # an absolute floor (e.g. Available Memory Bytes): healthy stays well above it.
        if state == "breaching":
            return _series(thr * 1.2, thr * 0.8)
        if state == "approaching":
            return _series(thr * 2, thr * 1.1)
        return _series(thr * 3, thr * 2.5)
    # Higher-is-worse percentage/count metric.
    if state == "breaching":
        peak = thr * (1.10 if rtype in _DATA_TYPES else 1.04)
        return _series(thr * 0.7, peak, noise=2)
    if state == "approaching":
        return _series(thr * 0.55, thr * 0.85)
    return _series(thr * 0.30, thr * 0.45)


def demo_metrics_by_resource(scope_id: str = CONTOSO_ID) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Metric series keyed by lowercased resource id → {metric → series}."""
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for res in resources_for(scope_id):
        alerts = _metric_alerts(res["type"])
        if not alerts:
            continue
        tier = res["tier"]
        # Headline = first metric alert with a numeric threshold (so red actually breaches).
        headline = next((i for i, a in enumerate(alerts) if a.get("threshold")), 0)
        headline_state = {"red": "breaching", "amber": "approaching", "green": "healthy"}[tier]
        series_map: dict[str, list[dict[str, Any]]] = {}
        for i, a in enumerate(alerts):
            state = headline_state if i == headline else "healthy"
            series_map[a["metric"]] = _value_series(state, a, res["type"])
        out[res["id"].lower()] = series_map
    return out


def build_demo_snapshot(*, scope_id: str = CONTOSO_ID, scope_name: str | None = None) -> dict[str, Any]:
    from app.amba.demo import demo_scope_name

    snap = compute_profile(resources_for(scope_id), demo_metrics_by_resource(scope_id))
    snap.update(
        {
            "scope_kind": "workload",
            "scope_id": scope_id,
            "scope_name": scope_name or demo_scope_name(scope_id),
            "connection_configured": False,
            "source": "demo_dummy_data",
            "window": "P1D",
            "demo": True,
            "error": "",
        }
    )
    return snap


def seed_demo(tenant_id: str = "default", *, scope_id: str = CONTOSO_ID, scope_name: str | None = None) -> dict[str, Any]:
    from app.amba.demo import ensure_demo_workload
    from app.perfprofile import cache

    ensure_demo_workload(scope_id)
    snap = build_demo_snapshot(scope_id=scope_id, scope_name=scope_name)
    cache.write_snapshot(tenant_id, "workload", scope_id, snap)
    return snap


def is_demo_scope(scope_kind: str, scope_id: str) -> bool:
    from app.amba.demo import is_demo_scope as _is

    return _is(scope_kind, scope_id)

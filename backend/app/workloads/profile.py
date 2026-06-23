"""WorkloadProfile aggregator — the keystone of the Workloads command center.

Assembles a compact, per-workload rollup (composition, health signals, risk, activity,
owners, freshness) by reading what each feature has ALREADY cached. It is strictly
cache-only and offline — NO live Azure scans — so rendering the fleet list is one cheap
request, not N expensive scans. A signal that was never computed is reported as ``None``
(the UI shows "Not analyzed"), never a misleading zero.

The deep, on-demand scan that warms these caches lives in the API layer
(``POST /workloads/{id}/analyze``); this module only reads.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.workloads import health, summarize, taxonomy


def _age_seconds(ts: Any) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - dt).total_seconds())


def _resource_nodes(workload: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in (workload.get("nodes") or []) if n.get("kind", "resource") == "resource"]


def _composition(workload: dict[str, Any]) -> dict[str, Any]:
    """Resource composition from the workload's own node list (cache-only, exact for
    resource nodes; scope nodes are counted separately)."""
    summary = workload.get("summary") or summarize.summarize_nodes(workload.get("nodes") or [])
    resources = _resource_nodes(workload)
    # Build by_type with a representative ARM type per friendly label (first match) so the
    # frontend can render the right icon next to each type's count.
    rep_type: dict[str, str] = {}
    for n in resources:
        t = (n.get("resource_type") or n.get("type") or "")
        lbl = summarize.friendly_type(t)
        rep_type.setdefault(lbl, t)
    by_type = [
        {"type": rep_type.get(item["label"], ""), "friendly": item["label"], "count": item["count"]}
        for item in summary.get("types", [])
    ]
    by_location: dict[str, int] = {}
    by_sub: dict[str, int] = {}
    for n in resources:
        loc = (n.get("location") or "").strip() or "unknown"
        by_location[loc] = by_location.get(loc, 0) + 1
        sub = (n.get("subscription_id") or "").strip() or "unknown"
        by_sub[sub] = by_sub.get(sub, 0) + 1
    return {
        "total": summary.get("total_resources", 0),
        "scope_counts": summary.get("scope_counts", {}),
        "by_category": taxonomy.category_breakdown(resources),
        "by_type": by_type,
        "by_location": [{"location": k, "count": v} for k, v in sorted(by_location.items(), key=lambda x: -x[1])],
        "by_subscription": [{"subscription_id": k, "count": v} for k, v in sorted(by_sub.items(), key=lambda x: -x[1])],
    }


def _read(cache_mod: Any, tenant_id: str, workload_id: str) -> dict[str, Any] | None:
    try:
        return cache_mod.read_snapshot(tenant_id, "workload", workload_id)
    except Exception:  # noqa: BLE001 — a missing/!corrupt cache must never break the profile
        return None


def _signals(tenant_id: str, workload_id: str) -> tuple[dict[str, float | None], dict[str, int | None], dict[str, Any]]:
    """Return (health metrics 0-100|None, freshness ages, extras) read from feature caches."""
    health_m: dict[str, float | None] = {s: None for s in health.SIGNALS}
    fresh: dict[str, int | None] = {}
    extras: dict[str, Any] = {}

    # Lazy imports keep the workloads package import-light and avoid cycles.
    try:
        from app.amba import cache as amba_cache
        snap = _read(amba_cache, tenant_id, workload_id)
        if snap is not None:
            health_m["monitoring"] = snap.get("coverage_pct")
            fresh["monitoring"] = _age_seconds(snap.get("generated_at"))
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.telemetry import cache as tel_cache
        snap = _read(tel_cache, tenant_id, workload_id)
        if snap is not None:
            health_m["telemetry"] = snap.get("coverage_pct")
            fresh["telemetry"] = _age_seconds(snap.get("generated_at"))
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.backupdr import cache as bdr_cache
        snap = _read(bdr_cache, tenant_id, workload_id)
        if snap is not None:
            sc = snap.get("scorecard") or {}
            health_m["backupdr"] = sc.get("pct_protected")
            fresh["backupdr"] = _age_seconds(snap.get("generated_at"))
            extras["backupdr"] = {
                "dr_pairs": sc.get("dr_pairs"),
                "dr_pairs_unhealthy": sc.get("dr_pairs_unhealthy"),
            }
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.perfprofile import cache as perf_cache
        snap = _read(perf_cache, tenant_id, workload_id)
        if snap is not None:
            sc = snap.get("scorecard") or {}
            health_m["performance"] = sc.get("workload_score")
            fresh["performance"] = _age_seconds(snap.get("generated_at"))
            extras["performance"] = {
                "bottlenecks": sc.get("bottleneck_count"),
                "breaching": sc.get("breaching"),
            }
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.ownership import cache as own_cache
        snap = _read(own_cache, tenant_id, workload_id)
        if snap is not None:
            health_m["ownership"] = snap.get("coverage_pct")
            fresh["ownership"] = _age_seconds(snap.get("generated_at"))
    except Exception:  # noqa: BLE001
        pass

    # Policy + tags are best-effort (their caches aren't workload-scoped today); left None
    # unless a workload-scoped snapshot exists. Radar is risk, handled separately below.
    return health_m, fresh, extras


def _risk(tenant_id: str, workload_id: str) -> dict[str, Any]:
    """Risk rollup: retirements impacting the workload (≤90 days) + criticals."""
    out: dict[str, Any] = {"retirements_90d": None, "retirements_total": None, "criticals": None}
    try:
        from app.radar import cache as radar_cache
        snap = _read(radar_cache, tenant_id, workload_id)
        if snap is not None:
            events = snap.get("events") or []
            out["retirements_total"] = len(events)
            out["retirements_90d"] = sum(
                1 for e in events if isinstance(e.get("days_until"), int) and 0 <= e["days_until"] <= 90
            )
            out["criticals"] = sum(1 for e in events if (e.get("severity") or "").lower() in ("critical", "high"))
    except Exception:  # noqa: BLE001
        pass
    return out


def build_profile(
    workload: dict[str, Any],
    tenant_id: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full cache-only profile for one workload."""
    wid = workload.get("id", "")
    health_m, fresh, extras = _signals(tenant_id, wid)
    score = health.composite_score(health_m, settings)
    owners = workload.get("owners") or []
    # Recent composite-score history (cheap read of the trend file) so cards/detail can render
    # a sparkline + delta arrow without an extra request.
    score_trend = _score_trend(tenant_id, wid)
    return {
        "id": wid,
        "name": workload.get("name", ""),
        "connection_id": workload.get("connection_id", ""),
        "classification": {
            "workload_type": workload.get("workload_type", "other"),
            "environment": workload.get("environment", "unknown"),
            "criticality": workload.get("criticality", "medium"),
            "data_classification": workload.get("data_classification", "unknown"),
        },
        "composition": _composition(workload),
        "health": {**health_m, **score, "extras": extras},
        "risk": _risk(tenant_id, wid),
        "activity": {
            "last_refreshed": workload.get("last_refreshed", ""),
            "last_refreshed_age_s": _age_seconds(workload.get("last_refreshed")),
            "updated_at": workload.get("updated_at", ""),
        },
        "freshness": fresh,
        "score_trend": score_trend,
        "analyzed": bool(score.get("contributing")),
    }


def _score_trend(tenant_id: str, workload_id: str) -> dict[str, Any]:
    """Recent composite-score points (sparkline + delta). Empty when none recorded yet."""
    try:
        from app.core import coverage_trends

        pts = coverage_trends.series("workload", tenant_id, "workload", workload_id)
        vals = [p.get("pct") for p in pts if isinstance(p.get("pct"), int)]
        cur = vals[-1] if vals else None
        prev = vals[-2] if len(vals) >= 2 else None
        delta = (cur - prev) if (isinstance(cur, int) and isinstance(prev, int)) else None
        return {"points": vals[-20:], "current": cur, "previous": prev, "delta": delta, "count": len(vals)}
    except Exception:  # noqa: BLE001
        return {"points": [], "current": None, "previous": None, "delta": None, "count": 0}


def record_trend(workload: dict[str, Any], tenant_id: str, settings: dict[str, Any] | None = None) -> int | None:
    """Compute the composite score and append a trend point (only when a score exists). Returns
    the recorded score, or None when nothing was analyzed yet."""
    health_m, _fresh, _extras = _signals(tenant_id, workload.get("id", ""))
    score = health.composite_score(health_m, settings)
    if score.get("score") is None:
        return None
    try:
        from app.core import coverage_trends

        coverage_trends.record(
            "workload", tenant_id, "workload", workload.get("id", ""),
            pct=score["score"], extra={"contributing": len(score.get("contributing", []))},
        )
    except Exception:  # noqa: BLE001
        pass
    return score["score"]


def build_profiles(
    workloads: list[dict[str, Any]],
    tenant_id: str,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [build_profile(w, tenant_id, settings) for w in workloads]

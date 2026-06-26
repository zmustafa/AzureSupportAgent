"""Performance profiling computation.

For each in-scope resource that has an AMBA reference entry, evaluate its *metric* alerts
against live metric readings: current/peak/avg value, % of the AMBA threshold, a state
(healthy / approaching / breaching), trend over the window, and a per-resource Performance
Score (0-100, severity-weighted). Rolls up to a workload score, a ranked bottleneck list,
and a heatmap matrix.

``compute_profile`` is a pure function over already-fetched ``resources`` + a
``metrics_by_resource`` map (resource_id → {metric → series}), so it's unit-testable and
powers the demo. ``profile_workload`` resolves the scope and gathers the metrics from
Azure Monitor (``az monitor metrics list``)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.amba.reference import load_reference
from app.core.coverage_resources import build_all_resources
from app.perfprofile.metrics_map import metric_semantics

log = logging.getLogger("app.perfprofile.collector")

STATE_HEALTHY = "healthy"
STATE_APPROACHING = "approaching"
STATE_BREACHING = "breaching"
STATE_NODATA = "no_data"

_SEV_WEIGHT = {"critical": 10, "error": 6, "warning": 3, "info": 1}
_STATE_RANK = {STATE_BREACHING: 0, STATE_APPROACHING: 1, STATE_HEALTHY: 2, STATE_NODATA: 3}

# A metric is "approaching" once it passes this fraction of its threshold (toward breach).
_APPROACH_FRAC = 0.70

# --- Managed disk performance ------------------------------------------------------------
# A managed disk's own metrics are absolute throughput counters (Composite Disk Read/Write
# Operations & Bytes per second), with NO built-in saturation %. So the profiler derives a
# saturation % itself: total (read+write) IOPS / throughput divided by the disk's PROVISIONED
# limits (diskIOPSReadWrite / diskMBpsReadWrite, hydrated from Azure Resource Graph). The two
# synthetic "% of provisioned" series are stored under these labels and then scored like any
# other %-of-threshold metric (threshold 80, ceiling 100).
DISK_TYPE = "microsoft.compute/disks"
DISK_IOPS_SAT = "Disk IOPS saturation"
DISK_BW_SAT = "Disk throughput saturation"
_DISK_READ_OPS = "Composite Disk Read Operations/sec"
_DISK_WRITE_OPS = "Composite Disk Write Operations/sec"
_DISK_READ_BYTES = "Composite Disk Read Bytes/sec"
_DISK_WRITE_BYTES = "Composite Disk Write Bytes/sec"


def _num(value: Any) -> float | None:
    """Best-effort float (None on blank/garbage), for resource properties like IOPS limits."""
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    from app.exec.command_runner import parse_kql_rows
    return parse_kql_rows(stdout)


def _series_stats(series: list[dict[str, Any]]) -> dict[str, Any]:
    """{current, peak, avg, trend_pct} from a [{timestamp,value}] series (time-ordered)."""
    vals = [float(p["value"]) for p in series if p.get("value") is not None]
    if not vals:
        return {"current": None, "peak": None, "avg": None, "trend_pct": 0.0}
    current = vals[-1]
    peak = max(vals)
    avg = sum(vals) / len(vals)
    # Trend: compare the mean of the last third vs the first third of the window.
    third = max(1, len(vals) // 3)
    early = sum(vals[:third]) / third
    late = sum(vals[-third:]) / third
    trend_pct = round(100.0 * (late - early) / early, 1) if early else 0.0
    return {"current": round(current, 2), "peak": round(peak, 2), "avg": round(avg, 2), "trend_pct": trend_pct}


def _series_key(rec: dict[str, Any]) -> str:
    """Cache key for a metric series. Two alerts can share a metric name but differ by
    dimension filter (e.g. Key Vault ServiceApiResult split into 401/403 vs 429), so the
    filter is folded into the key to keep their series distinct."""
    metric = rec.get("metric", "")
    dim = (rec.get("dimension_filter") or "").strip()
    return f"{metric}|{dim}" if dim else metric


def _evaluate_metric(rec: dict[str, Any], arm_type: str, series: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate one AMBA metric alert against a metric series → a profile cell."""
    metric = rec.get("metric", "")
    unit = rec.get("unit", "")
    threshold = rec.get("threshold")
    operator = rec.get("operator", "GreaterThan")
    sem = metric_semantics(arm_type, metric, unit)
    higher_is_worse = sem["higher_is_worse"]
    # Operator overrides the default direction when present.
    if operator in ("LessThan", "LessThanOrEqual"):
        higher_is_worse = False
    elif operator in ("GreaterThan", "GreaterThanOrEqual"):
        higher_is_worse = True

    stats = _series_stats(series)
    # A dimension-filtered error COUNT (e.g. Transactions split to ResponseType 403/503)
    # returns an EMPTY series when zero such errors occurred — which is the HEALTHY case, not
    # "no data". Treat that as an observed 0 so a clean resource shows green ✓ rather than grey.
    if stats["current"] is None and rec.get("dimension_filter") and threshold == 0:
        stats = {"current": 0.0, "peak": 0.0, "avg": 0.0, "trend_pct": 0.0}
        series = [{"timestamp": "", "value": 0.0}]
    # The value we compare = the worst observed in the direction of concern.
    if stats["current"] is None:
        observed = None
    else:
        observed = stats["peak"] if higher_is_worse else min(
            float(p["value"]) for p in series if p.get("value") is not None
        )

    pct_of_threshold = None
    state = STATE_NODATA
    if observed is not None and threshold:
        if higher_is_worse:
            pct_of_threshold = round(100.0 * observed / float(threshold), 1)
            if observed >= float(threshold):
                state = STATE_BREACHING
            elif observed >= _APPROACH_FRAC * float(threshold):
                state = STATE_APPROACHING
            else:
                state = STATE_HEALTHY
        else:
            # Lower is worse (availability, health check, available memory): breach when
            # observed drops below threshold. "Approaching" is judged by position within the
            # healthy operating range [threshold, ceiling]: near the ceiling = healthy, near
            # the threshold = approaching. Falls back to a 5% relative margin when no ceiling.
            pct_of_threshold = round(100.0 * float(threshold) / observed, 1) if observed else None
            ceiling_v = sem.get("ceiling")
            if observed < float(threshold):
                state = STATE_BREACHING
            elif ceiling_v and ceiling_v > float(threshold):
                position = (observed - float(threshold)) / (ceiling_v - float(threshold))
                state = STATE_HEALTHY if position >= 0.5 else STATE_APPROACHING
            elif ceiling_v and float(threshold) >= ceiling_v:
                # The alert threshold is at (or above) the metric's ceiling — e.g. an
                # availability/health-probe metric that must stay at 100% (ceiling=100,
                # threshold=100). There is no operating range ABOVE the threshold, so meeting
                # it (observed >= threshold) is the best-possible state → healthy, not
                # "approaching". (Without this, a perfect 100% read shows amber at 100%.)
                state = STATE_HEALTHY
            else:
                state = STATE_APPROACHING if observed < float(threshold) * 1.05 else STATE_HEALTHY
    elif observed is not None and threshold == 0:
        # Count metric with a hard-zero threshold (e.g. ServerErrors, ThrottledRequests):
        # any nonzero is a breach signal.
        state = STATE_BREACHING if observed > 0 else STATE_HEALTHY
    elif observed is not None:
        # Informational metric (AMBA threshold None) — surfaced but not scored as a breach.
        state = STATE_HEALTHY

    # Headroom toward a fixed ceiling (e.g. 100% metrics), if defined.
    ceiling = sem.get("ceiling")
    headroom_pct = None
    if ceiling and observed is not None and higher_is_worse:
        headroom_pct = round(max(0.0, 100.0 * (ceiling - observed) / ceiling), 1)

    return {
        "alert_key": rec.get("key", ""),
        "metric": metric,
        "name": rec.get("name", ""),
        "amba_category": rec.get("amba_category", ""),
        "severity": rec.get("severity", "info"),
        "unit": unit,
        "operator": operator,
        "threshold": threshold,
        "aggregation": sem["aggregation"],
        "higher_is_worse": higher_is_worse,
        "current": stats["current"],
        "peak": stats["peak"],
        "avg": stats["avg"],
        "observed": None if observed is None else round(observed, 2),
        "pct_of_threshold": pct_of_threshold,
        "headroom_pct": headroom_pct,
        "trend_pct": stats["trend_pct"],
        "state": state,
        "why": rec.get("why", ""),
        "series": series[-60:],  # cap for payload size
    }


def _resource_score(cells: list[dict[str, Any]]) -> int:
    """Severity-weighted 0-100 performance score for a resource. Breaching costs the full
    weight, approaching costs half, healthy/no-data cost nothing."""
    scored = [c for c in cells if c["state"] != STATE_NODATA]
    if not scored:
        return 100
    total_w = sum(_SEV_WEIGHT.get(c["severity"], 1) for c in scored)
    if total_w == 0:
        return 100
    penalty = 0.0
    for c in scored:
        w = _SEV_WEIGHT.get(c["severity"], 1)
        if c["state"] == STATE_BREACHING:
            penalty += w
        elif c["state"] == STATE_APPROACHING:
            penalty += w * 0.5
    return max(0, round(100 * (1 - penalty / total_w)))


def compute_profile(
    resources: list[dict[str, Any]],
    metrics_by_resource: dict[str, dict[str, list[dict[str, Any]]]],
    *,
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure profile computation. ``metrics_by_resource``: resource_id(lower) → {metric →
    [{timestamp,value}]}. Returns scorecard + per-resource rows + ranked bottlenecks +
    heatmap matrix."""
    ref = reference if reference is not None else load_reference()
    ref_types: dict[str, Any] = ref.get("types", {})

    rows: list[dict[str, Any]] = []
    bottlenecks: list[dict[str, Any]] = []
    n_breaching = n_approaching = n_healthy = 0

    for res in resources:
        rtype = str(res.get("type", "")).lower()
        spec = ref_types.get(rtype)
        if not spec:
            continue
        rid = str(res.get("id", "")).lower()
        metric_alerts = [a for a in (spec.get("alerts") or []) if a.get("signal", "metric") == "metric" and a.get("metric")]
        if not metric_alerts:
            continue
        series_map = metrics_by_resource.get(rid, {})
        cells = [_evaluate_metric(rec, rtype, series_map.get(_series_key(rec), [])) for rec in metric_alerts]
        score = _resource_score(cells)
        worst = min(cells, key=lambda c: (_STATE_RANK.get(c["state"], 3), -(c.get("pct_of_threshold") or 0)), default=None)
        row_state = worst["state"] if worst else STATE_NODATA
        if row_state == STATE_BREACHING:
            n_breaching += 1
        elif row_state == STATE_APPROACHING:
            n_approaching += 1
        elif row_state == STATE_HEALTHY:
            n_healthy += 1

        rows.append(
            {
                "resource_id": res.get("id", ""),
                "resource_name": res.get("name", ""),
                "resource_type": rtype,
                "display": spec.get("display", rtype),
                "resource_group": res.get("resourceGroup", res.get("resource_group", "")),
                "subscription_id": res.get("subscriptionId", res.get("subscription_id", "")),
                "region": res.get("location", ""),
                "score": score,
                "state": row_state,
                "cells": cells,
            }
        )

        # Each non-healthy metric is a bottleneck candidate, ranked by pct_of_threshold.
        for c in cells:
            if c["state"] in (STATE_BREACHING, STATE_APPROACHING):
                bottlenecks.append(
                    {
                        "resource_id": res.get("id", ""),
                        "resource_name": res.get("name", ""),
                        "resource_type": rtype,
                        "metric": c["metric"],
                        "metric_name": c["name"],
                        "severity": c["severity"],
                        "state": c["state"],
                        "observed": c["observed"],
                        "threshold": c["threshold"],
                        "unit": c["unit"],
                        "pct_of_threshold": c["pct_of_threshold"],
                        "trend_pct": c["trend_pct"],
                        "why": c["why"],
                    }
                )

    bottlenecks.sort(
        key=lambda b: (_STATE_RANK.get(b["state"], 3), -(b.get("pct_of_threshold") or 0), _SEV_WEIGHT.get(b["severity"], 0) * -1)
    )
    rows.sort(key=lambda r: (r["score"], r["resource_name"]))

    scored_rows = [r for r in rows if r["state"] != STATE_NODATA]
    workload_score = round(sum(r["score"] for r in scored_rows) / len(scored_rows)) if scored_rows else 100

    return {
        "generated_at": _now_iso(),
        "scorecard": {
            "workload_score": workload_score,
            "resources_profiled": len(rows),
            "breaching": n_breaching,
            "approaching": n_approaching,
            "healthy": n_healthy,
            "bottleneck_count": len(bottlenecks),
        },
        "top_bottleneck": bottlenecks[0] if bottlenecks else None,
        "bottlenecks": bottlenecks,
        "resources": rows,
    }


# --------------------------------------------------------------------------- live gather
async def _query_resources(predicates: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.assessments.runner import query_resources_batched

    return await query_resources_batched(
        predicates,
        connection,
        projection="id, name, type, resourceGroup, subscriptionId, location, sku",
    )


def _parse_metric_series(stdout: str, aggregation: str) -> list[dict[str, Any]]:
    """Parse an `az monitor metrics list` JSON blob into [{timestamp, value}].

    A dimension filter (e.g. status code) can return MULTIPLE timeseries; for count-style
    metrics (Total/Count aggregation) those are summed per timestamp so the combined total
    is reported (e.g. 401 + 403). Other aggregations only ever have a single series."""
    try:
        data = json.loads(stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    agg = (aggregation or "average").lower()
    sum_mode = agg in ("total", "count")
    acc: dict[str, float] = {}
    for m in data.get("value", []) or []:
        for ts in (m.get("timeseries") or []):
            for pt in (ts.get("data") or []):
                t = pt.get("timeStamp") or pt.get("timestamp")
                if not t:
                    continue
                val = pt.get(agg)
                if val is None:
                    val = pt.get("average") or pt.get("maximum") or pt.get("total") or pt.get("count") or pt.get("minimum")
                if val is None:
                    continue
                if t in acc:
                    # Multiple dimension series at the same bucket: sum counts; for other
                    # aggregations keep the first (single-series metrics never collide).
                    if sum_mode:
                        acc[t] += float(val)
                else:
                    acc[t] = float(val)
    return [{"timestamp": t, "value": acc[t]} for t in sorted(acc)]


def _parse_combined_series(stdout: str) -> list[dict[str, Any]]:
    """Parse `az monitor metrics list` JSON and SUM every metric's datapoints per timestamp.

    Used to combine the separate read + write disk rate counters into one total-rate series
    (avg read/sec + avg write/sec = avg total/sec over the same bucket)."""
    try:
        data = json.loads(stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    acc: dict[str, float] = {}
    for m in data.get("value", []) or []:
        for ts in (m.get("timeseries") or []):
            for pt in (ts.get("data") or []):
                t = pt.get("timeStamp") or pt.get("timestamp")
                if not t:
                    continue
                val = pt.get("average")
                if val is None:
                    val = pt.get("total") or pt.get("maximum") or pt.get("count") or pt.get("minimum")
                if val is None:
                    continue
                acc[t] = acc.get(t, 0.0) + float(val)
    return [{"timestamp": t, "value": acc[t]} for t in sorted(acc)]


async def _hydrate_disk_limits(targets: list[dict[str, Any]], connection: dict[str, Any] | None) -> None:
    """Backfill provisioned IOPS / MB-per-second onto in-scope managed-disk resources.

    The main resource query stays light (no `properties`); this runs ONE supplementary ARG
    query for just the disk ids so we can compute each disk's saturation against its own
    provisioned limits. No-op when the scope has no disks."""
    disk_ids = [
        str(r.get("id", "")) for r in targets
        if str(r.get("type", "")).lower() == DISK_TYPE and r.get("id")
    ]
    if not disk_ids:
        return
    from app.assessments.runner import query_resources_batched

    id_list = ", ".join(f"'{_esc(i.lower())}'" for i in disk_ids)
    pred = f"type =~ '{DISK_TYPE}' and tolower(id) in ({id_list})"
    try:
        rows = await query_resources_batched(
            [pred], connection,
            projection="id, provisioned_iops=properties.diskIOPSReadWrite, provisioned_mbps=properties.diskMBpsReadWrite",
        )
    except RuntimeError:
        return
    by_id = {str(r.get("id", "")).lower(): r for r in rows}
    for r in targets:
        if str(r.get("type", "")).lower() == DISK_TYPE:
            hit = by_id.get(str(r.get("id", "")).lower())
            if hit:
                r["provisioned_iops"] = hit.get("provisioned_iops")
                r["provisioned_mbps"] = hit.get("provisioned_mbps")


async def _disk_saturation_series(
    res: dict[str, Any],
    connection: dict[str, Any] | None,
    *,
    interval: str,
    start: str,
    end: str,
    sem_lock: "asyncio.Semaphore",
    run_metrics_capture,
) -> dict[str, list[dict[str, Any]]]:
    """Derive a managed disk's IOPS + throughput saturation as a % of its provisioned limits.

    Fetches the Composite read+write counters, sums them to a total rate, and divides by the
    disk's provisioned IOPS / MB-per-second (hydrated by ``_hydrate_disk_limits``). Returns a
    {label: %-series} map consumed exactly like any other metric series."""
    rid = res.get("id", "")
    iops_limit = _num(res.get("provisioned_iops"))
    mbps_limit = _num(res.get("provisioned_mbps"))
    out: dict[str, list[dict[str, Any]]] = {}

    if iops_limit and iops_limit > 0:
        async with sem_lock:
            cap = await run_metrics_capture(
                rid, [_DISK_READ_OPS, _DISK_WRITE_OPS], connection,
                aggregation="Average", interval=interval,
                timespan=start or None, end_time=end or None,
            )
        if cap.ok:
            total = _parse_combined_series(cap.stdout)
            out[DISK_IOPS_SAT] = [
                {"timestamp": p["timestamp"], "value": round(100.0 * p["value"] / iops_limit, 2)}
                for p in total
            ]

    if mbps_limit and mbps_limit > 0:
        async with sem_lock:
            cap = await run_metrics_capture(
                rid, [_DISK_READ_BYTES, _DISK_WRITE_BYTES], connection,
                aggregation="Average", interval=interval,
                timespan=start or None, end_time=end or None,
            )
        if cap.ok:
            total = _parse_combined_series(cap.stdout)
            # bytes/sec → MB/sec (÷1e6) → % of provisioned MB/sec.
            out[DISK_BW_SAT] = [
                {"timestamp": p["timestamp"], "value": round(100.0 * (p["value"] / 1_000_000.0) / mbps_limit, 2)}
                for p in total
            ]
    return out


async def profile_workload(
    connection: dict[str, Any] | None,
    *,
    scope_kind: str,
    scope_id: str,
    workload: dict[str, Any] | None,
    timespan: str = "P1D",
    interval: str = "PT15M",
    scan_cap: int = 200,
    start_time: str = "",
    end_time: str = "",
    progress=None,
) -> dict[str, Any]:
    from app.assessments.runner import _resolve_scope, scope_predicate_batches
    from app.exec.command_runner import run_metrics_capture

    # Resolve the effective metric window. An explicit start/end range wins; otherwise the
    # duration window (e.g. P1D) is converted to an absolute --start-time for correctness.
    requested_window = "" if (start_time and end_time) else timespan
    if start_time and end_time:
        eff_start, eff_end, window_label = start_time, end_time, f"{start_time} → {end_time}"
    else:
        eff_start = _window_to_start(timespan)
        eff_end = ""
        window_label = timespan

    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        predicate = scope.get("predicate") or ""
        if scope.get("error") and not predicate:
            return _empty(scope_kind, scope_id, error=scope["error"])
        predicates = scope_predicate_batches(scope)
    elif scope_kind == "subscription" and scope_id:
        predicates = [f"subscriptionId =~ '{_esc(scope_id)}'"]
    else:
        return _empty(scope_kind, scope_id, error="No resolvable scope.")

    try:
        resources = await _query_resources(predicates, connection)
    except RuntimeError as exc:
        return _empty(scope_kind, scope_id, error=str(exc)[:300])

    ref_types = load_reference().get("types", {})
    targets = [r for r in resources if str(r.get("type", "")).lower() in ref_types][:scan_cap]
    # Managed disks evaluate against their own provisioned IOPS/MB-per-second, so hydrate
    # those limits for any in-scope disks before gathering metrics.
    await _hydrate_disk_limits(targets, connection)

    sem_lock = asyncio.Semaphore(6)
    metrics_by_resource: dict[str, dict[str, list[dict[str, Any]]]] = {}

    async def _gather(res: dict[str, Any]) -> None:
        rtype = str(res.get("type", "")).lower()
        spec = ref_types.get(rtype) or {}
        alerts = [a for a in (spec.get("alerts") or []) if a.get("signal", "metric") == "metric" and a.get("metric")]
        rid = str(res.get("id", "")).lower()
        out: dict[str, list[dict[str, Any]]] = {}
        if rtype == DISK_TYPE:
            # Disks: derive saturation % from the Composite counters + provisioned limits.
            out = await _disk_saturation_series(
                res, connection, interval=interval, start=eff_start, end=eff_end,
                sem_lock=sem_lock, run_metrics_capture=run_metrics_capture,
            )
        else:
            for rec in alerts:
                metric = rec.get("metric", "")
                sem = metric_semantics(rtype, metric, rec.get("unit", ""))
                async with sem_lock:
                    cap = await run_metrics_capture(
                        res.get("id", ""), [metric], connection,
                        aggregation=sem["aggregation"], interval=interval,
                        timespan=eff_start or None, end_time=eff_end or None,
                        dimension_filter=rec.get("dimension_filter") or None,
                    )
                if cap.ok:
                    out[_series_key(rec)] = _parse_metric_series(cap.stdout, sem["aggregation"])
        metrics_by_resource[rid] = out
        if progress is not None:
            await progress(res.get("name", ""), rtype)

    await asyncio.gather(*[_gather(r) for r in targets])

    snap = compute_profile(targets, metrics_by_resource)
    snap["all_resources"] = build_all_resources(resources, ref_types)
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": (workload or {}).get("name") if scope_kind == "workload" else scope_id,
            "connection_configured": connection is not None,
            "source": "azure_monitor_metrics",
            "window": window_label,
            "requested_window": requested_window,
            "requested_start": start_time,
            "requested_end": end_time,
            "interval": interval,
            "demo": False,
            "error": "",
        }
    )
    return snap


def _window_to_start(window: str) -> str:
    """Convert an ISO-8601 duration window (P1D, PT6H, P7D, P30D…) to an absolute UTC
    --start-time. Returns "" when it can't parse (CLI then defaults to the last hour)."""
    import re

    w = (window or "").strip().upper()
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$", w)
    if not m or not any(m.groups()):
        return ""
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    from datetime import timedelta

    start = datetime.now(timezone.utc) - timedelta(days=days, hours=hours, minutes=minutes)
    return start.replace(microsecond=0).isoformat()


def _empty(scope_kind: str, scope_id: str, *, error: str) -> dict[str, Any]:
    snap = compute_profile([], {})
    snap["all_resources"] = []
    snap.update(
        {
            "scope_kind": scope_kind, "scope_id": scope_id, "scope_name": scope_id,
            "connection_configured": False, "source": "azure_monitor_metrics", "window": "",
            "demo": False, "error": error,
        }
    )
    return snap

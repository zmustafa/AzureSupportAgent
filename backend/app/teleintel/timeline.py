"""Cross-signal correlation timeline.

Buckets ≥4 telemetry signals — failure rate, latency p95, exception volume, dependency
failure rate — onto a shared time axis and overlays deploy/config change events from
Resource Graph ``resourcechanges``, so cause precedes effect visually."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.teleintel import kql_library as KQL
from app.teleintel.resolver import run_component_kql

log = logging.getLogger("app.teleintel.timeline")


def _index_by_ts(rows: list[dict[str, Any]], *fields: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        ts = str(r.get("timestamp", ""))
        if not ts:
            continue
        bucket = out.setdefault(ts, {"timestamp": ts})
        for f in fields:
            if r.get(f) is not None:
                bucket[f] = r[f]
    return out


async def build_timeline(
    component: dict[str, Any],
    connection: dict[str, Any] | None,
    *,
    predicate: str = "",
    timespan: str = "P1D",
    bin_minutes: int = 5,
) -> dict[str, Any]:
    """Return {series_keys, points[], change_events[], notes}. Each point is a dict keyed
    by timestamp with the available signal values merged in."""
    notes: list[str] = []
    merged: dict[str, dict[str, Any]] = {}

    async def _add(kql: str, *fields: str, label: str) -> None:
        res = await run_component_kql(component, kql, connection, timespan=timespan)
        if not res.get("ok"):
            notes.append(f"{label}: {res.get('error', 'unavailable')[:80]}")
            return
        for ts, vals in _index_by_ts(res.get("rows", []) or [], *fields).items():
            merged.setdefault(ts, {"timestamp": ts}).update(vals)

    await _add(KQL.failure_rate_timeseries(bin_minutes), "failure_rate_pct", "failed", "total", label="failure rate")
    await _add(KQL.latency_p95_timeseries(bin_minutes), "p95_ms", "p50_ms", label="latency p95")
    await _add(KQL.exception_volume_timeseries(bin_minutes), "exceptions", label="exceptions")
    await _add(KQL.dependency_health_timeseries(bin_minutes), "dep_failure_pct", "failures", label="dependency health")

    points = sorted(merged.values(), key=lambda p: p["timestamp"])
    change_events = await _change_events(predicate, connection)
    series_keys = ["failure_rate_pct", "p95_ms", "exceptions", "dep_failure_pct"]
    return {
        "series_keys": series_keys,
        "points": points,
        "change_events": change_events,
        "bin_minutes": bin_minutes,
        "signal_count": sum(1 for k in series_keys if any(k in p for p in points)),
        "notes": "; ".join(notes),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _change_events(predicate: str, connection: dict[str, Any] | None, *, hours: int = 48) -> list[dict[str, Any]]:
    from app.exec.command_runner import run_kql_capture

    if not predicate:
        return []
    kql = (
        "resourcechanges "
        "| extend ts=todatetime(properties.changeAttributes.timestamp), "
        "ct=tostring(properties.changeType), targetId=tostring(properties.targetResourceId) "
        f"| where ts > ago({int(hours)}h) "
        "| project ts, ct, targetId | order by ts desc | take 50"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    if not cap.ok:
        return []
    try:
        data = json.loads(cap.stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("data") or []
    out = []
    for r in data if isinstance(data, list) else []:
        out.append(
            {
                "timestamp": r.get("ts", ""),
                "change_type": r.get("ct", ""),
                "target": str(r.get("targetId", "")).rsplit("/", 1)[-1],
                "target_id": r.get("targetId", ""),
            }
        )
    return out

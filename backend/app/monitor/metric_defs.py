"""Azure Monitor metric *definitions* — the catalog the portal shows under Resource → Metrics.

``az monitor metrics list-definitions`` returns, for a resource, every available metric with
its primary aggregation, supported aggregations, and unit. The catalog is identical for every
instance of a resource TYPE (all storage accounts expose the same metrics), so results are
cached by ARM type to avoid repeated CLI calls.

This lets the agent's metric tool:
* pick each metric's CORRECT aggregation (counts use Total/Count, gauges use Average/Maximum),
* fill sensible default metrics for resource types not in the AMBA reference set, and
* return a helpful "available metrics" list when the model asks for a name that doesn't exist.
All READ-ONLY.
"""
from __future__ import annotations

import time
from typing import Any

from app.exec.command_runner import run_az_json_capture

from .datasources.base import parse_json_output

# arm_type (lowercase) -> (expires_epoch, list[MetricDef])
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_TTL = 3600.0  # 1h — metric catalogs change very rarely
_MAX = 256


def _arm_type(resource_id: str) -> str:
    parts = [p for p in (resource_id or "").split("/") if p]
    low = [p.lower() for p in parts]
    if "providers" in low:
        i = low.index("providers")
        if i + 2 < len(parts):
            # Include any sub-type segments (e.g. servers/databases) for an exact catalog key.
            tail = parts[i + 1 :]
            # provider + type[/subtype...] — drop the instance names (every other segment).
            segs = [tail[0]] + tail[1::2]
            return "/".join(segs).lower()
    return ""


def _parse(stdout: str) -> list[dict[str, Any]]:
    data, _ = parse_json_output(stdout)
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        name = name.get("value") if isinstance(name, dict) else name
        if not name:
            continue
        disp = d.get("name")
        disp = disp.get("localizedValue") if isinstance(disp, dict) else None
        out.append(
            {
                "name": str(name),
                "display": str(disp or name),
                "primary": str(d.get("primaryAggregationType") or "Average"),
                "supported": [str(a) for a in (d.get("supportedAggregationTypes") or [])],
                "unit": str(d.get("unit") or ""),
            }
        )
    return out


async def get_metric_definitions(
    resource_id: str, conn: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Return the metric catalog for a resource (cached by ARM type). Empty list on failure."""
    key = _arm_type(resource_id)
    now = time.time()
    if key:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    cap = await run_az_json_capture(
        ["monitor", "metrics", "list-definitions", "--resource", resource_id],
        conn,
        label="az monitor metrics list-definitions",
    )
    if not cap.ok:
        return []
    defs = _parse(cap.stdout)
    if key and defs:
        if len(_CACHE) >= _MAX:
            _CACHE.clear()
        _CACHE[key] = (now + _TTL, defs)
    return defs


def index_by_name(defs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Lowercased-name -> definition, for quick per-metric lookup."""
    return {d["name"].lower(): d for d in defs}


def clear_cache() -> None:
    _CACHE.clear()

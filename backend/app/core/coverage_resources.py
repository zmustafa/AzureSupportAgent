"""Shared helper for the coverage dashboards (AMBA / Telemetry / Backup-DR).

The "All Resources" tab on each coverage screen lists *every* resource in the workload /
subscription scope — not just the ones the reference set covers — so users can see the full
footprint and which resources fall outside the reference. This projects the raw Resource
Graph rows onto a compact, UI-friendly shape with an ``in_reference`` flag.
"""
from __future__ import annotations

from typing import Any, Iterable


def build_all_resources(
    resources: Iterable[dict[str, Any]], ref_types: dict[str, Any]
) -> list[dict[str, Any]]:
    """Flat list of every in-scope resource, annotated with whether the reference covers it.

    Accepts raw Resource Graph rows (camelCase keys) or already-normalised rows (snake_case);
    sorted by type then name for a stable grid."""
    out: list[dict[str, Any]] = []
    for r in resources:
        rtype = str(r.get("type", "")).lower()
        out.append(
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "type": rtype,
                "resource_group": r.get("resourceGroup", r.get("resource_group", "")),
                "subscription_id": r.get("subscriptionId", r.get("subscription_id", "")),
                "location": r.get("location", "") or r.get("region", ""),
                "in_reference": rtype in ref_types,
            }
        )
    out.sort(key=lambda x: (x["type"], (x["name"] or "").lower()))
    return out

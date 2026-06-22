"""Whole-estate / event-volume guardrail for the Change Explorer (mirrors tagintel.scale and
workloads.autopilot). Bounds how many change events one analysis pass enumerates so a noisy
window or a tenant-wide scope can't melt the estate."""
from __future__ import annotations

from typing import Any, Sequence

# Max change events one analysis run keeps. Above this the run is flagged ``truncated`` and the
# UI recommends a narrower range/scope.
MAX_EVENTS = 5000

# Per-subscription Activity Log page cap (the CLI pages itself; this bounds our own slicing).
MAX_PER_SUBSCRIPTION = 1000


def cap_events(events: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Bound a list of events to ``MAX_EVENTS``. Returns ``(capped, truncated)``."""
    if len(events) <= MAX_EVENTS:
        return list(events), False
    return list(events[:MAX_EVENTS]), True

"""Whole-estate guardrail for Tag Intelligence.

Mirrors ``app.workloads.autopilot``: every full-estate enumeration is capped at
``MAX_ESTATE`` resources, and any per-resource AI / clustering pass is map-reduced in
``AI_BATCH``-sized chunks. Above the cap the analysis degrades to a bounded sample (and the
caller surfaces a ``truncated`` flag) rather than failing or melting the estate.
"""
from __future__ import annotations

from typing import Any, Iterable, Iterator, Sequence, TypeVar

# Whole-estate cap on resources we enumerate / analyse in one pass (the "max 5k estate"
# contract). Kept identical to workloads.autopilot._MAX_RESOURCES so both flagship features
# scale the same way.
MAX_ESTATE = 5000

# Per-batch resource budget for AI / clustering passes; larger estates map-reduce.
AI_BATCH = 500

T = TypeVar("T")


def cap_estate(resources: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Bound a resource list to ``MAX_ESTATE``. Returns ``(capped, truncated)`` where
    ``truncated`` is True when the input exceeded the cap (so callers can badge the result and
    recommend a narrower scope)."""
    if len(resources) <= MAX_ESTATE:
        return list(resources), False
    return list(resources[:MAX_ESTATE]), True


def batches(seq: Iterable[T], size: int = AI_BATCH) -> Iterator[list[T]]:
    """Yield ``seq`` in lists of at most ``size`` (default ``AI_BATCH``)."""
    size = max(1, int(size))
    batch: list[T] = []
    for item in seq:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch

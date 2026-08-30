"""Elapsed time and a *measured* estimate for long-running IAM work.

Two numbers belong on any progress indicator that runs for tens of seconds: how long it has
been going, and how much longer it will take. The first is arithmetic. The second is a claim,
and this module exists so that claim is only ever made from evidence.

The rule: **an estimate comes from this tenant's own previous runs, or it is not given.**
A default constant dressed up as an ETA is worse than no ETA — a bar that says "8 seconds
remaining" for four minutes teaches people the number is decorative, and after that no progress
indicator in the product is believed. So a first run reports ``None`` and the UI says "no
estimate yet (first run)", which is both honest and, once, unavoidable.

Estimates use the **median** of recent runs rather than the mean: one throttled 429-riddled
refresh is an outlier that should not move the estimate for the next ten.
"""
from __future__ import annotations

import statistics
from typing import Any

from app.iam import cache

STATE_KEY = "run_durations"

# Enough to absorb a slow outlier, few enough that a genuine change in tenant size shows up
# quickly rather than being averaged away for weeks.
MAX_SAMPLES = 7


def _read(tenant_id: str) -> dict[str, list[float]]:
    raw = cache.read_state(tenant_id, STATE_KEY)
    out: dict[str, list[float]] = {}
    for key, values in (raw or {}).items():
        if isinstance(values, list):
            out[str(key)] = [float(v) for v in values if isinstance(v, (int, float))]
    return out


def record(tenant_id: str, kind: str, seconds: float) -> None:
    """Remember how long a completed run of ``kind`` took."""
    if seconds <= 0:
        return
    def _mutate(data: dict[str, Any]) -> None:
        samples = data.setdefault(kind, [])
        samples.append(round(float(seconds), 2))
        del samples[:-MAX_SAMPLES]

    cache.mutate_state(tenant_id, STATE_KEY, _mutate)


def estimate(tenant_id: str, kind: str) -> tuple[float | None, str]:
    """``(seconds, basis)`` for a run of ``kind``.

    ``seconds`` is None when this tenant has never completed one, and ``basis`` always explains
    where the number came from so the UI can show it rather than presenting a bare figure as
    fact."""
    samples = _read(tenant_id).get(kind) or []
    if not samples:
        return None, "no previous run to estimate from"
    value = statistics.median(samples)
    n = len(samples)
    return value, f"median of the last {n} run{'s' if n != 1 else ''} on this tenant"


def remaining(tenant_id: str, kind: str, elapsed: float) -> tuple[float | None, str]:
    """Seconds left, or None when unknown.

    Never returns a negative number and never returns zero while the job is still running: a
    run that has already outlasted its estimate is *overdue*, not finished, and claiming
    "0 seconds remaining" for a minute is the fastest way to make the whole indicator
    untrustworthy."""
    total, basis = estimate(tenant_id, kind)
    if total is None:
        return None, basis
    left = total - elapsed
    if left <= 0:
        return None, f"taking longer than usual ({basis})"
    return left, basis


def format_seconds(seconds: float | None) -> str:
    """`1:04`, `12s`, or an em dash when there is nothing to say."""
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}:{seconds % 60:02d}"


def public(tenant_id: str, kind: str, elapsed: float) -> dict[str, Any]:
    """The progress block shared by the SSE stream and the job endpoints."""
    left, basis = remaining(tenant_id, kind, elapsed)
    total, _ = estimate(tenant_id, kind)
    return {
        "elapsed_seconds": round(elapsed, 1),
        "elapsed_label": format_seconds(elapsed),
        "eta_seconds": round(left, 1) if left is not None else None,
        "eta_label": format_seconds(left),
        "eta_basis": basis,
        "typical_seconds": round(total, 1) if total is not None else None,
    }

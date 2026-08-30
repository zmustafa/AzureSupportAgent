"""Event-loop lag monitor.

This application runs a single uvicorn worker, so **one** blocked coroutine stops every request
in the product — login, the dashboard, an unrelated inventory query. That has happened twice
here already: a 40-second right-sizing analysis run inline in an async handler, and an IAM
refresh doing gzip + file writes and a full row recompose on the loop. Both presented
identically to a user ("the app froze") and both were mis-diagnosed as database locking,
because the visible symptom is SQLite `database is locked` on unrelated session writes — an
`await db.commit()` cannot resume while the loop is not scheduling anything.

So the loop's own health is measured directly. The probe sleeps for a fixed interval and
compares the wall time it actually slept against the interval it asked for; the difference is
time the loop spent unable to run a ready callback. It is the cheapest possible detector: one
`asyncio.sleep` per interval and two clock reads.

It is a DETECTOR, not a fix. When it fires, the message names the lag so the next question is
"what ran just then", not "is it the database".
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
import threading
import time

log = logging.getLogger("app.core.loopwatch")

# How often to probe. Short enough to catch a multi-second stall in progress, long enough to be
# free: at 250 ms this is four wake-ups a second and nothing else.
INTERVAL_S = 0.25

# Report at/above this much lag. A healthy loop under load drifts by a few milliseconds; a
# blocking call shows up in hundreds. 0.5 s is well clear of scheduler noise and well below the
# point a human calls the app frozen.
DEFAULT_THRESHOLD_S = 0.5

#: Only sample a stack for the serious stalls. Walking frames on every sub-second blip would
#: add cost to the exact situation the monitor exists to observe.
_STACK_THRESHOLD_S = float(os.getenv("LOOPWATCH_STACK_THRESHOLD_S", "2.0"))

_task: asyncio.Task | None = None
_max_lag_s = 0.0
_events = 0
_recent_lags: deque[tuple[float, float]] = deque()

# Readiness must recover after a transient stall. Keep only a short rolling history rather than
# making the all-time maximum a sticky reason to drain a healthy replica forever.
RECENT_HISTORY_S = 120.0


def _record_lag(lag_s: float, *, observed_at: float | None = None) -> None:
    """Record one sample and discard history too old to affect any readiness decision."""
    now = time.monotonic() if observed_at is None else observed_at
    _recent_lags.append((now, max(0.0, lag_s)))
    cutoff = now - RECENT_HISTORY_S
    while _recent_lags and _recent_lags[0][0] < cutoff:
        _recent_lags.popleft()


def recent_max_lag_s(window_s: float = 30.0, *, now: float | None = None) -> float:
    """Largest lag in a bounded recent window; old incidents deliberately age out."""
    observed_at = time.monotonic() if now is None else now
    cutoff = observed_at - max(0.0, window_s)
    return round(max((lag for stamp, lag in _recent_lags if stamp >= cutoff), default=0.0), 3)


def stats() -> dict[str, float | int | bool]:
    """Observed loop health since process start (or since :func:`reset`)."""
    return {
        "running": _task is not None and not _task.done(),
        "max_lag_s": round(_max_lag_s, 3),
        "recent_max_lag_s": recent_max_lag_s(),
        "events": _events,
    }


def reset() -> None:
    """Zero the counters. For tests and for before/after measurement runs."""
    global _max_lag_s, _events
    _max_lag_s = 0.0
    _events = 0
    _recent_lags.clear()


def _blocking_stack(limit: int = 12) -> str:
    """Where the loop's own thread is executing right now.

    The probe runs immediately AFTER the stall, so the frame that blocked has usually returned
    and this is a best-effort sample rather than proof. It is still the difference between
    "something blocked for 72 seconds" and a filename: eight thousand warnings that name no
    code tell you there is a problem and nothing about where to look.

    Frames from the standard library, site-packages and this module are dropped so application
    code is what survives.
    """
    import sys
    import traceback

    try:
        frames = sys._current_frames()
    except Exception:  # pragma: no cover - not available on every implementation
        return ""
    frame = frames.get(threading.main_thread().ident or 0)
    if frame is None:
        return ""
    try:
        stack = traceback.extract_stack(frame)
    except Exception:  # pragma: no cover
        return ""
    ours = [
        f"{f.filename.rsplit('/app/', 1)[-1]}:{f.lineno} in {f.name}"
        for f in stack
        if "site-packages" not in f.filename
        and "/lib/python" not in f.filename
        and "loopwatch" not in f.filename
    ]
    return " <- ".join(reversed(ours[-limit:]))


async def _probe(threshold_s: float) -> None:
    global _max_lag_s, _events
    while True:
        before = time.monotonic()
        await asyncio.sleep(INTERVAL_S)
        lag = time.monotonic() - before - INTERVAL_S
        _record_lag(lag)
        if lag > _max_lag_s:
            _max_lag_s = lag
        if lag >= threshold_s:
            _events += 1
            # `debug=True` on the loop additionally names the offending handle. This message is
            # the trigger to go and look; the stack sample is a hint, not a verdict.
            where = _blocking_stack() if lag >= _STACK_THRESHOLD_S else ""
            log.warning(
                "event loop blocked for %.2fs (threshold %.2fs) — a synchronous call is running "
                "on the loop; every request in the process was stalled for that long%s",
                lag, threshold_s, f" | main thread at: {where}" if where else "",
            )


def start(threshold_s: float | None = None) -> None:
    """Begin monitoring. Idempotent; safe to call when no loop is running yet."""
    global _task
    if _task is not None and not _task.done():
        return
    if os.getenv("LOOPWATCH_ENABLED", "1").lower() in ("0", "false", "no"):
        return
    threshold = threshold_s if threshold_s is not None else float(os.getenv("LOOPWATCH_THRESHOLD_S", DEFAULT_THRESHOLD_S))
    try:
        _task = asyncio.get_running_loop().create_task(_probe(threshold))
    except RuntimeError:  # pragma: no cover - no running loop (import-time / sync tests)
        _task = None


async def stop() -> None:
    """Cancel the probe. Called from shutdown so the loop can close cleanly."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
        pass
    _task = None

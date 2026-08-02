"""One place to run IAM's CPU-bound work, off the event loop and under a concurrency cap.

Three separate incidents in this product had the same root cause: an expensive synchronous
computation running inline in an async handler. One event loop is shared by every request, so
that does not slow a screen down — it stops the whole application, and it surfaces as SQLite
`database is locked` on unrelated writes because an awaited commit cannot resume while nothing
is being scheduled.

`asyncio.to_thread` fixes the blocking. Measured on a real 5,514-row tenant: the same work costs
**0.98 s of event-loop lag inline and 0.04 s in a thread**.

What a thread does NOT fix is the GIL. Pure-Python CPU work in a worker thread still holds the
interpreter lock between switch intervals, so while it runs, every other request gets slower —
measured at the HTTP boundary during a 43-second graph rebuild, `/healthz` (no auth, no DB) went
from 0 ms to 63 ms median while authenticated endpoints reached seconds. That is degradation,
not a freeze, and it is bounded here rather than left to multiply:

* the **semaphore** stops N heavy jobs stacking. Two users opening Findings, a scheduled refresh
  and an export used to mean four simultaneous CPU burns competing for one GIL, which is
  indistinguishable from a freeze however well each one is threaded. The work is memoised, so a
  caller that waits usually finds the answer already built when its turn comes.
* the **cap is small on purpose.** More workers do not make Python CPU work finish sooner; they
  only spread the same GIL across more contenders and make every interactive request slower.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, TypeVar

log = logging.getLogger("app.iam.cpu")

T = TypeVar("T")

#: How many IAM CPU jobs may run at once. Not derived from the core count: the limit here is the
#: GIL, not the CPUs, so a 24-core box is no more able to run four of these concurrently than a
#: 2-core one. Tunable for operators who move this work to a dedicated instance.
MAX_CONCURRENT = max(1, int(os.getenv("IAM_CPU_WORKERS", "2")))

#: Log any job that runs longer than this so a slow computation is visible without a profiler.
SLOW_JOB_S = float(os.getenv("IAM_CPU_SLOW_S", "5"))

_sema: asyncio.Semaphore | None = None
_inflight = 0


def _semaphore() -> asyncio.Semaphore:
    # Created lazily on first use so importing this module never needs a running loop.
    global _sema
    if _sema is None:
        _sema = asyncio.Semaphore(MAX_CONCURRENT)
    return _sema


def stats() -> dict[str, int]:
    """How many CPU jobs are running or queued, for diagnostics."""
    return {"max_concurrent": MAX_CONCURRENT, "inflight": _inflight}


async def run(fn: Callable[..., T], *args: Any, label: str = "", **kwargs: Any) -> T:
    """Run a synchronous IAM computation off the event loop, one of at most ``MAX_CONCURRENT``.

    Use for anything that recomposes the estate, indexes role definitions, builds the escalation
    graph, evaluates signals or serialises a full row set. Do NOT use it for I/O — an Azure call
    belongs in the async client, and holding a CPU slot across a network round trip would block
    real CPU work behind a socket."""
    global _inflight
    _inflight += 1
    try:
        async with _semaphore():
            started = time.monotonic()
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            finally:
                elapsed = time.monotonic() - started
                if elapsed >= SLOW_JOB_S:
                    log.info(
                        "iam cpu: %s took %.1fs (other requests are slower while this runs)",
                        label or getattr(fn, "__name__", "job"), elapsed,
                    )
    finally:
        _inflight -= 1

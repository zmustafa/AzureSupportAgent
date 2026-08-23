"""Bounded concurrent fan-out.

The repository has ~48 ad-hoc ``asyncio.Semaphore`` sites and ~188 loops that await one
item at a time. Both shapes are re-derived per caller, and the serial ones are the reason a
multi-subscription read takes N times longer than a single-subscription one.

Two properties matter more than raw speed here, and are why this is a shared primitive
rather than a `gather` at each call site:

* **Order is preserved.** Results come back positionally aligned with ``items``. Collectors
  zip results against their input (subscription ids, resource ids); a completion-ordered
  result silently mis-attributes data to the wrong scope, which is worse than being slow.
* **A failure is visible.** The default re-raises. Azure fan-out failures are usually
  throttling or permission gaps, and a helper that quietly returned partial results would
  render as "this scope has no resources" — the reassuring reading of missing data.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

#: Default ceiling. Azure Resource Graph and Microsoft Graph both throttle per-tenant, so
#: more concurrency past this point converts into 429s and retries rather than throughput.
DEFAULT_LIMIT = 8


async def bounded(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limit: int = DEFAULT_LIMIT,
    return_exceptions: bool = False,
) -> list[R]:
    """Run ``worker`` over ``items`` with at most ``limit`` in flight.

    Returns results in the order of ``items``, not completion order.

    With ``return_exceptions=True`` a failed item yields its exception in place, so the
    caller can report a partial result *and say which scopes failed*. ``CancelledError`` is
    always propagated — a cancelled refresh must not look like a completed one.
    """
    seq = list(items)
    if not seq:
        return []
    if limit <= 0:
        raise ValueError("limit must be >= 1")
    if len(seq) == 1:
        # One item cannot benefit from a semaphore, and skipping it keeps tracebacks flat.
        try:
            return [await worker(seq[0])]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - mirrors gather(return_exceptions=True)
            if return_exceptions:
                return [exc]  # type: ignore[list-item]
            raise

    gate = asyncio.Semaphore(min(limit, len(seq)))

    async def _run(item: T) -> R:
        async with gate:
            return await worker(item)

    results = await asyncio.gather(
        *(_run(item) for item in seq), return_exceptions=return_exceptions,
    )
    if return_exceptions:
        for r in results:
            if isinstance(r, asyncio.CancelledError):
                raise r
    return list(results)


async def bounded_map(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limit: int = DEFAULT_LIMIT,
) -> tuple[dict[T, R], dict[T, BaseException]]:
    """``(ok, failed)`` keyed by item, for callers that must report which scopes failed.

    Requires hashable items — it is meant for id-shaped inputs (subscription id, resource id).
    """
    seq = list(items)
    out = await bounded(seq, worker, limit=limit, return_exceptions=True)
    ok: dict[T, R] = {}
    failed: dict[T, BaseException] = {}
    for item, result in zip(seq, out, strict=True):
        if isinstance(result, BaseException):
            failed[item] = result
        else:
            ok[item] = result
    return ok, failed

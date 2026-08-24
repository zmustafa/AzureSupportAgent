"""Backoff for background loops that share the database pool.

Every one of these loops used a flat ``await asyncio.sleep(1.0)`` on failure. That is fine when
the failure is a one-off and actively harmful when it is contention: during a pool exhaustion
each worker waited the full checkout timeout, failed, slept one second and asked again, so the
loops held the pool busy for as long as the condition lasted and the request path never got a
connection back. The workers are in a loop; the person waiting on the login page is not.

Two behaviours matter here:

* **Exponential, with jitter.** Without jitter four workers that failed together retry together
  forever, which is the same thundering herd one step removed.
* **Pool exhaustion is not an ordinary error.** It means "the database has no room for you",
  and the only useful response is to get out of the way for materially longer.
"""
from __future__ import annotations

import asyncio
import random


class Backoff:
    """Escalating delay with jitter, reset on every success."""

    def __init__(self, base: float = 1.0, cap: float = 60.0, starved_cap: float = 120.0) -> None:
        self.base = base
        self.cap = cap
        self.starved_cap = starved_cap
        self.failures = 0

    def reset(self) -> None:
        self.failures = 0

    def delay(self, *, starved: bool = False) -> float:
        self.failures += 1
        cap = self.starved_cap if starved else self.cap
        # Full jitter: uniform over [0, backoff]. Keeps workers that failed together from
        # waking together, which a fixed multiplier does not.
        return random.uniform(0.0, min(cap, self.base * 2 ** min(self.failures - 1, 10)))

    async def sleep(self, *, starved: bool = False) -> float:
        seconds = self.delay(starved=starved)
        await asyncio.sleep(seconds)
        return seconds


def is_pool_exhausted(exc: BaseException) -> bool:
    """True when the failure is "no database connection was available".

    Matched on the SQLAlchemy type rather than the message so a wording change upstream cannot
    silently turn this back into an ordinary retry."""
    try:
        from sqlalchemy.exc import TimeoutError as SATimeoutError
    except Exception:  # pragma: no cover - sqlalchemy always present in the app
        return False
    return isinstance(exc, SATimeoutError)

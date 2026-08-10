"""Process-wide admission control for Performance Profiler metric reads.

A profile used to create its own semaphore, so three fleet items each admitted six Azure
Monitor calls (18 Azure CLI processes on the one-CPU production container).  This gate is
shared by every profiler entry point in the process: Fleet, the single-scope screen, Mission
Control, and the investigation tool.

The deployed app is deliberately fixed at one replica.  If that changes, this process-local
gate must be backed by a distributed lease (for example Redis) before increasing replicas.
"""
from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class GateSnapshot:
    active: int = 0
    waiting: int = 0
    max_observed: int = 0


class _MetricGate:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._snapshot = GateSnapshot()

    @staticmethod
    def _limit() -> int:
        from app.core.app_settings import load_settings

        return max(1, int(load_settings().get("perfprofile_metric_concurrency", 2) or 2))

    async def acquire(self) -> None:
        async with self._condition:
            self._snapshot.waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: self._snapshot.active < self._limit()
                )
                self._snapshot.active += 1
                self._snapshot.max_observed = max(
                    self._snapshot.max_observed, self._snapshot.active
                )
            finally:
                self._snapshot.waiting -= 1

    async def release(self) -> None:
        async with self._condition:
            self._snapshot.active = max(0, self._snapshot.active - 1)
            self._condition.notify_all()

    def snapshot(self) -> GateSnapshot:
        return GateSnapshot(
            active=self._snapshot.active,
            waiting=self._snapshot.waiting,
            max_observed=self._snapshot.max_observed,
        )

    def reset_observed(self) -> None:
        self._snapshot.max_observed = self._snapshot.active


# asyncio synchronization primitives are event-loop bound once they contend.  Tests use many
# asyncio.run() loops, so keep one process-wide gate PER event loop rather than accidentally
# reusing a Condition bound to a closed test loop.  In production there is one loop and one gate.
_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _MetricGate]" = weakref.WeakKeyDictionary()


def _gate() -> _MetricGate:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _MetricGate()
        _gates[loop] = gate
    return gate


@asynccontextmanager
async def metric_slot() -> AsyncIterator[None]:
    """Admit one Azure Monitor request under the process-wide profiler limit."""
    gate = _gate()
    await gate.acquire()
    try:
        yield
    finally:
        await gate.release()


def current_gate_snapshot() -> GateSnapshot:
    """Testing/diagnostic view for the current event loop."""
    return _gate().snapshot()


def reset_gate_observed() -> None:
    """Reset the high-water mark without changing active work (tests/diagnostics)."""
    _gate().reset_observed()

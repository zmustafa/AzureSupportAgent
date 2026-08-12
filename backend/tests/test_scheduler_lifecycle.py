"""Lifecycle regressions for process-wide asynchronous services."""
from __future__ import annotations

import asyncio

from app.automations.scheduler import Scheduler


def test_scheduler_can_restart_on_a_new_event_loop(monkeypatch) -> None:
    scheduler = Scheduler()

    async def idle_loop() -> None:
        await scheduler._stop.wait()

    monkeypatch.setattr(scheduler, "_loop", idle_loop)

    async def lifecycle() -> None:
        scheduler.start()
        await asyncio.sleep(0)
        await scheduler.stop()

    asyncio.run(lifecycle())
    asyncio.run(lifecycle())
    assert scheduler._task is None
    assert scheduler._inflight == set()
    assert scheduler._running_ids == set()

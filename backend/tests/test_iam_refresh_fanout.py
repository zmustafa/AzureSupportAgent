"""Whole-tenant IAM refresh fans out three at a time, and fans back in.

The refresh was strictly sequential: 26 subscriptions collected one after another, each waiting
on ARM round trips the previous one had already finished with. Three concurrent scopes is the
ceiling Azure Resource Graph allows comfortably (15 queries / 5s per security principal, shared
tenant-wide), so this asserts the shape of the concurrency rather than a wall-clock number:

  * never more than three scopes in flight;
  * every management group is written BEFORE the first subscription starts, because dedupe
    attributes an inherited grant to the MG and needs an authoritative copy first;
  * one scope failing does not lose the rest of the run, and does not get counted as collected.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.iam import orchestrator


class _Tracker:
    """Records concurrency and the order phases ran in."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0
        self.order: list[str] = []
        self.started: list[str] = []

    async def run(self, scope: str, _name: str, *, fail: bool = False, delay: float = 0.01) -> Any:
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.started.append(scope)
        try:
            await asyncio.sleep(delay)
            if fail:
                raise RuntimeError(f"boom {scope}")
            self.order.append(scope)
            return {"scope": scope}
        finally:
            self.live -= 1


async def _noop(_level: str, _message: str) -> None:
    return None


@pytest.mark.asyncio
async def test_never_more_than_three_scopes_in_flight():
    t = _Tracker()
    targets = [(f"/subscriptions/s{i}", f"Sub {i}") for i in range(12)]

    ok = await orchestrator._fan_out(targets, t.run, progress=_noop, label="subscription")

    assert len(ok) == 12
    # The ceiling is Azure's quota, not our CPU. Widening it does not go faster, it just queues.
    assert t.peak == orchestrator._SCOPE_FANOUT == 3


@pytest.mark.asyncio
async def test_it_actually_overlaps_rather_than_running_one_at_a_time():
    t = _Tracker()
    targets = [(f"/subscriptions/s{i}", f"Sub {i}") for i in range(6)]

    await orchestrator._fan_out(targets, t.run, progress=_noop, label="subscription")

    # peak == 1 would mean the semaphore serialised everything and nothing was gained.
    assert t.peak > 1


@pytest.mark.asyncio
async def test_a_failing_scope_does_not_lose_the_rest_of_the_run():
    t = _Tracker()
    targets = [(f"/subscriptions/s{i}", f"Sub {i}") for i in range(6)]

    async def run(scope: str, name: str) -> Any:
        return await t.run(scope, name, fail=scope.endswith("s3"))

    ok = await orchestrator._fan_out(targets, run, progress=_noop, label="subscription")

    # A refresh that dies on one of six subscriptions is worse than one that reports which broke.
    assert len(ok) == 5
    assert "/subscriptions/s3" not in ok
    assert set(ok) == {f"/subscriptions/s{i}" for i in (0, 1, 2, 4, 5)}


@pytest.mark.asyncio
async def test_a_failed_scope_is_never_reported_as_collected():
    t = _Tracker()

    async def run(scope: str, name: str) -> Any:
        return await t.run(scope, name, fail=True)

    ok = await orchestrator._fan_out(
        [("/subscriptions/s0", "Sub 0")], run, progress=_noop, label="subscription")
    assert ok == []


@pytest.mark.asyncio
async def test_the_failure_is_reported_to_the_operator_not_swallowed():
    seen: list[tuple[str, str]] = []

    async def progress(level: str, message: str) -> None:
        seen.append((level, message))

    async def run(scope: str, _name: str) -> Any:
        raise RuntimeError("ARM said no")

    await orchestrator._fan_out(
        [("/subscriptions/s0", "Contoso Prod")], run, progress=progress, label="subscription")

    errors = [m for lvl, m in seen if lvl == "error"]
    assert errors and "Contoso Prod" in errors[0] and "ARM said no" in errors[0]


@pytest.mark.asyncio
async def test_an_empty_target_list_does_no_work_and_says_nothing():
    seen: list[str] = []

    async def progress(_level: str, message: str) -> None:
        seen.append(message)

    async def run(_scope: str, _name: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("nothing to collect")

    assert await orchestrator._fan_out([], run, progress=progress, label="subscription") == []
    assert seen == []


@pytest.mark.asyncio
async def test_results_are_returned_in_target_order_not_completion_order():
    """The caller pairs scopes with their collection path, so order must be stable."""
    async def run(scope: str, _name: str) -> Any:
        # Reverse the delays so completion order is the opposite of target order.
        await asyncio.sleep(0.03 - 0.01 * int(scope[-1]))
        return {"scope": scope}

    targets = [(f"/subscriptions/s{i}", f"Sub {i}") for i in range(3)]
    ok = await orchestrator._fan_out(targets, run, progress=_noop, label="subscription")
    assert ok == ["/subscriptions/s0", "/subscriptions/s1", "/subscriptions/s2"]


@pytest.mark.asyncio
async def test_management_groups_all_finish_before_any_subscription_starts():
    """The phase boundary is not negotiable.

    An assignment made once at a management group covering 26 subscriptions comes back from all
    26 subscription queries as an inherited row. Collecting the MG in its own right first is what
    gives compose an authoritative copy to attribute the grant to. Overlapping the two phases
    would reintroduce the 26-way misattribution the ordering exists to prevent.
    """
    t = _Tracker()
    mgs = [(f"/providers/Microsoft.Management/managementGroups/mg{i}", f"MG {i}") for i in range(5)]
    subs = [(f"/subscriptions/s{i}", f"Sub {i}") for i in range(5)]

    await orchestrator._fan_out(mgs, t.run, progress=_noop, label="management group")
    await orchestrator._fan_out(subs, t.run, progress=_noop, label="subscription")

    last_mg = max(i for i, s in enumerate(t.started) if "managementGroups" in s)
    first_sub = min(i for i, s in enumerate(t.started) if s.startswith("/subscriptions/"))
    assert last_mg < first_sub

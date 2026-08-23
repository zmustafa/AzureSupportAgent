"""`core.fanout` — the properties collectors depend on, not just "it runs concurrently"."""
from __future__ import annotations

import asyncio

import pytest

from app.core import fanout


@pytest.mark.asyncio
async def test_results_come_back_in_input_order_not_completion_order():
    """Collectors zip results against their input; completion order mis-attributes data."""
    async def work(n: int) -> int:
        # Reverse the natural completion order: the first item finishes last.
        await asyncio.sleep((10 - n) / 100)
        return n * 10

    assert await fanout.bounded(range(10), work, limit=10) == [n * 10 for n in range(10)]


@pytest.mark.asyncio
async def test_concurrency_never_exceeds_the_limit():
    live = 0
    peak = 0

    async def work(_: int) -> None:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1

    await fanout.bounded(range(25), work, limit=4)
    assert peak <= 4, f"ran {peak} concurrently with limit=4"
    assert peak > 1, "did not actually run concurrently"


@pytest.mark.asyncio
async def test_it_is_actually_faster_than_awaiting_one_at_a_time():
    async def work(_: int) -> None:
        await asyncio.sleep(0.05)

    loop = asyncio.get_running_loop()
    start = loop.time()
    await fanout.bounded(range(8), work, limit=8)
    elapsed = loop.time() - start
    # Serial would be ~0.40 s; allow generous slack for a loaded CI box.
    assert elapsed < 0.25, f"took {elapsed:.3f}s — not running concurrently"


@pytest.mark.asyncio
async def test_a_failure_is_raised_by_default():
    async def work(n: int) -> int:
        if n == 3:
            raise RuntimeError("boom")
        return n

    with pytest.raises(RuntimeError, match="boom"):
        await fanout.bounded(range(5), work, limit=2)


@pytest.mark.asyncio
async def test_return_exceptions_keeps_position_so_the_caller_can_name_the_failure():
    async def work(n: int) -> int:
        if n == 2:
            raise ValueError("bad scope")
        return n

    out = await fanout.bounded(range(4), work, limit=2, return_exceptions=True)
    assert out[0] == 0 and out[1] == 1 and out[3] == 3
    assert isinstance(out[2], ValueError)


@pytest.mark.asyncio
async def test_cancellation_is_never_swallowed_even_with_return_exceptions():
    """A cancelled refresh must not be indistinguishable from a completed one."""
    async def work(n: int) -> int:
        if n == 1:
            raise asyncio.CancelledError()
        return n

    with pytest.raises(asyncio.CancelledError):
        await fanout.bounded(range(3), work, limit=3, return_exceptions=True)


@pytest.mark.asyncio
async def test_empty_and_single_item_are_handled_without_a_semaphore():
    async def work(n: int) -> int:
        return n * 2

    assert await fanout.bounded([], work) == []
    assert await fanout.bounded([7], work) == [14]


@pytest.mark.asyncio
async def test_single_item_still_raises_and_still_honours_return_exceptions():
    async def boom(_: int) -> int:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await fanout.bounded([1], boom)
    out = await fanout.bounded([1], boom, return_exceptions=True)
    assert isinstance(out[0], RuntimeError)


@pytest.mark.asyncio
async def test_zero_limit_is_rejected_rather_than_deadlocking():
    async def work(n: int) -> int:
        return n

    with pytest.raises(ValueError):
        await fanout.bounded([1, 2], work, limit=0)


@pytest.mark.asyncio
async def test_bounded_map_separates_the_scopes_that_worked_from_the_ones_that_failed():
    async def work(name: str) -> str:
        if name == "sub-b":
            raise PermissionError("no access")
        return name.upper()

    ok, failed = await fanout.bounded_map(["sub-a", "sub-b", "sub-c"], work, limit=3)
    assert ok == {"sub-a": "SUB-A", "sub-c": "SUB-C"}
    assert set(failed) == {"sub-b"} and isinstance(failed["sub-b"], PermissionError)

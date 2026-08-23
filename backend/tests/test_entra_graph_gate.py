"""Unit tests for the adaptive throttle gate on the Entra Graph client.

The gate is the thing that makes a wide fan-out safe: on 429 it halves the client's width
and holds *every* caller off for the ``Retry-After`` window, instead of letting each request
back off alone while the gate admits a replacement. These tests run against a fake transport
and a fake clock — no sleeping, no Microsoft Graph.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.entra import graphclient as gc
from app.entra.graphclient import AdaptiveGate, GraphClient


class FakeTransport(httpx.AsyncBaseTransport):
    """Scripted responses — no Microsoft Graph is contacted."""

    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        return self.handler(request, body)


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _client(handler, **kw) -> GraphClient:
    client = GraphClient({"auth_method": "service_principal"}, **kw)
    client._token = "header.eyJyb2xlcyI6W119.sig"  # noqa: SLF001 - bypass token acquisition
    client._client = httpx.AsyncClient(transport=FakeTransport(handler), timeout=5)  # noqa: SLF001
    return client


@pytest.fixture
def nowait(monkeypatch):
    """Collapse retry backoff to nothing.

    Two patches are needed: the sleep itself, and `_backoff`, because the gate's pause
    window is measured against the real event-loop clock and would otherwise be waited out
    for real. `gc.asyncio` is the global module, so bind the real sleep first or the
    replacement calls itself.
    """
    real = asyncio.sleep
    monkeypatch.setattr(gc.asyncio, "sleep", lambda *_a, **_k: real(0))
    monkeypatch.setattr(GraphClient, "_backoff", staticmethod(lambda *_a, **_k: 0.0))


# --------------------------------------------------------------------------- the gate alone
def test_the_gate_admits_no_more_than_its_ceiling():
    async def run():
        gate = AdaptiveGate(6)
        live = peak = 0
        lock = asyncio.Lock()

        async def one():
            nonlocal live, peak
            await gate.acquire()
            async with lock:
                live += 1
                peak = max(peak, live)
            await asyncio.sleep(0.01)
            async with lock:
                live -= 1
            await gate.release()

        await asyncio.gather(*(one() for _ in range(40)))
        return peak, gate._in_flight  # noqa: SLF001

    peak, left = asyncio.run(run())
    assert peak == 6
    assert left == 0, "a permit leaked"


def test_a_429_halves_the_width():
    gate = AdaptiveGate(8)
    assert gate.limit == 8
    gate.record_throttled()
    assert gate.limit == 4
    gate.record_throttled()
    assert gate.limit == 2
    gate.record_throttled()
    assert gate.limit == 1


def test_the_width_never_falls_below_the_floor():
    gate = AdaptiveGate(8, floor=2)
    for _ in range(10):
        gate.record_throttled()
    assert gate.limit == 2


def test_sustained_success_widens_the_gate_again():
    gate = AdaptiveGate(8)
    gate.record_throttled()
    assert gate.limit == 4
    for _ in range(gc._GATE_GROW_AFTER):
        gate.record_ok()
    assert gate.limit == 5, "should widen by one, not leap straight back to the ceiling"


def test_widening_stops_at_the_ceiling():
    gate = AdaptiveGate(3)
    for _ in range(gc._GATE_GROW_AFTER * 20):
        gate.record_ok()
    assert gate.limit == 3


def test_a_partial_success_streak_does_not_widen():
    gate = AdaptiveGate(8)
    gate.record_throttled()
    for _ in range(gc._GATE_GROW_AFTER - 1):
        gate.record_ok()
    assert gate.limit == 4


def test_a_later_429_resets_the_recovery_streak():
    gate = AdaptiveGate(8)
    gate.record_throttled()          # -> 4
    for _ in range(gc._GATE_GROW_AFTER - 1):
        gate.record_ok()
    gate.record_throttled()          # -> 2, streak cleared
    for _ in range(gc._GATE_GROW_AFTER - 1):
        gate.record_ok()
    assert gate.limit == 2


def test_the_narrowest_width_is_recorded_for_diagnostics():
    gate = AdaptiveGate(8)
    gate.record_throttled()
    gate.record_throttled()
    for _ in range(gc._GATE_GROW_AFTER):
        gate.record_ok()
    assert gate.narrowed == 2
    assert gate.min_limit == 2, "min_limit must remember the trough, not the current width"


def test_retry_after_holds_back_every_caller_not_just_the_one_refused():
    """The whole point of the gate: one 429 pauses the entire client."""
    async def run():
        gate = AdaptiveGate(4)
        loop = asyncio.get_running_loop()
        gate.record_throttled(retry_after=0.2)
        start = loop.time()
        await gate.acquire()
        waited = loop.time() - start
        await gate.release()
        return waited

    waited = asyncio.run(run())
    assert waited >= 0.15, f"caller resumed after only {waited:.3f}s; the window was ignored"


def test_the_pause_window_does_not_deadlock_waiting_callers():
    async def run():
        gate = AdaptiveGate(2)
        gate.record_throttled(retry_after=0.05)

        async def one():
            await gate.acquire()
            await gate.release()

        await asyncio.wait_for(asyncio.gather(*(one() for _ in range(12))), timeout=5)
        return gate._in_flight  # noqa: SLF001

    assert asyncio.run(run()) == 0


def test_a_permit_is_released_even_when_the_body_raises():
    async def run():
        gate = AdaptiveGate(1)
        with pytest.raises(ValueError):
            await gate.acquire()
            try:
                raise ValueError("boom")
            finally:
                await gate.release()
        # The gate must still be usable.
        await asyncio.wait_for(gate.acquire(), timeout=1)
        await gate.release()
        return gate._in_flight  # noqa: SLF001

    assert asyncio.run(run()) == 0


# --------------------------------------------------------------------------- through the client
def test_a_throttled_request_narrows_the_client_and_then_succeeds(nowait):
    """429 once, then 200: the retry works and the gate is left narrower."""
    state = {"n": 0}

    def handler(request, body):  # noqa: ARG001
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return _json({"value": [{"id": "a"}]})

    async def run():
        client = _client(handler, concurrency=6)
        try:
            items, _ = await client.get_all("/applications", select=["id"])
            return items, client.stats, client._gate  # noqa: SLF001
        finally:
            await client.aclose()

    items, stats, gate = asyncio.run(run())
    assert [i["id"] for i in items] == ["a"]
    assert stats.throttled == 1
    assert gate.limit == 3, "a 429 must halve the client's width"
    assert stats.gate_narrowed == 1
    assert stats.gate_min_limit == 3


def test_an_unthrottled_run_leaves_the_gate_at_full_width():
    def handler(request, body):  # noqa: ARG001
        return _json({"value": [{"id": "a"}]})

    async def run():
        client = _client(handler, concurrency=6)
        try:
            await client.get_all("/applications", select=["id"])
            return client.stats, client._gate  # noqa: SLF001
        finally:
            await client.aclose()

    stats, gate = asyncio.run(run())
    assert gate.limit == 6
    assert stats.throttled == 0
    assert stats.gate_narrowed == 0
    assert stats.gate_min_limit == 6


def test_the_client_never_exceeds_its_configured_width(nowait):
    """Concurrent reads through one client stay within the ceiling."""
    seen = {"live": 0, "peak": 0}

    def handler(request, body):  # noqa: ARG001
        seen["live"] += 1
        seen["peak"] = max(seen["peak"], seen["live"])
        seen["live"] -= 1
        return _json({"value": [{"id": "a"}]})

    async def run():
        client = _client(handler, concurrency=3)
        try:
            await asyncio.gather(*(client.get_all("/applications", select=["id"]) for _ in range(20)))
            return seen["peak"], client._gate._in_flight  # noqa: SLF001
        finally:
            await client.aclose()

    peak, left = asyncio.run(run())
    assert peak <= 3
    assert left == 0


def test_repeated_throttling_still_terminates(nowait):
    """A permanently throttled endpoint must raise, not spin or deadlock."""

    def handler(request, body):  # noqa: ARG001
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})

    async def run():
        client = _client(handler, concurrency=6)
        try:
            with pytest.raises(gc.GraphError):
                await asyncio.wait_for(client.get_all("/applications", select=["id"]), timeout=10)
            return client._gate  # noqa: SLF001
        finally:
            await client.aclose()

    gate = asyncio.run(run())
    assert gate.limit == gate.floor, "sustained 429s should drive the width to the floor"
    assert gate._in_flight == 0  # noqa: SLF001

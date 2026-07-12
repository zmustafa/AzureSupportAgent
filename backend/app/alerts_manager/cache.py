"""Short-lived, bounded async cache for live Alerts Manager inventories.

Values are copied on both ingress and egress so callers cannot mutate shared cache state.
Concurrent misses for the same tenant/connection/scope key share one producer task.
"""
from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

_TTL_SECONDS = 20.0
_MAX_ENTRIES = 128
_entries: OrderedDict[tuple[Any, ...], tuple[float, Any]] = OrderedDict()
_flights: dict[tuple[Any, ...], asyncio.Task[Any]] = {}
_lock = asyncio.Lock()


def connection_key(connection: dict[str, Any], tenant_id: str = "") -> tuple[str, str, str]:
    """Return a secret-free application-tenant/Azure-tenant/connection identity."""
    return (
        str(tenant_id or "default"),
        str(connection.get("tenant_id") or ""),
        str(connection.get("id") or connection.get("connection_id") or "default"),
    )


def inventory_key(
    kind: str,
    connection: dict[str, Any],
    *,
    tenant_id: str = "",
    workload_id: str | None = None,
    subscription_id: str | None = None,
    management_group_id: str | None = None,
    dimensions: tuple[Any, ...] = (),
) -> tuple[Any, ...]:
    return (
        kind,
        *connection_key(connection, tenant_id),
        str(workload_id or "").lower(),
        str(subscription_id or "").lower(),
        str(management_group_id or "").lower(),
        *dimensions,
    )


async def get_or_create(key: tuple[Any, ...], producer: Callable[[], Awaitable[T]]) -> T:
    """Read a defensive copy or coalesce concurrent production for ``key``."""
    now = time.monotonic()
    async with _lock:
        cached = _entries.get(key)
        if cached and cached[0] > now:
            _entries.move_to_end(key)
            return copy.deepcopy(cached[1])
        if cached:
            _entries.pop(key, None)
        task = _flights.get(key)
        if task is None:
            task = asyncio.create_task(producer())
            _flights[key] = task
    try:
        value = await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling one request must not cancel or inspect an unfinished producer
        # that other coalesced waiters still depend on.
        raise
    finally:
        async with _lock:
            if _flights.get(key) is task and task.done():
                _flights.pop(key, None)
    async with _lock:
        # Another waiter may have inserted the same result; replacing it is harmless.
        _entries[key] = (time.monotonic() + _TTL_SECONDS, copy.deepcopy(value))
        _entries.move_to_end(key)
        while len(_entries) > _MAX_ENTRIES:
            _entries.popitem(last=False)
    return copy.deepcopy(value)


async def invalidate(
    *,
    kinds: set[str] | None = None,
    tenant_id: str | None = None,
    connection_id: str | None = None,
) -> None:
    """Invalidate only matching completed entries; in-flight reads finish normally."""
    async with _lock:
        for key in list(_entries):
            if kinds is not None and str(key[0]) not in kinds:
                continue
            if tenant_id is not None and str(key[1]) != str(tenant_id or "default"):
                continue
            if connection_id is not None and str(key[3]) != str(connection_id or "default"):
                continue
            _entries.pop(key, None)


async def clear() -> None:
    """Clear cache state. Intended for tests and process lifecycle hooks."""
    async with _lock:
        _entries.clear()
        flights = list(_flights.values())
        _flights.clear()
    for task in flights:
        if not task.done():
            task.cancel()


def reset_for_tests() -> None:
    """Reset state between hermetic tests when no application requests are running."""
    for task in _flights.values():
        if not task.done():
            task.cancel()
    _flights.clear()
    _entries.clear()

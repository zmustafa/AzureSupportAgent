"""Resource-picker background resource-group prefetch contracts."""
from __future__ import annotations

import asyncio

import pytest

from app.api import workloads
from app.workloads.cache import discovery_cache


@pytest.mark.asyncio
async def test_rg_prefetch_skips_cache_and_caps_concurrency_at_four(monkeypatch) -> None:
    connection_id = "prefetch-test"
    nodes = [
        {"kind": "subscription", "id": f"sub-{index}", "name": f"Sub {index}"}
        for index in range(6)
    ]
    discovery_cache.put(
        discovery_cache.key(connection_id, "tree:subscription", "sub-0"),
        [{"kind": "resource_group", "id": "/cached", "name": "cached"}],
    )
    active = 0
    max_active = 0
    called: list[str] = []

    async def open_session(_connection):
        return "session", None

    def close_session(_session):
        return None

    async def expand(_connection, kind, node_id, *, session_config_dir=None):
        nonlocal active, max_active
        assert kind == "subscription"
        assert session_config_dir == "session"
        called.append(node_id)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [{"kind": "resource_group", "id": f"/{node_id}/rg", "name": "rg"}]

    monkeypatch.setattr("app.exec.command_runner.open_sp_session", open_session)
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", close_session)
    monkeypatch.setattr(workloads.discovery, "expand_node", expand)

    await workloads._prefetch_missing_resource_groups(connection_id, {"id": "connection"}, nodes)

    assert "sub-0" not in called
    assert sorted(called) == [f"sub-{index}" for index in range(1, 6)]
    assert max_active == 4
    for index in range(6):
        assert discovery_cache.has_fresh(
            discovery_cache.key(connection_id, "tree:subscription", f"sub-{index}")
        )
    discovery_cache.invalidate_connection(connection_id)


@pytest.mark.asyncio
async def test_rg_prefetch_does_nothing_when_all_lists_are_cached(monkeypatch) -> None:
    connection_id = "prefetch-cached-test"
    nodes = [{"kind": "subscription", "id": "sub-a", "name": "A"}]
    discovery_cache.put(
        discovery_cache.key(connection_id, "tree:subscription", "sub-a"), []
    )

    async def should_not_open(_connection):
        raise AssertionError("No Azure session should be opened for a complete cache")

    monkeypatch.setattr("app.exec.command_runner.open_sp_session", should_not_open)
    await workloads._prefetch_missing_resource_groups(connection_id, {}, nodes)
    discovery_cache.invalidate_connection(connection_id)

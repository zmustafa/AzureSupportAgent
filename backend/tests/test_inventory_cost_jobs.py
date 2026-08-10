"""Inventory Cost detailed progress and navigation-safe background refresh tests."""
from __future__ import annotations

import asyncio

from app.inventory import cost, cost_jobs


async def test_cost_collection_emits_detailed_subscription_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(cost, "_CACHE_PATH", tmp_path / "cost.json")
    monkeypatch.setattr(cost, "_mem", None)
    events: list[dict] = []

    async def fake_subscription(_connection, sub_id, _body, **_kwargs):
        await asyncio.sleep(0.001)
        number = int(sub_id[1:])
        return {f"/subscriptions/{sub_id}/resource": float(number)}, "USD", ""

    async def progress(event):
        events.append(dict(event))

    monkeypatch.setattr(cost, "_subscription_cost", fake_subscription)
    result = await cost.get_cost(
        None,
        ["s1", "s2", "s3"],
        "tenant",
        "connection",
        force=True,
        progress=progress,
    )

    assert result["total"] == 6.0
    assert events[0]["type"] == "started"
    assert events[0]["subscriptions_total"] == 3
    assert sum(event["type"] == "subscription_started" for event in events) == 3
    done = [event for event in events if event["type"] == "subscription_done"]
    assert len(done) == 3
    assert sorted(event["subscriptions_done"] for event in done) == [1, 2, 3]
    assert all(event["duration_ms"] >= 0 for event in done)
    assert any(event["type"] == "aggregating" for event in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["subscriptions_succeeded"] == 3
    assert events[-1]["cached"] is True


async def test_cost_job_continues_without_a_mounted_client_and_reattaches(monkeypatch):
    manager = cost_jobs.CostJobManager()
    collection_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_get_cost(
        _connection,
        subscriptions,
        _tenant_id,
        _connection_id,
        *,
        force,
        scope,
        progress,
    ):
        await progress(
            {
                "type": "started",
                "subscriptions_total": len(subscriptions),
                "subscriptions_visible": len(subscriptions),
                "subscriptions_omitted": 0,
                "message": "started",
            }
        )
        await progress(
            {
                "type": "subscription_started",
                "subscription_id": subscriptions[0],
                "index": 1,
                "subscriptions_total": len(subscriptions),
                "message": "querying",
            }
        )
        collection_started.set()
        await release.wait()
        await progress(
            {
                "type": "subscription_done",
                "subscription_id": subscriptions[0],
                "subscriptions_done": 1,
                "subscriptions_total": len(subscriptions),
                "resource_cost_rows": 1,
                "subscription_total": 4.2,
                "currency": "USD",
                "duration_ms": 25,
                "message": "complete",
            }
        )
        return {
            "available": True,
            "currency": "USD",
            "period": "test",
            "fetched_at": "2026-08-09T00:00:00+00:00",
            "by_resource": {"/resource": 4.2},
            "by_subscription": {subscriptions[0]: 4.2},
            "total": 4.2,
            "errors": [],
            "cached": False,
        }

    monkeypatch.setattr(cost_jobs.cost, "get_cost", fake_get_cost)
    first = manager.start(
        tenant_id="tenant",
        connection_id="connection",
        scope="",
        force=True,
        connection=None,
        subscriptions=["sub-1"],
    )
    await collection_started.wait()

    # Simulate leaving the route: there is no subscriber, stream, request, or component owner.
    # The server task remains active and a later status call reattaches to the same job.
    reattached = manager.latest("tenant", "connection", "")
    assert reattached is not None
    assert reattached["id"] == first["id"]
    assert reattached["status"] == "running"
    assert reattached["active_subscriptions"][0]["subscription_id"] == "sub-1"

    duplicate = manager.start(
        tenant_id="tenant",
        connection_id="connection",
        scope="",
        force=True,
        connection=None,
        subscriptions=["sub-1"],
    )
    assert duplicate["id"] == first["id"]

    release.set()
    task = manager._jobs[first["id"]].task
    assert task is not None
    await task
    terminal = manager.latest("tenant", "connection", "")
    assert terminal is not None
    assert terminal["status"] == "succeeded"
    assert terminal["subscriptions_done"] == 1
    assert terminal["subscriptions_succeeded"] == 1
    assert terminal["result"]["total"] == 4.2


async def test_cost_job_surfaces_partial_subscription_failures(monkeypatch):
    manager = cost_jobs.CostJobManager()

    async def fake_get_cost(*_args, progress, **_kwargs):
        await progress(
            {
                "type": "started",
                "subscriptions_total": 2,
                "subscriptions_visible": 2,
                "subscriptions_omitted": 0,
                "message": "started",
            }
        )
        await progress(
            {
                "type": "subscription_error",
                "subscription_id": "sub-2",
                "subscriptions_done": 2,
                "subscriptions_total": 2,
                "duration_ms": 100,
                "error": "Cost Management Reader is missing",
                "message": "failed",
            }
        )
        return {
            "available": True,
            "currency": "USD",
            "period": "test",
            "by_resource": {"/resource": 1.0},
            "by_subscription": {"sub-1": 1.0},
            "total": 1.0,
            "errors": ["sub-2: Cost Management Reader is missing"],
            "cached": False,
        }

    monkeypatch.setattr(cost_jobs.cost, "get_cost", fake_get_cost)
    public = manager.start(
        tenant_id="tenant",
        connection_id="connection",
        scope="",
        force=True,
        connection=None,
        subscriptions=["sub-1", "sub-2"],
    )
    task = manager._jobs[public["id"]].task
    assert task is not None
    await task
    terminal = manager.latest("tenant", "connection", "")
    assert terminal is not None
    assert terminal["status"] == "partial"
    assert terminal["subscriptions_failed"] == 1
    assert "Cost Management Reader" in terminal["error"]

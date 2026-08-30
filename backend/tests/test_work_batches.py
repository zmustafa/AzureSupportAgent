"""Durability, admission, retry and feature-dispatch tests for generic work batches."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.core.db as dbmod
from app.core import work_batches
from app.models import Base, WorkBatch, WorkBatchItem


def _env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'work-batches.db'}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    return engine, Session


def _items(count: int, *, connections: int = 1):
    return [
        {
            "item_key": f"w{i:02}",
            "workload_id": f"w{i:02}",
            "workload_name": f"W{i:02}",
            "connection_id": f"c{i % connections}",
        }
        for i in range(count)
    ]


def test_batch_idempotency_and_thirty_items_survive_server_owned_queue(monkeypatch, tmp_path):
    engine, _Session = _env(monkeypatch, tmp_path)
    active = 0
    max_active = 0

    async def execute(item, _batch):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.002)
        active -= 1
        return work_batches.ItemResult(result_ref={"kind": "test", "id": item["id"]})

    monkeypatch.setitem(work_batches._EXECUTORS, "nightly", execute)

    async def run():
        first, created = await work_batches.create_batch(
            tenant_id="t1", feature="nightly", actor="tester",
            idempotency_key="thirty", items=_items(30), start_worker=False,
        )
        duplicate, duplicate_created = await work_batches.create_batch(
            tenant_id="t1", feature="nightly", actor="tester",
            idempotency_key="thirty", items=_items(1), start_worker=False,
        )
        assert created is True
        assert duplicate_created is False
        assert duplicate["id"] == first["id"]
        await work_batches.worker.start()
        try:
            async def complete():
                while True:
                    current = await work_batches.get_batch(first["id"], "t1")
                    if current and current["status"] in work_batches.TERMINAL:
                        return current
                    await asyncio.sleep(0.01)

            # SQLite commits contend with the other xdist workers in the full suite;
            # five seconds made this deterministic queue check fail intermittently
            # despite every item completing. The production worker remains bounded.
            return await asyncio.wait_for(complete(), timeout=15)
        finally:
            await work_batches.worker.stop()
            await engine.dispose()

    result = asyncio.run(run())
    assert result["status"] == "succeeded"
    assert result["completed"] == result["total"] == 30
    assert max_active == 1  # every item shared one tenant+connection lane


def test_different_connection_lanes_progress_in_parallel(monkeypatch, tmp_path):
    engine, _Session = _env(monkeypatch, tmp_path)
    active = 0
    max_active = 0
    two_started = asyncio.Event()

    async def execute(_item, _batch):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active >= 2:
            two_started.set()
        try:
            await asyncio.wait_for(two_started.wait(), timeout=0.2)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.01)
        active -= 1
        return work_batches.ItemResult()

    monkeypatch.setitem(work_batches._EXECUTORS, "assessment", execute)

    async def run():
        batch, _ = await work_batches.create_batch(
            tenant_id="t1", feature="assessment", actor="tester",
            idempotency_key="lanes", items=_items(4, connections=2), start_worker=False,
        )
        await work_batches.worker.start()
        try:
            while True:
                current = await work_batches.get_batch(batch["id"], "t1")
                if current and current["status"] in work_batches.TERMINAL:
                    return current
                await asyncio.sleep(0.01)
        finally:
            await work_batches.worker.stop()
            await engine.dispose()

    result = asyncio.run(run())
    assert result["status"] == "succeeded"
    assert max_active == 2


def test_restart_requeues_running_and_cancel_marks_pending(monkeypatch, tmp_path):
    engine, Session = _env(monkeypatch, tmp_path)

    async def run():
        batch, _ = await work_batches.create_batch(
            tenant_id="t1", feature="assessment", actor="tester",
            idempotency_key="restart", items=_items(3), start_worker=False,
        )
        async with Session() as db:
            first = (
                await db.execute(
                    select(WorkBatchItem).where(WorkBatchItem.batch_id == batch["id"]).limit(1)
                )
            ).scalar_one()
            first.status = "running"
            first.started_at = datetime.now(timezone.utc)
            row = await db.get(WorkBatch, batch["id"])
            assert row is not None
            row.status = "running"
            await db.commit()
        recovered = await work_batches.recover_interrupted()
        after_recovery = await work_batches.get_batch(batch["id"], "t1")
        cancelled = await work_batches.cancel_batch(batch["id"], "t1")
        final = await work_batches.get_batch(batch["id"], "t1")
        await engine.dispose()
        return recovered, after_recovery, cancelled, final

    recovered, after_recovery, cancelled, final = asyncio.run(run())
    assert recovered == 1
    assert {item["status"] for item in after_recovery["items"]} == {"queued"}
    assert cancelled is True
    assert final["status"] == "cancelled"
    assert final["cancelled"] == 3


def test_retry_creates_new_batch_only_for_non_success_items(monkeypatch, tmp_path):
    engine, Session = _env(monkeypatch, tmp_path)

    async def run():
        batch, _ = await work_batches.create_batch(
            tenant_id="t1", feature="assessment", actor="tester",
            idempotency_key="original", items=_items(4), start_worker=False,
        )
        async with Session() as db:
            rows = list(
                (
                    await db.execute(select(WorkBatchItem).where(WorkBatchItem.batch_id == batch["id"]))
                ).scalars().all()
            )
            rows[0].status = "succeeded"
            rows[1].status = "failed"
            rows[2].status = "partial"
            rows[3].status = "cancelled"
            for row in rows:
                row.ended_at = datetime.now(timezone.utc)
            await work_batches.refresh_batch(db, batch["id"])
            await db.commit()
        retried = await work_batches.retry_batch(batch["id"], "t1", "tester", "retry-1")
        assert retried is not None
        # Stop the automatically-started retry worker before it can obscure the creation facts.
        await work_batches.worker.stop()
        await engine.dispose()
        return retried

    retried = asyncio.run(run())
    assert retried["trigger"] == "retry"
    assert retried["total"] == 3
    assert {item["item_key"] for item in retried["items"]} == {"w01", "w02", "w03"}


def test_transient_failure_is_backed_off_and_remains_durable(monkeypatch, tmp_path):
    engine, Session = _env(monkeypatch, tmp_path)

    async def run():
        batch, _ = await work_batches.create_batch(
            tenant_id="t1", feature="assessment", actor="tester",
            idempotency_key="transient", items=_items(1), start_worker=False,
        )
        async with Session() as db:
            item = (
                await db.execute(select(WorkBatchItem).where(WorkBatchItem.batch_id == batch["id"]))
            ).scalar_one()
            item.status = "running"
            item.attempt = 1
            item.started_at = datetime.now(timezone.utc)
            await db.commit()
            item_id = item.id
        await work_batches.worker._fail_or_retry(item_id, "Azure 429 Too Many Requests")
        current = await work_batches.get_batch(batch["id"], "t1")
        await engine.dispose()
        return current

    current = asyncio.run(run())
    item = current["items"][0]
    assert current["status"] == "queued"
    assert item["status"] == "queued"
    assert item["retryable"] is True
    assert item["available_at"]
    assert "retrying" in item["message"].lower()


def test_all_feature_ids_dispatch_through_registered_executors(monkeypatch, tmp_path):
    engine, _Session = _env(monkeypatch, tmp_path)
    seen: list[str] = []

    def executor(feature: str):
        async def run(_item, _batch):
            seen.append(feature)
            return work_batches.ItemResult()

        return run

    for feature in work_batches.FEATURES:
        monkeypatch.setitem(work_batches._EXECUTORS, feature, executor(feature))

    async def run():
        batches = []
        for index, feature in enumerate(sorted(work_batches.FEATURES)):
            batch, _ = await work_batches.create_batch(
                tenant_id=f"t{index}", feature=feature, actor="tester",
                idempotency_key=f"dispatch-{feature}", items=_items(1), start_worker=False,
            )
            batches.append(batch)
        await work_batches.worker.start()
        try:
            async def complete():
                while True:
                    states = [await work_batches.get_batch(batch["id"], f"t{i}") for i, batch in enumerate(batches)]
                    if all(state and state["status"] in work_batches.TERMINAL for state in states):
                        return states
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(complete(), timeout=5)
        finally:
            await work_batches.worker.stop()
            await engine.dispose()

    asyncio.run(run())
    assert set(seen) == work_batches.FEATURES

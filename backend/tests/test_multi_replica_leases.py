"""Focused multi-replica lease and idle-wakeup regressions."""
from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.automations.scheduler as scheduler_mod
import app.core.db as dbmod
from app.automations.runner import _complete_task_schedule
from app.automations.scheduler import Scheduler
from app.core import work_batches
from app.models import (
    Base,
    PerfProfileFleetItem,
    ScheduledTask,
    WorkBatch,
    WorkBatchItem,
)
from app.perfprofile import fleet


def _env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'leases.db'}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(scheduler_mod, "SessionLocal", Session)
    dbmod.reset_background_gate()
    return engine, Session


def _work_item() -> list[dict[str, str]]:
    return [{
        "item_key": "w1",
        "workload_id": "w1",
        "workload_name": "Workload 1",
        "connection_id": "c1",
    }]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def test_two_generic_workers_cannot_claim_the_same_sqlite_item(monkeypatch, tmp_path) -> None:
    engine, _Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        await work_batches.create_batch(
            tenant_id="t1",
            feature="assessment",
            actor="tester",
            idempotency_key="one-item",
            items=_work_item(),
            start_worker=False,
        )
        first = work_batches.WorkBatchWorker(identity="replica-a")
        second = work_batches.WorkBatchWorker(identity="replica-b")
        first._claim_lock = asyncio.Lock()
        second._claim_lock = asyncio.Lock()
        claims = await asyncio.gather(first._claim_next(), second._claim_next())
        assert sum(claim is not None for claim in claims) == 1
        async with _Session() as db:
            item = (await db.execute(select(WorkBatchItem))).scalar_one()
            assert item.status == "running"
            assert item.lease_owner in {"replica-a", "replica-b"}
            assert item.lease_token
        await engine.dispose()

    asyncio.run(run())


def test_generic_recovery_respects_live_foreign_lease_and_reclaims_expired(
    monkeypatch, tmp_path
) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        batch, _ = await work_batches.create_batch(
            tenant_id="t1",
            feature="assessment",
            actor="tester",
            idempotency_key="recovery",
            items=_work_item(),
            start_worker=False,
        )
        async with Session() as db:
            item = (
                await db.execute(
                    select(WorkBatchItem).where(WorkBatchItem.batch_id == batch["id"])
                )
            ).scalar_one()
            item.status = "running"
            item.lease_owner = "foreign-live"
            item.lease_token = "live-token"
            item.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            row = await db.get(WorkBatch, batch["id"])
            assert row is not None
            row.status = "running"
            await db.commit()
            item_id = item.id

        assert await work_batches.recover_interrupted() == 0
        async with Session() as db:
            live = await db.get(WorkBatchItem, item_id)
            assert live is not None
            assert live.status == "running"
            live.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

        assert await work_batches.recover_interrupted() == 1
        async with Session() as db:
            reclaimed = await db.get(WorkBatchItem, item_id)
            assert reclaimed is not None
            assert reclaimed.status == "queued"
            assert reclaimed.lease_owner is None
        await engine.dispose()

    asyncio.run(run())


def test_stale_generic_worker_cannot_finalize_a_reclaimed_lease(monkeypatch, tmp_path) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        await work_batches.create_batch(
            tenant_id="t1",
            feature="assessment",
            actor="tester",
            idempotency_key="fencing",
            items=_work_item(),
            start_worker=False,
        )
        first = work_batches.WorkBatchWorker(identity="replica-a")
        second = work_batches.WorkBatchWorker(identity="replica-b")
        first._claim_lock = asyncio.Lock()
        second._claim_lock = asyncio.Lock()
        old_claim = await first._claim_next()
        assert old_claim is not None
        async with Session() as db:
            item = await db.get(WorkBatchItem, old_claim.item_id)
            assert item is not None
            item.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        new_claim = await second._claim_next()
        assert new_claim is not None
        assert new_claim.item_id == old_claim.item_id
        assert new_claim.token != old_claim.token
        await first._finish_item(
            old_claim.item_id, old_claim.token, work_batches.ItemResult(status="succeeded")
        )
        async with Session() as db:
            item = await db.get(WorkBatchItem, old_claim.item_id)
            assert item is not None
            assert item.status == "running"
            assert item.lease_owner == "replica-b"
            assert item.lease_token == new_claim.token
        await engine.dispose()

    asyncio.run(run())


def test_generic_worker_renews_a_live_lease_in_background(monkeypatch, tmp_path) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        monkeypatch.setattr(work_batches, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(work_batches, "LEASE_SECONDS", 0.1)
        await work_batches.create_batch(
            tenant_id="t1",
            feature="assessment",
            actor="tester",
            idempotency_key="heartbeat",
            items=_work_item(),
            start_worker=False,
        )
        worker = work_batches.WorkBatchWorker(identity="replica-a")
        worker._claim_lock = asyncio.Lock()
        claim = await worker._claim_next()
        assert claim is not None
        async with Session() as db:
            item = await db.get(WorkBatchItem, claim.item_id)
            assert item is not None
            original_heartbeat = item.lease_heartbeat_at
        heartbeat = asyncio.create_task(worker._heartbeat(claim.item_id, claim.token))
        try:
            await asyncio.sleep(0.04)
            async with Session() as db:
                item = await db.get(WorkBatchItem, claim.item_id)
                assert item is not None
                assert item.lease_heartbeat_at is not None
                assert original_heartbeat is not None
                assert _utc(item.lease_heartbeat_at) > _utc(original_heartbeat)
                assert item.lease_expires_at is not None
                assert _utc(item.lease_expires_at) > datetime.now(timezone.utc)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        await engine.dispose()

    asyncio.run(run())


def test_generic_shutdown_releases_only_own_lease(monkeypatch, tmp_path) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        batch, _ = await work_batches.create_batch(
            tenant_id="t1",
            feature="assessment",
            actor="tester",
            idempotency_key="shutdown",
            items=[*_work_item(), {
                "item_key": "w2",
                "workload_id": "w2",
                "workload_name": "Workload 2",
                "connection_id": "c2",
            }],
            start_worker=False,
        )
        async with Session() as db:
            items = list(
                (
                    await db.execute(
                        select(WorkBatchItem)
                        .where(WorkBatchItem.batch_id == batch["id"])
                        .order_by(WorkBatchItem.item_key)
                    )
                ).scalars()
            )
            for item, owner in zip(items, ("mine", "foreign"), strict=True):
                item.status = "running"
                item.lease_owner = owner
                item.lease_token = f"{owner}-token"
                item.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            await db.commit()
        worker = work_batches.WorkBatchWorker(identity="mine")
        await worker.stop()
        async with Session() as db:
            items = list((await db.execute(select(WorkBatchItem).order_by(WorkBatchItem.item_key))).scalars())
            assert items[0].status == "queued"
            assert items[0].lease_owner is None
            assert items[1].status == "running"
            assert items[1].lease_owner == "foreign"
        await engine.dispose()

    asyncio.run(run())


def test_perf_fleet_claim_is_exclusive_and_expired_lease_is_reclaimable(
    monkeypatch, tmp_path
) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        async with Session() as db:
            await fleet.create_batch(
                db,
                tenant_id="t1",
                actor="tester",
                idempotency_key="fleet-one",
                workloads=[{"id": "w1", "name": "W1", "connection_id": "c1"}],
                window="P1D",
                start_time="",
                end_time="",
            )
        first = fleet.FleetWorker(identity="fleet-a")
        second = fleet.FleetWorker(identity="fleet-b")
        first._claim_lock = asyncio.Lock()
        second._claim_lock = asyncio.Lock()
        claims = await asyncio.gather(first._claim_next(), second._claim_next())
        assert sum(claim is not None for claim in claims) == 1
        assert await fleet.recover_interrupted() == 0
        async with Session() as db:
            item = (await db.execute(select(PerfProfileFleetItem))).scalar_one()
            item.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        recovered = await fleet.recover_interrupted()
        assert recovered == 1
        async with Session() as db:
            item = (await db.execute(select(PerfProfileFleetItem))).scalar_one()
            assert item.status == "queued"
            assert item.lease_owner is None
        await engine.dispose()

    asyncio.run(run())


def test_perf_fleet_shutdown_requeues_only_its_own_item(monkeypatch, tmp_path) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        async with Session() as db:
            await fleet.create_batch(
                db,
                tenant_id="t1",
                actor="tester",
                idempotency_key="fleet-shutdown",
                workloads=[{"id": "w1", "name": "W1", "connection_id": "c1"}],
                window="P1D",
                start_time="",
                end_time="",
            )
        worker = fleet.FleetWorker(identity="fleet-a")
        worker._claim_lock = asyncio.Lock()
        assert await worker._claim_next() is not None
        await worker.stop()
        async with Session() as db:
            item = (await db.execute(select(PerfProfileFleetItem))).scalar_one()
            assert item.status == "queued"
            assert item.lease_owner is None
        await engine.dispose()

    asyncio.run(run())


def test_perf_fleet_worker_renews_a_live_lease(monkeypatch, tmp_path) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        monkeypatch.setattr(fleet, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(fleet, "LEASE_SECONDS", 0.1)
        async with Session() as db:
            await fleet.create_batch(
                db,
                tenant_id="t1",
                actor="tester",
                idempotency_key="fleet-heartbeat",
                workloads=[{"id": "w1", "name": "W1", "connection_id": "c1"}],
                window="P1D",
                start_time="",
                end_time="",
            )
        worker = fleet.FleetWorker(identity="fleet-a")
        worker._claim_lock = asyncio.Lock()
        claim = await worker._claim_next()
        assert claim is not None
        async with Session() as db:
            item = await db.get(PerfProfileFleetItem, claim.item_id)
            assert item is not None
            original = item.lease_heartbeat_at
        heartbeat = asyncio.create_task(worker._heartbeat(claim.item_id, claim.token))
        try:
            await asyncio.sleep(0.04)
            async with Session() as db:
                item = await db.get(PerfProfileFleetItem, claim.item_id)
                assert item is not None
                assert item.lease_heartbeat_at is not None
                assert original is not None
                assert _utc(item.lease_heartbeat_at) > _utc(original)
                assert item.lease_expires_at is not None
                assert _utc(item.lease_expires_at) > datetime.now(timezone.utc)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        await engine.dispose()

    asyncio.run(run())


def test_scheduler_claims_each_occurrence_once_and_reclaims_expired(
    monkeypatch, tmp_path
) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        async with Session() as db:
            task = ScheduledTask(
                id="scheduled-1",
                tenant_id="t1",
                name="Daily",
                status="on",
                schedule_kind="daily",
                time_of_day="08:00",
                timezone="UTC",
                next_run_at=due,
            )
            db.add(task)
            await db.commit()
        first = Scheduler(identity="scheduler-a")
        second = Scheduler(identity="scheduler-b")
        claims = await asyncio.gather(first._claim_due(), second._claim_due())
        assert sum(claim is not None for claim in claims) == 1
        assert await first._claim_due() is None
        assert await second._claim_due() is None
        async with Session() as db:
            task = await db.get(ScheduledTask, "scheduled-1")
            assert task is not None
            advanced = task.next_run_at
            assert advanced is not None and _utc(advanced) > due
            assert task.lease_occurrence_at is not None
            task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        reclaiming = first if claims[0] is None else second
        reclaimed = await reclaiming._claim_due()
        assert reclaimed is not None
        assert reclaimed.occurrence_at.replace(tzinfo=timezone.utc) == due.replace(tzinfo=timezone.utc)
        async with Session() as db:
            task = await db.get(ScheduledTask, "scheduled-1")
            assert task is not None
            assert task.next_run_at is not None
            assert _utc(task.next_run_at) == _utc(advanced)
        await engine.dispose()

    asyncio.run(run())


def test_scheduler_backfill_preserves_a_pending_final_occurrence(monkeypatch, tmp_path) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        occurrence = datetime.now(timezone.utc) - timedelta(minutes=1)
        async with Session() as db:
            db.add(ScheduledTask(
                id="pending-final",
                tenant_id="t1",
                name="Final run",
                status="on",
                schedule_kind="daily",
                next_run_at=None,
                lease_owner="dead-replica",
                lease_token="old-token",
                lease_expires_at=occurrence,
                lease_occurrence_at=occurrence,
            ))
            await db.commit()
        scheduler = Scheduler(identity="scheduler-a")
        await scheduler._backfill_next_runs()
        claim = await scheduler._claim_due()
        assert claim is not None
        assert claim.task_id == "pending-final"
        assert _utc(claim.occurrence_at) == occurrence
        async with Session() as db:
            task = await db.get(ScheduledTask, "pending-final")
            assert task is not None
            assert task.next_run_at is None
            assert task.status == "on"
        await engine.dispose()

    asyncio.run(run())


def test_leased_schedule_completion_does_not_advance_twice() -> None:
    next_run = datetime.now(timezone.utc) + timedelta(days=1)
    task = ScheduledTask(
        tenant_id="t1",
        name="Daily",
        status="on",
        next_run_at=next_run,
        lease_owner="scheduler-a",
        lease_token="token",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        lease_occurrence_at=datetime.now(timezone.utc),
    )
    _complete_task_schedule(task, lease_owner="scheduler-a", lease_token="token")
    assert task.completed_runs == 1
    assert task.next_run_at == next_run
    assert task.lease_owner is None
    assert task.lease_occurrence_at is None


def test_generic_idle_loop_uses_wakeups_and_safety_poll(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(work_batches, "IDLE_SAFETY_POLL_SECONDS", 0.05)
        worker = work_batches.WorkBatchWorker(identity="idle-test")
        worker._wake = asyncio.Event()
        worker._claim_lock = asyncio.Lock()
        calls = 0

        async def no_work():
            nonlocal calls
            calls += 1
            return None

        monkeypatch.setattr(worker, "_claim_next", no_work)
        loop = asyncio.create_task(worker._loop(0))
        try:
            await asyncio.sleep(0.01)
            initial = calls
            assert initial == 2
            worker.wake()
            await asyncio.sleep(0.01)
            after_wake = calls
            assert after_wake >= initial + 2
            # No one-second-style pressure: while inside the bounded safety window an idle
            # loop does not touch the database again.
            await asyncio.sleep(0.02)
            assert calls == after_wake
            await asyncio.sleep(0.06)
            assert calls >= after_wake + 2
        finally:
            loop.cancel()
            try:
                await loop
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def test_unchanged_progress_does_not_issue_an_update(monkeypatch, tmp_path) -> None:
    engine, Session = _env(monkeypatch, tmp_path)

    async def run() -> None:
        batch, _ = await work_batches.create_batch(
            tenant_id="t1",
            feature="assessment",
            actor="tester",
            idempotency_key="unchanged-progress",
            items=_work_item(),
            start_worker=False,
        )
        async with Session() as db:
            item = (
                await db.execute(
                    select(WorkBatchItem).where(WorkBatchItem.batch_id == batch["id"])
                )
            ).scalar_one()
            item_id = item.id

        statements: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            statements.append(statement.strip().upper())

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        await work_batches.update_item_progress(
            item_id, current=0, total=0, message=""
        )
        assert not any(statement.startswith("UPDATE ") for statement in statements)
        await engine.dispose()

    asyncio.run(run())


_EXPECTED_INDEXES = {
    "tool_calls": {
        "ix_tool_calls_tenant_created",
        "ix_tool_calls_tenant_status_created",
    },
    "approvals": {"ix_approvals_tenant_decision_created"},
    "usage": {
        "ix_usage_tenant_provider_model",
        "ix_usage_tenant_model_created",
    },
    "scheduled_tasks": {"ix_scheduled_tasks_due_claim"},
    "assessment_runs": {
        "ix_assessment_runs_tenant_deleted_started",
        "ix_assessment_runs_tenant_workload_deleted_started",
    },
    "cases": {"ix_cases_tenant_deleted_status_updated"},
    "case_events": {"ix_case_events_tenant_case_created"},
    "notifications": {"ix_notifications_tenant_created"},
    "notification_deliveries": {
        "ix_notification_deliveries_tenant_channel_notification"
    },
    "sessions": {
        "ix_sessions_revoked",
        "ix_sessions_expires",
        "ix_sessions_last_seen",
    },
    "rbac_scan_runs": {"ix_rbac_scan_runs_tenant_started"},
    "quota_scan_runs": {"ix_quota_scan_runs_tenant_started"},
}


def test_performance_indexes_exist_in_metadata_and_migration(monkeypatch) -> None:
    for table_name, expected in _EXPECTED_INDEXES.items():
        actual = {index.name for index in Base.metadata.tables[table_name].indexes}
        assert expected <= actual

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0011_background_work_leases.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0011", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    created: set[tuple[str, str]] = set()
    monkeypatch.setattr(migration, "_add_missing_columns", lambda *_args: None)
    monkeypatch.setattr(
        migration,
        "_create_index",
        lambda name, table, _columns: created.add((table, name)),
    )
    monkeypatch.setattr(migration, "_drop_index_if_present", lambda *_args: None)
    migration.upgrade()

    for table_name, expected in _EXPECTED_INDEXES.items():
        assert expected <= {
            name for table, name in created if table == table_name
        }

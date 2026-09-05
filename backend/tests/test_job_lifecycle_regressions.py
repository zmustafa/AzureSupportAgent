"""Job lifecycle regressions: tenant isolation, atomic batch replay, and loop-safe sampling.

SQLite uses only tmp_path. PostgreSQL is opt-in via JOB_LIFECYCLE_TEST_POSTGRES_URL:
only postgresql+asyncpg on a loopback host and database aznetagent_lifecycle_test
is accepted. Each case creates/drops its own UUID schema, without a public-schema
fallback. Never fall back to DATABASE_URL, start services, or invoke providers.
The parent conftest's schema fixture is overridden here to avoid application DB writes.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core import db as dbmod, work_batches
from app.core.durable_jobs import DurableJobExecutor
from app.models import Base, DurableJob, DurableJobEvent, DurableJobSlot, WorkBatch, WorkBatchItem
from app.monitor.sampler import PingSampler


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """Override the parent fixture: every database used here is explicitly injected."""


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def lifecycle_sql(request, tmp_path, monkeypatch):
    schema = None
    if request.param == "postgresql":
        raw_url = os.environ.get("JOB_LIFECYCLE_TEST_POSTGRES_URL")
        if not raw_url:
            pytest.skip("Requires an explicitly configured local lifecycle-test PostgreSQL database")
        url = make_url(raw_url)
        if (
            url.drivername != "postgresql+asyncpg"
            or url.host not in {"127.0.0.1", "localhost", "::1"}
            or url.database != "aznetagent_lifecycle_test"
            or url.query
        ):
            pytest.fail("Refusing a PostgreSQL URL outside the dedicated local lifecycle-test database")
        schema = "job_lifecycle_" + uuid.uuid4().hex
        engine = create_async_engine(
            url,
            isolation_level="READ COMMITTED",
            connect_args={"server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "5000",
            }},
        )
    else:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'lifecycle.db').as_posix()}",
            connect_args={"timeout": 10},
        )

        @event.listens_for(engine.sync_engine, "connect")
        def pragmas(connection, _record):
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=10000")
            finally:
                cursor.close()

    class LocalSession(Session):
        pass

    sessions = async_sessionmaker(
        engine, expire_on_commit=False, sync_session_class=LocalSession
    )
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    monkeypatch.setattr(work_batches, "worker", SimpleNamespace(
        ensure_running=AsyncMock(side_effect=AssertionError("Do not start a worker")),
        wake=lambda: None,
    ))
    sql = SimpleNamespace(
        sessions=sessions, engine=engine, session_type=LocalSession,
        executors=set(), handles=set(),
    )
    created_schema = False
    try:
        async with engine.begin() as connection:
            if schema is not None:
                await connection.execute(CreateSchema(schema))
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                Base.metadata.tables[model.__tablename__] for model in (
                    DurableJob, DurableJobSlot, DurableJobEvent, WorkBatch, WorkBatchItem
                )
            ]))
        created_schema = schema is not None
        yield sql
    finally:
        try:
            # Capture old handles as well as current maps: overwritten handles are
            # precisely the regression these tests must be able to clean up after.
            for executor in sql.executors:
                _remember(sql, executor)
            for task in sql.handles:
                if not task.done() and not task.cancelling():
                    task.cancel()
            if sql.handles:
                await asyncio.wait_for(
                    asyncio.gather(*sql.handles, return_exceptions=True), timeout=15
                )
        finally:
            try:
                if created_schema and schema is not None:
                    async with engine.begin() as connection:
                        await connection.execute(DropSchema(schema, cascade=True))
            finally:
                await engine.dispose()


def _remember(sql, executor):
    sql.executors.add(executor)
    sql.handles.update(executor.tasks.values())
    sql.handles.update(executor._heartbeats.values())


async def _rows(sql, model):
    async with sql.sessions() as session:
        result = await session.execute(select(model.__table__).order_by(model.id))
        return [dict(row) for row in result.mappings()]


async def _assert_terminal_jobs(sql, expected):
    jobs = await _rows(sql, DurableJob)
    slots = await _rows(sql, DurableJobSlot)
    events = await _rows(sql, DurableJobEvent)
    assert {row["id"]: row["status"] for row in jobs} == expected
    assert len(slots) == len({(row["tenant_id"], row["feature"], row["job_key"]) for row in jobs})
    by_id = {row["id"]: row for row in jobs}
    for slot in slots:
        job = by_id[slot["current_job_id"]]
        assert (slot["tenant_id"], slot["feature"], slot["job_key"]) == (
            job["tenant_id"], job["feature"], job["job_key"]
        )
        assert slot["lease_owner"] is slot["lease_token"] is slot["lease_expires_at"] is None
    assert {row["job_id"] for row in events} == set(expected)
    assert all(row["finished_at"] is not None for row in jobs)
    assert all(row["lease_token"] is None and row["lease_expires_at"] is None for row in jobs)


@pytest.fixture
def controlled_executor(lifecycle_sql, monkeypatch):
    executor = DurableJobExecutor(
        "lifecycle", session_factory=lifecycle_sql.sessions,
        owner_id="fixture-owner", lease_seconds=3600, poll_seconds=0.01,
    )
    gates = {}

    async def monitor(*, job_id, lease_token, should_stop):
        gate = gates.setdefault(job_id, asyncio.Event())
        await gate.wait()
        if should_stop():
            return False, False
        return await executor.store.lease_state(job_id=job_id, lease_token=lease_token)

    monkeypatch.setattr(executor.store, "monitor_lease", monitor)
    _remember(lifecycle_sql, executor)
    return SimpleNamespace(executor=executor, gates=gates, sql=lifecycle_sql)


async def _start(control, tenant, key="same"):
    entered, release = asyncio.Event(), asyncio.Event()

    async def runner(context):
        await context.emit("progress", {"tenant": tenant})
        entered.set()
        await release.wait()
        return {"tenant": tenant}

    claim = await control.executor.start(tenant_id=tenant, key=key, metadata={}, runner=runner)
    _remember(control.sql, control.executor)
    assert claim.acquired
    task = control.executor.tasks[(tenant or "default", key)]
    heartbeat = control.executor._heartbeats[(tenant or "default", key)]
    await asyncio.wait_for(entered.wait(), timeout=10)
    return SimpleNamespace(claim=claim, task=task, heartbeat=heartbeat, release=release)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation", ["local", "heartbeat"])
async def test_same_key_cancellation_is_tenant_scoped(controlled_executor, cancellation):
    control = controlled_executor
    executor = control.executor
    first = await _start(control, "tenant-a")
    # Preserve the actual Mission Control adapter's key-only .get access.
    assert executor.tasks.get("same") is first.task
    second = await _start(control, "tenant-b")
    assert set(executor.tasks) == set(executor._heartbeats) == {
        ("tenant-a", "same"), ("tenant-b", "same")
    }
    sentinel = object()
    assert executor.tasks.get("same") is None
    assert executor.tasks.get("same", sentinel) is sentinel
    assert executor.tasks.get("absent", sentinel) is sentinel
    if cancellation == "local":
        assert await executor.cancel(tenant_id="tenant-a", key="same")
    else:
        assert await executor.store.request_cancel(
            tenant_id="tenant-a", feature=executor.feature, key="same"
        )
        control.gates[first.claim.job["id"]].set()
    await asyncio.wait_for(first.task, timeout=10)
    assert not second.task.done() and not second.heartbeat.done()
    assert set(executor.tasks) == {("tenant-b", "same")}
    second.release.set()
    await asyncio.wait_for(second.task, timeout=10)
    await executor.stop()
    assert not executor.tasks and not executor._heartbeats
    assert first.heartbeat.done() and second.heartbeat.done()
    await _assert_terminal_jobs(control.sql, {
        first.claim.job["id"]: "cancelled", second.claim.job["id"]: "done",
    })
    jobs = {row["tenant_id"]: row for row in await _rows(control.sql, DurableJob)}
    assert jobs["tenant-b"]["result_json"] == {"tenant": "tenant-b"}
    assert not jobs["tenant-b"]["cancel_requested"]


@pytest.mark.asyncio
async def test_shutdown_accounts_for_all_same_key_tenants(controlled_executor):
    control = controlled_executor
    first = await _start(control, "tenant-a")
    second = await _start(control, "tenant-b")
    await control.executor.stop()
    await control.executor.stop()
    assert all(handle.done() for handle in control.sql.handles)
    assert not control.executor.tasks and not control.executor._heartbeats
    await _assert_terminal_jobs(control.sql, {
        first.claim.job["id"]: "error", second.claim.job["id"]: "error",
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_tenant", ["", "default"])
async def test_default_tenant_normalization_matches_sql_and_local_maps(controlled_executor, cancel_tenant):
    control = controlled_executor
    started = await _start(control, "")
    duplicate = await control.executor.start(
        tenant_id="default", key="same", metadata={},
        runner=AsyncMock(side_effect=AssertionError("Duplicate runner started")),
    )
    assert not duplicate.acquired and duplicate.job["id"] == started.claim.job["id"]
    assert set(control.executor.tasks) == set(control.executor._heartbeats) == {("default", "same")}
    assert await control.executor.cancel(tenant_id=cancel_tenant, key="same")
    await asyncio.wait_for(started.task, timeout=10)
    assert not await control.executor.cancel(tenant_id=cancel_tenant, key="same")
    await _assert_terminal_jobs(control.sql, {started.claim.job["id"]: "cancelled"})


@pytest.mark.asyncio
async def test_old_heartbeat_and_done_callback_cannot_touch_replacement(controlled_executor, monkeypatch):
    control = controlled_executor
    executor = control.executor
    first = await _start(control, "tenant-a")
    finalized, release_finalize = asyncio.Event(), asyncio.Event()
    original_finalize = executor.store.finalize

    async def delayed_finalize(**kwargs):
        result = await original_finalize(**kwargs)
        if kwargs["job_id"] == first.claim.job["id"]:
            finalized.set()
            await release_finalize.wait()
        return result

    monkeypatch.setattr(executor.store, "finalize", delayed_finalize)
    first.release.set()
    await asyncio.wait_for(finalized.wait(), timeout=10)
    second = await _start(control, "tenant-a")
    assert second.claim.job["id"] != first.claim.job["id"]
    # The old monitor resumes only AFTER a new attempt has replaced both maps.
    control.gates[first.claim.job["id"]].set()
    await asyncio.wait_for(first.task, timeout=10)
    assert executor.tasks[("tenant-a", "same")] is second.task
    assert executor._heartbeats[("tenant-a", "same")] is second.heartbeat
    assert first.heartbeat.done()
    assert not second.task.done() and not second.heartbeat.done()
    second.release.set()
    await asyncio.wait_for(second.task, timeout=10)
    assert not executor.tasks and not executor._heartbeats
    await _assert_terminal_jobs(control.sql, {
        first.claim.job["id"]: "done", second.claim.job["id"]: "done",
    })
    assert (await _rows(control.sql, DurableJobSlot))[0]["current_job_id"] == second.claim.job["id"]


@pytest.mark.asyncio
async def test_real_cost_manager_same_connection_two_app_tenants_shutdown(lifecycle_sql, monkeypatch):
    from app.inventory import cost_jobs

    manager = cost_jobs.CostJobManager(
        session_factory=lifecycle_sql.sessions, owner_id="cost-fixture",
        lease_seconds=3600, poll_seconds=0.01,
    )
    tenants = ("tenant-a", "tenant-b")
    entered = {tenant: asyncio.Event() for tenant in tenants}

    async def collect(connection, subscriptions, tenant, connection_id, **kwargs):
        assert connection is None and connection_id == "shared-connection"
        assert subscriptions == ["sub-1"] and kwargs["scope"] == "sub-1"
        entered[tenant].set()
        await asyncio.Event().wait()
        raise AssertionError("Collection should have been cancelled")

    async def monitor(**_kwargs):
        await asyncio.Event().wait()
        raise AssertionError("Monitor should have been cancelled")

    monkeypatch.setattr(cost_jobs.cost, "get_cost", collect)
    monkeypatch.setattr(manager._executor.store, "monitor_lease", monitor)
    jobs = []
    for tenant in tenants:
        jobs.append(await manager.start(
            tenant_id=tenant, connection_id="shared-connection", scope="sub-1",
            force=True, connection=None, subscriptions=["sub-1"],
        ))
        _remember(lifecycle_sql, manager._executor)
        await asyncio.wait_for(entered[tenant].wait(), timeout=10)
    assert len({job["id"] for job in jobs}) == 2
    assert len({row["job_key"] for row in await _rows(lifecycle_sql, DurableJob)}) == 1
    await manager._executor.stop()
    for tenant, job in zip(tenants, jobs):
        current = await manager.get(job["id"], tenant)
        assert current is not None and current["status"] == "failed"
        assert "interrupted" in current["error"].lower()
        other = tenants[1] if tenant == tenants[0] else tenants[0]
        assert await manager.get(job["id"], other) is None
    assert all(handle.done() for handle in lifecycle_sql.handles)
    assert not manager._executor.tasks and not manager._executor._heartbeats
    await _assert_terminal_jobs(lifecycle_sql, {job["id"]: "error" for job in jobs})


async def _batch(*, tenant="tenant-a", feature="assessment", key="request", items=None):
    return await work_batches.create_batch(
        tenant_id=tenant, feature=feature, actor="fixture", idempotency_key=key,
        items=items if items is not None else [{"item_key": "one"}, {"item_key": "two"}],
        config={"fixture": True}, start_worker=False,
    )


@pytest.mark.asyncio
async def test_concurrent_batch_claim_replays_complete_rows_without_orphans(lifecycle_sql, monkeypatch):
    barrier = asyncio.Barrier(2)
    contenders = []

    class RacingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if (
                getattr(statement, "is_insert", False)
                and statement.table.name == "work_batches"
                and not self.info.get("claim_gate")
            ):
                self.info["claim_gate"] = True
                # BEFORE executing the claim: never wait at a barrier with a DB
                # write lock, nor after a SELECT whose transaction can hold one.
                assert not self.in_transaction()
                contenders.append(self)
                await barrier.wait()
            return await super().execute(statement, *args, **kwargs)

    sessions = async_sessionmaker(lifecycle_sql.engine, class_=RacingSession, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    items = [{"item_key": " one "}, {"item_key": "one"}, {"item_key": "two"}, {"item_key": " "}]
    outcomes = await asyncio.wait_for(asyncio.gather(
        _batch(tenant="", key=" request ", items=items),
        _batch(tenant="default", key="request", items=items),
        return_exceptions=True,
    ), timeout=15)
    parents, children = await _rows(lifecycle_sql, WorkBatch), await _rows(lifecycle_sql, WorkBatchItem)
    assert len(contenders) == 2 and contenders[0] is not contenders[1]
    assert len(parents) == 1 and len(children) == 2
    assert parents[0]["total"] == 2
    assert parents[0]["tenant_id"] == "default" and parents[0]["idempotency_key"] == "request"
    assert {row["batch_id"] for row in children} == {parents[0]["id"]}
    assert {row["tenant_id"] for row in children} == {"default"}
    assert {row["item_key"] for row in children} == {"one", "two"}
    results = []
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcomes
        results.append(outcome)
    assert sorted(outcome[1] for outcome in results) == [False, True]
    assert results[0][0] == results[1][0]
    assert all(outcome[0]["total"] == len(outcome[0]["items"]) == 2 for outcome in results)


@pytest.mark.asyncio
async def test_batch_normalizes_stored_keys_and_replays_original_payload(lifecycle_sql):
    prefix = "x" * 256
    first, created = await _batch(tenant="", key=" request ", items=[
        {"item_key": prefix + "a"}, {"item_key": prefix + "b"},
        {"item_key": " one "}, {"item_key": "one"}, {"workload_id": "two"},
    ])
    assert created and first["total"] == len(first["items"]) == 3
    assert {row["item_key"] for row in first["items"]} == {prefix, "one", "two"}
    replay, created = await _batch(tenant="default", items=[{"item_key": "different"}])
    assert not created and replay == first
    other_tenant, tenant_created = await _batch(tenant="other")
    other_feature, feature_created = await _batch(tenant="default", feature="mission")
    assert tenant_created and feature_created
    parents, children = await _rows(lifecycle_sql, WorkBatch), await _rows(lifecycle_sql, WorkBatchItem)
    assert {row["id"] for row in parents} == {first["id"], other_tenant["id"], other_feature["id"]}
    assert len(children) == 7
    assert {row["batch_id"] for row in children} == {row["id"] for row in parents}


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [[], [{"item_key": " "}], [{"item_key": "one", "max_attempts": "bad"}]])
async def test_invalid_batch_rolls_back_key_parent_and_children(lifecycle_sql, items):
    with pytest.raises(ValueError):
        await _batch(items=items)
    assert await _rows(lifecycle_sql, WorkBatch) == []
    assert await _rows(lifecycle_sql, WorkBatchItem) == []
    retried, created = await _batch()
    assert created and retried["total"] == 2
    assert {row["batch_id"] for row in await _rows(lifecycle_sql, WorkBatchItem)} == {retried["id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["child_constraint", "after_flush"])
async def test_unrelated_integrity_failure_rolls_back_whole_batch(lifecycle_sql, failure):
    fired = []

    def break_child(session, *_args):
        if fired:
            return
        fired.append(True)
        if failure == "child_constraint":
            child = next(row for row in session.new if isinstance(row, WorkBatchItem))
            # Deliberately bypass the annotated setter to provoke a real SQL
            # NOT NULL violation, rather than the replayable unique constraint.
            setattr(child, "tenant_id", None)
        else:
            # Real child INSERTs have completed; an error here must undo all of them.
            raise IntegrityError("injected post-flush failure", {}, RuntimeError("unrelated"))

    hook = "before_flush" if failure == "child_constraint" else "after_flush_postexec"
    event.listen(lifecycle_sql.session_type, hook, break_child)
    try:
        with pytest.raises(IntegrityError):
            await _batch()
    finally:
        event.remove(lifecycle_sql.session_type, hook, break_child)
    assert fired == [True]
    assert await _rows(lifecycle_sql, WorkBatch) == []
    assert await _rows(lifecycle_sql, WorkBatchItem) == []
    retried, created = await _batch()
    assert created and retried["total"] == 2
    assert len(await _rows(lifecycle_sql, WorkBatch)) == 1
    assert len(await _rows(lifecycle_sql, WorkBatchItem)) == 2
    assert {row["batch_id"] for row in await _rows(lifecycle_sql, WorkBatchItem)} == {retried["id"]}


@pytest.mark.asyncio
async def test_other_unique_constraint_is_not_treated_as_idempotent_replay(lifecycle_sql, monkeypatch):
    original, _ = await _batch()
    before = await _rows(lifecycle_sql, WorkBatchItem)
    monkeypatch.setattr(work_batches, "uuid", SimpleNamespace(uuid4=lambda: original["id"]))
    with pytest.raises(IntegrityError):
        await _batch(key="different-request")
    assert [row["id"] for row in await _rows(lifecycle_sql, WorkBatch)] == [original["id"]]
    assert await _rows(lifecycle_sql, WorkBatchItem) == before


async def _wait_until_sampler_is_waiting(sampler, monkeypatch):
    stop = sampler._stop
    assert stop is not None
    entered = asyncio.Event()
    original_wait = stop.wait

    async def wait():
        entered.set()
        # Event.wait binds before this task yields back to the test. No wall-clock
        # delay or private asyncio _loop/_waiters inspection is required.
        await original_wait()

    monkeypatch.setattr(stop, "wait", wait)
    await asyncio.wait_for(entered.wait(), timeout=5)


@pytest.mark.parametrize("explicit_stop", [True, False])
def test_sampler_restarts_on_fresh_event_loops(monkeypatch, explicit_stop):
    sampler = PingSampler()
    monkeypatch.setattr(sampler, "_tick", AsyncMock())
    events = []

    async def lifecycle():
        sampler.start()
        events.append(sampler._stop)
        task = sampler._task
        sampler.start()
        assert sampler._task is task and sampler._stop is events[-1]
        await _wait_until_sampler_is_waiting(sampler, monkeypatch)
        if explicit_stop:
            await sampler.stop()
            await sampler.stop()
            assert sampler._task is None and sampler._stop is None

    for _ in range(3):
        # The implicit-stop case lets asyncio.run cancel the prior run's task.
        asyncio.run(lifecycle())
    assert len({id(stop) for stop in events}) == 3
    asyncio.run(sampler.stop())
    assert sampler._task is None and sampler._stop is None


@pytest.mark.asyncio
async def test_sampler_concurrent_stops_do_not_recancel_cleanup(monkeypatch):
    sampler = PingSampler()
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def tick():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    monkeypatch.setattr(sampler, "_tick", tick)
    sampler.start()
    await asyncio.wait_for(entered.wait(), timeout=5)
    first = asyncio.create_task(sampler.stop())
    await asyncio.wait_for(cleaning.wait(), timeout=5)
    second = asyncio.create_task(sampler.stop())
    try:
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert cleaned.is_set()
    assert sampler._task is None and sampler._stop is None
    monkeypatch.setattr(sampler, "_tick", AsyncMock())
    sampler.start()
    await _wait_until_sampler_is_waiting(sampler, monkeypatch)
    await sampler.stop()


@pytest.mark.asyncio
async def test_sampler_old_stop_preserves_a_new_run_started_by_done_callback(monkeypatch):
    sampler = PingSampler()
    monkeypatch.setattr(sampler, "_tick", AsyncMock())
    sampler.start()
    old_task, old_stop = sampler._task, sampler._stop
    assert old_task is not None and old_stop is not None
    await _wait_until_sampler_is_waiting(sampler, monkeypatch)
    # Registered before stop awaits old_task, so restart runs before stop's
    # finally block. That block may clear only the run it originally captured.
    old_task.add_done_callback(lambda _task: sampler.start())
    try:
        await sampler.stop()
        assert old_task.done() and old_stop.is_set()
        assert sampler._task is not None and sampler._task is not old_task
        assert sampler._stop is not None and sampler._stop is not old_stop
        assert not sampler._stop.is_set()
    finally:
        await sampler.stop()
    assert sampler._task is None and sampler._stop is None


@pytest.mark.asyncio
async def test_sampler_failed_task_stop_clears_handles_and_allows_restart(monkeypatch):
    sampler = PingSampler()
    original_loop = sampler._loop

    async def fail(_stop):
        raise RuntimeError("injected sampler failure")

    monkeypatch.setattr(sampler, "_loop", fail)
    sampler.start()
    task = sampler._task
    assert task is not None
    with pytest.raises(RuntimeError, match="injected sampler failure"):
        await task
    with pytest.raises(RuntimeError, match="injected sampler failure"):
        await sampler.stop()
    assert sampler._task is None and sampler._stop is None
    monkeypatch.setattr(sampler, "_loop", original_loop)
    monkeypatch.setattr(sampler, "_tick", AsyncMock())
    sampler.start()
    await _wait_until_sampler_is_waiting(sampler, monkeypatch)
    await sampler.stop()
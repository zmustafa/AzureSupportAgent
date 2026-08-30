from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.turn_runner import TurnRegistry
from app.core.durable_jobs import DurableJobStore, utcnow
from app.core.genjob import JobRegistry
from app.core.db import Base
from app.models import DurableJob, DurableJobEvent, DurableJobSlot


def test_migration_adopts_runtime_created_durable_tables(tmp_path: Path) -> None:
    """Upgrade succeeds when create_all reached the tables before Alembic did."""
    database = tmp_path / "runtime-before-alembic.db"
    sync_url = f"sqlite:///{database.as_posix()}"
    sync_engine = create_engine(sync_url)
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    backend = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
    for arguments in (("stamp", "0011_background_work_leases"), ("upgrade", "head")):
        completed = subprocess.run(  # noqa: S603 - fixed interpreter/module/test arguments
            [sys.executable, "-m", "alembic", *arguments],
            cwd=backend,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    sync_engine = create_engine(sync_url)
    with sync_engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0012_durable_job_registries"
        )
    assert {
        "durable_jobs",
        "durable_job_slots",
        "durable_job_events",
    } <= set(inspect(sync_engine).get_table_names())
    sync_engine.dispose()


@pytest.fixture
async def durable_sessions(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'durable-jobs.db'}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield sessions
    await engine.dispose()


async def _wait_job(
    registry: JobRegistry,
    key: str,
    *,
    tenant_id: str = "tenant-a",
    status: str | None = None,
    progress: int | None = None,
) -> dict:
    for _ in range(200):
        job = await registry.get_job(key, tenant_id=tenant_id)
        if job is not None:
            status_ready = status is None or job["status"] == status
            progress_ready = progress is None or len(job["progress"]) >= progress
            if status_ready and progress_ready:
                return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {key!r} did not reach status={status!r}, progress={progress!r}")


@pytest.mark.asyncio
async def test_two_instances_start_one_execution(durable_sessions) -> None:
    first = JobRegistry(
        "duplicate", session_factory=durable_sessions, owner_id="replica-a", poll_seconds=0.01
    )
    second = JobRegistry(
        "duplicate", session_factory=durable_sessions, owner_id="replica-b", poll_seconds=0.01
    )
    release = asyncio.Event()
    executions = 0

    async def runner(progress):
        nonlocal executions
        executions += 1
        await progress("work", "started")
        await release.wait()
        return {"ok": True}

    left, right = await asyncio.gather(
        first.start("same", runner, tenant_id="tenant-a"),
        second.start("same", runner, tenant_id="tenant-a"),
    )
    await asyncio.sleep(0)
    assert left["id"] == right["id"]
    assert executions == 1
    release.set()
    completed = await _wait_job(second, "same", status="done")
    assert completed["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_cross_instance_get_and_replay(durable_sessions) -> None:
    owner = JobRegistry(
        "replay", session_factory=durable_sessions, owner_id="replica-a", poll_seconds=0.01
    )
    reader = JobRegistry(
        "replay", session_factory=durable_sessions, owner_id="replica-b", poll_seconds=0.01
    )
    release = asyncio.Event()

    async def runner(progress):
        await progress("one", "first")
        await release.wait()
        await progress("two", "second")
        return {"value": 7}

    started = await owner.start("job", runner, tenant_id="tenant-a")
    remote = await _wait_job(reader, "job", progress=1)
    assert remote["id"] == started["id"]
    assert remote["progress"][0]["message"] == "first"

    async def collect() -> list[dict]:
        return [frame async for frame in reader.stream("job", tenant_id="tenant-a")]

    replay = asyncio.create_task(collect())
    release.set()
    frames = await asyncio.wait_for(replay, timeout=2)
    assert [frame["event"] for frame in frames] == ["start", "status", "status", "done"]


@pytest.mark.asyncio
async def test_cross_instance_cancel_reaches_owner(durable_sessions) -> None:
    owner = JobRegistry(
        "cancel", session_factory=durable_sessions, owner_id="replica-a",
        lease_seconds=0.15, poll_seconds=0.01,
    )
    remote = JobRegistry(
        "cancel", session_factory=durable_sessions, owner_id="replica-b",
        lease_seconds=0.15, poll_seconds=0.01,
    )
    cancelled = asyncio.Event()

    async def runner(_progress):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await owner.start("job", runner, tenant_id="tenant-a")
    assert await remote.cancel("job", tenant_id="tenant-a") is True
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    job = await _wait_job(remote, "job", status="error")
    assert job["error"] == "Generation was cancelled."


@pytest.mark.asyncio
async def test_stale_owner_cannot_finalize_new_fence(durable_sessions) -> None:
    old = DurableJobStore(
        session_factory=durable_sessions, owner_id="replica-a", lease_seconds=1,
        poll_seconds=0.01,
    )
    new = DurableJobStore(
        session_factory=durable_sessions, owner_id="replica-b", lease_seconds=1,
        poll_seconds=0.01,
    )
    first = await old.claim(tenant_id="tenant-a", feature="fence", key="job")
    assert first.acquired and first.lease_token
    expired = utcnow() - timedelta(seconds=1)
    async with durable_sessions() as db:
        await db.execute(
            update(DurableJobSlot)
            .where(DurableJobSlot.current_job_id == first.job["id"])
            .values(lease_expires_at=expired)
        )
        await db.execute(
            update(DurableJob)
            .where(DurableJob.id == first.job["id"])
            .values(lease_expires_at=expired)
        )
        await db.commit()
    recovered = await new.claim(tenant_id="tenant-a", feature="fence", key="job")
    assert recovered.acquired and recovered.lease_token
    assert recovered.job["id"] == first.job["id"]
    assert recovered.lease_token != first.lease_token
    assert await old.finalize(
        job_id=first.job["id"], lease_token=first.lease_token,
        status="done", result={"winner": "old"},
    ) is False
    assert await new.finalize(
        job_id=recovered.job["id"], lease_token=recovered.lease_token,
        status="done", result={"winner": "new"},
    ) is True
    current = await new.load_current(tenant_id="tenant-a", feature="fence", key="job")
    assert current is not None and current["result"] == {"winner": "new"}


@pytest.mark.asyncio
async def test_expired_owner_is_recovered_and_old_runner_is_fenced(durable_sessions) -> None:
    old = JobRegistry(
        "recover", session_factory=durable_sessions, owner_id="replica-a",
        lease_seconds=10, poll_seconds=0.01,
    )
    new = JobRegistry(
        "recover", session_factory=durable_sessions, owner_id="replica-b",
        lease_seconds=10, poll_seconds=0.01,
    )
    old_cancelled = asyncio.Event()

    async def old_runner(_progress):
        try:
            await asyncio.Event().wait()
        finally:
            old_cancelled.set()

    async def new_runner(progress):
        await progress("recovered", "new owner")
        return {"owner": "new"}

    initial = await old.start("job", old_runner, tenant_id="tenant-a")
    expired = utcnow() - timedelta(seconds=1)
    async with durable_sessions() as db:
        await db.execute(
            update(DurableJobSlot)
            .where(DurableJobSlot.current_job_id == initial["id"])
            .values(lease_expires_at=expired)
        )
        await db.execute(
            update(DurableJob)
            .where(DurableJob.id == initial["id"])
            .values(lease_expires_at=expired)
        )
        await db.commit()
    recovered = await new.start("job", new_runner, tenant_id="tenant-a")
    assert recovered["id"] == initial["id"]
    completed = await _wait_job(new, "job", status="done")
    assert completed["result"] == {"owner": "new"}
    assert completed["progress"][-1]["message"] == "new owner"
    old_task = old._tasks.get("job")  # noqa: SLF001 - prove stale completion is fenced
    if old_task is not None:
        old_task.cancel()
    await asyncio.wait_for(old_cancelled.wait(), timeout=1)
    still_new = await new.get_job("job", tenant_id="tenant-a")
    assert still_new is not None and still_new["result"] == {"owner": "new"}


@pytest.mark.asyncio
async def test_two_turn_registries_start_one_worker(durable_sessions) -> None:
    first = TurnRegistry(
        session_factory=durable_sessions, owner_id="replica-a", poll_seconds=0.01
    )
    second = TurnRegistry(
        session_factory=durable_sessions, owner_id="replica-b", poll_seconds=0.01
    )
    release = asyncio.Event()
    executions = 0

    async def worker(run):
        nonlocal executions
        executions += 1
        run.emit("token", {"content": "one"})
        await release.wait()
        run.emit("done", {"content": "one"})

    left, right = await asyncio.gather(
        first.start("chat-duplicate", "message-a", worker, tenant_id="tenant-a"),
        second.start("chat-duplicate", "message-b", worker, tenant_id="tenant-a"),
    )
    await asyncio.sleep(0)
    assert left.job_id == right.job_id
    assert executions == 1
    release.set()
    await left.wait()


@pytest.mark.asyncio
async def test_cross_instance_turn_replays_events_and_cancels(durable_sessions) -> None:
    owner = TurnRegistry(
        session_factory=durable_sessions, owner_id="replica-a",
        lease_seconds=0.15, poll_seconds=0.01,
    )
    remote = TurnRegistry(
        session_factory=durable_sessions, owner_id="replica-b",
        lease_seconds=0.15, poll_seconds=0.01,
    )
    emitted = asyncio.Event()
    stopped = asyncio.Event()

    async def worker(run):
        try:
            run.emit("token", {"content": "hello"})
            emitted.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.emit("done", {"content": "hello", "stopped": True})
            raise
        finally:
            stopped.set()

    started = await owner.start("chat-1", "message-1", worker, tenant_id="tenant-a")
    await emitted.wait()
    for _ in range(100):
        attached = await remote.get("chat-1", tenant_id="tenant-a")
        if attached is not None:
            events = await remote._store.events_after(started.job_id, -1)  # noqa: SLF001
            if events:
                break
        await asyncio.sleep(0.01)
    assert attached is not None and attached.job_id == started.job_id
    assert await remote.active_chat_ids(tenant_id="tenant-a") == ["chat-1"]
    live = await remote.live_snapshot(tenant_id="tenant-a")
    assert live["chat-1"]["kind"] == "chat"
    replay_task = asyncio.create_task(
        _collect_turn(attached)
    )
    assert await remote.cancel("chat-1", tenant_id="tenant-a") is True
    await asyncio.wait_for(stopped.wait(), timeout=1)
    replay = await asyncio.wait_for(replay_task, timeout=1)
    assert replay == [
        {"event": "token", "data": {"content": "hello"}},
        {"event": "done", "data": {"content": "hello", "stopped": True}},
    ]
    assert await remote.active_chat_ids(tenant_id="tenant-a") == []


async def _collect_turn(run) -> list[dict]:
    return [frame async for frame in run.subscribe()]


@pytest.mark.asyncio
async def test_terminal_cleanup_removes_metadata_and_events(durable_sessions) -> None:
    store = DurableJobStore(
        session_factory=durable_sessions, owner_id="replica-a", poll_seconds=0.01
    )
    claim = await store.claim(tenant_id="tenant-a", feature="cleanup", key="job")
    assert claim.lease_token
    await store.append_events(
        job_id=claim.job["id"], lease_token=claim.lease_token,
        events=[("status", {"message": str(index)}) for index in range(15)], event_limit=10,
    )
    bounded = await store.events_after(claim.job["id"], -1)
    assert len(bounded) == 10
    assert bounded[0]["data"]["message"] == "5"
    await store.finalize(
        job_id=claim.job["id"], lease_token=claim.lease_token,
        status="done", result={"ok": True}, retention_seconds=0,
    )
    assert await store.cleanup(feature="cleanup", now=utcnow() + timedelta(seconds=1)) == 1
    async with durable_sessions() as db:
        assert await db.get(DurableJob, claim.job["id"]) is None
        assert (
            await db.execute(
                select(DurableJobEvent).where(DurableJobEvent.job_id == claim.job["id"])
            )
        ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_payloads_are_bounded_before_persistence(durable_sessions) -> None:
    store = DurableJobStore(
        session_factory=durable_sessions, owner_id="replica-a", poll_seconds=0.01
    )
    claim = await store.claim(
        tenant_id="tenant-a",
        feature="bounds",
        key="job",
        metadata={"value": "x" * (70 * 1024)},
    )
    assert claim.lease_token
    current = await store.load_current(
        tenant_id="tenant-a", feature="bounds", key="job"
    )
    assert current is not None and current["metadata"] == {}
    stored = await store.append_events(
        job_id=claim.job["id"],
        lease_token=claim.lease_token,
        events=[("progress", {"value": "x" * (600 * 1024)})],
        event_limit=10,
    )
    assert stored[0]["data"] == {
        "message": "Event payload exceeded the durable replay limit."
    }
    assert await store.finalize(
        job_id=claim.job["id"],
        lease_token=claim.lease_token,
        status="done",
        result={"value": "x" * (3 * 1024 * 1024)},
    )
    terminal = await store.load_current(
        tenant_id="tenant-a", feature="bounds", key="job"
    )
    assert terminal is not None and terminal["result"] is None
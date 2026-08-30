"""Feature adapters on the shared durable-job substrate."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.architectures import jobs as architecture_jobs
from app.core.durable_jobs import DurableJobExecutor, utcnow
from app.core.security import Principal
from app.entra import job as entra_job
from app.iam import job as iam_job
from app.identity import appregs, appregs_job
from app.insights import jobs as insights_jobs
from app.inventory import cost_jobs
from app.models import DurableJob, DurableJobSlot
from app.missions import orchestrator as mission_jobs
from app.missions import systems as mission_systems


async def _wait_for(getter, *, status: str, predicate=None) -> dict:
    for _ in range(200):
        current = await getter()
        if (
            current is not None
            and current["status"] == status
            and (predicate is None or predicate(current))
        ):
            return current
        await asyncio.sleep(0.01)
    raise AssertionError(f"job did not reach {status}")


@pytest.mark.asyncio
async def test_architecture_job_is_visible_from_another_manager(
    durable_job_sessions, monkeypatch
) -> None:
    from app.workloads import registry as workload_registry

    monkeypatch.setattr(workload_registry, "get_workload", lambda _job_id: None)
    owner = architecture_jobs._Manager(
        session_factory=durable_job_sessions, owner_id="arch-a", poll_seconds=0.01
    )
    reader = architecture_jobs._Manager(
        session_factory=durable_job_sessions, owner_id="arch-b", poll_seconds=0.01
    )
    started = await owner.create(
        tenant_id="tenant-a",
        workload_id="missing",
        workload_name="Missing",
        connection_id="connection-a",
        created_by="user-a",
    )
    failed = await _wait_for(
        lambda: reader.get(started["id"], "tenant-a"), status="error"
    )
    assert failed["phase"] == "done"
    assert failed["message"] == "Workload not found."
    assert [item["id"] for item in await reader.list("tenant-a")] == [started["id"]]


@pytest.mark.asyncio
async def test_architecture_job_can_be_cancelled_from_another_manager(
    durable_job_sessions, monkeypatch
) -> None:
    from app.architectures import reverse
    from app.core import azure_connections
    from app.workloads import registry as workload_registry

    entered = asyncio.Event()

    monkeypatch.setattr(
        workload_registry,
        "get_workload",
        lambda _job_id: {"name": "Workload", "connection_id": ""},
    )
    monkeypatch.setattr(azure_connections, "resolve_connection", lambda _connection_id: None)

    async def dump_resources(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(reverse, "dump_resources", dump_resources)
    owner = architecture_jobs._Manager(
        session_factory=durable_job_sessions, owner_id="arch-a", poll_seconds=0.01
    )
    reader = architecture_jobs._Manager(
        session_factory=durable_job_sessions, owner_id="arch-b", poll_seconds=0.01
    )
    started = await owner.create(
        tenant_id="tenant-a",
        workload_id="workload-a",
        workload_name="Workload",
        connection_id="",
        created_by="user-a",
    )
    await entered.wait()
    assert await reader.cancel(started["id"], "tenant-a") is True
    cancelled = await _wait_for(
        lambda: reader.get(started["id"], "tenant-a"), status="canceled"
    )
    assert cancelled["message"] == "Canceled."


@pytest.mark.asyncio
async def test_appregs_two_managers_execute_once_and_do_not_persist_credentials(
    durable_job_sessions, monkeypatch, tmp_path
) -> None:
    from app.identity import appregs_cache

    monkeypatch.setattr(appregs_cache, "_CACHE_PATH", tmp_path / "snapshots.json")
    monkeypatch.setattr(appregs_cache, "_CHECKPOINT_PATH", tmp_path / "checkpoints.json")
    monkeypatch.setattr(appregs_cache, "_mem_cache", None)
    monkeypatch.setattr(appregs_cache, "_checkpoint_cache", None)
    release = asyncio.Event()
    executions = 0

    async def collect(*_args, **_kwargs):
        nonlocal executions
        executions += 1
        await release.wait()
        return {"source": "microsoft_graph", "apps": [], "summary": {"total": 0}}

    monkeypatch.setattr(appregs, "collect_app_registrations", collect)
    first = appregs_job.AppRegistrationsJobManager(
        session_factory=durable_job_sessions, owner_id="apps-a", poll_seconds=0.01
    )
    second = appregs_job.AppRegistrationsJobManager(
        session_factory=durable_job_sessions, owner_id="apps-b", poll_seconds=0.01
    )
    connection = {"id": "connection-a", "client_secret": "must-not-persist"}
    left, right = await asyncio.gather(
        first.start_job(
            key="tenant-a|connection-a",
            tenant_id="tenant-a",
            connection=connection,
            connection_id="connection-a",
        ),
        second.start_job(
            key="tenant-a|connection-a",
            tenant_id="tenant-a",
            connection=connection,
            connection_id="connection-a",
        ),
    )
    assert left["id"] == right["id"]
    for _ in range(200):
        if executions:
            break
        await asyncio.sleep(0.01)
    assert executions == 1
    durable = await second._executor.store.load_current(
        tenant_id="tenant-a", feature="identity.appregs", key="tenant-a|connection-a"
    )
    assert durable is not None
    assert "client_secret" not in durable["metadata"]
    assert "must-not-persist" not in str(durable["metadata"])
    assert await second.cancel_job("tenant-a|connection-a") is True
    await _wait_for(
        lambda: second.get_job("tenant-a|connection-a"), status="cancelled"
    )
    release.set()


@pytest.mark.asyncio
async def test_entra_two_managers_execute_once_and_replay_progress(
    durable_job_sessions, monkeypatch
) -> None:
    from app.entra import snapshot

    release = asyncio.Event()
    executions = 0

    async def refresh(*_args, progress, **_kwargs):
        nonlocal executions
        executions += 1
        await progress("info", "collecting")
        await release.wait()
        return {"ok": True}

    async def no_backfill(*_args, **_kwargs):
        return None

    monkeypatch.setattr(snapshot, "refresh", refresh)
    monkeypatch.setattr(entra_job, "_backfill_signin_outcomes", no_backfill)
    first = entra_job.EntraJobManager(
        session_factory=durable_job_sessions, owner_id="entra-a", poll_seconds=0.01
    )
    second = entra_job.EntraJobManager(
        session_factory=durable_job_sessions, owner_id="entra-b", poll_seconds=0.01
    )
    connection = {"id": "connection-a", "access_token": "must-not-persist"}
    left, right = await asyncio.gather(
        first.start_job(
            tenant_id="tenant-a", connection=connection, domains=["apps"], connection_id="connection-a"
        ),
        second.start_job(
            tenant_id="tenant-a", connection=connection, domains=["apps"], connection_id="connection-a"
        ),
    )
    assert left["id"] == right["id"]
    assert executions == 1
    remote = await _wait_for(
        lambda: second.get_job(entra_job.job_key("tenant-a")),
        status="running",
        predicate=lambda job: bool(job["progress"]),
    )
    assert remote["progress"][0]["message"] == "collecting"
    durable = await second._executor.store.load_current(
        tenant_id="tenant-a", feature="entra.refresh", key=entra_job.job_key("tenant-a")
    )
    assert durable is not None and "must-not-persist" not in str(durable["metadata"])
    replay_task = asyncio.create_task(
        _collect_events(second.stream(entra_job.job_key("tenant-a")))
    )
    release.set()
    await _wait_for(
        lambda: second.get_job(entra_job.job_key("tenant-a")), status="done"
    )
    frames = await asyncio.wait_for(replay_task, timeout=2)
    assert [frame["event"] for frame in frames] == ["start", "progress", "done"]


@pytest.mark.asyncio
async def test_iam_two_managers_execute_once(durable_job_sessions, monkeypatch) -> None:
    release = asyncio.Event()
    executions = 0

    async def refresh_scope(_tenant, _connection, _scope, *, display_name, progress):
        nonlocal executions
        executions += 1
        await progress("info", f"refreshing {display_name}")
        await release.wait()

    async def no_warm(*_args, **_kwargs):
        return None

    monkeypatch.setattr(iam_job.orchestrator, "refresh_scope", refresh_scope)
    monkeypatch.setattr(iam_job, "_warm_derived", no_warm)
    first = iam_job.IamJobManager(
        session_factory=durable_job_sessions, owner_id="iam-a", poll_seconds=0.01
    )
    second = iam_job.IamJobManager(
        session_factory=durable_job_sessions, owner_id="iam-b", poll_seconds=0.01
    )
    kwargs = {
        "tenant_id": "tenant-a",
        "connection": {"id": "connection-a", "secret": "must-not-persist"},
        "scope": "subscription-a",
        "mode": "scope",
        "display_name": "Subscription A",
        "record_run": False,
    }
    left, right = await asyncio.gather(first.start_job(**kwargs), second.start_job(**kwargs))
    assert left["id"] == right["id"]
    assert executions == 1
    release.set()
    completed = await _wait_for(
        lambda: second.get_job(iam_job.job_key("tenant-a", "subscription-a")),
        status="done",
    )
    assert completed["progress"][0]["message"] == "refreshing Subscription A"


@pytest.mark.asyncio
async def test_cost_job_cross_instance_latest(durable_job_sessions, monkeypatch) -> None:
    release = asyncio.Event()
    executions = 0

    async def get_cost(*_args, progress, **_kwargs):
        nonlocal executions
        executions += 1
        await progress({"type": "started", "subscriptions_total": 1, "message": "started"})
        await release.wait()
        return {"total": 1.0, "errors": []}

    monkeypatch.setattr(cost_jobs.cost, "get_cost", get_cost)
    first = cost_jobs.CostJobManager(
        session_factory=durable_job_sessions, owner_id="cost-a", poll_seconds=0.01
    )
    second = cost_jobs.CostJobManager(
        session_factory=durable_job_sessions, owner_id="cost-b", poll_seconds=0.01
    )
    kwargs = {
        "tenant_id": "tenant-a",
        "connection_id": "connection-a",
        "scope": "subscription-a",
        "force": True,
        "connection": None,
        "subscriptions": ["subscription-a"],
    }
    left, right = await asyncio.gather(first.start(**kwargs), second.start(**kwargs))
    assert left["id"] == right["id"]
    for _ in range(200):
        if executions:
            break
        await asyncio.sleep(0.01)
    assert executions == 1
    remote = None
    for _ in range(200):
        remote = await second.latest("tenant-a", "connection-a", "subscription-a")
        if remote is not None and remote["subscriptions_total"] == 1:
            break
        await asyncio.sleep(0.01)
    assert remote is not None and remote["subscriptions_total"] == 1
    release.set()
    await _wait_for(
        lambda: second.latest("tenant-a", "connection-a", "subscription-a"),
        status="succeeded",
    )


@pytest.mark.asyncio
async def test_insight_job_is_pollable_from_another_manager(
    durable_job_sessions,
) -> None:
    release = asyncio.Event()

    async def execute(progress):
        progress(stage="gather", label="Gathering", pct=25)
        await release.wait()
        return {"id": "run-a"}

    first = insights_jobs.InsightsJobManager(
        session_factory=durable_job_sessions, owner_id="insight-a", poll_seconds=0.01
    )
    second = insights_jobs.InsightsJobManager(
        session_factory=durable_job_sessions, owner_id="insight-b", poll_seconds=0.01
    )
    started = await first.start("tenant-a", execute, pack_name="Pack A")
    remote = None
    for _ in range(200):
        remote = await second.get("tenant-a", started["id"])
        if remote is not None and remote["steps"]:
            break
        await asyncio.sleep(0.01)
    assert remote is not None and remote["steps"]
    assert remote["steps"][0]["label"] == "Gathering"
    release.set()
    completed = await _wait_for(
        lambda: second.get("tenant-a", started["id"]), status="succeeded"
    )
    assert completed["run"] == {"id": "run-a"}


@pytest.mark.asyncio
async def test_non_resumable_insight_job_becomes_failed_after_lease_expiry(
    durable_job_sessions,
) -> None:
    never = asyncio.Event()

    async def execute(_progress):
        await never.wait()
        return {"unreachable": True}

    owner = insights_jobs.InsightsJobManager(
        session_factory=durable_job_sessions,
        owner_id="expired-owner",
        lease_seconds=10,
        poll_seconds=0.01,
    )
    reader = insights_jobs.InsightsJobManager(
        session_factory=durable_job_sessions,
        owner_id="status-reader",
        lease_seconds=10,
        poll_seconds=0.01,
    )
    started = await owner.start("tenant-a", execute)
    expired = utcnow() - timedelta(seconds=1)
    async with durable_job_sessions() as db:
        await db.execute(
            update(DurableJob)
            .where(DurableJob.job_key == started["id"])
            .values(lease_expires_at=expired)
        )
        await db.execute(
            update(DurableJobSlot)
            .where(DurableJobSlot.job_key == started["id"])
            .values(lease_expires_at=expired)
        )
        await db.commit()
    interrupted = await reader.get("tenant-a", started["id"])
    assert interrupted is not None
    assert interrupted["status"] == "failed"
    assert "interrupted" in interrupted["error"].lower()
    task = owner._executor.tasks.get(started["id"])
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_mission_duplicate_start_replay_and_remote_cancel(
    durable_job_sessions, monkeypatch
) -> None:
    from app.core import db as dbmod
    from app.workloads import registry as workload_registry

    entered = asyncio.Event()
    executions = 0

    async def run(_context, *, force, progress=None):
        nonlocal executions
        del force, progress
        executions += 1
        entered.set()
        await asyncio.Event().wait()

    async def state(_context):
        return None

    system = mission_systems.SystemDef(
        key="test", label="Test", icon="T", run=run, last_state=state
    )
    monkeypatch.setattr(dbmod, "SessionLocal", durable_job_sessions)
    monkeypatch.setattr(workload_registry, "get_workload", lambda _wid: {"id": "workload-a"})
    monkeypatch.setattr(mission_systems, "SYSTEMS", [system])
    monkeypatch.setattr(mission_systems, "_BY_KEY", {"test": system})
    monkeypatch.setattr(mission_jobs, "_admission", mission_jobs._AdmissionQueue())
    monkeypatch.setattr(mission_jobs, "_MISSION_START_STAGGER_S", 0)
    first = mission_jobs._Manager(
        session_factory=durable_job_sessions, owner_id="mission-a", poll_seconds=0.01
    )
    second = mission_jobs._Manager(
        session_factory=durable_job_sessions, owner_id="mission-b", poll_seconds=0.01
    )
    kwargs = {
        "tenant_id": "tenant-a",
        "workload_id": "workload-a",
        "workload_name": "Workload A",
        "connection_id": "",
        "actor": "user-a",
        "force": True,
        "trigger": "manual",
        "system_keys": ["test"],
        "mission_id": "mission-a",
    }
    left, right = await asyncio.gather(first.create(**kwargs), second.create(**kwargs))
    assert left["id"] == right["id"] == "mission-a"
    await entered.wait()
    assert executions == 1
    replay_task = asyncio.create_task(_collect_events(second.stream("mission-a", "tenant-a")))
    assert await second.cancel("mission-a", "tenant-a") is True
    for _ in range(500):
        current = await mission_jobs.get_mission("mission-a", "tenant-a")
        if current is not None and current["status"] == "cancelled":
            break
        await asyncio.sleep(0.01)
    assert current is not None and current["status"] == "cancelled"
    frames = await asyncio.wait_for(replay_task, timeout=2)
    assert frames[0]["event"] == "snapshot"
    assert frames[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_resiliency_job_is_visible_from_another_executor(
    durable_job_sessions, monkeypatch
) -> None:
    from app.api import resiliency

    owner = DurableJobExecutor(
        "resiliency.analyze",
        session_factory=durable_job_sessions,
        owner_id="resiliency-a",
        poll_seconds=0.01,
        event_limit=50,
    )
    reader = DurableJobExecutor(
        "resiliency.analyze",
        session_factory=durable_job_sessions,
        owner_id="resiliency-b",
        poll_seconds=0.01,
        event_limit=50,
    )
    monkeypatch.setattr(resiliency, "_job_executor", owner)
    monkeypatch.setattr(resiliency, "_scope", lambda *_args: ("subscription", "subscription-a"))
    monkeypatch.setattr(resiliency, "_connection", lambda *_args: {"id": "connection-a"})
    monkeypatch.setattr(resiliency.snapshot_store, "get_lock", lambda *_args: asyncio.Lock())
    monkeypatch.setattr(resiliency.snapshot_store, "write", lambda *_args: None)
    monkeypatch.setattr(resiliency.history_store, "record", lambda *_args: None)

    async def subscriptions(*_args, **_kwargs):
        return ["subscription-a"]

    async def analyze(*_args, progress, **_kwargs):
        await progress("info", "analyzing")
        return {"summary": {"total": 1}}

    monkeypatch.setattr(resiliency, "_subscriptions_for", subscriptions)
    monkeypatch.setattr(resiliency.analyze_mod, "analyze", analyze)
    principal = Principal("user-a", "a@example.test", "tenant-a", "admin")
    scope = resiliency.ScopeParams(subscription_id="subscription-a", connection_id="connection-a")
    async with durable_job_sessions() as db:
        response = await resiliency.analyze_start(scope=scope, principal=principal, db=db)
    key = response["job"]["key"]
    monkeypatch.setattr(resiliency, "_job_executor", reader)
    completed = await _wait_for(
        lambda: resiliency._load_analysis_job("tenant-a", key), status="done"
    )
    assert completed["messages"][0]["message"] == "analyzing"


async def _collect_events(stream) -> list[dict]:
    return [frame async for frame in stream]
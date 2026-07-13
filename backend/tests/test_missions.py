"""Tests for Workload Mission Control — system registry, orchestrator rollup/persistence,
freshness-skip, and the scheduler mission target."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.core.db as dbmod
from app.missions import orchestrator as orch
from app.missions import systems as sysreg
from app.models import Base, MissionRun


# --------------------------------------------------------------------- registry
def test_default_and_resolve_keys():
    keys = sysreg.default_system_keys()
    assert keys[0] == "architecture" and "assessment" in keys and "radar" in keys
    # Subset is validated + re-ordered to canonical order; unknown keys dropped.
    assert sysreg.resolve_keys(["radar", "assessment", "bogus"]) == ["assessment", "radar"]
    # Empty/None -> all systems.
    assert sysreg.resolve_keys([]) == keys
    assert sysreg.resolve_keys(None) == keys


def test_headline_extractors():
    h, score, att = sysreg._h_amba({"coverage_pct": 40, "kpis": {"alerts_missing": 12}})
    assert "40%" in h and score == 40 and att is True
    h2, s2, a2 = sysreg._h_backupdr({"scorecard": {"pct_protected": 100, "protected": 3, "total": 3}})
    assert "100%" in h2 and a2 is False
    h3, _s3, a3 = sysreg._h_radar({"counts": {"total": 0}})
    assert "clear" in h3 and a3 is False
    h4, _s4, a4 = sysreg._h_radar({"counts": {"total": 4, "red": 2, "retirement": 1}})
    assert "4 item" in h4 and a4 is True


def test_alerts_manager_registered_after_monitoring_and_headlines():
    keys = sysreg.all_system_keys()
    assert keys[keys.index("monitoring") + 1] == "alerts_manager"
    system = sysreg.get_system("alerts_manager")
    assert system is not None and system.label == "Alerts Manager" and system.informational is False

    clean, score, attention = sysreg._h_alerts_manager({
        "rationalization_score": 100,
        "kpis": {"total_rules": 24, "overlap_groups": 0, "gap_count": 0},
    })
    assert clean == "100/100 · 24 rules · no overlaps or gaps"
    assert score == 100 and attention is False

    partial, score, attention = sysreg._h_alerts_manager({
        "rationalization_score": 82,
        "partial": True,
        "kpis": {"total_rules": 47, "overlap_groups": 3, "gap_count": 6},
    })
    assert partial == "Partial · 82/100 · 47 rules · 3 overlaps · 6 gaps"
    assert score == 82 and attention is True


def _alerts_context():
    return sysreg.MissionContext(
        tenant_id="mission-alerts-tenant",
        actor="mission-test",
        workload_id="w-alerts",
        workload={"id": "w-alerts", "connection_id": "c-alerts"},
        connection={"id": "c-alerts", "tenant_id": "azure-tenant"},
        connection_id="c-alerts",
    )


def test_alerts_manager_cached_state_is_scope_isolated_and_read_only(monkeypatch, tmp_path):
    from app.alert_analysis import cache
    from app.api import alert_analysis

    monkeypatch.setattr(cache, "_PATH", tmp_path / "alert-analysis-cache.json")
    monkeypatch.setattr(alert_analysis, "_effective_connection_id", lambda *_args: "c-alerts")
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_exists": True,
        "rationalization_score": 74,
        "kpis": {"total_rules": 12, "overlap_groups": 1, "gap_count": 2},
    }
    cache.write_snapshot("another-tenant", "c-alerts", "workload", "w-alerts", snapshot)
    assert asyncio.run(sysreg._state_alerts_manager(_alerts_context())) is None

    cache.write_snapshot("mission-alerts-tenant", "wrong-connection", "workload", "w-alerts", snapshot)
    assert asyncio.run(sysreg._state_alerts_manager(_alerts_context())) is None

    cache.write_snapshot("mission-alerts-tenant", "c-alerts", "workload", "w-alerts", snapshot)
    state = asyncio.run(sysreg._state_alerts_manager(_alerts_context()))
    assert state is not None
    assert state["status"] == "done" and state["score"] == 74 and state["attention"] is True
    assert "workload_id=w-alerts" in state["link"] and "connection_id=c-alerts" in state["link"]
    assert state["age_seconds"] is not None


def test_alerts_manager_run_reuses_refresh_persistence_and_progress(monkeypatch):
    from app.api import alert_analysis
    import app.core.db as core_db

    calls: list[tuple] = []
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_exists": True,
        "partial": True,
        "error": "Result cap reached; findings may be partial.",
        "rationalization_score": 81,
        "kpis": {"total_rules": 33, "overlap_groups": 0, "gap_count": 1},
    }

    async def fake_snapshot(principal, scope_kind, scope_id, **kwargs):
        calls.append(("snapshot", principal.tenant_id, scope_kind, scope_id, kwargs["force"], kwargs["connection_id"]))
        await kwargs["progress"]("query", "Loaded alert rules")
        return snapshot

    async def fake_invalidate(principal, scope_kind, scope_id, connection_id):
        calls.append(("invalidate", principal.tenant_id, scope_kind, scope_id, connection_id))

    async def fake_persist(value, principal, scope_kind, scope_id, db, progress):
        calls.append(("persist", value is snapshot, principal.tenant_id, scope_kind, scope_id, db))
        await progress("save", "Saved trend and history")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(alert_analysis, "_snapshot", fake_snapshot)
    monkeypatch.setattr(alert_analysis, "_invalidate_live_inventory", fake_invalidate)
    monkeypatch.setattr(alert_analysis, "_persist_refresh", fake_persist)
    monkeypatch.setattr(core_db, "SessionLocal", FakeSession)
    messages: list[str] = []

    async def progress(message: str):
        messages.append(message)

    result = asyncio.run(sysreg._run_alerts_manager(_alerts_context(), force=False, progress=progress))
    assert result.status == "done" and result.attention is True and result.score == 81
    assert calls[0] == ("snapshot", "mission-alerts-tenant", "workload", "w-alerts", True, "c-alerts")
    assert [call[0] for call in calls] == ["snapshot", "invalidate", "persist"]
    assert messages == ["Loaded alert rules", "Saved trend and history"]


def test_alerts_manager_unusable_error_is_failure(monkeypatch):
    from app.api import alert_analysis

    async def failed_snapshot(*_args, **_kwargs):
        return {"report_exists": False, "error": "Azure authentication failed", "kpis": {}}

    monkeypatch.setattr(alert_analysis, "_snapshot", failed_snapshot)
    result = asyncio.run(sysreg._run_alerts_manager(_alerts_context(), force=True))
    assert result.status == "fail" and result.attention is True
    assert result.error == "Azure authentication failed"


def test_unknown_system_keys_are_rejected():
    from app.api.missions import _validate_systems

    _validate_systems(None)
    _validate_systems([])
    _validate_systems(["monitoring", "alerts_manager"])
    with pytest.raises(HTTPException) as exc:
        _validate_systems(["monitoring", "typo-system"])
    assert exc.value.status_code == 422
    assert exc.value.detail["unknown"] == ["typo-system"]


def test_explicit_unknown_connection_is_not_accepted(monkeypatch):
    from app.api.missions import _resolve

    monkeypatch.setattr("app.workloads.registry.get_workload", lambda _wid: {"id": "w1"})
    monkeypatch.setattr("app.core.azure_connections.get_connection", lambda _cid: None)
    workload, connection_id = _resolve("w1", "missing-connection")
    assert workload == {"id": "w1"}
    assert connection_id is None


def test_cancelled_rollup_is_never_all_systems_go():
    mission = orch._Mission(
        id="cancelled", tenant_id="t1", workload_id="w1", workload_name="WL",
        connection_id="c1", actor="tester", force=False, trigger="manual",
        system_keys=["monitoring"], cancel_requested=True,
        systems={"monitoring": {"status": "done", "attention": False}},
    )
    orch.manager._rollup(mission)
    assert mission.status == "cancelled"
    assert mission.readiness == "cancelled"


def test_fmea_system_registered_and_headline():
    # The FMEA system is in the catalog with its full name and depends on memory.
    assert "fmea" in sysreg.all_system_keys()
    fmea = sysreg.get_system("fmea")
    assert fmea is not None
    assert fmea.label == "Failure Mode and Effects Analysis"
    assert "memory" in fmea.depends_on
    # Headline extractor: critical risks flag attention; an empty doc does not.
    head, att = sysreg._fmea_headline({"counts": {"critical": 2, "high": 1}, "total_rows": 8, "top_rpn": 560})
    assert "8 risks" in head and "2 critical" in head and "top RPN 560" in head and att is True
    head0, att0 = sysreg._fmea_headline({"counts": {"critical": 0, "high": 0}, "total_rows": 0, "top_rpn": 0})
    assert att0 is False and "No failure modes" in head0



# --------------------------------------------------------------------- helpers
def _fake_system(key, *, status="done", attention=False, headline="ok", last=None):
    async def run(ctx, *, force, progress=None):
        if progress:
            await progress("working")
        return sysreg.SystemResult(status=status, headline=headline, attention=attention, link=f"/x/{key}", result_ref={"kind": key})

    async def last_state(ctx):
        return last

    return sysreg.SystemDef(key=key, label=key.title(), icon="•", run=run, last_state=last_state)


def _patch_env(monkeypatch, tmp_path, fakes):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr("app.workloads.registry.get_workload", lambda wid, **k: {"id": wid, "name": "WL", "connection_id": "c1"})
    monkeypatch.setattr("app.core.azure_connections.connection_for_workload", lambda wl: {"id": "c1"})
    monkeypatch.setattr("app.core.azure_connections.resolve_connection", lambda cid: {"id": cid or "c1"})
    monkeypatch.setattr(sysreg, "SYSTEMS", fakes)
    monkeypatch.setattr(sysreg, "_BY_KEY", {s.key: s for s in fakes})
    return Session


def _drive(keys, *, force=True, trigger="manual"):
    async def go():
        pub = orch.manager.create(
            tenant_id="t1", workload_id="w1", workload_name="WL", connection_id="c1",
            actor="tester", force=force, trigger=trigger, system_keys=keys,
        )
        m = orch.manager.get_live(pub["id"], "t1")
        await m.task
        return await orch.get_mission(pub["id"], "t1")

    return asyncio.run(go())


# --------------------------------------------------------------------- orchestrator
def test_mission_runs_persists_and_rolls_up_warn(tmp_path, monkeypatch):
    fakes = [
        _fake_system("assessment", attention=True, headline="62/100 · 5 fail"),
        _fake_system("monitoring", attention=False, headline="90% coverage"),
    ]
    Session = _patch_env(monkeypatch, tmp_path, fakes)
    final = _drive(["assessment", "monitoring"])

    assert final["status"] == "succeeded"
    assert final["readiness"] == "warn"  # one system needs attention
    assert final["systems_total"] == 2 and final["systems_done"] == 2
    assert final["systems_attention"] == 1

    async def fetch():
        async with Session() as db:
            return await db.get(MissionRun, final["id"])

    row = asyncio.run(fetch())
    assert row is not None and row.status == "succeeded" and row.readiness == "warn"
    assert len(row.systems_json) == 2


def test_mission_all_green_is_go(tmp_path, monkeypatch):
    fakes = [_fake_system("monitoring", headline="100%"), _fake_system("telemetry", headline="95%")]
    _patch_env(monkeypatch, tmp_path, fakes)
    final = _drive(["monitoring", "telemetry"])
    assert final["readiness"] == "go" and final["status"] == "succeeded"


def test_mission_hard_fail_is_nogo_and_partial(tmp_path, monkeypatch):
    fakes = [_fake_system("assessment", status="fail", headline="boom"), _fake_system("monitoring", headline="90%")]
    _patch_env(monkeypatch, tmp_path, fakes)
    final = _drive(["assessment", "monitoring"])
    assert final["readiness"] == "nogo"
    assert final["status"] == "partial"  # one ran, one failed


def test_freshness_skip_unless_forced(tmp_path, monkeypatch):
    # last_state reports a recent run -> a non-forced launch should SKIP it.
    fresh = {"status": "done", "headline": "cached 88%", "score": 88, "attention": False, "age_seconds": 30, "link": "/x/monitoring"}
    fakes = [_fake_system("monitoring", headline="recomputed", last=fresh)]
    _patch_env(monkeypatch, tmp_path, fakes)
    final = _drive(["monitoring"], force=False)
    sys0 = final["systems"][0]
    assert sys0["status"] == "skipped"
    assert "cached" in sys0["headline"]

    # Forced launch re-runs it.
    final2 = _drive(["monitoring"], force=True)
    assert final2["systems"][0]["status"] == "done"
    assert final2["systems"][0]["headline"] == "recomputed"


def test_state_board_without_running(tmp_path, monkeypatch):
    fresh = {"status": "done", "headline": "88%", "score": 88, "attention": False, "age_seconds": 100, "link": "/x/monitoring"}
    fakes = [_fake_system("monitoring", last=fresh), _fake_system("telemetry", last=None)]
    _patch_env(monkeypatch, tmp_path, fakes)

    board = asyncio.run(orch.manager.state(tenant_id="t1", workload_id="w1", actor="tester"))
    by = {s["key"]: s for s in board["systems"]}
    assert by["monitoring"]["status"] == "done" and by["monitoring"]["fresh"] is True
    assert by["telemetry"]["status"] == "idle"


# ------------------------------------------------------- orphan reaper + reconnect
def test_reap_orphaned_missions(tmp_path, monkeypatch):
    """A mission left running/queued by a dead process is reaped to a terminal state so the
    board's reconnect never tries to follow a non-existent live stream."""
    Session = _patch_env(monkeypatch, tmp_path, [_fake_system("monitoring")])

    async def seed_and_reap():
        async with Session() as db:
            db.add(MissionRun(id="orphan-run", tenant_id="t1", workload_id="w1", status="running"))
            db.add(MissionRun(id="orphan-queued", tenant_id="t1", workload_id="w1", status="queued"))
            db.add(MissionRun(id="done-run", tenant_id="t1", workload_id="w1", status="succeeded"))
            await db.commit()
        reaped = await orch.reap_orphaned_missions()
        async with Session() as db:
            orphan = await db.get(MissionRun, "orphan-run")
            queued = await db.get(MissionRun, "orphan-queued")
            done = await db.get(MissionRun, "done-run")
        return reaped, orphan, queued, done

    reaped, orphan, queued, done = asyncio.run(seed_and_reap())
    assert reaped == 2  # only the two non-terminal rows
    assert orphan.status == "failed" and orphan.error and orphan.ended_at is not None
    assert queued.status == "failed"
    assert done.status == "succeeded"  # terminal rows are untouched


def test_delete_missions_for_workload(tmp_path, monkeypatch):
    """Deleting a workload's Mission Control hard-removes all of its mission runs (no trash)
    and leaves other workloads' and tenants' runs untouched."""
    Session = _patch_env(monkeypatch, tmp_path, [_fake_system("monitoring")])

    async def seed_and_delete():
        async with Session() as db:
            db.add(MissionRun(id="w1-a", tenant_id="t1", workload_id="w1", status="succeeded"))
            db.add(MissionRun(id="w1-b", tenant_id="t1", workload_id="w1", status="failed"))
            db.add(MissionRun(id="w2-a", tenant_id="t1", workload_id="w2", status="succeeded"))
            db.add(MissionRun(id="w1-other-tenant", tenant_id="t2", workload_id="w1", status="succeeded"))
            await db.commit()
        deleted = await orch.delete_missions_for_workload("t1", "w1")
        remaining = await orch.list_missions("t1", None, 50)
        async with Session() as db:
            other = await db.get(MissionRun, "w1-other-tenant")
        return deleted, remaining, other

    deleted, remaining, other = asyncio.run(seed_and_delete())
    assert deleted == 2  # both t1/w1 rows, hard-deleted
    assert {m["id"] for m in remaining} == {"w2-a"}  # w1 gone, w2 kept
    assert other is not None  # cross-tenant isolation


def test_latest_missions_returns_one_per_workload_without_global_limit(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'latest.db'}")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)

    async def seed_and_read():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with Session() as db:
            for index in range(205):
                db.add(MissionRun(
                    id=f"w1-{index}", tenant_id="t1", workload_id="w1",
                    workload_name="One", status="succeeded",
                    started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ))
            db.add(MissionRun(
                id="w2-only", tenant_id="t1", workload_id="w2",
                workload_name="Two", status="succeeded",
                started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ))
            db.add(MissionRun(id="other-tenant", tenant_id="t2", workload_id="w3", status="succeeded"))
            await db.commit()
        result = await orch.latest_missions_by_workload("t1")
        await engine.dispose()
        return result

    latest = asyncio.run(seed_and_read())
    assert {mission["workload_id"] for mission in latest} == {"w1", "w2"}


def test_central_queue_limits_global_and_per_connection(tmp_path, monkeypatch):
    running = 0
    max_running = 0
    lane_running: dict[str, int] = {}
    max_lane: dict[str, int] = {}

    async def run(ctx, *, force, progress=None):  # noqa: ARG001
        nonlocal running, max_running
        lane = ctx.connection_id
        running += 1
        lane_running[lane] = lane_running.get(lane, 0) + 1
        max_running = max(max_running, running)
        max_lane[lane] = max(max_lane.get(lane, 0), lane_running[lane])
        await asyncio.sleep(0.04)
        lane_running[lane] -= 1
        running -= 1
        return sysreg.SystemResult(status="done", headline="ok")

    async def state(_ctx):
        return None

    fake = sysreg.SystemDef(key="azure", label="Azure", icon="A", run=run, last_state=state)
    _patch_env(monkeypatch, tmp_path, [fake])
    async def no_db(_mission):
        return None
    monkeypatch.setattr(orch.manager, "_create_row", no_db)
    monkeypatch.setattr(orch.manager, "_persist", no_db)
    monkeypatch.setattr(orch, "_admission", orch._AdmissionQueue())
    monkeypatch.setattr(orch, "_MAX_ACTIVE_MISSIONS_GLOBAL", 2)
    monkeypatch.setattr(orch, "_MAX_ACTIVE_MISSIONS_PER_CONNECTION", 1)
    monkeypatch.setattr(orch, "_MISSION_START_STAGGER_S", 0)

    async def launch():
        missions = []
        for index, connection_id in enumerate(("c1", "c1", "c1", "c2", "c2")):
            public = orch.manager.create(
                tenant_id="queue-tenant", workload_id=f"w{index}", workload_name=f"W{index}",
                connection_id=connection_id, actor="tester", force=True, trigger="fleet",
                system_keys=["azure"],
            )
            missions.append(orch.manager.get_live(public["id"], "queue-tenant"))
        await asyncio.gather(*(mission.task for mission in missions if mission and mission.task))
        return [mission.public() for mission in missions if mission]

    results = asyncio.run(launch())
    assert max_running == 2
    assert max_lane == {"c1": 1, "c2": 1}
    assert all(result["status"] == "succeeded" for result in results)


def test_central_queue_cancel_waiting_mission_never_runs(tmp_path, monkeypatch):
    gate = asyncio.Event()
    started: list[str] = []

    async def run(ctx, *, force, progress=None):  # noqa: ARG001
        started.append(ctx.workload_id)
        await gate.wait()
        return sysreg.SystemResult(status="done", headline="ok")

    async def state(_ctx):
        return None

    fake = sysreg.SystemDef(key="azure", label="Azure", icon="A", run=run, last_state=state)
    _patch_env(monkeypatch, tmp_path, [fake])
    async def no_db(_mission):
        return None
    monkeypatch.setattr(orch.manager, "_create_row", no_db)
    monkeypatch.setattr(orch.manager, "_persist", no_db)
    monkeypatch.setattr(orch, "_admission", orch._AdmissionQueue())
    monkeypatch.setattr(orch, "_MAX_ACTIVE_MISSIONS_GLOBAL", 1)
    monkeypatch.setattr(orch, "_MAX_ACTIVE_MISSIONS_PER_CONNECTION", 1)
    monkeypatch.setattr(orch, "_MISSION_START_STAGGER_S", 0)

    async def launch_and_cancel():
        first = orch.manager.create(tenant_id="cancel-queue", workload_id="first", workload_name="First", connection_id="c1", actor="tester", force=True, trigger="fleet", system_keys=["azure"])
        second = orch.manager.create(tenant_id="cancel-queue", workload_id="second", workload_name="Second", connection_id="c1", actor="tester", force=True, trigger="fleet", system_keys=["azure"])
        await asyncio.sleep(0.03)
        second_live = orch.manager.get_live(second["id"], "cancel-queue")
        assert second_live is not None and second_live.status == "queued" and second_live.queue_position == 1
        assert orch.manager.cancel(second["id"], "cancel-queue") is True
        await second_live.task
        gate.set()
        first_live = orch.manager.get_live(first["id"], "cancel-queue")
        await first_live.task
        return second_live.public()

    cancelled = asyncio.run(launch_and_cancel())
    assert started == ["first"]
    assert cancelled["status"] == "cancelled"


def test_stream_falls_back_to_db_when_not_live(tmp_path, monkeypatch):
    """Reconnecting to a mission that isn't live in this process (finished + evicted, or
    orphaned by a restart) emits the DB snapshot + done — NOT a 'Mission not found.' error."""
    Session = _patch_env(monkeypatch, tmp_path, [_fake_system("monitoring")])

    async def go():
        async with Session() as db:
            db.add(MissionRun(id="past-run", tenant_id="t1", workload_id="w1",
                              workload_name="WL", status="succeeded", readiness="go"))
            await db.commit()
        # past-run is NOT in manager._missions (simulating an evicted/orphaned mission).
        events = [ev async for ev in orch.manager.stream("past-run", "t1")]
        # A genuinely unknown id still errors.
        missing = [ev async for ev in orch.manager.stream("nope", "t1")]
        return events, missing

    events, missing = asyncio.run(go())
    kinds = [e["event"] for e in events]
    assert kinds == ["snapshot", "done"] and "error" not in kinds
    assert [e["event"] for e in missing] == ["error"]


# --------------------------------------------------------------------- scheduler target
def test_mission_target_validate_and_label():
    from app.automations.targets import get_target

    t = get_target("mission")
    assert t.validate({}) is not None  # needs workloads
    assert t.validate({"workload_ids": ["w1"]}) is None
    label = t.label({"workload_ids": ["w1", "w2"], "systems": ["assessment"]})
    assert "2 workload" in label and "1 systems" in label


def test_mission_target_registered():
    from app.automations.targets import TARGET_TYPES, get_target

    assert "mission" in TARGET_TYPES
    assert get_target("mission").type_name == "mission"

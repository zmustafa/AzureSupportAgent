"""Tests for Workload Mission Control — system registry, orchestrator rollup/persistence,
freshness-skip, and the scheduler mission target."""
from __future__ import annotations

import asyncio

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

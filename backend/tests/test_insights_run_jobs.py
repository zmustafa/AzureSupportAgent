"""Background on-demand run jobs — progress registry + runner instrumentation tests.

Covers ``app.insights.jobs`` (durable start / progress / finish / tenant-scoped get) and the
progress callbacks emitted by ``runner.run_pack`` (the scope → gather → reason → gate → done
milestones the polling UI renders). The reason + source stages are stubbed so the test never
touches the LLM or Azure.
"""
import asyncio

import pytest

from app.insights import jobs
from app.insights import runner
from app.insights import runs as runs_store
from app.insights import sources as sources_mod
from app.insights import reason as reason_mod


@pytest.fixture(autouse=True)
async def _durable_jobs(monkeypatch, durable_job_sessions):
    manager = jobs.InsightsJobManager(
        session_factory=durable_job_sessions, owner_id="insights-test", poll_seconds=0.01
    )
    monkeypatch.setattr(jobs, "manager", manager)
    yield
    tasks = list(manager._executor.tasks.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------- jobs store
@pytest.mark.asyncio
async def test_job_lifecycle_progress_finish_snapshot():
    async def execute(progress):
        progress(stage="gather", label="Gathering…", detail="2 sources", pct=20)
        return {"id": "r1", "verdict": "notable"}

    job = await jobs.start("t1", execute, pack_name="Pack", scope_label="wl-a")
    await jobs.manager._executor.tasks[job["id"]]
    current = await jobs.get("t1", job["id"])
    assert current is not None
    snap = jobs.snapshot(current)
    assert snap["status"] == "succeeded"
    assert snap["pct"] == 100
    assert snap["run"] == {"id": "r1", "verdict": "notable"}
    # steps include our milestone + the terminal "Digest ready"
    labels = [s["label"] for s in snap["steps"]]
    assert "Gathering…" in labels
    assert "Digest ready" in labels


@pytest.mark.asyncio
async def test_job_fail_records_error():
    async def execute(_progress):
        raise RuntimeError("boom")

    job = await jobs.start("t1", execute)
    await jobs.manager._executor.tasks[job["id"]]
    current = await jobs.get("t1", job["id"])
    assert current is not None
    snap = jobs.snapshot(current)
    assert snap["status"] == "failed"
    assert snap["error"] == "boom"
    assert snap["steps"][-1]["state"] == "error"


@pytest.mark.asyncio
async def test_job_get_is_tenant_scoped():
    async def execute(_progress):
        return {"ok": True}

    job = await jobs.start("tenant-a", execute)
    await jobs.manager._executor.tasks[job["id"]]
    assert await jobs.get("tenant-a", job["id"]) is not None
    assert await jobs.get("tenant-b", job["id"]) is None
    assert await jobs.get("tenant-a", "nope") is None


@pytest.mark.asyncio
async def test_job_pct_is_clamped():
    async def execute(progress):
        progress(stage="x", label="high", pct=250)
        progress(stage="x", label="low", pct=-5)
        return {"ok": True}

    job = await jobs.start("t1", execute)
    await jobs.manager._executor.tasks[job["id"]]
    current = await jobs.get("t1", job["id"])
    assert current is not None
    assert next(step for step in current["steps"] if step["label"] == "high")["pct"] == 100
    assert next(step for step in current["steps"] if step["label"] == "low")["pct"] == 0


# --------------------------------------------------------------------------- runner instrumentation
@pytest.mark.asyncio
async def test_run_pack_emits_progress_milestones(monkeypatch):
    async def _fake_gather(sources, scope, *, tenant_id, lookback_hours, filters, pack_id="", on_source=None):
        bundles = [{"source": "change_explorer", "ok": True, "note": "", "events": [],
                    "flag_codes": set(), "counts": {"total": 3}}]
        if on_source is not None:
            on_source(0, 1, bundles[0])
        return bundles

    async def _fake_reason(*, instructions, bundles, output):
        return {"verdict": "notable", "headline": "Something happened",
                "bullets": ["a"], "table": [], "ai_error": None}

    monkeypatch.setattr(sources_mod, "gather", _fake_gather)
    monkeypatch.setattr(reason_mod, "reason", _fake_reason)
    monkeypatch.setattr(runs_store, "save_run", lambda tenant_id, digest: None)

    events: list[dict] = []
    pack = {"id": "p1", "name": "Pack", "sources": ["change_explorer"]}
    digest = await runner.run_pack(
        pack, {"mode": "workload", "workload_ids": ["w1"]}, tenant_id="t1",
        trigger="manual", notify=False,
        progress=lambda **ev: events.append(ev),
    )

    assert digest["verdict"] == "notable"
    stages = [e["stage"] for e in events]
    # the four-stage loop should surface each milestone in order
    assert stages[0] == "scope"
    assert "gather" in stages
    assert "reason" in stages
    assert "gate" in stages
    assert stages[-1] == "done"
    assert events[-1]["pct"] == 100
    # per-source progress carried a human count detail
    gather_events = [e for e in events if e["stage"] == "gather"]
    assert any("3" in (e.get("detail") or "") or "3" in e["label"] for e in gather_events)


@pytest.mark.asyncio
async def test_run_pack_progress_is_optional(monkeypatch):
    async def _fake_gather(sources, scope, *, tenant_id, lookback_hours, filters, pack_id="", on_source=None):
        return [{"source": "change_explorer", "ok": True, "note": "", "events": [],
                 "flag_codes": set(), "counts": {"total": 0}}]

    async def _fake_reason(*, instructions, bundles, output):
        return {"verdict": "nothing_notable", "headline": "Quiet", "bullets": [], "table": [], "ai_error": None}

    monkeypatch.setattr(sources_mod, "gather", _fake_gather)
    monkeypatch.setattr(reason_mod, "reason", _fake_reason)
    monkeypatch.setattr(runs_store, "save_run", lambda tenant_id, digest: None)

    # No progress callback → must still run cleanly.
    digest = await runner.run_pack(
        {"id": "p1", "name": "Pack", "sources": ["change_explorer"]},
        {"mode": "workload", "workload_ids": ["w1"]}, tenant_id="t1", notify=False,
    )
    assert digest["verdict"] == "nothing_notable"

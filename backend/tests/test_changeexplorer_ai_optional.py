"""Change Explorer — optional AI analysis (run_ai flag + on-demand re-enrichment).

The AI pass is the slowest phase, so it's opt-in. These tests verify a run can complete WITHOUT
AI (aiAnalyzed=False) and that ``ai_enrich_run`` later enriches the persisted run in place,
flipping aiAnalyzed and rebuilding the derived views.
"""
import asyncio

from app.changeexplorer import service


def _fake_run_with_events():
    return {
        "runId": "r1", "tenantId": "t1", "workloadId": "w1", "workloadName": "WL",
        "startTime": "2026-06-25T00:00:00+00:00", "endTime": "2026-06-25T04:00:00+00:00",
        "scopeMode": "workload", "demo": False, "aiAnalyzed": False,
        "scopeInfo": {"production": True}, "notes": [],
        "events": [
            {"changeId": "c1", "resourceName": "vm1", "resourceType": "microsoft.compute/virtualmachines",
             "operation": "Microsoft.Compute/virtualMachines/write", "category": "", "riskScore": 30,
             "riskLabel": "Low", "actor": "u@x.com", "actorType": "User", "actorKind": "User",
             "eventTime": "2026-06-25T01:00:00+00:00", "details": [], "riskFactors": []},
        ],
    }


def test_ai_enrich_run_flips_flag_and_applies(monkeypatch):
    async def _fake_enrich(events):
        yield {"phase": "ai", "message": "x", "done": 1, "total": 1}
        yield {"result": {0: {"category": "Compute", "summary": "AI says hi", "impact": "none",
                              "why": "because", "risk": 50}}}

    monkeypatch.setattr(service.ai_enrich, "enrich_stream", _fake_enrich)

    run = _fake_run_with_events()

    async def _drain():
        final = None
        phases = []
        async for ev in service.ai_enrich_run(run):
            if ev.get("phase") == "done":
                final = ev["run"]
            else:
                phases.append(ev.get("phase"))
        return phases, final

    phases, final = asyncio.run(_drain())
    assert "ai" in phases and "insights" in phases
    assert final["aiAnalyzed"] is True
    ev0 = final["events"][0]
    assert ev0["category"] == "Compute"
    assert ev0["plainEnglishSummary"] == "AI says hi"
    assert ev0["confidence"] == "AI-analyzed"
    # Risk blended UP to at least the AI hint.
    assert ev0["riskScore"] >= 50
    # Derived views rebuilt.
    assert "headline" in final and "actors" in final and "resources" in final
    assert any("AI analyzed" in n for n in final["notes"])


def test_ai_enrich_run_demo_is_noop():
    run = {"runId": "r2", "demo": True, "aiAnalyzed": False, "events": [{"changeId": "c"}]}

    async def _drain():
        final = None
        async for ev in service.ai_enrich_run(run):
            if ev.get("phase") == "done":
                final = ev["run"]
        return final

    final = asyncio.run(_drain())
    assert final["aiAnalyzed"] is True


def test_ai_enrich_run_empty_is_noop():
    run = {"runId": "r3", "demo": False, "aiAnalyzed": False, "events": []}

    async def _drain():
        final = None
        async for ev in service.ai_enrich_run(run):
            if ev.get("phase") == "done":
                final = ev["run"]
        return final

    final = asyncio.run(_drain())
    assert final["aiAnalyzed"] is True

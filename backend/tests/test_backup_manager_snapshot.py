"""The Backup Manager snapshot contract: analysis is explicit, and reads never touch Azure.

The whole point of this layer is that opening the module — or switching tabs, or reloading the
page — must not start an Azure sweep. Only an explicit analysis does. These tests pin that
behavior down, because it is invisible in the UI until it regresses and starts costing
operators minutes and Azure throttling.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.api import backup_manager as api
from app.backup_manager import analysis, demo, snapshot as snapshot_store
from app.core.security import Principal

TENANT = "tenant-snap"
CONNECTION_ID = "conn-snap"
ALL_PERMS = frozenset({"backup_manager.read"})


def _principal() -> Principal:
    return Principal("op@example.test", "op@example.test", TENANT, "operator", ALL_PERMS)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the snapshot store at a temp file so tests never touch the real one."""
    monkeypatch.setattr(snapshot_store, "_PATH", tmp_path / "snapshots.json")
    monkeypatch.setattr(snapshot_store, "_locks", {})
    return tmp_path


# --------------------------------------------------------------------------- store
def test_reading_an_unanalyzed_scope_returns_an_empty_shell_not_a_computation() -> None:
    assert snapshot_store.read_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1") is None
    shell = snapshot_store.empty_snapshot("subscription", "sub-1")
    assert shell["report_exists"] is False
    # Every section is present so the UI renders without null guards.
    for section in ("summary", "inventory", "jobs", "job_analysis", "policies", "posture",
                    "vaults", "gaps", "dr", "cost"):
        assert section in shell


def test_snapshot_round_trips_per_scope() -> None:
    snapshot_store.write_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1", {"counts": {"vaults": 2}})
    snapshot_store.write_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-2", {"counts": {"vaults": 9}})
    assert snapshot_store.read_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1")["counts"]["vaults"] == 2
    assert snapshot_store.read_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-2")["counts"]["vaults"] == 9
    # Another tenant must never see it.
    assert snapshot_store.read_snapshot("other", CONNECTION_ID, "subscription", "sub-1") is None
    # Nor another connection.
    assert snapshot_store.read_snapshot(TENANT, "other-conn", "subscription", "sub-1") is None


def test_a_snapshot_written_by_an_older_shape_is_treated_as_absent(monkeypatch) -> None:
    """A schema bump must degrade to 'analyze again', never to a half-rendered tab."""
    snapshot_store.write_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1", {"counts": {}})
    monkeypatch.setattr(snapshot_store, "SNAPSHOT_SCHEMA_VERSION", 999)
    assert snapshot_store.read_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1") is None


def test_row_lists_are_bounded_so_one_large_estate_cannot_bloat_the_store() -> None:
    snapshot = snapshot_store.empty_snapshot("subscription", "sub-1")
    snapshot["jobs"]["rows"] = [{"id": i} for i in range(snapshot_store.MAX_ROWS["jobs"] + 500)]
    snapshot["inventory"]["rows"] = [{"id": i} for i in range(3)]
    snapshot_store.bound(snapshot)
    assert len(snapshot["jobs"]["rows"]) == snapshot_store.MAX_ROWS["jobs"]
    assert snapshot["jobs"]["truncated"] is True
    # Untruncated sections must not be falsely flagged.
    assert snapshot["inventory"]["truncated"] is False


def test_least_recent_scopes_are_pruned(monkeypatch) -> None:
    monkeypatch.setattr(snapshot_store, "MAX_SCOPES", 3)
    for index in range(5):
        snapshot_store.write_snapshot(
            TENANT, CONNECTION_ID, "subscription", f"sub-{index}",
            {"generated_at": f"2026-01-0{index + 1}T00:00:00Z"},
        )
    surviving = [f"sub-{i}" for i in range(5)
                 if snapshot_store.read_snapshot(TENANT, CONNECTION_ID, "subscription", f"sub-{i}")]
    assert surviving == ["sub-2", "sub-3", "sub-4"]


# --------------------------------------------------------------------------- API reads
@pytest.mark.asyncio
async def test_get_snapshot_never_calls_azure_for_an_unanalyzed_scope(monkeypatch) -> None:
    """The regression this whole feature exists to prevent."""
    monkeypatch.setattr(api, "_connection", lambda *a, **k: {"id": CONNECTION_ID})

    async def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("reading a snapshot must not collect an estate")

    monkeypatch.setattr(api.analysis_ops, "build_snapshot", explode)
    monkeypatch.setattr(api.inventory_ops, "collect_estate", explode)

    class _Db:
        async def execute(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("no ledger query is needed when nothing was analyzed")

    result = await api.get_snapshot(
        connection_id=CONNECTION_ID, workload_id="", subscription_id="sub-1",
        management_group_id="", principal=_principal(), db=_Db(),
    )
    assert result["report_exists"] is False


@pytest.mark.asyncio
async def test_get_snapshot_serves_the_stored_analysis_with_a_live_change_count(monkeypatch) -> None:
    monkeypatch.setattr(api, "_connection", lambda *a, **k: {"id": CONNECTION_ID})
    stored = snapshot_store.empty_snapshot("subscription", "sub-1")
    stored["summary"] = {"actionable_changes": 0, "protection": {"vaults": 4}}
    stored["generated_at"] = "2026-01-01T00:00:00+00:00"
    snapshot_store.write_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1", stored)

    async def fake_actionable(*_a: Any, **_k: Any) -> int:
        return 7

    monkeypatch.setattr(api, "_actionable_changes", fake_actionable)
    result = await api.get_snapshot(
        connection_id=CONNECTION_ID, workload_id="", subscription_id="sub-1",
        management_group_id="", principal=_principal(), db=object(),
    )
    assert result["report_exists"] is True
    assert result["summary"]["protection"]["vaults"] == 4
    # The ledger moves without an analysis, so it is layered on live rather than frozen.
    assert result["summary"]["actionable_changes"] == 7
    assert result["age_seconds"] is not None


@pytest.mark.asyncio
async def test_demo_scope_is_composed_on_read_without_an_analysis() -> None:
    workload = demo.DEMO_WORKLOADS[0] if hasattr(demo, "DEMO_WORKLOADS") else "demo-zava-shoes-website"
    result = await api._demo_snapshot(workload)
    assert result["report_exists"] is True
    assert result["demo"] is True
    assert result["summary"]["protection"]["protected_items"] >= 0
    assert "rows" in result["inventory"]


# --------------------------------------------------------------------------- refresh job
@pytest.mark.asyncio
async def test_refresh_is_idempotent_per_scope(monkeypatch) -> None:
    """A double click, or two operators on one scope, must not launch two Azure sweeps."""
    monkeypatch.setattr(api, "_connection", lambda *a, **k: {"id": CONNECTION_ID})
    monkeypatch.setattr(api, "_refresh_jobs", type(api._refresh_jobs)("test-refresh"))
    started = 0
    release = asyncio.Event()

    async def slow_build(*_a: Any, **_k: Any) -> dict[str, Any]:
        nonlocal started
        started += 1
        await release.wait()
        return snapshot_store.empty_snapshot("subscription", "sub-1")

    monkeypatch.setattr(api.analysis_ops, "build_snapshot", slow_build)

    first = await api.refresh_start(
        connection_id=CONNECTION_ID, workload_id="", subscription_id="sub-1",
        management_group_id="", principal=_principal(),
    )
    second = await api.refresh_start(
        connection_id=CONNECTION_ID, workload_id="", subscription_id="sub-1",
        management_group_id="", principal=_principal(),
    )
    assert first["job"]["id"] == second["job"]["id"]
    assert first["job"]["status"] == "running"
    release.set()
    await asyncio.sleep(0)
    assert started == 1


@pytest.mark.asyncio
async def test_refresh_job_replays_progress_and_hands_back_the_result(monkeypatch) -> None:
    monkeypatch.setattr(api, "_connection", lambda *a, **k: {"id": CONNECTION_ID})
    monkeypatch.setattr(api, "_refresh_jobs", type(api._refresh_jobs)("test-replay"))

    async def build(*_a: Any, progress: Any = None, **_k: Any) -> dict[str, Any]:
        await progress("query", "Received 3 vault row(s).")
        snapshot = snapshot_store.empty_snapshot("subscription", "sub-1")
        snapshot["counts"] = {"vaults": 3, "protected_items": 5, "jobs": 9, "gaps": 1}
        snapshot["summary"] = {"actionable_changes": 0}
        return snapshot

    monkeypatch.setattr(api.analysis_ops, "build_snapshot", build)
    monkeypatch.setattr(api, "_actionable_changes", lambda *a, **k: _zero())

    await api.refresh_start(
        connection_id=CONNECTION_ID, workload_id="", subscription_id="sub-1",
        management_group_id="", principal=_principal(),
    )
    for _ in range(200):
        state = await api.refresh_job(
            connection_id=CONNECTION_ID, workload_id="", subscription_id="sub-1",
            management_group_id="", principal=_principal(),
        )
        if state["job"]["status"] != "running":
            break
        await asyncio.sleep(0.01)

    assert state["job"]["status"] == "done", state["job"].get("error")
    messages = [line["message"] for line in state["progress"]]
    assert any("Received 3 vault row(s)." == m for m in messages)
    assert any(m.startswith("Analysis complete") for m in messages)
    assert state["result"]["counts"]["vaults"] == 3
    # The completed analysis is durable, not just held in the job.
    assert snapshot_store.read_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1") is not None


async def _zero() -> int:
    return 0


@pytest.mark.asyncio
async def test_refresh_refuses_a_scope_that_was_never_selected(monkeypatch) -> None:
    monkeypatch.setattr(api, "_connection", lambda *a, **k: {"id": CONNECTION_ID})
    with pytest.raises(Exception) as excinfo:
        await api.refresh_start(
            connection_id=CONNECTION_ID, workload_id="", subscription_id="",
            management_group_id="", principal=_principal(),
        )
    assert "Select a workload" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_total_collection_failure_keeps_the_previous_analysis(monkeypatch) -> None:
    """An expired token must not overwrite a good analysis with a convincing 'you have no
    backups' one."""
    good = snapshot_store.empty_snapshot("subscription", "sub-1")
    good["counts"] = {"vaults": 5, "protected_items": 12}
    snapshot_store.write_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1", good)

    async def all_sources_fail(*_a: Any, progress: Any = None, **_k: Any) -> dict[str, Any]:
        return {
            "errors": {name: "Pasted token has expired." for name in _source_names()},
            "vaults": [], "instances": [], "jobs": [], "policies": [],
            "scope": {"subscriptions": ["s1"]}, "generated_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(analysis.inventory_ops, "collect_estate", all_sources_fail)
    with pytest.raises(analysis.CollectionFailed):
        await analysis.build_snapshot(
            {"id": CONNECTION_ID}, tenant_id=TENANT, scope_kind="subscription", scope_id="sub-1",
            subscription_id="sub-1",
        )
    kept = snapshot_store.read_snapshot(TENANT, CONNECTION_ID, "subscription", "sub-1")
    assert kept["counts"]["protected_items"] == 12


def _source_names() -> list[str]:
    from app.backup_manager import inventory

    return list(inventory.SOURCE_LABELS)


@pytest.mark.asyncio
async def test_a_partial_failure_still_saves_and_is_flagged(monkeypatch) -> None:
    """One broken table degrades a section; it must not discard the rest of the sweep."""
    async def one_source_fails(*_a: Any, progress: Any = None, **_k: Any) -> dict[str, Any]:
        return {
            "errors": {"replication": "not supported in this cloud"},
            "vaults": [], "instances": [], "jobs": [], "policies": [],
            "scope": {"subscriptions": ["s1"]}, "generated_at": "2026-01-01T00:00:00Z",
            "job_window_days": 7,
        }

    monkeypatch.setattr(analysis.inventory_ops, "collect_estate", one_source_fails)

    async def no_gaps(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"gaps": [], "coverage_pct": 100, "eligible_total": 0, "protected_total": 0}

    monkeypatch.setattr(analysis.gap_ops, "detect", no_gaps)

    async def no_cost(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"monthly_total": 0.0, "currency": "USD", "confidence": "assumed", "waste": {}}

    monkeypatch.setattr(analysis, "build_cost", no_cost)
    snapshot = await analysis.build_snapshot(
        {"id": CONNECTION_ID}, tenant_id=TENANT, scope_kind="subscription", scope_id="sub-1",
        subscription_id="sub-1",
    )
    assert snapshot["partial"] is True
    assert snapshot["errors"]["replication"]


# --------------------------------------------------------------------------- composition
def test_compose_makes_every_tab_agree_on_the_same_rows() -> None:
    """The overview's job count must be the rows the job inbox lists, by construction."""
    estate = demo.build_demo_estate("demo-zava-shoes-website")
    from app.backup_manager import dr as dr_ops
    from app.backup_manager import jobs as job_ops
    from app.backup_manager import policies as policy_ops
    from app.backup_manager import posture as posture_ops

    enriched = job_ops.enrich(estate.get("jobs", []))
    snapshot = analysis.compose(
        estate,
        enriched_jobs=enriched,
        posture=posture_ops.build_posture(estate.get("vaults", [])),
        policies=policy_ops.analyze(estate.get("policies", []), estate.get("instances", [])),
        compliance=policy_ops.compliance(estate.get("instances", []), estate.get("policies", [])),
        gaps={"gaps": [], "coverage_gaps": []},
        readiness=dr_ops.build_readiness(estate),
        rpo=dr_ops.rpo_attainment(estate.get("instances", [])),
        cost={"monthly_total": 1.0, "currency": "USD", "confidence": "assumed", "waste": {}},
    )
    assert snapshot["summary"]["jobs"] == snapshot["jobs"]["summary"]
    assert snapshot["summary"]["protection"]["protected_items"] == len(snapshot["inventory"]["rows"])
    assert snapshot["counts"]["jobs"] == len(snapshot["jobs"]["rows"])
    assert snapshot["summary"]["protection"]["vaults"] == len(snapshot["vaults"]["vaults"])

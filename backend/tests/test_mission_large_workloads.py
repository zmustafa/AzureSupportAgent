"""Synthetic large-workload regressions for Mission Control collection and Radar truthfulness."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from app.architectures import reverse
from app.exec.command_runner import CaptureResult, KqlResult
from app.missions import systems as mission_systems
from app.radar import collector as radar


_SUB = "11111111-2222-3333-4444-555555555555"


def _resource(index: int, *, resource_type: str = "microsoft.compute/virtualmachines") -> dict:
    return {
        "id": f"/subscriptions/{_SUB}/resourceGroups/rg-example/providers/{resource_type}/item-{index}",
        "name": f"item-{index}",
        "type": resource_type,
        "location": "example-region",
        "resourceGroup": "rg-example",
        "subscriptionId": _SUB,
        "tags": {"environment": "synthetic"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_count", [1500, 5000])
async def test_light_inventory_pages_beyond_the_old_capture_and_row_caps(monkeypatch, resource_count):
    rows = [_resource(index) for index in range(resource_count)]
    seen_queries: list[str] = []

    async def fake_open(_connection):
        return "/synthetic/session", None

    async def fake_collect(kql, _connection, **_kwargs):
        seen_queries.append(kql)
        if "resourcecontainers" in kql:
            return KqlResult(ok=True, rows=[], complete=True, pages=1, total=0)
        return KqlResult(ok=True, rows=rows, complete=True, pages=(len(rows) + 999) // 1000, total=len(rows))

    monkeypatch.setattr(reverse, "open_sp_session", fake_open)
    monkeypatch.setattr(reverse, "close_sp_session", lambda _path: None)
    monkeypatch.setattr(reverse, "run_kql_collect", fake_collect)

    result = await reverse.collect_workload_inventory(
        {"nodes": [{"kind": "subscription", "id": _SUB}]}, {"id": "connection-example"}
    )

    assert result["error"] == ""
    assert result["count"] == resource_count
    assert result["known_total"] == resource_count
    assert result["complete"] is True and result["partial"] is False
    assert result["pages"] >= 2
    assert all("limit 1000" not in query.lower() for query in seen_queries)


@pytest.mark.asyncio
async def test_inventory_cap_is_explicit_partial_with_known_total(monkeypatch):
    rows = [_resource(index) for index in range(1000)]

    async def fake_open(_connection):
        return "/synthetic/session", None

    async def fake_collect(kql, _connection, **_kwargs):
        if "resourcecontainers" in kql:
            return KqlResult(ok=True, rows=[], complete=True, pages=1, total=0)
        return KqlResult(ok=True, rows=rows, complete=False, pages=1, total=1500)

    monkeypatch.setenv("MISSION_INVENTORY_MAX_ROWS", "1000")
    monkeypatch.setattr(reverse, "open_sp_session", fake_open)
    monkeypatch.setattr(reverse, "close_sp_session", lambda _path: None)
    monkeypatch.setattr(reverse, "run_kql_collect", fake_collect)

    result = await reverse.collect_workload_inventory(
        {"nodes": [{"kind": "subscription", "id": _SUB}]}, {"id": "connection-example"}
    )

    assert result["count"] == 1000
    assert result["known_total"] == 1500
    assert result["complete"] is False and result["partial"] is True
    assert result["truncated"] is True
    assert "1,000" in result["limit_reason"]


def test_hundreds_of_individual_resource_ids_are_length_bounded():
    resources = {_resource(index)["id"].lower() for index in range(300)}
    scope = {"subs": set(), "rg_pairs": set(), "resource_ids": resources, "resource_rgs": set()}
    predicates = reverse._resource_predicates(scope)

    assert len(predicates) > 1
    assert all(len(f"Resources | where {predicate} | project id, name, type") < 8000 for predicate in predicates)
    covered = {
        token for predicate in predicates for token in re.findall(r"'([^']+)'", predicate)
    }
    assert covered == resources


def test_membership_exclusions_fail_closed_but_an_explicit_child_can_reinclude():
    resource = _resource(1)
    parent = {
        "kind": "subscription", "subscriptions": {_SUB.lower()},
        "excludes": {resource["id"].lower()},
    }
    assert reverse._row_in_membership(resource, {"memberships": [parent]}) is False

    explicit = {"kind": "resource", "resource_id": resource["id"].lower(), "excludes": set()}
    assert reverse._row_in_membership(resource, {"memberships": [parent, explicit]}) is True
    assert radar._resource_in_workload(resource, {"memberships": [parent]}) is False
    assert radar._resource_in_workload(resource, {"memberships": [parent, explicit]}) is True


@pytest.mark.asyncio
async def test_cancelling_inventory_interrupts_the_current_page_and_closes_session(monkeypatch):
    entered = asyncio.Event()
    closed: list[str] = []

    async def fake_open(_connection):
        return "/synthetic/session", None

    async def blocked_collect(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled collection resumed")

    monkeypatch.setattr(reverse, "open_sp_session", fake_open)
    monkeypatch.setattr(reverse, "close_sp_session", closed.append)
    monkeypatch.setattr(reverse, "run_kql_collect", blocked_collect)
    task = asyncio.create_task(reverse.collect_workload_inventory(
        {"nodes": [{"kind": "subscription", "id": _SUB}]}, {"id": "connection-example"}
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == ["/synthetic/session"]


@pytest.mark.asyncio
async def test_architecture_context_is_deterministic_bounded_and_adaptive(monkeypatch):
    resources = [
        _resource(index, resource_type=f"microsoft.example/type{index}")
        for index in range(80)
    ]
    inventory = {
        "resources": resources, "count": len(resources), "known_total": len(resources),
        "complete": True, "partial": False, "warnings": [], "error": "",
    }
    capture_calls: list[int] = []

    async def fake_open(_connection):
        return "/synthetic/session", None

    async def fake_capture(kql, _connection, **_kwargs):
        ids = re.findall(r"'([^']+)'", kql)
        capture_calls.append(len(ids))
        if len(ids) > 2:
            return CaptureResult(ok=False, error="Output truncated at 12000 KB.")
        return CaptureResult(ok=True, stdout=json.dumps([
            {"id": resource_id, "properties": {"serverFarmId": f"/plans/{index}"}}
            for index, resource_id in enumerate(ids)
        ]))

    monkeypatch.setenv("MISSION_ARCHITECTURE_CONTEXT_RESOURCES", "20")
    monkeypatch.setenv("MISSION_ARCHITECTURE_CONTEXT_BYTES", "50000")
    monkeypatch.setattr(reverse, "open_sp_session", fake_open)
    monkeypatch.setattr(reverse, "close_sp_session", lambda _path: None)
    monkeypatch.setattr(reverse, "run_kql_capture", fake_capture)

    first = await reverse.build_architecture_context(inventory, {"id": "connection-example"})
    second = await reverse.build_architecture_context(inventory, {"id": "connection-example"})

    assert first["context"]["mode"] == "summarized"
    assert first["context"]["total_resource_count"] == 80
    assert first["context"]["direct_resource_count"] <= 20
    assert first["context"]["serialized_bytes"] <= 50000
    assert [row["id"] for row in first["resources"]] == [row["id"] for row in second["resources"]]
    assert any(size > 2 for size in capture_calls) and any(size <= 2 for size in capture_calls)


@pytest.mark.asyncio
async def test_mission_inventory_is_collected_once_and_reused(monkeypatch):
    calls = 0
    result = {"resources": [_resource(1)], "count": 1, "complete": True, "partial": False, "error": ""}

    async def fake_collect(_workload, _connection):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(reverse, "collect_workload_inventory", fake_collect)
    context = mission_systems.MissionContext(
        tenant_id="tenant-example", actor="operator-example", workload_id="workload-example",
        workload={"id": "workload-example"}, connection={"id": "connection-example"},
        connection_id="connection-example",
    )

    left, right = await asyncio.gather(
        mission_systems._mission_inventory(context), mission_systems._mission_inventory(context)
    )
    assert calls == 1
    assert left is right is result


@pytest.mark.asyncio
async def test_radar_uses_effective_subscriptions_and_filters_exact_workload(monkeypatch):
    from app.assessments import runner

    selected_id = _resource(1)["id"]
    outside_id = _resource(2)["id"]
    seen_predicates: list[str] = []

    async def fake_scope(_workload, _connection):
        return {
            "predicate": f"id in~ ('{selected_id}')", "subscriptions": [],
            "effective_subscriptions": [_SUB], "sub_predicate": f"subscriptionId =~ '{_SUB}'",
            "rg_pairs": [], "resource_ids": [selected_id], "error": "",
        }

    async def fake_advisor(predicate, _connection):
        seen_predicates.append(predicate)
        return [{
            "source": "advisor", "tracking_id": "recommendation-example", "title": "Upgrade",
            "summary": "Upgrade", "impacted_resources": [
                {"id": selected_id}, {"id": outside_id},
            ],
        }]

    async def fake_health(_subscriptions, _connection):
        return []

    async def fake_aoai(_predicate, _connection):
        return []

    monkeypatch.setattr(runner, "_resolve_scope", fake_scope)
    monkeypatch.setattr(radar, "_query_advisor", fake_advisor)
    monkeypatch.setattr(radar, "_query_service_health", fake_health)
    monkeypatch.setattr(radar, "_query_aoai_deployments", fake_aoai)

    snapshot = await radar.collect_radar(
        {"id": "connection-example"}, scope_kind="workload", scope_id="workload-example",
        workload={"id": "workload-example"}, tenant_id="tenant-example",
    )

    assert seen_predicates == [f"subscriptionId in~ ('{_SUB}')"]
    advisor_events = [event for event in snapshot["events"] if "advisor" in event.get("sources", [])]
    assert len(advisor_events) == 1
    assert advisor_events[0]["impacted_count"] == 1
    assert snapshot["partial"] is False and snapshot["error"] == ""


@pytest.mark.asyncio
async def test_radar_one_source_failure_is_partial_but_all_failures_are_terminal(monkeypatch):
    async def fake_scope(_workload, _connection):
        return {
            "predicate": f"subscriptionId =~ '{_SUB}'", "subscriptions": [_SUB],
            "effective_subscriptions": [_SUB], "rg_pairs": [], "resource_ids": [], "error": "",
        }

    async def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic source failure")

    async def empty(*_args, **_kwargs):
        return []

    from app.assessments import runner
    monkeypatch.setattr(runner, "_resolve_scope", fake_scope)
    monkeypatch.setattr(radar, "_query_advisor", fail)
    monkeypatch.setattr(radar, "_query_service_health", empty)
    monkeypatch.setattr(radar, "_query_aoai_deployments", empty)
    partial = await radar.collect_radar(
        {"id": "connection-example"}, scope_kind="workload", scope_id="workload-example",
        workload={"id": "workload-example"}, tenant_id="tenant-example",
    )
    assert partial["partial"] is True
    assert partial["collection_failed"] is False
    assert partial["error"] == ""
    assert partial["source_status"]["advisor"]["status"] == "failed"

    monkeypatch.setattr(radar, "_query_service_health", fail)
    monkeypatch.setattr(radar, "_query_aoai_deployments", fail)
    failed = await radar.collect_radar(
        {"id": "connection-example"}, scope_kind="workload", scope_id="workload-example",
        workload={"id": "workload-example"}, tenant_id="tenant-example",
    )
    assert failed["collection_failed"] is True
    assert failed["error"]


@pytest.mark.asyncio
async def test_total_radar_failure_preserves_last_good_snapshot(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from app.api import radar as radar_api
    from app.core.security import Principal
    from app.radar import cache

    monkeypatch.setattr(cache, "_PATH", tmp_path / "radar-cache.json")
    monkeypatch.setattr(radar_api, "_settings", lambda: (3600, 30))
    monkeypatch.setattr("app.workloads.registry.get_workload", lambda _workload_id: {"id": "workload-example"})
    monkeypatch.setattr(
        "app.core.azure_connections.connection_for_scope",
        lambda *_args, **_kwargs: {"id": "connection-example"},
    )

    previous = radar.compute_radar([], [])
    previous.update({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope_kind": "workload", "scope_id": "workload-example", "scope_name": "Example",
        "connection_configured": True, "source": "azure_resource_graph", "demo": False,
        "error": "", "marker": "last-good",
    })
    cache.write_snapshot("tenant-example", "workload", "workload-example", previous)

    async def failed_collect(*_args, **_kwargs):
        snapshot = radar.compute_radar([], [])
        snapshot.update({
            "collection_failed": True, "partial": True,
            "error": "Synthetic required sources failed.",
            "source_status": {"advisor": {"status": "failed", "rows": 0}},
        })
        return snapshot

    monkeypatch.setattr(radar, "collect_radar", failed_collect)
    principal = Principal(
        subject="operator-example", email="", tenant_id="tenant-example", role="admin"
    )
    result = await radar_api._get_snapshot(
        principal, "workload", "workload-example", force=True,
        connection_id="connection-example",
    )

    assert result["marker"] == "last-good"
    assert result["last_good_preserved"] is True
    assert "Synthetic" in result["last_refresh_error"]
    persisted = cache.read_snapshot("tenant-example", "workload", "workload-example")
    assert persisted is not None and persisted["marker"] == "last-good"
    assert persisted["last_good_preserved"] is True
    assert "Synthetic" in persisted["last_refresh_error"]

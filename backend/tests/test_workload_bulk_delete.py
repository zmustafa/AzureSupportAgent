"""Bulk workload Trash operations: one-write registry behavior, API bounds, and audit."""
from __future__ import annotations

import asyncio
from typing import cast

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import workloads as api
from app.core.security import Principal
from app.workloads import registry


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_PATH", tmp_path / "workloads.json")
    return tmp_path


def _workload(name: str, *, tenant: str = "azure-tenant", group_id: str = "") -> dict:
    return registry.upsert_workload(
        {
            "name": name,
            "tenant_id": tenant,
            "connection_id": "conn",
            "group_id": group_id,
            "nodes": [
                {
                    "kind": "resource",
                    "id": f"/subscriptions/s/resourceGroups/rg/providers/Test/items/{name}",
                    "name": name,
                }
            ],
        }
    )


def test_bulk_soft_delete_deduplicates_and_writes_once(store, monkeypatch):
    first = _workload("first", group_id="group-a")
    second = _workload("second")
    already = _workload("already")
    assert registry.delete_workload(already["id"])

    mutations = 0
    real_mutate = registry.jsonstore.mutate_json

    def counting_mutate(*args, **kwargs):
        nonlocal mutations
        mutations += 1
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(registry.jsonstore, "mutate_json", counting_mutate)
    result = registry.delete_workloads(
        [first["id"], first["id"], second["id"], already["id"], "missing"]
    )

    assert result == {
        "requested": 4,
        "deleted": 2,
        "already_trashed": 1,
        "not_found": 1,
        "deleted_ids": [first["id"], second["id"]],
    }
    assert mutations == 1
    assert registry.list_workloads() == []
    assert len(registry.list_trashed_workloads()) == 3

    restored = registry.restore_workload(first["id"])
    assert restored is not None
    assert restored["group_id"] == "group-a", "Trash must preserve group metadata for restore"


def test_bulk_soft_delete_with_no_valid_active_ids_does_not_write(store, monkeypatch):
    already = _workload("already")
    registry.delete_workload(already["id"])
    monkeypatch.setattr(
        registry.jsonstore,
        "_atomic_write",
        lambda *_args, **_kwargs: pytest.fail("unexpected write"),
    )
    result = registry.delete_workloads([already["id"], "missing", "missing"])
    assert result["requested"] == 2
    assert result["deleted"] == 0
    assert result["already_trashed"] == 1
    assert result["not_found"] == 1


def test_bulk_request_bounds_and_autopilot_threshold_validation():
    with pytest.raises(ValidationError):
        api.BulkTrashRequest(workload_ids=[])
    with pytest.raises(ValidationError):
        api.BulkTrashRequest(workload_ids=[str(i) for i in range(501)])
    assert len(api.BulkTrashRequest(workload_ids=[str(i) for i in range(500)]).workload_ids) == 500

    assert api.AutopilotRequest(min_candidate_resources=1).min_candidate_resources == 1
    assert api.AutopilotRequest(min_candidate_resources=5_000).min_candidate_resources == 5_000
    with pytest.raises(ValidationError):
        api.AutopilotRequest(min_candidate_resources=0)
    with pytest.raises(ValidationError):
        api.AutopilotRequest(min_candidate_resources=5_001)


class _FakeDb:
    def __init__(self):
        self.rows = []
        self.commits = 0

    def add(self, row):
        self.rows.append(row)

    async def commit(self):
        self.commits += 1


def _principal() -> Principal:
    return Principal(
        subject="operator-id",
        email="operator@example.test",
        tenant_id="app-tenant",
        role="operator",
        permissions=frozenset({"workloads.write"}),
    )


def test_bulk_endpoint_returns_partial_counts_and_records_one_audit(store):
    first = _workload("first")
    second = _workload("second")
    db = _FakeDb()

    result = asyncio.run(
        api.bulk_trash_workloads_endpoint(
            api.BulkTrashRequest(workload_ids=[first["id"], second["id"], "missing"]),
            _principal(),
            cast(AsyncSession, db),
        )
    )

    assert result["deleted"] == 2
    assert result["not_found"] == 1
    assert db.commits == 1
    assert len(db.rows) == 1
    audit = db.rows[0]
    assert audit.action == "workloads.bulk_trash"
    assert audit.tenant_id == "app-tenant"
    assert audit.metadata_json["deleted"] == 2
    assert audit.metadata_json["not_found"] == 1


def test_seed_discovery_ignores_minimum_candidate_resources(monkeypatch):
    captured: dict = {}

    async def fake_seed(_connection, _seed_id, **kwargs):
        captured.update(kwargs)
        yield {"type": "done", "candidates": [], "meta": {"seed": True}}

    monkeypatch.setattr(api, "resolve_connection", lambda _id: {"id": "conn"})
    monkeypatch.setattr(api, "discover_from_seed", fake_seed)

    async def run():
        response = await api.autopilot_discover_endpoint(
            api.AutopilotRequest(
                connection_id="conn",
                scope_kind="resource",
                scope_id="/subscriptions/s/resourceGroups/rg/providers/Test/items/seed",
                min_candidate_resources=500,
            ),
            _principal(),
        )
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(run())
    assert chunks
    assert "min_candidate_resources" not in captured


def test_bulk_route_is_registered_before_dynamic_workload_route():
    routes = [route for route in api.router.routes if isinstance(route, APIRoute)]
    paths = [route.path for route in routes]
    assert paths.index("/workloads/bulk/trash") < paths.index("/workloads/{workload_id}")

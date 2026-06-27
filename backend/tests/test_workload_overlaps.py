"""Tests for workload overlap detection (resources shared across multiple workloads)."""
from __future__ import annotations

import pytest

from app.workloads import registry as reg


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "_PATH", tmp_path / "workloads.json")
    return reg


def _resource(rid: str, name: str = "", rtype: str = "microsoft.compute/virtualmachines", rg: str = "rg1", sub: str = "sub1"):
    return {
        "kind": "resource",
        "id": rid,
        "name": name or rid.split("/")[-1],
        "resource_type": rtype,
        "resource_group": rg,
        "subscription_id": sub,
    }


VM = "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm-shared"
DISK = "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/disks/disk-shared"
UNIQUE_A = "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm-a-only"
UNIQUE_B = "/subscriptions/sub1/resourceGroups/rg2/providers/Microsoft.Storage/storageAccounts/sa-b-only"


def test_no_overlap_when_resources_unique(store):
    store.upsert_workload({"name": "A", "connection_id": "c1", "nodes": [_resource(UNIQUE_A)]})
    store.upsert_workload({"name": "B", "connection_id": "c1", "nodes": [_resource(UNIQUE_B, rg="rg2")]})
    res = store.find_overlaps()
    assert res["overlaps"] == []
    assert res["summary"]["duplicated_resources"] == 0
    assert res["by_pair"] == []


def test_explicit_overlap_across_three_workloads(store):
    store.upsert_workload({"name": "A", "connection_id": "c1", "nodes": [_resource(VM), _resource(DISK, rtype="microsoft.compute/disks"), _resource(UNIQUE_A)]})
    store.upsert_workload({"name": "B", "connection_id": "c1", "nodes": [_resource(VM), _resource(UNIQUE_B, rg="rg2")]})
    store.upsert_workload({"name": "C", "connection_id": "c1", "nodes": [_resource(DISK, rtype="microsoft.compute/disks")]})

    res = store.find_overlaps()
    by_id = {o["id"]: o for o in res["overlaps"]}
    # vm-shared in A+B (count 2); disk-shared in A+C (count 2). Unique ones excluded.
    assert set(by_id) == {VM, DISK}
    assert by_id[VM]["count"] == 2
    assert sorted(w["name"] for w in by_id[VM]["workloads"]) == ["A", "B"]
    assert by_id[DISK]["count"] == 2
    assert by_id[VM]["friendly_type"] == "Virtual Machines"
    assert by_id[DISK]["friendly_type"] == "Managed Disks"

    s = res["summary"]
    assert s["duplicated_resources"] == 2
    assert s["workloads_involved"] == 3          # A, B, C all involved
    assert s["total_extra_memberships"] == 2     # (2-1) + (2-1)


def test_duplicate_node_in_same_workload_counts_once(store):
    # The same resource listed twice in ONE workload is not an overlap by itself.
    store.upsert_workload({"name": "A", "connection_id": "c1", "nodes": [_resource(VM), _resource(VM)]})
    res = store.find_overlaps()
    assert res["overlaps"] == []


def test_pairwise_tally(store):
    store.upsert_workload({"name": "A", "connection_id": "c1", "nodes": [_resource(VM), _resource(DISK, rtype="microsoft.compute/disks")]})
    store.upsert_workload({"name": "B", "connection_id": "c1", "nodes": [_resource(VM), _resource(DISK, rtype="microsoft.compute/disks")]})
    res = store.find_overlaps()
    # A and B share BOTH the vm and the disk → shared_count 2.
    assert len(res["by_pair"]) == 1
    pair = res["by_pair"][0]
    assert pair["shared_count"] == 2
    assert {pair["a"]["name"], pair["b"]["name"]} == {"A", "B"}


def test_connection_filter(store):
    store.upsert_workload({"name": "A", "connection_id": "c1", "nodes": [_resource(VM)]})
    store.upsert_workload({"name": "B", "connection_id": "c2", "nodes": [_resource(VM)]})
    # Filtering to c1 drops B → no longer an overlap (only A claims it on c1).
    res = store.find_overlaps(connection_id="c1")
    assert res["overlaps"] == []
    # No filter → both seen → overlap.
    res_all = store.find_overlaps()
    assert len(res_all["overlaps"]) == 1


def test_scope_implied_overlap(store):
    # A claims the VM explicitly; B includes the whole RG (scope-implied).
    store.upsert_workload({"name": "A", "connection_id": "c1", "nodes": [_resource(VM)]})
    store.upsert_workload({"name": "B", "connection_id": "c1", "nodes": [
        {"kind": "resource_group", "id": "/subscriptions/sub1/resourceGroups/rg1", "name": "rg1", "subscription_id": "sub1"},
    ]})
    scope_members = {
        VM.lower(): [{
            "workload_id": [w for w in store.list_workloads() if w["name"] == "B"][0]["id"],
            "workload_name": "B",
            "via": "resource_group",
            "id": VM,
            "name": "vm-shared",
            "resource_type": "microsoft.compute/virtualmachines",
            "resource_group": "rg1",
            "subscription_id": "sub1",
            "location": "eastus",
        }],
    }
    res = store.find_overlaps_with_memberships(None, scope_members)
    assert len(res["overlaps"]) == 1
    o = res["overlaps"][0]
    assert o["count"] == 2
    assert o["all_explicit"] is False
    vias = {w["name"]: w["via"] for w in o["workloads"]}
    assert vias == {"A": "explicit", "B": "resource_group"}

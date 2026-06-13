"""Tests for inventory cost-optimization analysis."""
from app.inventory.optimization import analyze_resources


def _res(rid, flags, name="r", rtype="microsoft.compute/disks"):
    return {
        "id": rid,
        "name": name,
        "type": rtype,
        "location": "eastus",
        "resource_group": "rg1",
        "subscription_id": "sub1",
        "flags": flags,
        "workloads": [],
    }


def test_empty_inventory():
    out = analyze_resources([], None)
    assert out["total_count"] == 0
    assert out["items"] == []
    assert out["categories"] == []


def test_flags_resources_are_detected_and_categorized():
    resources = [
        _res("/d/disk1", ["unattached_disk"]),
        _res("/n/nic1", ["orphaned_nic"], rtype="microsoft.network/networkinterfaces"),
        _res("/p/ip1", ["idle_public_ip"], rtype="microsoft.network/publicipaddresses"),
        _res("/vm/vm1", []),  # healthy — ignored
        _res("/t/tag1", ["untagged"]),  # governance only — not a cleanup flag
    ]
    out = analyze_resources(resources, None)
    assert out["total_count"] == 3
    cats = {c["flag"] for c in out["categories"]}
    assert cats == {"unattached_disk", "orphaned_nic", "idle_public_ip"}


def test_cost_join_and_totals():
    resources = [
        _res("/d/disk1", ["unattached_disk"]),
        _res("/p/ip1", ["idle_public_ip"], rtype="microsoft.network/publicipaddresses"),
    ]
    cost = {
        "available": True,
        "currency": "USD",
        "period": "Apr 1 – May 1",
        "by_resource": {"/d/disk1": 45.5, "/p/ip1": 3.25},
    }
    out = analyze_resources(resources, cost)
    assert out["cost_available"] is True
    assert out["currency"] == "USD"
    assert out["total_monthly_cost"] == 48.75
    # Items sorted by cost desc — the disk (more expensive) comes first.
    assert out["items"][0]["id"] == "/d/disk1"
    assert out["items"][0]["monthly_cost"] == 45.5


def test_cost_lookup_is_case_insensitive():
    resources = [_res("/D/Disk1", ["unattached_disk"])]
    cost = {"available": True, "by_resource": {"/d/disk1": 10.0}}
    out = analyze_resources(resources, cost)
    assert out["items"][0]["monthly_cost"] == 10.0


def test_no_cost_payload_reports_zero_cost():
    resources = [_res("/d/disk1", ["unattached_disk"])]
    out = analyze_resources(resources, None)
    assert out["cost_available"] is False
    assert out["items"][0]["monthly_cost"] == 0.0
    assert out["total_monthly_cost"] == 0.0


def test_malformed_cost_payload_does_not_crash():
    # Cost data is best-effort upstream: a non-dict by_resource, or non-numeric values,
    # must be tolerated (skip) rather than crash the request path.
    resources = [_res("/d/disk1", ["unattached_disk"])]
    # by_resource is a list, not a dict.
    out = analyze_resources(resources, {"available": True, "by_resource": ["junk"]})
    assert out["cost_available"] is True
    assert out["total_monthly_cost"] == 0.0
    # Non-numeric value for the resource → skipped (0), other valid values still counted.
    resources2 = [
        _res("/d/disk1", ["unattached_disk"]),
        _res("/p/ip1", ["idle_public_ip"], rtype="microsoft.network/publicipaddresses"),
    ]
    out2 = analyze_resources(
        resources2,
        {"available": True, "by_resource": {"/d/disk1": "not-a-number", "/p/ip1": 4.0}},
    )
    assert out2["total_monthly_cost"] == 4.0
    disk = next(i for i in out2["items"] if i["id"] == "/d/disk1")
    assert disk["monthly_cost"] == 0.0


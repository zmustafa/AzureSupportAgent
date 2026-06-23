"""Workloads command center — resource taxonomy, composite health score, and the cache-only
WorkloadProfile aggregator."""
from __future__ import annotations

from app.workloads import health, profile, taxonomy


# ----------------------------------------------------------------- taxonomy
def test_category_for_known_types():
    assert taxonomy.category_for("microsoft.compute/virtualmachines") == "Compute"
    assert taxonomy.category_for("microsoft.network/networksecuritygroups") == "Security"
    assert taxonomy.category_for("microsoft.network/virtualnetworks") == "Networking"
    assert taxonomy.category_for("microsoft.insights/actiongroups") == "Monitoring"
    assert taxonomy.category_for("microsoft.keyvault/vaults") == "Security"
    assert taxonomy.category_for("microsoft.sql/servers") == "Data"
    assert taxonomy.category_for("microsoft.storage/storageaccounts") == "Storage"
    assert taxonomy.category_for("microsoft.web/sites") == "Web"
    assert taxonomy.category_for("microsoft.containerservice/managedclusters") == "Containers"
    assert taxonomy.category_for("microsoft.cognitiveservices/accounts") == "AI / ML"
    assert taxonomy.category_for("microsoft.datafactory/factories") == "Analytics"


def test_category_for_unknown_falls_back_to_other():
    assert taxonomy.category_for("microsoft.weird/thing") == "Other"
    assert taxonomy.category_for("") == "Other"
    assert taxonomy.category_for(None) == "Other"


def test_category_breakdown_counts_and_orders():
    res = [
        {"resource_type": "microsoft.compute/virtualmachines"},
        {"resource_type": "microsoft.compute/virtualmachines"},
        {"resource_type": "microsoft.network/networksecuritygroups"},
        {"resource_type": "microsoft.insights/actiongroups"},
    ]
    out = taxonomy.category_breakdown(res)
    by = {c["category"]: c["count"] for c in out}
    assert by == {"Compute": 2, "Security": 1, "Monitoring": 1}
    # ordering follows CATEGORIES (Compute before Security before Monitoring)
    assert [c["category"] for c in out] == ["Compute", "Security", "Monitoring"]


# ----------------------------------------------------------------- composite score
def test_composite_score_weights_present_signals_only():
    # monitoring=80 (w1.0), backupdr=40 (w1.5); telemetry None excluded.
    out = health.composite_score({"monitoring": 80, "backupdr": 40, "telemetry": None})
    assert out["score"] == 56  # (1.0*80 + 1.5*40) / (1.0+1.5) = 140/2.5
    assert out["contributing"] == ["monitoring", "backupdr"]
    assert "telemetry" in out["missing"]
    assert out["band"] == "warn"


def test_composite_score_none_when_nothing_analyzed():
    out = health.composite_score({})
    assert out["score"] is None
    assert out["band"] == "unknown"
    assert out["contributing"] == []


def test_resolve_weights_merges_overrides_ignores_garbage():
    w = health.resolve_weights({"workload_health_weights": {"backupdr": 3, "bogus": 9, "tags": "x"}})
    assert w["backupdr"] == 3.0          # override applied
    assert "bogus" not in w               # unknown key dropped
    assert w["tags"] == health.DEFAULT_WEIGHTS["tags"]  # non-numeric ignored -> default


def test_band_thresholds():
    assert health.band_for(85) == "good"
    assert health.band_for(60) == "warn"
    assert health.band_for(20) == "poor"
    assert health.band_for(None) == "unknown"


# ----------------------------------------------------------------- profile (cache-only)
def _wl():
    return {
        "id": "w1", "name": "Demo", "connection_id": "c1",
        "workload_type": "web_app", "environment": "production", "criticality": "high",
        "nodes": [
            {"kind": "resource", "resource_type": "microsoft.compute/virtualmachines", "location": "eastus", "subscription_id": "s1"},
            {"kind": "resource", "resource_type": "microsoft.compute/virtualmachines", "location": "eastus", "subscription_id": "s1"},
            {"kind": "resource", "resource_type": "microsoft.network/networksecuritygroups", "location": "westus", "subscription_id": "s1"},
            {"kind": "subscription", "id": "s1"},
        ],
        "summary": {
            "types": [{"label": "Virtual Machines", "count": 2}, {"label": "Network Security Groups", "count": 1}],
            "total_resources": 3, "scope_counts": {"subscription": 1, "resource": 3},
        },
    }


def test_build_profile_composition_and_unanalyzed(monkeypatch):
    # No caches present -> every signal None -> score None, analyzed False.
    p = profile.build_profile(_wl(), "default", None)
    comp = p["composition"]
    assert comp["total"] == 3
    cats = {c["category"]: c["count"] for c in comp["by_category"]}
    assert cats == {"Compute": 2, "Security": 1}
    # by_type carries a representative ARM type for icon rendering
    vm = next(t for t in comp["by_type"] if t["friendly"] == "Virtual Machines")
    assert vm["type"] == "microsoft.compute/virtualmachines" and vm["count"] == 2
    assert comp["by_location"][0]["count"] == 2  # eastus has 2
    assert p["health"]["score"] is None
    assert p["analyzed"] is False
    assert p["classification"]["environment"] == "production"


def test_build_profile_reads_cached_signals(monkeypatch):
    # Stub the amba + backupdr caches to return workload-scoped snapshots.
    import app.amba.cache as amba_cache
    import app.backupdr.cache as bdr_cache
    monkeypatch.setattr(amba_cache, "read_snapshot", lambda t, k, i: {"coverage_pct": 90, "generated_at": ""} if k == "workload" and i == "w1" else None)
    monkeypatch.setattr(bdr_cache, "read_snapshot", lambda t, k, i: {"scorecard": {"pct_protected": 50, "dr_pairs": 2, "dr_pairs_unhealthy": 1}, "generated_at": ""} if k == "workload" and i == "w1" else None)
    p = profile.build_profile(_wl(), "default", None)
    assert p["health"]["monitoring"] == 90
    assert p["health"]["backupdr"] == 50
    # (1.0*90 + 1.5*50) / 2.5 = 165/2.5 = 66
    assert p["health"]["score"] == 66
    assert p["analyzed"] is True
    assert p["health"]["extras"]["backupdr"]["dr_pairs_unhealthy"] == 1


# ----------------------------------------------------------------- P6 trend recording
def test_record_trend_and_embed(monkeypatch, tmp_path):
    from app.core import coverage_trends
    monkeypatch.setattr(coverage_trends, "_PATH", tmp_path / "trends.json")
    import app.amba.cache as amba_cache
    monkeypatch.setattr(amba_cache, "read_snapshot", lambda t, k, i: {"coverage_pct": 70, "generated_at": ""} if i == "w1" else None)
    # First record stores 70.
    assert profile.record_trend(_wl(), "default", None) == 70
    p = profile.build_profile(_wl(), "default", None)
    assert p["score_trend"]["current"] == 70 and p["score_trend"]["count"] == 1


def test_record_trend_noop_when_unanalyzed(monkeypatch, tmp_path):
    from app.core import coverage_trends
    monkeypatch.setattr(coverage_trends, "_PATH", tmp_path / "trends.json")
    # No caches → score None → nothing recorded.
    assert profile.record_trend(_wl(), "default", None) is None
    p = profile.build_profile(_wl(), "default", None)
    assert p["score_trend"]["count"] == 0


# ----------------------------------------------------------------- P5 grouping templates
def _r(rid, rtype="microsoft.compute/virtualmachines", sub="s1", rg="rg1", tags=None):
    return {"id": rid, "name": rid.rsplit("/", 1)[-1], "resource_type": rtype,
            "subscription_id": sub, "resource_group": rg, "tags": tags or {}}


def test_rg_grouping_template():
    from app.workloads import autopilot
    res = [_r("/r/1", rg="rg-a"), _r("/r/2", rg="rg-a"), _r("/r/3", rg="rg-b")]
    groups = autopilot._rg_grouping(res)
    sizes = {g["name"]: len(g["members"]) for g in groups}
    assert sizes == {"rg-a": 2, "rg-b": 1}


def test_subscription_grouping_template():
    from app.workloads import autopilot
    res = [_r("/r/1", sub="sub-a"), _r("/r/2", sub="sub-b"), _r("/r/3", sub="sub-a")]
    groups = autopilot._sub_grouping(res)
    assert sum(len(g["members"]) for g in groups) == 3
    assert len(groups) == 2


def test_tag_grouping_explicit_key():
    from app.workloads import autopilot
    res = [_r("/r/1", tags={"app": "pay"}), _r("/r/2", tags={"app": "pay"}), _r("/r/3", tags={"app": "web"})]
    groups = autopilot._tag_grouping(res, tag_key="app")
    sizes = {g["name"]: len(g["members"]) for g in groups}
    assert sizes.get("pay") == 2 and sizes.get("web") == 1


def test_tag_grouping_untagged_bucket():
    from app.workloads import autopilot
    res = [_r("/r/1", tags={"app": "pay"}), _r("/r/2", tags={})]
    groups = autopilot._tag_grouping(res, tag_key="app")
    names = {g["name"] for g in groups}
    assert "pay" in names and "Untagged" in names

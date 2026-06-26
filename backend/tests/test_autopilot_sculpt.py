"""Tests for the Scope Sculptor pre-flight layer — filters, tag-seeding, facets, naming
detection, cost estimation, priority ordering, orphan re-attachment, saved profiles, and the
survey/estimate cache. All offline (no Azure / no LLM)."""
from __future__ import annotations

import asyncio

from app.workloads import autopilot as ap
from app.workloads import discovery_profiles as dp
from app.workloads import sculpt


def _r(rid, name, rtype, rg="rg1", sub="s1", loc="eastus", tags=None):
    return {
        "id": rid, "name": name, "resource_type": rtype, "resource_group": rg,
        "subscription_id": sub, "location": loc, "tags": tags or {},
    }


# --------------------------------------------------------------------- noise / system RGs
def test_is_noise_matches_children():
    assert sculpt.is_noise(_r("/a", "disk", "microsoft.compute/disks"))
    assert sculpt.is_noise(_r("/a", "nic", "microsoft.network/networkinterfaces"))
    assert sculpt.is_noise(_r("/a", "alert", "microsoft.insights/metricalerts"))
    assert not sculpt.is_noise(_r("/a", "web", "microsoft.web/sites"))
    assert not sculpt.is_noise(_r("/a", "sql", "microsoft.sql/servers"))


def test_rg_glob_match():
    assert sculpt.rg_matches_globs("MC_myaks_rg_eastus", sculpt.DEFAULT_SYSTEM_RG_GLOBS)
    assert sculpt.rg_matches_globs("NetworkWatcherRG", sculpt.DEFAULT_SYSTEM_RG_GLOBS)
    assert sculpt.rg_matches_globs("databricks-rg-foo-abc", sculpt.DEFAULT_SYSTEM_RG_GLOBS)
    assert not sculpt.rg_matches_globs("rg-billing-prod", sculpt.DEFAULT_SYSTEM_RG_GLOBS)


# --------------------------------------------------------------------- filtering
def test_apply_filters_separates_noise_and_system_rgs():
    res = [
        _r("/1", "web-prod", "microsoft.web/sites", rg="rg-app"),
        _r("/2", "disk1", "microsoft.compute/disks", rg="rg-app"),         # noise -> removed
        _r("/3", "node", "microsoft.compute/virtualmachines", rg="MC_aks"),  # system rg -> removed
    ]
    kept, removed, reasons = sculpt.apply_filters(res, sculpt.FilterConfig())
    assert [k["id"] for k in kept] == ["/1"]
    assert reasons["noise"] == 1
    assert reasons["system_rg"] == 1
    assert len(removed) == 2


def test_apply_filters_can_disable_noise_filter():
    res = [_r("/1", "web", "microsoft.web/sites"), _r("/2", "disk", "microsoft.compute/disks")]
    kept, removed, _ = sculpt.apply_filters(res, sculpt.FilterConfig(exclude_noise=False, exclude_system_rgs=False))
    assert len(kept) == 2 and not removed


def test_apply_filters_scoping_drops_by_region_and_env_and_type():
    res = [
        _r("/1", "web-prod", "microsoft.web/sites", loc="eastus", tags={"env": "prod"}),
        _r("/2", "web-dev", "microsoft.web/sites", loc="westus", tags={"env": "dev"}),
        _r("/3", "sql", "microsoft.sql/servers", loc="eastus", tags={"env": "prod"}),
    ]
    cfg = sculpt.FilterConfig(regions=["eastus"], environments=["production"], exclude_types=["microsoft.sql/servers"])
    kept, _removed, reasons = sculpt.apply_filters(res, cfg)
    assert [k["id"] for k in kept] == ["/1"]
    assert reasons["region"] == 1  # /2
    assert reasons["type"] == 1    # /3


def test_apply_filters_include_types_allowlist():
    res = [_r("/1", "web", "microsoft.web/sites"), _r("/2", "sql", "microsoft.sql/servers")]
    kept, _r2, _reasons = sculpt.apply_filters(res, sculpt.FilterConfig(include_types=["microsoft.web/sites"]))
    assert [k["id"] for k in kept] == ["/1"]


# --------------------------------------------------------------------- tag seeding
def test_tag_seed_partition_buckets_by_priority_key():
    res = [
        _r("/1", "a", "microsoft.web/sites", tags={"app": "billing"}),
        _r("/2", "b", "microsoft.sql/servers", tags={"app": "billing"}),
        _r("/3", "c", "microsoft.web/sites", tags={"app": "portal"}),
        _r("/4", "d", "microsoft.storage/storageaccounts", tags={}),  # untagged -> remainder
    ]
    groups, remainder = sculpt.tag_seed_partition(res, ["app"])
    names = sorted(g["name"] for g in groups)
    assert names == ["billing", "portal"]
    billing = next(g for g in groups if g["name"] == "billing")
    assert len(billing["members"]) == 2
    assert billing["seeded_by_tag"] == "app"
    assert [r["id"] for r in remainder] == ["/4"]


def test_tag_seed_partition_no_keys_returns_all_remainder():
    res = [_r("/1", "a", "microsoft.web/sites", tags={"app": "x"})]
    groups, remainder = sculpt.tag_seed_partition(res, [])
    assert not groups and len(remainder) == 1


# --------------------------------------------------------------------- naming detection
def test_detect_naming_convention_finds_pattern():
    res = [
        _r("/1", "billing-prod-eastus-01", "microsoft.web/sites"),
        _r("/2", "billing-prod-eastus-02", "microsoft.sql/servers"),
        _r("/3", "portal-dev-westus-01", "microsoft.web/sites"),
        _r("/4", "portal-dev-westus-02", "microsoft.storage/storageaccounts"),
    ]
    nm = sculpt.detect_naming_convention(res)
    assert nm["delimiter"] == "-"
    assert nm["segments"] == 4
    assert "{env}" in nm["pattern"]
    assert "{region}" in nm["pattern"]


def test_detect_naming_convention_handles_no_convention():
    res = [_r("/1", "x", "microsoft.web/sites"), _r("/2", "y", "microsoft.sql/servers")]
    nm = sculpt.detect_naming_convention(res)
    assert nm["confidence"] == 0.0


# --------------------------------------------------------------------- facets
def test_compute_facets_tallies_everything():
    res = [
        _r("/1", "web-prod", "microsoft.web/sites", rg="rg-a", loc="eastus", tags={"app": "x"}),
        _r("/2", "disk", "microsoft.compute/disks", rg="MC_aks", loc="eastus"),
        _r("/3", "sql-dev", "microsoft.sql/servers", rg="rg-b", loc="westus", tags={"env": "dev"}),
    ]
    f = sculpt.compute_facets(res)
    assert f["total"] == 3
    assert f["noise_count"] == 1          # the disk
    assert f["system_rg_count"] == 1      # MC_aks
    assert f["distinct_regions"] == 2
    assert any(t["label"] == "app" for t in f["tag_keys"])


# --------------------------------------------------------------------- cost estimate
def test_estimate_cost_resource_granularity():
    est = sculpt.estimate_cost(1200, granularity="resource")
    assert est["ai_calls"] == 3  # ceil(1200/500)
    assert est["est_seconds"] > 0


def test_estimate_cost_resource_group_is_cheaper():
    est = sculpt.estimate_cost(4500, granularity="resource_group", n_resource_groups=150)
    assert est["ai_calls"] == 1  # 150 RGs <= RG_BATCH(300)
    assert est["unit"] == "resource groups"


def test_estimate_cost_subscription_is_free():
    est = sculpt.estimate_cost(4500, granularity="subscription")
    assert est["ai_calls"] == 0


def test_estimate_cost_budget_cap():
    est = sculpt.estimate_cost(5000, granularity="resource", max_ai_calls=3)
    assert est["ai_calls"] == 3 and est["capped"] is True


def test_estimate_cost_tag_seeded_reduces_effective():
    est = sculpt.estimate_cost(1000, granularity="resource", tag_seeded=600)
    assert est["effective_resources"] == 400
    assert est["ai_calls"] == 1  # ceil(400/500)


# --------------------------------------------------------------------- priority ordering
def test_priority_sort_prod_first_then_larger_rg():
    res = [
        _r("/d1", "dev-a", "microsoft.web/sites", rg="rg-dev", tags={"env": "dev"}),
        _r("/p1", "prod-a", "microsoft.web/sites", rg="rg-prod", tags={"env": "prod"}),
        _r("/p2", "prod-b", "microsoft.sql/servers", rg="rg-prod", tags={"env": "prod"}),
    ]
    ordered = sculpt.priority_sort(res)
    # Prod RG (2 members) comes before the dev one.
    assert ordered[0]["id"] in ("/p1", "/p2")
    assert ordered[-1]["id"] == "/d1"


# --------------------------------------------------------------------- orphan attach
def test_reattach_orphans_joins_parent_rg():
    groups = [{"name": "Billing", "members": [_r("/1", "web", "microsoft.web/sites", rg="rg-bill")]}]
    orphans = [
        _r("/2", "disk", "microsoft.compute/disks", rg="rg-bill"),   # joins Billing
        _r("/3", "loner", "microsoft.compute/disks", rg="rg-other"),  # no match -> stays out
    ]
    attached = sculpt.reattach_orphans(groups, orphans)
    assert attached == 1
    assert len(groups[0]["members"]) == 2


# --------------------------------------------------------------------- aggregated grouping
def test_build_units_resource_group():
    res = [
        _r("/1", "a", "microsoft.web/sites", rg="rg-a"),
        _r("/2", "b", "microsoft.sql/servers", rg="rg-a"),
        _r("/3", "c", "microsoft.web/sites", rg="rg-b"),
    ]
    units = ap._build_units(res, "resource_group")
    assert len(units) == 2
    assert sum(len(u["members"]) for u in units) == 3
    assert units[0]["members"]  # largest first


def test_build_units_sample_by_name_prefix():
    res = [
        _r("/1", "billing-web", "microsoft.web/sites", rg="rg-a"),
        _r("/2", "billing-sql", "microsoft.sql/servers", rg="rg-b"),
        _r("/3", "portal-web", "microsoft.web/sites", rg="rg-c"),
    ]
    units = ap._build_units(res, "sample")
    # billing-* collapse to one cluster (spanning rg-a + rg-b), portal-* another.
    assert len(units) == 2
    billing = next(u for u in units if "billing" in u["label"])
    assert len(billing["members"]) == 2


def test_ai_group_aggregated_falls_back_when_ai_unavailable(monkeypatch):
    async def _none(*a, **k):
        return None

    monkeypatch.setattr(ap, "_ai_group_units_batch", _none)
    res = [_r("/1", "a", "microsoft.web/sites", rg="rg-a"), _r("/2", "b", "microsoft.sql/servers", rg="rg-b")]
    groups, used_ai = asyncio.run(
        ap._ai_group_aggregated(res, {"provenance": {}, "private_endpoints": [], "network": {}}, "", granularity="resource_group")
    )
    assert used_ai is False
    assert sum(len(g["members"]) for g in groups) == 2


def test_ai_group_budget_cap_limits_calls(monkeypatch):
    calls = {"n": 0}

    async def _count(batch, *a, **k):
        calls["n"] += 1
        return [{
            "name": f"G{calls['n']}", "description": "", "reasoning": "", "confidence": 0.7,
            "members": list(batch), "workload_type": "other", "environment": "unknown",
            "criticality": "", "data_classification": "",
        }]

    monkeypatch.setattr(ap, "_ai_group_batch", _count)
    res = [_r(f"/{i}", f"r{i}", "microsoft.web/sites", rg=f"rg{i}") for i in range(1500)]  # 3 batches
    groups, used_ai = asyncio.run(
        ap._ai_group(res, {"provenance": {}, "private_endpoints": [], "network": {}}, max_calls=1)
    )
    assert used_ai is True
    assert calls["n"] == 1  # budget capped to one AI call; remainder deterministic
    assert sum(len(g["members"]) for g in groups) == 1500  # nothing lost


# --------------------------------------------------------------------- survey / estimate cache
def test_survey_cache_roundtrip_and_estimate():
    key = ap._survey_key("t1", "c1", "subscription", "sub-1")
    res = [
        _r("/1", "billing-web", "microsoft.web/sites", rg="rg-a", tags={"app": "billing"}),
        _r("/2", "disk", "microsoft.compute/disks", rg="rg-a"),
        _r("/3", "portal-web", "microsoft.web/sites", rg="rg-b", tags={"app": "portal"}),
    ]
    ap._survey_cache_put(key, res, False)
    got = ap._survey_cache_get(key)
    assert got is not None and len(got[0]) == 3

    out = ap.compute_estimate("t1", "c1", "subscription", "sub-1", {
        "granularity": "resource", "exclude_noise": True, "exclude_system_rgs": True,
        "tag_seed_keys": ["app"],
    })
    assert out is not None
    # noise disk removed (kept=2), both remaining are tag-seeded -> 0 effective for AI.
    assert out["filter_preview"]["kept"] == 2
    assert out["filter_preview"]["tag_seeded"] == 2
    assert out["estimate"]["ai_calls"] == 0
    ap._survey_cache.pop(key, None)


def test_compute_estimate_returns_none_on_cache_miss():
    assert ap.compute_estimate("nope", "nope", "subscription", "missing", {}) is None


# --------------------------------------------------------------------- profiles
def test_profiles_save_list_update_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "_PATH", tmp_path / "profiles.json")
    p = dp.save_profile("t1", "c1", name="Prod fast", config={"preset": "fast", "granularity": "resource_group", "bogus": 1}, scope_kind="subscription", scope_id="s1")
    assert p["name"] == "Prod fast"
    assert "bogus" not in p["config"]          # sanitized away
    assert p["config"]["preset"] == "fast"

    listed = dp.list_profiles("t1", "c1")
    assert len(listed) == 1 and listed[0]["id"] == p["id"]

    # Update in place keeps the id.
    p2 = dp.save_profile("t1", "c1", name="Prod faster", config={"preset": "balanced"}, profile_id=p["id"])
    assert p2["id"] == p["id"] and p2["name"] == "Prod faster"
    assert len(dp.list_profiles("t1", "c1")) == 1

    # Isolation across tenants.
    assert dp.list_profiles("t2", "c1") == []

    assert dp.delete_profile("t1", "c1", p["id"]) is True
    assert dp.list_profiles("t1", "c1") == []
    assert dp.delete_profile("t1", "c1", "missing") is False

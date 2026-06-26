"""Tests for per-scope inventory: cache-key isolation, scope subscription resolution, and
scoped collection. Covers the back-compat guarantee that an empty scope reuses the legacy
``tenant|connection`` cache key so pre-scope entries stay valid.
"""
import re

import pytest

from app.inventory import cache, cost, service


# --------------------------------------------------------------------------- cache keys
def test_cache_key_empty_scope_is_legacy():
    # Empty scope must equal the pre-scope key so old cached payloads remain addressable.
    assert cache._key("t1", "c1", "") == "t1|c1"
    assert cache._key("t1", "c1") == "t1|c1"


def test_cache_key_scopes_are_distinct():
    tenant = cache._key("t1", "c1", "")
    sub = cache._key("t1", "c1", "sub:abc")
    mg = cache._key("t1", "c1", "mg:root")
    assert tenant == "t1|c1"
    assert sub == "t1|c1|sub:abc"
    assert mg == "t1|c1|mg:root"
    assert len({tenant, sub, mg}) == 3


def test_cost_cache_key_empty_scope_is_legacy():
    assert cost._key("t1", "c1", "") == "t1|c1"
    assert cost._key("t1", "c1", "sub:abc") == "t1|c1|sub:abc"


# --------------------------------------------------------------------------- cache get/set isolation
@pytest.fixture()
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_PATH", tmp_path / "inventory_cache.json")
    monkeypatch.setattr(cache, "_mem_cache", None)


def test_cache_isolates_payloads_per_scope(_isolated_cache):
    cache.set_("t1", "c1", {"resources": ["tenant"]}, scope="")
    cache.set_("t1", "c1", {"resources": ["sub"]}, scope="sub:s1")
    cache.set_("t1", "c1", {"resources": ["mg"]}, scope="mg:root")

    assert cache.get("t1", "c1", scope="")["payload"]["resources"] == ["tenant"]
    assert cache.get("t1", "c1", scope="sub:s1")["payload"]["resources"] == ["sub"]
    assert cache.get("t1", "c1", scope="mg:root")["payload"]["resources"] == ["mg"]
    # A scope that was never written is a miss even though the tenant scope is populated.
    assert cache.get("t1", "c1", scope="sub:other") is None


def test_cache_scope_does_not_leak_into_tenant(_isolated_cache):
    cache.set_("t1", "c1", {"resources": ["only-sub"]}, scope="sub:s1")
    # The whole-tenant scope must remain a miss — a scoped collect never satisfies it.
    assert cache.get("t1", "c1", scope="") is None


# --------------------------------------------------------------------------- scope resolution
async def test_resolve_scope_empty_returns_all():
    ids, err = await service.resolve_scope_sub_ids(None, "", ["s1", "s2", "s3"])
    assert ids == ["s1", "s2", "s3"]
    assert err == ""


async def test_resolve_scope_single_subscription_visible():
    ids, err = await service.resolve_scope_sub_ids(None, "sub:S2", ["s1", "s2", "s3"])
    # Case-insensitive match against the visible set; returns the requested id.
    assert ids == ["S2"]
    assert err == ""


async def test_resolve_scope_single_subscription_not_visible():
    ids, err = await service.resolve_scope_sub_ids(None, "sub:nope", ["s1", "s2"])
    assert ids == []
    assert "isn't visible" in err


async def test_resolve_scope_management_group(monkeypatch):
    async def fake_under(_conn, mg_id):
        assert mg_id == "root"
        return ["s2", "s3", "s-hidden"]

    monkeypatch.setattr("app.workloads.discovery.subscriptions_under_mg", fake_under)
    ids, err = await service.resolve_scope_sub_ids(None, "mg:root", ["s1", "s2", "s3"])
    # Only subscriptions BOTH under the MG and visible to the connection are kept.
    assert ids == ["s2", "s3"]
    assert err == ""


async def test_resolve_scope_management_group_none_visible(monkeypatch):
    async def fake_under(_conn, _mg):
        return ["x1", "x2"]

    monkeypatch.setattr("app.workloads.discovery.subscriptions_under_mg", fake_under)
    ids, err = await service.resolve_scope_sub_ids(None, "mg:root", ["s1", "s2"])
    assert ids == []
    assert "management group" in err


async def test_resolve_scope_unknown_kind_falls_back_to_tenant():
    ids, err = await service.resolve_scope_sub_ids(None, "bogus:x", ["s1", "s2"])
    assert ids == ["s1", "s2"]
    assert err == ""


# --------------------------------------------------------------------------- multi-select scope
def test_normalize_scope_sorts_and_dedupes():
    assert service.normalize_scope("sub:b,sub:a") == "sub:a,sub:b"
    assert service.normalize_scope("sub:a , sub:a , mg:root") == "mg:root,sub:a"
    assert service.normalize_scope("") == ""
    assert service.normalize_scope("  ") == ""


def test_cache_key_normalizes_multi_scope_order():
    # Order/dupes/whitespace in a multi-token scope must collapse to one cache key.
    assert cache._key("t", "c", "sub:b,sub:a") == cache._key("t", "c", "sub:a,sub:b")
    assert cache._key("t", "c", "sub:a, sub:b") == cache._key("t", "c", "sub:b,sub:a")
    assert cost._key("t", "c", "mg:x,sub:a") == cost._key("t", "c", "sub:a,mg:x")


async def test_resolve_scope_multi_union_of_subscriptions():
    ids, err = await service.resolve_scope_sub_ids(None, "sub:s1,sub:s3", ["s1", "s2", "s3"])
    assert sorted(ids) == ["s1", "s3"]
    assert err == ""


async def test_resolve_scope_multi_dedupes_overlap(monkeypatch):
    async def fake_under(_conn, _mg):
        return ["s1", "s2"]

    monkeypatch.setattr("app.workloads.discovery.subscriptions_under_mg", fake_under)
    # mg:root → {s1,s2}; plus sub:s2 (overlap) → union is still {s1,s2}, no dupes.
    ids, err = await service.resolve_scope_sub_ids(None, "mg:root,sub:s2", ["s1", "s2", "s3"])
    assert sorted(ids) == ["s1", "s2"]
    assert err == ""


# --------------------------------------------------------------------------- IP7: parallel cost
async def test_get_cost_aggregates_across_subscriptions_concurrently(tmp_path, monkeypatch):
    """IP7 — get_cost fans the per-subscription queries out concurrently (bounded by a
    semaphore) and aggregates every subscription's result without dropping any."""
    monkeypatch.setattr(cost, "_CACHE_PATH", tmp_path / "cost.json")
    monkeypatch.setattr(cost, "_mem", None)

    seen: list[str] = []

    async def fake_sub_cost(_conn, sub_id, _body):
        seen.append(sub_id)
        # Each subscription contributes one resource with a deterministic cost.
        return ({f"/subscriptions/{sub_id}/r": float(int(sub_id[1:]))}, "USD", "")

    monkeypatch.setattr(cost, "_subscription_cost", fake_sub_cost)
    subs = [f"s{i}" for i in range(1, 11)]
    payload = await cost.get_cost(None, subs, "t1", "c1", force=True)

    assert payload["available"] is True
    assert len(seen) == 10  # every subscription was queried
    assert len(payload["by_resource"]) == 10  # none dropped
    assert payload["total"] == round(sum(range(1, 11)), 2)
    assert payload["errors"] == []


async def test_get_cost_records_per_subscription_errors(tmp_path, monkeypatch):
    """A subscription that errors is reported but doesn't abort the others (IP7 parallel)."""
    monkeypatch.setattr(cost, "_CACHE_PATH", tmp_path / "cost.json")
    monkeypatch.setattr(cost, "_mem", None)

    async def fake_sub_cost(_conn, sub_id, _body):
        if sub_id == "s2":
            return ({}, "", "throttled")
        return ({f"/subscriptions/{sub_id}/r": 5.0}, "USD", "")

    monkeypatch.setattr(cost, "_subscription_cost", fake_sub_cost)
    payload = await cost.get_cost(None, ["s1", "s2", "s3"], "t1", "c1", force=True)
    assert payload["total"] == 10.0  # s1 + s3
    assert any("s2" in e for e in payload["errors"])  # the failure is surfaced


# --------------------------------------------------------------------------- IP6: response cap
def test_cap_resources_passthrough_under_cap():
    from app.api import inventory as inv_api

    payload = {"resources": [{"id": i} for i in range(5)], "summary": {"total_resources": 5}}
    out = inv_api._cap_resources(payload, top=0)
    assert out["truncated_total"] is False
    assert out["returned"] == 5
    assert len(out["resources"]) == 5


def test_cap_resources_honours_explicit_top():
    from app.api import inventory as inv_api

    payload = {"resources": [{"id": i} for i in range(50)]}
    out = inv_api._cap_resources(payload, top=10)
    assert out["truncated_total"] is True
    assert out["returned"] == 10
    assert out["total_resources_full"] == 50
    assert len(out["resources"]) == 10


async def test_resolve_scope_multi_partial_visibility_keeps_visible_only():
    # One valid sub + one not-visible sub → keep the valid one, no error (something resolved).
    ids, err = await service.resolve_scope_sub_ids(None, "sub:s1,sub:nope", ["s1", "s2"])
    assert ids == ["s1"]
    assert err == ""


async def test_collect_multi_scope_queries_union(_stub_collect):
    out = await service.collect(None, scope="sub:s1,sub:s2")
    assert sorted(_stub_collect) == ["s1", "s2"]
    assert out["summary"]["total_resources"] == 2


# --------------------------------------------------------------------------- partial workloads
def test_workload_span_subs_collects_all_referenced_subs():
    w = {
        "subs": {"a"},
        "rg_pairs": {("b", "rg1")},
        "resource_ids": {"/subscriptions/c/resourcegroups/rg/providers/x/r"},
    }
    assert service._workload_span_subs(w) == {"a", "b", "c"}


def _wl_scope(wid, subs):
    return {"id": wid, "name": wid, "subs": set(subs), "rg_pairs": set(), "resource_ids": set()}


@pytest.fixture()
def _stub_collect_partial(monkeypatch):
    """Like _stub_collect but with two workloads: ``span2`` spans subs s1+s2, ``only1`` spans
    only s1. Lets us assert the (Partial) flag when a scope excludes part of a workload."""
    async def fake_open(_conn):
        return ("session-dir", None)

    async def fake_subs(_conn, _dir):
        return [{"id": "s1", "name": "Sub One"}, {"id": "s2", "name": "Sub Two"}]

    async def fake_wl_scopes(_conn):
        return [_wl_scope("span2", ["s1", "s2"]), _wl_scope("only1", ["s1"])]

    async def fake_arg(kql, _conn, _dir):
        m = re.search(r"subscriptionId =~ '([^']+)'", kql)
        sub = m.group(1) if m else ""
        # Resource-GROUP container query returns no extra rows here (the resource query carries
        # the workload-attribution assertions).
        if "resourcecontainers" in kql:
            return [], ""
        # Each sub returns one resource attributed to BOTH workloads that include it.
        wls = [{"id": "span2", "name": "span2"}]
        if sub == "s1":
            wls.append({"id": "only1", "name": "only1"})
        return [{
            "id": f"/subscriptions/{sub}/providers/x/r1",
            "name": "r1",
            "type": "Microsoft.Compute/virtualMachines",
            "subscriptionId": sub,
            "resourceGroup": "rg1",
        }], ""

    # Resource→workload attribution: map by the sub embedded in the id.
    def fake_resource_workloads(rid, sub_id, rg, wl_scopes):
        hits = [{"id": "span2", "name": "span2"}]
        if (sub_id or "").lower() == "s1":
            hits.append({"id": "only1", "name": "only1"})
        return hits

    monkeypatch.setattr(service, "open_sp_session", fake_open)
    monkeypatch.setattr(service, "close_sp_session", lambda _d: None)
    monkeypatch.setattr(service, "_subscriptions", fake_subs)
    monkeypatch.setattr(service, "_workload_scopes", fake_wl_scopes)
    monkeypatch.setattr(service, "_resource_workloads", fake_resource_workloads)
    monkeypatch.setattr(service, "_arg", fake_arg)


def _wl_facet(out, wid):
    return next(w for w in out["facets"]["workloads"] if w["id"] == wid)


async def test_partial_flag_false_for_whole_tenant(_stub_collect_partial):
    out = await service.collect(None, scope="")
    # Both subs in scope → neither workload is partial.
    assert _wl_facet(out, "span2")["partial"] is False
    assert _wl_facet(out, "only1")["partial"] is False


async def test_partial_flag_true_when_scope_excludes_a_workload_sub(_stub_collect_partial):
    out = await service.collect(None, scope="sub:s1")
    # span2 also lives in s2 (out of scope) → Partial. only1 is fully within s1 → not partial.
    assert _wl_facet(out, "span2")["partial"] is True
    assert _wl_facet(out, "only1")["partial"] is False



# --------------------------------------------------------------------------- scoped collect
@pytest.fixture()
def _stub_collect(monkeypatch):
    """Stub the Azure-touching parts of ``collect`` so we can assert which subscriptions get
    queried for a given scope, with no live calls."""
    async def fake_open(_conn):
        return ("session-dir", None)

    def fake_close(_dir):
        return None

    async def fake_subs(_conn, _dir):
        return [{"id": "s1", "name": "Sub One"}, {"id": "s2", "name": "Sub Two"}]

    async def fake_wl_scopes(_conn):
        return []

    queried: list[str] = []

    async def fake_arg(kql, _conn, _dir):
        m = re.search(r"subscriptionId =~ '([^']+)'", kql)
        sub = m.group(1) if m else ""
        # The resource-GROUP container query doesn't count toward the per-sub `queried` list or
        # the resource totals these scope tests assert on.
        if "resourcecontainers" in kql:
            return [], ""
        queried.append(sub)
        return [{
            "id": f"/subscriptions/{sub}/providers/x/r1",
            "name": "r1",
            "type": "Microsoft.Compute/virtualMachines",
            "subscriptionId": sub,
            "resourceGroup": "rg1",
        }], ""

    monkeypatch.setattr(service, "open_sp_session", fake_open)
    monkeypatch.setattr(service, "close_sp_session", fake_close)
    monkeypatch.setattr(service, "_subscriptions", fake_subs)
    monkeypatch.setattr(service, "_workload_scopes", fake_wl_scopes)
    monkeypatch.setattr(service, "_arg", fake_arg)
    return queried


async def test_collect_tenant_scope_queries_all_subs(_stub_collect):
    out = await service.collect(None, scope="")
    assert sorted(_stub_collect) == ["s1", "s2"]
    assert out["summary"]["total_resources"] == 2
    assert not out["errors"]


async def test_collect_single_sub_scope_queries_only_that_sub(_stub_collect):
    out = await service.collect(None, scope="sub:s2")
    assert _stub_collect == ["s2"]
    subs = {r["subscription_id"] for r in out["resources"]}
    assert subs == {"s2"}
    assert out["summary"]["total_resources"] == 1


async def test_collect_mg_scope_queries_only_subs_under_mg(monkeypatch, _stub_collect):
    async def fake_under(_conn, _mg):
        return ["s1"]

    monkeypatch.setattr("app.workloads.discovery.subscriptions_under_mg", fake_under)
    out = await service.collect(None, scope="mg:root")
    assert _stub_collect == ["s1"]
    assert {r["subscription_id"] for r in out["resources"]} == {"s1"}


async def test_collect_invalid_scope_surfaces_error_and_queries_nothing(_stub_collect):
    out = await service.collect(None, scope="sub:not-visible")
    assert _stub_collect == []
    assert out["resources"] == []
    assert any("isn't visible" in e for e in out["errors"])


async def test_collect_includes_resource_groups(monkeypatch):
    """Resource GROUPS (resourcecontainers) are enumerated alongside resources so tag
    operations cover RG-level tags — not just the resources inside them."""
    async def fake_open(_conn):
        return ("session-dir", None)

    async def fake_subs(_conn, _dir):
        return [{"id": "s1", "name": "Sub One"}]

    async def fake_wl_scopes(_conn):
        return []

    async def fake_arg(kql, _conn, _dir):
        if "resourcecontainers" in kql:
            # An RG carrying a tag.
            return [{
                "id": "/subscriptions/s1/resourceGroups/rg1",
                "name": "rg1",
                "type": "microsoft.resources/subscriptions/resourcegroups",
                "subscriptionId": "s1", "resourceGroup": "rg1",
                "tags": {"Owner": "team-a"},
            }], ""
        return [{
            "id": "/subscriptions/s1/resourceGroups/rg1/providers/x/r1",
            "name": "r1", "type": "Microsoft.Compute/virtualMachines",
            "subscriptionId": "s1", "resourceGroup": "rg1", "tags": {},
        }], ""

    monkeypatch.setattr(service, "open_sp_session", fake_open)
    monkeypatch.setattr(service, "close_sp_session", lambda _d: None)
    monkeypatch.setattr(service, "_subscriptions", fake_subs)
    monkeypatch.setattr(service, "_workload_scopes", fake_wl_scopes)
    monkeypatch.setattr(service, "_arg", fake_arg)

    out = await service.collect(None, scope="sub:s1")
    by_id = {r["id"]: r for r in out["resources"]}
    # Both the resource AND its resource group are present.
    assert "/subscriptions/s1/resourceGroups/rg1" in by_id
    rg = by_id["/subscriptions/s1/resourceGroups/rg1"]
    assert rg["type"] == "microsoft.resources/subscriptions/resourcegroups"
    assert rg["tags"] == {"Owner": "team-a"}
    assert out["summary"]["total_resources"] == 2

"""Tests for Workload Groups (non-destructive application / service-family associations).

Covers three layers, all fast + hermetic (no Azure, no TestClient):

  * registry ``group_id`` plumbing — assign / clear / survive-upsert / trash handling;
  * the ``groups`` registry — CRUD, member detach on delete, rollup math, auto-suggest;
  * route ordering — ``/workloads/groups*`` must resolve before the ``/{workload_id}`` catch-all.
"""
from __future__ import annotations

import pytest

from app.workloads import groups as grp
from app.workloads import registry as reg


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def store(tmp_path, monkeypatch):
    """Registry + groups registry pointed at isolated temp JSON files."""
    monkeypatch.setattr(reg, "_PATH", tmp_path / "workloads.json")
    monkeypatch.setattr(grp, "_PATH", tmp_path / "workload_groups.json")
    return reg


def _wl(name: str, **extra):
    return reg.upsert_workload({"name": name, "connection_id": "c1", "nodes": [], **extra})


# --------------------------------------------------------------------------- registry: group_id
def test_new_workload_has_empty_group_id(store):
    w = _wl("A")
    assert w["group_id"] == ""


def test_assign_group_sets_and_is_idempotent(store):
    a, b = _wl("A"), _wl("B")
    changed = reg.assign_group([a["id"], b["id"]], "g1")
    assert changed == 2
    assert reg.get_workload(a["id"])["group_id"] == "g1"
    assert reg.get_workload(b["id"])["group_id"] == "g1"
    # Re-assigning the same group is a no-op (no spurious writes / count).
    assert reg.assign_group([a["id"], b["id"]], "g1") == 0


def test_assign_group_skips_trashed(store):
    a = _wl("A")
    reg.delete_workload(a["id"])  # soft-delete → Trash
    assert reg.assign_group([a["id"]], "g1") == 0
    assert reg.get_workload(a["id"], include_deleted=True)["group_id"] == ""


def test_assign_group_empty_string_clears(store):
    a = _wl("A")
    reg.assign_group([a["id"]], "g1")
    assert reg.assign_group([a["id"]], "") == 1
    assert reg.get_workload(a["id"])["group_id"] == ""


def test_group_id_survives_upsert_without_it(store):
    """Editing a workload (which never sends group_id) must not clobber the association."""
    a = _wl("A")
    reg.assign_group([a["id"]], "g1")
    reg.upsert_workload({"id": a["id"], "name": "A renamed", "connection_id": "c1", "nodes": []})
    got = reg.get_workload(a["id"])
    assert got["name"] == "A renamed"
    assert got["group_id"] == "g1"


def test_clear_group_clears_all_including_trashed(store):
    a, b, c = _wl("A"), _wl("B"), _wl("C")
    reg.assign_group([a["id"], b["id"], c["id"]], "g1")
    reg.delete_workload(b["id"])  # trashed but still carries g1
    other = _wl("D")
    reg.assign_group([other["id"]], "g2")

    cleared = reg.clear_group("g1")
    assert cleared == 3  # a, b (trashed), c
    assert reg.get_workload(a["id"])["group_id"] == ""
    assert reg.get_workload(b["id"], include_deleted=True)["group_id"] == ""
    assert reg.get_workload(other["id"])["group_id"] == "g2"  # untouched
    assert reg.clear_group("") == 0


# --------------------------------------------------------------------------- groups registry
def test_upsert_get_list_group_lifecycle(store):
    g = grp.upsert_group({"name": "CRM", "description": "Customer app"})
    assert g["id"]
    assert g["name"] == "CRM"
    created = g["created_at"]
    assert created

    # get + list see it.
    assert grp.get_group(g["id"])["name"] == "CRM"
    assert [x["id"] for x in grp.list_groups()] == [g["id"]]

    # Update preserves created_at, refreshes updated_at, keeps the same id.
    g2 = grp.upsert_group({"id": g["id"], "name": "CRM Platform"})
    assert g2["id"] == g["id"]
    assert g2["name"] == "CRM Platform"
    assert g2["created_at"] == created


def test_get_group_missing_returns_none(store):
    assert grp.get_group("nope") is None
    assert grp.get_group("") is None


def test_delete_group_detaches_members(store):
    a, b = _wl("A"), _wl("B")
    g = grp.upsert_group({"name": "CRM"})
    reg.assign_group([a["id"], b["id"]], g["id"])

    assert grp.delete_group(g["id"]) is True
    assert grp.get_group(g["id"]) is None
    # Members survive, just detached.
    assert reg.get_workload(a["id"])["group_id"] == ""
    assert reg.get_workload(b["id"])["group_id"] == ""


def test_delete_group_missing_returns_false(store):
    assert grp.delete_group("nope") is False


# --------------------------------------------------------------------------- rollup math
def _profile(score=None, band="unknown", total=0, categories=None, env="unknown",
             crit="", retire=0, criticals=0, analyzed=True):
    return {
        "health": {"score": score, "band": band},
        "composition": {"total": total, "by_category": categories or []},
        "classification": {"environment": env, "criticality": crit},
        "risk": {"retirements_90d": retire, "criticals": criticals},
        "analyzed": analyzed,
    }


def test_rollup_empty():
    r = grp.rollup_from_profiles([])
    assert r["member_count"] == 0
    assert r["total_resources"] == 0
    assert r["health"]["avg_score"] is None
    assert r["health"]["band"] == "unknown"
    assert r["criticality"] == ""
    assert r["risk"] == {"retirements_90d": 0, "criticals": 0}
    assert r["by_category"] == []
    assert r["by_environment"] == []


def test_rollup_aggregates():
    profiles = [
        _profile(score=90, band="good", total=10,
                 categories=[{"category": "compute", "count": 4}, {"category": "storage", "count": 6}],
                 env="production", crit="high", retire=1, criticals=2, analyzed=True),
        _profile(score=30, band="poor", total=5,
                 categories=[{"category": "compute", "count": 2}, {"category": "network", "count": 3}],
                 env="development", crit="critical", retire=0, criticals=1, analyzed=True),
        _profile(score=None, band="unknown", total=0, env="staging", crit="low", analyzed=False),
    ]
    r = grp.rollup_from_profiles(profiles)
    assert r["member_count"] == 3
    assert r["analyzed_count"] == 2
    assert r["total_resources"] == 15
    # avg of [90, 30] = 60 → warn band (>=50, <80).
    assert r["health"]["avg_score"] == 60
    assert r["health"]["band"] == "warn"
    assert r["health"]["distribution"] == {"good": 1, "poor": 1, "warn": 0, "unknown": 1}
    # Worst-case criticality across members.
    assert r["criticality"] == "critical"
    assert r["risk"] == {"retirements_90d": 1, "criticals": 3}
    cats = {c["category"]: c["count"] for c in r["by_category"]}
    assert cats == {"compute": 6, "storage": 6, "network": 3}
    envs = {e["environment"]: e["count"] for e in r["by_environment"]}
    assert envs == {"production": 1, "development": 1, "staging": 1}


@pytest.mark.parametrize("avg_scores,expected_band", [
    ([80, 100], "good"),   # avg 90
    ([50, 60], "warn"),    # avg 55
    ([10, 40], "poor"),    # avg 25
])
def test_rollup_band_thresholds(avg_scores, expected_band):
    profiles = [_profile(score=s, total=1) for s in avg_scores]
    assert grp.rollup_from_profiles(profiles)["health"]["band"] == expected_band


# --------------------------------------------------------------------------- auto-suggest
def test_suggest_clusters_env_families():
    workloads = [
        {"id": "1", "name": "CRM PROD", "environment": "production"},
        {"id": "2", "name": "CRM DEV", "environment": "development"},
        {"id": "3", "name": "Data Lake Prod", "environment": "production"},
        {"id": "4", "name": "Data Lake Dev", "environment": "development"},
        {"id": "5", "name": "Standalone App", "environment": ""},          # no env token → skip
        {"id": "6", "name": "Billing PROD", "environment": "production", "group_id": "g1"},  # grouped → skip
        {"id": "7", "name": "Billing DEV", "environment": "development"},  # only 1 ungrouped → skip
    ]
    out = grp.suggest_groups(workloads)
    names = [s["name"] for s in out]
    assert names == ["Crm", "Data Lake"]  # sorted by -count then name
    by_name = {s["name"]: set(s["workload_ids"]) for s in out}
    assert by_name["Crm"] == {"1", "2"}
    assert by_name["Data Lake"] == {"3", "4"}
    assert "Billing" not in names
    assert "Standalone App" not in names


def test_suggest_ignores_workload_named_only_for_environment():
    # A workload literally named "prod" strips to an empty stem → never clustered.
    workloads = [
        {"id": "1", "name": "prod", "environment": "production"},
        {"id": "2", "name": "dev", "environment": "development"},
    ]
    assert grp.suggest_groups(workloads) == []


def test_suggest_empty():
    assert grp.suggest_groups([]) == []


# --------------------------------------------------------------------------- route ordering
def test_group_routes_registered_before_workload_id_catch_all():
    """``/workloads/groups`` and ``/workloads/groups/{group_id}`` must be registered before the
    ``/workloads/{workload_id}`` catch-all, otherwise "groups" is captured as a workload id."""
    from app.api.workloads import router

    paths = [getattr(r, "path", "") for r in router.routes]

    def first_ending(suffix: str) -> int:
        return next(i for i, p in enumerate(paths) if p.endswith(suffix))

    workload_id_idx = first_ending("/{workload_id}")
    assert first_ending("/groups") < workload_id_idx
    assert first_ending("/groups/{group_id}") < workload_id_idx


def test_compare_route_registered_before_workload_id_catch_all():
    """``/workloads/groups/{group_id}/compare`` must also beat the ``/{workload_id}`` catch-all."""
    from app.api.workloads import router

    paths = [getattr(r, "path", "") for r in router.routes]
    compare_idx = next(i for i, p in enumerate(paths) if p.endswith("/groups/{group_id}/compare"))
    workload_id_idx = next(i for i, p in enumerate(paths) if p.endswith("/{workload_id}"))
    assert compare_idx < workload_id_idx


# --------------------------------------------------------------------------- compare (drift)
def _cprofile(pid, name, env="", crit="", data_class="", wl_type="", total=0,
              score=None, band="unknown", signals=None, by_type=None, by_category=None,
              retire=0, criticals=0, analyzed=True):
    """A ``WorkloadProfile``-shaped dict for compare tests. ``signals`` maps a health-signal key
    (monitoring/telemetry/…) to a score; ``by_type`` is a list of (type, friendly, count)."""
    health = {"score": score, "band": band}
    for key, val in (signals or {}).items():
        health[key] = val
    return {
        "id": pid,
        "name": name,
        "classification": {"environment": env, "criticality": crit,
                           "data_classification": data_class, "workload_type": wl_type},
        "health": health,
        "composition": {
            "total": total,
            "by_type": [{"type": t, "friendly": f, "count": c} for (t, f, c) in (by_type or [])],
            "by_category": [{"category": cat, "count": c} for (cat, c) in (by_category or [])],
        },
        "risk": {"retirements_90d": retire, "criticals": criticals},
        "analyzed": analyzed,
    }


def test_compare_empty_and_single_have_no_drift():
    for profiles in ([], [_cprofile("1", "CRM PROD")]):
        c = grp.compare_profiles(profiles)
        assert c["summary"]["drift_types"] == 0
        assert c["summary"]["drift_signals"] == 0
        assert c["summary"]["drift_categories"] == 0
        assert c["highlights"] == []
        assert all(not t["drift"] for t in c["types"])


def test_compare_members_are_aligned():
    profiles = [
        _cprofile("1", "CRM PROD", env="production", crit="high", data_class="confidential",
                  wl_type="web", total=12, score=88, band="good", retire=1, criticals=2),
        _cprofile("2", "CRM DEV", env="development", crit="low", total=4, score=60, band="warn"),
    ]
    c = grp.compare_profiles(profiles)
    assert [m["id"] for m in c["members"]] == ["1", "2"]
    prod = c["members"][0]
    assert prod["environment"] == "production"
    assert prod["criticality"] == "high"
    assert prod["data_classification"] == "confidential"
    assert prod["total_resources"] == 12
    assert prod["health_score"] == 88
    assert prod["retirements_90d"] == 1
    assert prod["criticals"] == 2
    assert c["summary"]["member_count"] == 2
    assert c["summary"]["health_spread"] == 28  # 88 - 60


def test_compare_resource_type_drift_detected():
    """A type present in PROD but absent in DEV is flagged drift and yields a highlight."""
    profiles = [
        _cprofile("1", "CRM PROD", env="production",
                  by_type=[("waf", "Web Application Firewall", 1), ("vm", "Virtual Machine", 3)]),
        _cprofile("2", "CRM DEV", env="development",
                  by_type=[("vm", "Virtual Machine", 2)]),
    ]
    c = grp.compare_profiles(profiles)
    by_type = {t["type"]: t for t in c["types"]}
    assert by_type["waf"]["drift"] is True          # only in PROD
    assert by_type["waf"]["counts"] == {"1": 1}     # DEV absent → no key
    assert by_type["vm"]["drift"] is False          # present in both
    assert c["summary"]["drift_types"] == 1
    # drift types sort first.
    assert c["types"][0]["type"] == "waf"
    # Highlight names the haves and have-nots by environment label.
    assert any("production" in h and "development" in h and "Web Application Firewall" in h
               for h in c["highlights"])


def test_compare_no_type_drift_when_all_present():
    profiles = [
        _cprofile("1", "A", env="prod", by_type=[("vm", "Virtual Machine", 3)]),
        _cprofile("2", "B", env="dev", by_type=[("vm", "Virtual Machine", 1)]),
    ]
    c = grp.compare_profiles(profiles)
    assert c["summary"]["drift_types"] == 0
    assert c["highlights"] == []
    assert c["types"][0]["drift"] is False


def test_compare_signal_coverage_drift():
    """A health signal scored in one member but missing in another is coverage drift."""
    profiles = [
        _cprofile("1", "PROD", env="production", signals={"monitoring": 80, "backupdr": 90}),
        _cprofile("2", "DEV", env="development", signals={"monitoring": 40}),  # no backupdr
    ]
    c = grp.compare_profiles(profiles)
    by_key = {s["key"]: s for s in c["signals"]}
    assert by_key["monitoring"]["drift"] is False              # covered in both
    assert by_key["monitoring"]["values"] == {"1": 80, "2": 40}
    assert by_key["backupdr"]["drift"] is True                 # only PROD covered
    assert by_key["backupdr"]["values"]["2"] is None
    assert c["summary"]["drift_signals"] == 1


def test_compare_category_drift():
    profiles = [
        _cprofile("1", "PROD", env="production",
                  by_category=[("compute", 5), ("network", 2)]),
        _cprofile("2", "DEV", env="development",
                  by_category=[("compute", 3)]),
    ]
    c = grp.compare_profiles(profiles)
    by_cat = {row["category"]: row for row in c["categories"]}
    assert by_cat["network"]["drift"] is True
    assert by_cat["compute"]["drift"] is False
    assert c["summary"]["drift_categories"] == 1
    assert c["categories"][0]["category"] == "network"  # drift first


def test_compare_highlights_capped_at_six():
    prod_types = [(f"t{i}", f"Type {i}", 1) for i in range(10)]
    profiles = [
        _cprofile("1", "PROD", env="production", by_type=prod_types),
        _cprofile("2", "DEV", env="development", by_type=[]),
    ]
    c = grp.compare_profiles(profiles)
    assert len(c["highlights"]) <= 6
    assert c["summary"]["drift_types"] == 10


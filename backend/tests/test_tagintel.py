"""Tag Intelligence (F1-F12) backend tests.

Exercises the pure analysis layer (census, hygiene clustering, coverage, FinOps, workload
inference, drift, policy generation, remediation, NL ask, RBAC advice) over a small synthetic
estate shaped like ``app.inventory.service`` output. File-backed registries (catalog, drift,
remediation plans) are redirected to tmp_path so tests never touch real ``.data``.
"""
import asyncio

import pytest

from app.tagintel import (
    analysis,
    ask as ask_mod,
    catalog,
    coverage as coverage_mod,
    drift,
    finops,
    policygen,
    rbac_advice,
    remediation,
    scale,
)


def _r(rid, name, rtype, rg, sub, tags, workloads=None):
    return {
        "id": rid, "name": name, "type": rtype, "location": "eastus",
        "resource_group": rg, "subscription_id": sub, "tags": tags,
        "workloads": workloads or [],
    }


@pytest.fixture()
def estate():
    return [
        _r("/s/sub1/vm1", "app-prod-vm1", "microsoft.compute/virtualmachines", "rg-app-prod", "sub1",
           {"CostCenter": "FIN-204", "Environment": "Prod", "Owner": "team-a"}, [{"id": "w1", "name": "Payments"}]),
        _r("/s/sub1/vm2", "app-prod-vm2", "microsoft.compute/virtualmachines", "rg-app-prod", "sub1",
           {"costcenter": "FIN-204", "Environment": "PRD"}),
        _r("/s/sub1/disk1", "data-disk", "microsoft.compute/disks", "rg-app-prod", "sub1", {}),
        _r("/s/sub2/st1", "sharedstore", "microsoft.storage/storageaccounts", "rg-shared", "sub2",
           {"Environment": "Production", "Owner": "team-b"},
           [{"id": "w1", "name": "Payments"}, {"id": "w2", "name": "Ledger"}]),
    ]


# --------------------------------------------------------------------------- F1 census
def test_census_counts_and_coverage(estate):
    cen = analysis.census(estate, {"sub1": "Sub One", "sub2": "Sub Two"})
    assert cen["total_resources"] == 4
    assert cen["tagged_count"] == 3
    assert cen["untagged_count"] == 1
    assert cen["tag_coverage_pct"] == 75.0
    # CostCenter and costcenter are distinct keys but cross-reference as casing variants.
    cc = next(k for k in cen["keys"] if k["key"] == "CostCenter")
    assert "costcenter" in cc["casing_variants"]
    assert cc["category"] == "billing"
    env = next(k for k in cen["keys"] if k["key"] == "Environment")
    assert env["category"] == "environment"
    assert env["distinct_values"] == 3  # Prod, PRD, Production


def test_census_scope_coverage(estate):
    cen = analysis.census(estate, {"sub1": "Sub One", "sub2": "Sub Two"})
    by_sub = {s["id"]: s for s in cen["scope_coverage"]["by_subscription"]}
    assert by_sub["sub1"]["total"] == 3
    assert by_sub["sub2"]["coverage_pct"] == 100.0


def test_classify_key():
    assert analysis.classify_key("BillingCode") == "billing"
    assert analysis.classify_key("Owner") == "ownership"
    assert analysis.classify_key("Environment") == "environment"
    assert analysis.classify_key("DataClassification") == "security"
    assert analysis.classify_key("ExpirationDate") == "lifecycle"
    assert analysis.classify_key("RandomThing") == "other"


# --------------------------------------------------------------------------- F2 hygiene
def test_key_clusters_casing(estate):
    clusters = analysis.key_clusters(estate)
    cc = next(c for c in clusters if c["canonical"].lower() == "costcenter")
    assert set(cc["members"]) == {"CostCenter", "costcenter"}
    assert cc["confidence"] == "high"
    assert cc["affected"] == 2


def test_value_clusters_environment(estate):
    clusters = analysis.value_clusters(estate)
    env = next(c for c in clusters if c["key"] == "Environment")
    variant = env["variants"][0]
    assert variant["canonical"] == "Production"
    # Members are the variants to normalize toward the canonical (Prod, PRD).
    assert set(variant["members"]) >= {"Prod", "PRD"}


def test_canonical_value():
    assert analysis.canonical_value("Environment", "prd") == "Production"
    assert analysis.canonical_value("Environment", "staging") == "Staging"
    assert analysis.canonical_value("CostCenter", "FIN-204") is None


# --------------------------------------------------------------------------- F3 grouping
def test_workload_inference(estate):
    g = analysis.workload_inference(estate)
    assert g["confirmed_resources"] == 2
    labels = {grp["label"] for grp in g["inferred_groups"]}
    assert "Payments" in labels


# --------------------------------------------------------------------------- F6 coverage
def test_coverage_missing_one(estate):
    cov = coverage_mod.coverage(estate, ["CostCenter", "Environment", "Owner"])
    assert cov["evaluated"] == 4
    assert cov["compliant"] == 1          # only vm1 has all three
    assert cov["coverage_pct"] == 25.0
    keys_missing_one = {g["key"] for g in cov["missing_one"]}
    assert keys_missing_one == {"Owner", "CostCenter"}  # vm2 missing Owner, st1 missing CostCenter
    assert cov["missing_one_total"] == 2


def test_coverage_exempt(estate):
    cov = coverage_mod.coverage(estate, ["Owner"], exempt_types=["storageaccounts"])
    assert cov["exempt"] == 1             # the storage account is exempt
    assert cov["evaluated"] == 3


# --------------------------------------------------------------------------- F4/F5 finops
def test_billing_map(estate):
    cost = {"/s/sub1/vm1": 100.0, "/s/sub1/vm2": 50.0, "/s/sub1/disk1": 5.0, "/s/sub2/st1": 200.0}
    bm = finops.billing_map(estate, cost)
    fin = next(r for r in bm["rows"] if r["billing_code"] == "FIN-204")
    assert fin["resource_count"] == 2
    assert fin["cost"] == 150.0
    unalloc = next(r for r in bm["rows"] if r["unallocated"])
    assert unalloc["cost"] == 205.0


def test_cost_allocation(estate):
    cost = {"/s/sub1/vm1": 100.0, "/s/sub1/vm2": 50.0, "/s/sub1/disk1": 5.0, "/s/sub2/st1": 200.0}
    alloc = finops.cost_allocation(estate, cost, "workload")
    assert alloc["total_cost"] == 355.0
    assert alloc["allocatable_cost"] == 150.0       # vm1 + vm2 carry a billing tag
    assert alloc["unallocatable_cost"] == 205.0
    # st1 belongs to two workloads and has cost -> a shared-split candidate.
    assert any(s["id"] == "/s/sub2/st1" for s in alloc["shared_candidates"])


def test_reconcile_cmdb(estate):
    bm = finops.billing_map(estate, {})
    discovered = [r["billing_code"] for r in bm["rows"] if not r["unallocated"]]
    rec = finops.reconcile_cmdb(discovered, ["FIN-204", "FIN-999"])
    assert "FIN-204" in rec["in_both"]
    assert "FIN-999" in rec["only_in_cmdb"]


# --------------------------------------------------------------------------- F2 catalog (file-backed)
@pytest.fixture()
def _catalog_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_PATH", tmp_path / "tagintel_catalog.json")


def test_catalog_crud(_catalog_tmp):
    saved = catalog.upsert("t1", {"canonical": "CostCenter", "category": "billing", "required": True})
    assert saved["category"] == "billing"
    assert catalog.required_keys("t1") == ["CostCenter"]
    rows = catalog.list_catalog("t1")
    assert len(rows) == 1
    assert catalog.delete("t1", saved["id"]) is True
    assert catalog.list_catalog("t1") == []


def test_catalog_seed(_catalog_tmp, estate):
    cen = analysis.census(estate)
    created = catalog.seed_from_census("t1", cen["keys"], analysis.key_clusters(estate), limit=5)
    assert any(e["canonical"] == "Environment" for e in created)
    # Environment is seeded as required (governance family).
    env = next(e for e in created if e["canonical"] == "Environment")
    assert env["required"] is True


# --------------------------------------------------------------------------- F7 drift (file-backed)
@pytest.fixture()
def _drift_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(drift, "_PATH", tmp_path / "tagintel_drift.json")


def test_drift_snapshot_and_diff(_drift_tmp, estate):
    s1 = drift.save_snapshot("t1", "c1", "", estate, coverage_pct=75.0)
    # Mutate a billing tag value, add a new key, then snapshot again.
    estate2 = [dict(r, tags=dict(r["tags"])) for r in estate]
    estate2[0]["tags"]["CostCenter"] = "FIN-209"
    estate2[0]["tags"]["PatchGroup"] = "ring1"
    s2 = drift.save_snapshot("t1", "c1", "", estate2, coverage_pct=75.0)
    snaps = drift.list_snapshots("t1", "c1", "")
    assert len(snaps) == 2
    d = drift.diff("t1", "c1", "", s1["id"], s2["id"])
    assert "PatchGroup" in d["added_keys"]
    assert any(c["key"] == "CostCenter" for c in d["billing_changes"])
    # Resource-level detail: PatchGroup was added to exactly vm1, with its name + value.
    pg = next(g for g in d["added_key_details"] if g["key"] == "PatchGroup")
    assert pg["count"] == 1 and pg["resources"][0]["id"] == "/s/sub1/vm1"
    assert pg["resources"][0]["name"] == "app-prod-vm1"
    # Value changes carry the resource name; changed-resources rollup shows what changed.
    vc = next(c for c in d["value_changes"] if c["key"] == "CostCenter")
    assert vc["name"] == "app-prod-vm1" and vc["from"] == "FIN-204" and vc["to"] == "FIN-209"
    chg = next(c for c in d["changed_resources"] if c["id"] == "/s/sub1/vm1")
    assert any(a["key"] == "PatchGroup" for a in chg["added"])
    assert any(c["key"] == "CostCenter" for c in chg["changed"])
    assert d["changed_resource_count"] == 1


def test_drift_diff_added_removed_resources(_drift_tmp, estate):
    # A resource present in base but gone in head (and vice-versa) is reported.
    s1 = drift.save_snapshot("t1", "c1", "", estate, coverage_pct=75.0)
    estate2 = [dict(r, tags=dict(r["tags"])) for r in estate if r["id"] != "/s/sub1/disk1"]
    estate2.append({"id": "/s/sub1/newvm", "name": "new-vm", "tags": {"Owner": "team-q"}})
    s2 = drift.save_snapshot("t1", "c1", "", estate2, coverage_pct=80.0)
    d = drift.diff("t1", "c1", "", s1["id"], s2["id"])
    assert any(r["id"] == "/s/sub1/newvm" for r in d["added_resources"])
    assert any(r["id"] == "/s/sub1/disk1" for r in d["removed_resources"])


# --------------------------------------------------------------------------- F8 policy
def test_policygen_effects():
    out = policygen.generate([
        {"tag": "CostCenter", "effect": "audit"},
        {"tag": "Owner", "effect": "deny"},
        {"tag": "Environment", "effect": "append", "default_value": "Production"},
        {"tag": "CostCenter", "effect": "inherit"},
    ])
    effects = {d["_effect"] for d in out["definitions"]}
    assert effects == {"audit", "deny", "modify"}
    assert out["warnings"]  # deny present -> warning
    assert len(out["initiative"]["properties"]["policyDefinitions"]) == 4


def test_policy_ladder():
    ladder = policygen.rollout_ladder()
    assert len(ladder) == 5
    assert ladder[-1]["name"] == "Deny"
    assert ladder[-1]["risk"] == "high"


# --------------------------------------------------------------------------- F9 remediation
@pytest.fixture()
def _plan_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(remediation, "_PATH", tmp_path / "tagintel_plans.json")


def test_remediation_add_tag(estate):
    op = {"type": "add_tag", "key": "Owner", "value": "team-x", "resource_ids": ["/s/sub1/disk1"]}
    plan = remediation.build_plan(estate, op)
    assert plan["count"] == 1
    item = plan["items"][0]
    assert item["after"]["Owner"] == "team-x"
    assert item["overwrite"] is False
    scripts = remediation.generate_scripts(plan)
    assert "Update-AzTag" in scripts["powershell"]
    assert "az tag update" in scripts["azcli"]
    # Rollback deletes the newly added key.
    assert "Delete" in scripts["rollback"]


def test_remediation_normalize_value(estate):
    op = {"type": "normalize_value", "key": "Environment", "from_value": "PRD", "to_value": "Production",
          "resource_ids": ["/s/sub1/vm2"]}
    plan = remediation.build_plan(estate, op)
    assert plan["count"] == 1
    assert plan["items"][0]["after"]["Environment"] == "Production"
    assert plan["items"][0]["overwrite"] is True


def test_remediation_rename_key(estate):
    op = {"type": "rename_key", "key": "costcenter", "to_key": "CostCenter", "resource_ids": ["/s/sub1/vm2"]}
    plan = remediation.build_plan(estate, op)
    after = plan["items"][0]["after"]
    assert "CostCenter" in after and "costcenter" not in after


def test_remediation_invalid_op(estate):
    with pytest.raises(ValueError):
        remediation.build_plan(estate, {"type": "nope"})


def test_remediation_save_plan(_plan_tmp, estate):
    op = {"type": "add_tag", "key": "Owner", "value": "x", "resource_ids": ["/s/sub1/disk1"]}
    plan = remediation.build_plan(estate, op)
    rec = remediation.save_plan("t1", plan, actor="tester")
    assert rec["count"] == 1
    assert remediation.list_plans("t1")[0]["id"] == rec["id"]


# --------------------------------------------------------------------------- F9 multi-op change-sets
def test_build_plan_ops_multi(estate):
    # A change-set with two pairs applied together: add Owner + add Environment.
    ops = [
        {"type": "add_tag", "key": "Owner", "value": "team-x"},
        {"type": "add_tag", "key": "Environment", "value": "Production"},
    ]
    plan = remediation.build_plan_ops(estate, ops, ["/s/sub1/disk1"])
    assert plan["count"] == 1
    after = plan["items"][0]["after"]
    assert after["Owner"] == "team-x" and after["Environment"] == "Production"
    scripts = remediation.generate_scripts(plan)
    # Both pairs land in a single Merge command.
    assert "'Owner' = 'team-x'" in scripts["powershell"]
    assert "'Environment' = 'Production'" in scripts["powershell"]


def test_build_plan_ops_rejects_empty(estate):
    with pytest.raises(ValueError):
        remediation.build_plan_ops(estate, [{"type": "bogus"}])


@pytest.fixture()
def _cs_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(remediation, "_CS_PATH", tmp_path / "tagintel_changesets.json")


def test_changeset_crud(_cs_tmp):
    cs = remediation.save_changeset("t1", {
        "name": "Baseline tags",
        "description": "Owner + Environment",
        "operations": [
            {"type": "add_tag", "key": "Owner", "value": "team-a"},
            {"type": "add_tag", "key": "Environment", "value": "Production"},
            {"type": "bogus"},  # dropped by _clean_ops
        ],
    }, actor="tester")
    assert cs["name"] == "Baseline tags"
    assert len(cs["operations"]) == 2  # bogus dropped
    rows = remediation.list_changesets("t1")
    assert len(rows) == 1 and rows[0]["id"] == cs["id"]
    # Update by id preserves created_at.
    cs2 = remediation.save_changeset("t1", {**cs, "description": "updated"})
    assert cs2["id"] == cs["id"] and cs2["created_at"] == cs["created_at"] and cs2["description"] == "updated"
    assert remediation.get_changeset("t1", cs["id"])["description"] == "updated"
    assert remediation.delete_changeset("t1", cs["id"]) is True
    assert remediation.list_changesets("t1") == []


def test_changeset_requires_name_and_ops(_cs_tmp):
    with pytest.raises(ValueError):
        remediation.save_changeset("t1", {"name": "", "operations": [{"type": "add_tag", "key": "X", "value": "1"}]})
    with pytest.raises(ValueError):
        remediation.save_changeset("t1", {"name": "Empty", "operations": []})


def test_changeset_preloads_for_preview(_cs_tmp, estate):
    # Save a change-set, then re-load its operations to dry-run — the "preload" flow.
    cs = remediation.save_changeset("t1", {
        "name": "Add ownership",
        "operations": [{"type": "add_tag", "key": "Owner", "value": "team-z"}],
    })
    loaded = remediation.get_changeset("t1", cs["id"])
    plan = remediation.build_plan_ops(estate, loaded["operations"], ["/s/sub1/disk1"])
    assert plan["count"] == 1
    assert plan["items"][0]["after"]["Owner"] == "team-z"


# --------------------------------------------------------------------------- change-set library (groups/metadata)
def test_changeset_metadata(_cs_tmp):
    cs = remediation.save_changeset("t1", {
        "name": "Mixed", "labels": ["baseline", "prod", " ", "baseline"],
        "operations": [
            {"type": "add_tag", "key": "Owner", "value": "a"},
            {"type": "add_tag", "key": "Environment", "value": "Production"},
            {"type": "rename_key", "key": "env", "to_key": "Environment"},
        ],
    })
    assert cs["op_breakdown"] == {"add_tag": 2, "rename_key": 1}
    assert set(cs["affected_keys"]) == {"Owner", "Environment", "env"}
    assert cs["labels"] == ["baseline", "prod"]  # trimmed + deduped
    assert cs["run_count"] == 0 and cs["last_run"] is None


def test_group_crud_and_assignment(_cs_tmp):
    g = remediation.save_group("t1", {"name": "Ownership Baseline", "color": "blue"})
    assert g["color"] == "blue"
    cs = remediation.save_changeset("t1", {
        "name": "Add owner", "group_id": g["id"],
        "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}],
    })
    assert cs["group_id"] == g["id"]
    groups = remediation.list_groups("t1")
    assert len(groups) == 1 and groups[0]["count"] == 1
    # Unknown group id is cleared on save (never dangles).
    cs2 = remediation.save_changeset("t1", {**cs, "group_id": "does-not-exist"})
    assert cs2["group_id"] == ""


def test_group_delete_ungroups_members(_cs_tmp):
    g = remediation.save_group("t1", {"name": "Temp"})
    cs = remediation.save_changeset("t1", {
        "name": "x", "group_id": g["id"],
        "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}],
    })
    assert remediation.delete_group("t1", g["id"]) is True
    # The change-set survives, now ungrouped.
    assert remediation.get_changeset("t1", cs["id"])["group_id"] == ""
    assert remediation.list_groups("t1") == []


def test_move_changeset(_cs_tmp):
    g = remediation.save_group("t1", {"name": "G"})
    cs = remediation.save_changeset("t1", {"name": "x", "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}]})
    assert cs["group_id"] == ""
    moved = remediation.move_changeset("t1", cs["id"], g["id"])
    assert moved["group_id"] == g["id"]
    # Move to ungrouped.
    assert remediation.move_changeset("t1", cs["id"], "")["group_id"] == ""
    # Unknown group -> None (rejected).
    assert remediation.move_changeset("t1", cs["id"], "bogus") is None


def test_duplicate_changeset(_cs_tmp):
    cs = remediation.save_changeset("t1", {"name": "Base", "labels": ["x"],
                                           "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}]})
    dup = remediation.duplicate_changeset("t1", cs["id"], actor="tester")
    assert dup["id"] != cs["id"]
    assert dup["name"] == "Base (copy)"
    assert dup["operations"] == cs["operations"] and dup["labels"] == cs["labels"]
    assert len(remediation.list_changesets("t1")) == 2
    assert remediation.duplicate_changeset("t1", "nope") is None


def test_record_changeset_run(_cs_tmp):
    cs = remediation.save_changeset("t1", {"name": "x", "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}]})
    remediation.record_changeset_run("t1", cs["id"], {"scope": "wl1", "applied": 5, "failed": 0, "total": 5})
    rec = remediation.get_changeset("t1", cs["id"])
    assert rec["run_count"] == 1 and rec["last_run"]["applied"] == 5 and rec["last_run"]["at"]
    # No-op for unknown id (ad-hoc runs aren't tracked).
    remediation.record_changeset_run("t1", "nope", {"applied": 1})


def test_export_bundle_includes_referenced_groups_only(_cs_tmp):
    g = remediation.save_group("t1", {"name": "Ownership", "color": "blue"})
    remediation.save_group("t1", {"name": "Unused", "color": "rose"})
    remediation.save_changeset("t1", {"name": "Owner tags", "group_id": g["id"], "labels": ["baseline"],
                                      "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}]})
    remediation.save_changeset("t1", {"name": "Ungrouped one",
                                      "operations": [{"type": "set_tag", "key": "Env", "value": "Prod"}]})
    bundle = remediation.export_changesets("t1")
    assert bundle["kind"] == "tagintel-changesets" and bundle["version"] == 1
    assert len(bundle["changesets"]) == 2
    # Only the group actually used by an exported change-set is included.
    assert [g["name"] for g in bundle["groups"]] == ["Ownership"]
    # Audit fields are stripped from the portable records.
    assert all("id" not in c and "last_run" not in c for c in bundle["changesets"])


def test_export_respects_id_subset(_cs_tmp):
    a = remediation.save_changeset("t1", {"name": "A", "operations": [{"type": "add_tag", "key": "K", "value": "v"}]})
    remediation.save_changeset("t1", {"name": "B", "operations": [{"type": "add_tag", "key": "K2", "value": "v"}]})
    bundle = remediation.export_changesets("t1", ids=[a["id"]])
    assert [c["name"] for c in bundle["changesets"]] == ["A"]


def test_import_adds_records_and_remaps_groups(_cs_tmp):
    # Build a bundle in one tenant, import into a fresh tenant.
    g = remediation.save_group("src", {"name": "Ownership", "color": "green"})
    remediation.save_changeset("src", {"name": "Owner tags", "group_id": g["id"],
                                       "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}]})
    bundle = remediation.export_changesets("src")

    res = remediation.import_changesets("dst", bundle, actor="importer")
    assert res["imported"] == 1 and res["groups_created"] == 1 and res["errors"] == []
    sets = remediation.list_changesets("dst")
    assert len(sets) == 1 and sets[0]["actor"] == "importer"
    # The change-set landed in a real (newly created) group with the original name.
    groups = remediation.list_groups("dst")
    assert len(groups) == 1 and groups[0]["name"] == "Ownership"
    assert sets[0]["group_id"] == groups[0]["id"]


def test_import_reuses_existing_group_by_name(_cs_tmp):
    existing = remediation.save_group("t1", {"name": "Ownership", "color": "blue"})
    bundle = {
        "kind": "tagintel-changesets", "version": 1,
        "groups": [{"id": "old-id", "name": "ownership", "color": "rose"}],   # different case + color
        "changesets": [{"name": "X", "group_id": "old-id",
                        "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}]}],
    }
    res = remediation.import_changesets("t1", bundle)
    assert res["imported"] == 1 and res["groups_created"] == 0   # matched the existing group by name
    sets = remediation.list_changesets("t1")
    assert sets[0]["group_id"] == existing["id"]


def test_import_is_additive_not_destructive(_cs_tmp):
    remediation.save_changeset("t1", {"name": "Keep me", "operations": [{"type": "add_tag", "key": "A", "value": "1"}]})
    bundle = {"changesets": [{"name": "Incoming", "operations": [{"type": "add_tag", "key": "B", "value": "2"}]}]}
    res = remediation.import_changesets("t1", bundle)
    assert res["imported"] == 1
    names = {c["name"] for c in remediation.list_changesets("t1")}
    assert names == {"Keep me", "Incoming"}   # nothing overwritten


def test_import_validates_payload(_cs_tmp):
    with pytest.raises(ValueError):
        remediation.import_changesets("t1", {"kind": "something-else", "changesets": [{"name": "x", "operations": []}]})
    with pytest.raises(ValueError):
        remediation.import_changesets("t1", {"changesets": []})
    # An invalid op makes that one change-set skipped (reported), not a hard failure.
    res = remediation.import_changesets("t1", {"changesets": [
        {"name": "good", "operations": [{"type": "add_tag", "key": "K", "value": "v"}]},
        {"name": "bad", "operations": [{"type": "nope"}]},
    ]})
    assert res["imported"] == 1 and res["skipped"] == 1 and res["errors"]


def test_legacy_flat_store_migrates(_cs_tmp, tmp_path):
    # Seed a legacy flat-format store ({cs_id: record}) and confirm it migrates on read.
    import json
    legacy = {"t1": {"old1": {"id": "old1", "name": "Legacy", "operations": [{"type": "add_tag", "key": "Owner", "value": "a"}],
                              "created_at": "2020-01-01", "updated_at": "2020-01-01"}}}
    (tmp_path / "tagintel_changesets.json").write_text(json.dumps(legacy), encoding="utf-8")
    rows = remediation.list_changesets("t1")
    assert len(rows) == 1 and rows[0]["id"] == "old1"
    # Groups list works (empty) and a new group can be added alongside the migrated set.
    assert remediation.list_groups("t1") == []
    g = remediation.save_group("t1", {"name": "New"})
    assert remediation.move_changeset("t1", "old1", g["id"])["group_id"] == g["id"]


# --------------------------------------------------------------------------- F9 apply (write path)
class _FakeCap:
    def __init__(self, ok=True, error=""):
        self.ok = ok
        self.error = error
        self.stderr = ""


def test_apply_blocked_without_connection(estate):
    op = {"type": "add_tag", "key": "Owner", "value": "x", "resource_ids": ["/s/sub1/disk1"]}
    plan = remediation.build_plan(estate, op)
    result = asyncio.run(remediation.apply_plan(plan, None))
    assert result["blocked"] is True and result["applied"] == 0
    assert "no azure connection" in result["reason"].lower()


def test_apply_blocked_on_read_only_connection(estate):
    op = {"type": "add_tag", "key": "Owner", "value": "x", "resource_ids": ["/s/sub1/disk1"]}
    plan = remediation.build_plan(estate, op)
    result = asyncio.run(remediation.apply_plan(plan, {"id": "c1", "read_only": True}))
    assert result["blocked"] is True
    assert "read-only" in result["reason"].lower()


def test_apply_runs_commands_on_writable_connection(estate, monkeypatch):
    calls = []

    async def _fake_run(cmd, connection, *, read_only, confirm):
        calls.append({"cmd": cmd, "read_only": read_only, "confirm": confirm})
        return _FakeCap(ok=True)

    monkeypatch.setattr("app.exec.command_runner.run_command_capture", _fake_run)
    op = {"type": "add_tag", "key": "Owner", "value": "team-x",
          "resource_ids": ["/s/sub1/disk1", "/s/sub1/vm2"]}
    plan = remediation.build_plan(estate, op)
    result = asyncio.run(remediation.apply_plan(plan, {"id": "c1", "read_only": False}, actor="tester"))
    assert result["blocked"] is False
    assert result["applied"] == plan["count"] and result["failed"] == 0
    # Executes with write intent + confirm (governance handled by the runner).
    assert calls and all(c["confirm"] is True and c["read_only"] is False for c in calls)
    assert all("az tag update" in c["cmd"] for c in calls)


def test_apply_reports_per_resource_failure(estate, monkeypatch):
    async def _fake_run(cmd, connection, *, read_only, confirm):
        return _FakeCap(ok=False, error="Forbidden")

    monkeypatch.setattr("app.exec.command_runner.run_command_capture", _fake_run)
    op = {"type": "add_tag", "key": "Owner", "value": "x", "resource_ids": ["/s/sub1/disk1"]}
    plan = remediation.build_plan(estate, op)
    result = asyncio.run(remediation.apply_plan(plan, {"id": "c1", "read_only": False}))
    assert result["applied"] == 0 and result["failed"] == 1
    assert result["results"][0]["ok"] is False and "Forbidden" in result["results"][0]["error"]


def _drain(agen):
    """Collect every event from an async generator into a list (test helper)."""
    async def _collect():
        return [ev async for ev in agen]
    return asyncio.run(_collect())


def test_apply_plan_stream_emits_item_events(estate, monkeypatch):
    async def _fake_run(cmd, connection, *, read_only, confirm):
        return _FakeCap(ok=True)

    monkeypatch.setattr("app.exec.command_runner.run_command_capture", _fake_run)
    op = {"type": "add_tag", "key": "Owner", "value": "team-x",
          "resource_ids": ["/s/sub1/disk1", "/s/sub1/vm2"]}
    plan = remediation.build_plan(estate, op)
    events = _drain(remediation.apply_plan_stream(plan, {"id": "c1", "name": "Prod", "read_only": False}, actor="t"))
    kinds = [e["event"] for e in events]
    # start → (item_start, item_done) per resource → done
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert kinds.count("item_start") == plan["count"] == kinds.count("item_done")
    start = events[0]
    assert start["total"] == plan["count"] and start["connection"] == "Prod"
    # Each item_start carries a human-readable change description.
    first_item = next(e for e in events if e["event"] == "item_start")
    assert "add Owner=team-x" in first_item["change"]
    # Running tallies climb and the final done matches the drained apply_plan summary.
    done = events[-1]
    assert done["applied"] == plan["count"] and done["failed"] == 0 and done["blocked"] is False


def test_apply_plan_stream_blocked_is_single_done(estate):
    op = {"type": "add_tag", "key": "Owner", "value": "x", "resource_ids": ["/s/sub1/disk1"]}
    plan = remediation.build_plan(estate, op)
    events = _drain(remediation.apply_plan_stream(plan, {"id": "c1", "read_only": True}))
    assert [e["event"] for e in events] == ["done"]
    assert events[0]["blocked"] is True and "read-only" in events[0]["reason"].lower()


def test_describe_item_summarizes_diff():
    item = {"before": {"env": "prod"}, "after": {"env": "Production", "Owner": "team-x"}}
    desc = remediation._describe_item(item)
    assert "add Owner=team-x" in desc and "set env=Production (was prod)" in desc



# --------------------------------------------------------------------------- F10 ask
def test_ask_intents(estate):
    cen = analysis.census(estate)
    assert ask_mod.answer("show all tag keys", cen, estate)["kind"] == "keys"
    val = ask_mod.answer("values for Environment", cen, estate)
    assert val["kind"] == "values" and val["key"] == "Environment"
    miss = ask_mod.answer("resources missing Owner", cen, estate)
    assert miss["kind"] == "missing" and miss["answer"].startswith("2")  # vm2 + disk1
    assert ask_mod.answer("show untagged resources", cen, estate)["kind"] == "untagged"
    assert ask_mod.answer("", cen, estate)["kind"] == "empty"


def test_ask_generates_query(estate):
    cen = analysis.census(estate)
    res = ask_mod.answer("values for Environment", cen, estate)
    assert "tags['Environment']" in res["generated_query"]


def test_ask_compound_defers_to_ai(estate):
    """A compound question ('missing Owner AND of virtual machine type') must NOT be parsed as a
    single tag key — it flags needs_ai so the endpoint routes it to the AI NL→ARG path."""
    cen = analysis.census(estate)
    res = ask_mod.answer("resources missing Owner and of virtual machine type", cen, estate)
    assert res.get("needs_ai") is True and res["kind"] == "unknown"


def test_ask_eval_filters_compound(estate):
    """The structured-filter evaluator handles 'missing Owner AND virtual machine type' over the
    cached estate, and the generated KQL reflects both conditions."""
    f = {"missing_all_tags": ["Owner"], "types": ["microsoft.compute/virtualmachines"]}
    rows = ask_mod._eval_filters(f, estate)
    # Every match is a VM that lacks the Owner tag.
    assert rows and all(r["type"] == "microsoft.compute/virtualmachines" for r in rows)
    assert all(not any(k.lower() == "owner" and str(v).strip() for k, v in (r.get("tags") or {}).items()) for r in rows)
    kql = ask_mod._build_kql(f)
    assert "isempty(tostring(tags['Owner']))" in kql and "type =~ 'microsoft.compute/virtualmachines'" in kql


def test_ask_ai_uses_provider_and_builds_query(estate, monkeypatch):
    """answer_ai calls the provider, evaluates the returned structured filter over the estate, and
    returns real rows + a real generated KQL."""
    import asyncio as _aio

    async def _fake_complete(system, user):
        return {"explanation": "VMs without an Owner tag",
                "missing_all_tags": ["Owner"], "types": ["microsoft.compute/virtualmachines"]}

    monkeypatch.setattr(ask_mod, "_complete_json", _fake_complete)
    cen = analysis.census(estate)
    res = _aio.run(ask_mod.answer_ai("resources missing Owner and of virtual machine type", cen, estate))
    assert res is not None and res["kind"] == "ai_query" and res["source"] == "ai"
    assert "isempty(tostring(tags['Owner']))" in res["generated_query"]
    assert "type =~ 'microsoft.compute/virtualmachines'" in res["generated_query"]
    assert isinstance(res["data"], list)


def test_ask_ai_returns_none_when_provider_unavailable(estate, monkeypatch):
    """A provider failure degrades to None so the endpoint falls back to the deterministic answer."""
    import asyncio as _aio

    async def _boom(system, user):
        raise RuntimeError("no provider")

    monkeypatch.setattr(ask_mod, "_complete_json", _boom)
    cen = analysis.census(estate)
    assert _aio.run(ask_mod.answer_ai("anything compound and weird", cen, estate)) is None


# --------------------------------------------------------------------------- F11 rbac
def test_rbac_advice():
    adv = rbac_advice.advice()
    roles = {r["role"] for r in adv["rows"]}
    assert "Tag Contributor" in roles
    assert "Reader" in roles
    assert all(r["role"] != "Owner" for r in adv["rows"])


# --------------------------------------------------------------------------- scale guardrail
def test_scale_cap():
    big = [{"id": str(i)} for i in range(scale.MAX_ESTATE + 100)]
    capped, truncated = scale.cap_estate(big)
    assert len(capped) == scale.MAX_ESTATE
    assert truncated is True
    small = [{"id": "1"}]
    capped, truncated = scale.cap_estate(small)
    assert truncated is False


def test_scale_batches():
    out = list(scale.batches(range(1050), 500))
    assert [len(b) for b in out] == [500, 500, 50]

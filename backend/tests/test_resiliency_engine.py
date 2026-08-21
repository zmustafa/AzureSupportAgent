"""The derivation engine: RTO bands, targets and breach, roll-up, and the join key.

The band tests exist because bands are the part of this module most likely to be quoted as
fact. Each of the six conditions from the plan has a test here; if one is deleted the
guardrail is gone and nothing else notices.
"""
from __future__ import annotations

import pytest

from app.resiliency import join, model, reference, rollup, rto, targets


@pytest.fixture(autouse=True)
def _isolated_reference(tmp_path):
    reference.reset_for_tests(tmp_path)
    yield
    reference.reset_for_tests(tmp_path)


# ============================================================== RTO bands
def test_no_band_for_classes_with_nothing_to_time():
    """`automatic`, `none` and `unknown` have no duration to estimate. A zero-width band
    would imply we measured something."""
    for cls in (model.RTO_AUTOMATIC, model.RTO_NONE, model.RTO_UNKNOWN):
        assert rto.band_for("microsoft.compute/virtualmachines", cls, size_gb=100) is None


def test_a_band_is_always_a_range_never_a_midpoint():
    band, _assumptions, _c = rto.band_for(
        "microsoft.compute/virtualmachines", model.RTO_DAY_PLUS, size_gb=500)
    low, high = band
    assert high > low


def test_a_band_always_carries_its_assumptions():
    _band, assumptions, _c = rto.band_for(
        "microsoft.compute/virtualmachines", model.RTO_DAY_PLUS, size_gb=500)
    assert assumptions
    joined = " ".join(assumptions)
    assert "500 GB" in joined
    assert "restore_rates.vm_restore_mbps" in joined, "the rate that produced it must be named"
    assert "unverified" in joined


def test_unknown_size_widens_the_band_and_drops_confidence():
    """It must never quietly assume a size — the width IS the honesty."""
    known, _a, known_conf = rto.band_for(
        "microsoft.compute/virtualmachines", model.RTO_DAY_PLUS, size_gb=100)
    unknown, assumptions, unknown_conf = rto.band_for(
        "microsoft.compute/virtualmachines", model.RTO_DAY_PLUS, size_gb=None)
    assert (unknown[1] - unknown[0]) > (known[1] - known[0])
    assert unknown_conf == model.CONFIDENCE_LOW
    assert known_conf == model.CONFIDENCE_MEDIUM
    assert any("unknown" in a for a in assumptions)


def test_changing_a_restore_rate_changes_the_band():
    """Proves the registry is actually consulted rather than a constant being inlined."""
    before, _a, _c = rto.band_for("microsoft.compute/virtualmachines",
                                  model.RTO_DAY_PLUS, size_gb=500)
    reference.save({"restore_rates": {"vm_restore_mbps": 5}}, actor="test")
    after, _a2, _c2 = rto.band_for("microsoft.compute/virtualmachines",
                                   model.RTO_DAY_PLUS, size_gb=500)
    assert after[1] > before[1]


def test_apply_bands_leaves_none_and_unknown_untouched():
    verdicts = {
        model.SCENARIO_REGION_LOSS: model.verdict(
            model.SCENARIO_REGION_LOSS, rpo_state=model.RPO_NONE, rto_class=model.RTO_NONE,
            basis=(model.Evidence("x", "y"),)),
        model.SCENARIO_ZONE_LOSS: model.verdict(model.SCENARIO_ZONE_LOSS),
    }
    out = rto.apply_bands(verdicts, resource_type="microsoft.compute/virtualmachines", size_gb=10)
    assert out[model.SCENARIO_REGION_LOSS].rto_band_minutes is None
    assert out[model.SCENARIO_ZONE_LOSS].rto_band_minutes is None


def test_a_band_never_raises_confidence_above_the_verdict_it_describes():
    verdicts = {
        model.SCENARIO_DATA_CORRUPTION: model.verdict(
            model.SCENARIO_DATA_CORRUPTION, rpo_minutes=1440, rpo_state=model.RPO_KNOWN,
            rto_class=model.RTO_DAY_PLUS, confidence=model.CONFIDENCE_LOW,
            basis=(model.Evidence(model.EV_BACKUP_POLICY, "Daily"),)),
    }
    out = rto.apply_bands(verdicts, resource_type="microsoft.compute/disks", size_gb=50)
    assert out[model.SCENARIO_DATA_CORRUPTION].confidence == model.CONFIDENCE_LOW


# ============================================================== targets and breach
def _row(scenario: str, **verdict) -> dict:
    base = {"rto_class": model.RTO_HOURS, "rpo_minutes": 60, "rpo_state": model.RPO_KNOWN,
            "applicable": True, "basis": [{"kind": "x", "detail": "y"}]}
    base.update(verdict)
    return {"id": "/subscriptions/s/rg/r", "name": "r", "type": "microsoft.compute/disks",
            "verdicts": {scenario: base}}


def test_no_recovery_path_always_breaches_whatever_the_target():
    v = {"rto_class": model.RTO_NONE, "rpo_state": model.RPO_NONE, "applicable": True}
    result = targets.evaluate(v, {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 100_000})
    assert result["state"] == targets.STATE_BREACHED
    assert result["rto"] is True and result["rpo"] is True


def test_unknown_counts_as_neither_met_nor_breached():
    """Counting it met hides risk; counting it breached floods the queue that should be
    for things that need fixing rather than investigating."""
    v = {"rto_class": model.RTO_UNKNOWN, "rpo_state": model.RPO_UNKNOWN, "applicable": True}
    result = targets.evaluate(v, {"rto_class": model.RTO_HOURS, "rpo_minutes": 60})
    assert result["state"] == targets.STATE_UNDETERMINED
    assert result["rpo"] is False and result["rto"] is False


def test_not_applicable_is_its_own_state():
    result = targets.evaluate({"applicable": False}, {"rto_class": model.RTO_HOURS})
    assert result["state"] == targets.STATE_NOT_APPLICABLE


def test_meeting_the_objective_is_met():
    v = {"rto_class": model.RTO_MINUTES, "rpo_minutes": 5, "rpo_state": model.RPO_KNOWN,
         "applicable": True}
    result = targets.evaluate(v, {"rto_class": model.RTO_HOURS, "rpo_minutes": 60})
    assert result["state"] == targets.STATE_MET


def test_a_slower_class_than_the_objective_breaches():
    v = {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 10, "rpo_state": model.RPO_KNOWN,
         "applicable": True}
    result = targets.evaluate(v, {"rto_class": model.RTO_HOURS, "rpo_minutes": 60})
    assert result["state"] == targets.STATE_BREACHED
    assert result["rto"] is True and result["rpo"] is False


def test_the_applied_tier_is_reported_on_every_row():
    """'Breached' is not actionable if the reader cannot see what it was measured against."""
    rows = [_row(model.SCENARIO_ZONE_LOSS)]
    rows[0]["tier"] = "mission_critical"
    targets.apply_targets(rows)
    assert rows[0]["tier"] == "mission_critical"
    assert rows[0]["tier_label"]
    assert rows[0]["verdicts"][model.SCENARIO_ZONE_LOSS]["target"]


def test_tier_resolution_follows_precedence_and_says_which_won():
    tier, how = targets.resolve_tier(resource_override="low", workload_criticality="critical")
    assert tier == "low" and "resource" in how
    tier, how = targets.resolve_tier(workload_criticality="critical")
    assert tier == "mission_critical" and "criticality" in how
    tier, how = targets.resolve_tier(tags={"criticality": "high"})
    assert tier == "business_critical" and "tag" in how
    tier, how = targets.resolve_tier()
    assert tier == reference.DEFAULT_TIER and "default" in how


def test_breaches_are_ordered_by_consequence_not_magnitude():
    """A missing recovery path outranks a large numeric miss."""
    no_path = _row(model.SCENARIO_REGION_LOSS, rto_class=model.RTO_NONE,
                   rpo_state=model.RPO_NONE, rpo_minutes=None)
    no_path["name"], no_path["id"], no_path["tier"] = "gone", "/x/gone", "low"
    big_miss = _row(model.SCENARIO_REGION_LOSS, rto_class=model.RTO_DAY_PLUS, rpo_minutes=99_999)
    big_miss["name"], big_miss["id"], big_miss["tier"] = "slow", "/x/slow", "mission_critical"
    rows = [big_miss, no_path]
    targets.apply_targets(rows)
    ordered = targets.breaches(rows)
    assert ordered[0]["name"] == "gone"
    assert ordered[0]["no_recovery_path"] is True


# ============================================================== roll-up
def _component(name: str, scenario: str, rto_class: str, rpo: int | None,
               rpo_state: str = model.RPO_KNOWN) -> dict:
    return {
        "id": f"/subscriptions/s/rg/{name}", "name": name, "type": "microsoft.compute/disks",
        "verdicts": {scenario: {"rto_class": rto_class, "rpo_minutes": rpo,
                                "rpo_state": rpo_state, "applicable": True,
                                "basis": [{"kind": "x", "detail": f"{name} basis"}]}},
    }


def test_the_aggregate_is_the_worst_component_and_names_it():
    scenario = model.SCENARIO_REGION_LOSS
    rows = [_component("fast", scenario, model.RTO_AUTOMATIC, 0),
            _component("slow", scenario, model.RTO_DAY_PLUS, 1440)]
    out = rollup.roll_up(rows, workload_id="w", workload_name="W")
    got = out["scenarios"][scenario]
    assert got["rto_class"] == model.RTO_DAY_PLUS
    assert got["weakest_link"]["name"] == "slow"
    assert got["rpo_minutes"] == 1440


def test_undetermined_components_are_excluded_and_counted():
    """One unmapped resource must not turn an application red, nor let a quarter-measured
    application look fully assessed."""
    scenario = model.SCENARIO_ZONE_LOSS
    rows = [_component("known", scenario, model.RTO_AUTOMATIC, 0),
            _component("unmapped", scenario, model.RTO_UNKNOWN, None, model.RPO_UNKNOWN)]
    got = rollup.roll_up(rows, workload_id="w")["scenarios"][scenario]
    assert got["rto_class"] == model.RTO_AUTOMATIC
    assert got["coverage"] == {"determined": 1, "total": 2}


def test_a_workload_where_nothing_is_measured_is_unknown_not_automatic():
    scenario = model.SCENARIO_ZONE_LOSS
    rows = [_component("a", scenario, model.RTO_UNKNOWN, None, model.RPO_UNKNOWN)]
    got = rollup.roll_up(rows, workload_id="w")["scenarios"][scenario]
    assert got["rto_class"] == model.RTO_UNKNOWN


def test_total_data_loss_on_one_component_dominates_the_workload_rpo():
    scenario = model.SCENARIO_DATA_CORRUPTION
    rows = [_component("ok", scenario, model.RTO_HOURS, 60),
            _component("lost", scenario, model.RTO_NONE, None, model.RPO_NONE)]
    got = rollup.roll_up(rows, workload_id="w")["scenarios"][scenario]
    assert got["rpo_state"] == model.RPO_NONE
    assert got["rto_class"] == model.RTO_NONE
    assert [n["name"] for n in got["no_recovery_path"]] == ["lost"]


def test_the_conservative_assumptions_travel_in_the_payload():
    """An exported figure has to carry the caveats that qualify it, not rely on UI copy."""
    scenario = model.SCENARIO_ZONE_LOSS
    got = rollup.roll_up([_component("a", scenario, model.RTO_HOURS, 10)],
                         workload_id="w")["scenarios"][scenario]
    assert got["assumptions"]
    assert any("required" in a for a in got["assumptions"])
    assert any("ordered" in a.lower() for a in got["assumptions"])


def test_the_headline_picks_the_worst_scenario_and_its_cause():
    rows = [{
        "id": "/x/a", "name": "a", "type": "microsoft.compute/disks",
        "verdicts": {
            model.SCENARIO_ZONE_LOSS: {"rto_class": model.RTO_AUTOMATIC, "rpo_minutes": 0,
                                       "rpo_state": model.RPO_KNOWN, "applicable": True,
                                       "basis": [{"kind": "z", "detail": "zones"}]},
            model.SCENARIO_ACCIDENTAL_DELETE: {"rto_class": model.RTO_NONE, "rpo_minutes": None,
                                               "rpo_state": model.RPO_NONE, "applicable": True,
                                               "basis": [{"kind": "n", "detail": "no backup"}]},
        },
    }]
    out = rollup.roll_up(rows, workload_id="w", workload_name="W")
    assert out["worst"]["scenario"] == model.SCENARIO_ACCIDENTAL_DELETE
    assert out["worst"]["rto_class"] == model.RTO_NONE
    assert out["worst"]["weakest_link"]["name"] == "a"


# ============================================================== the join key
def test_every_eligible_child_suffix_round_trips():
    """The reverse mapping is derived from Backup Manager's own table; a second copy would
    drift and the symptom — protected here, unprotected there — is silent."""
    from app.backup_manager.gaps import ELIGIBLE_TYPES

    base = "/subscriptions/00000000-0000-0000-0000-000000000000/resourcegroups/rg/providers"
    for rtype, spec in ELIGIBLE_TYPES.items():
        suffix = spec.get("child_suffix")
        if not suffix:
            continue
        resource = f"{base}/{rtype}/thing"
        assert join.normalize_resource_id(resource + suffix) == resource


def test_an_unrecognised_id_yields_nothing_rather_than_a_mis_join():
    """A mis-join attributes one resource's protection to another — worse than no join."""
    for raw in ("", "not-an-id", "https://example.com", "/providers/x/y"):
        assert join.normalize_resource_id(raw) == ""


def test_ids_are_case_normalised():
    lower = join.normalize_resource_id(
        "/subscriptions/S/resourceGroups/RG/providers/Microsoft.Compute/disks/D")
    assert lower == lower.lower()
    assert lower.endswith("/disks/d")


# ============================================================== type classification
# Adding a type to the Resource Graph query is the easy half. If it is not ALSO classified
# — stateful or not, global or not, self-healing or not — it produces `unknown` everywhere
# or, worse, a confident finding about a risk it does not carry. These two tests turn that
# from a thing you have to remember into a thing you cannot skip.
def _probe_row(rtype: str, props: dict | None = None) -> dict:
    return {
        "id": f"/subscriptions/11111111-1111-1111-1111-111111111111/rg/x/{rtype}",
        "name": "probe", "type": rtype, "location": "westeurope",
        "resourceGroup": "rg", "subscriptionId": "sub", "zones": [],
        "skuName": "Standard_LRS", "skuTier": "Standard", "props": props or {},
    }


def test_every_supported_type_is_classified_somewhere():
    """A type the engine collects but cannot reason about adds noise, not coverage: every
    verdict is `unknown` and the Undetermined KPI rises for no reason the reader can act
    on."""
    from app.resiliency import collect, derive

    unclassified = []
    for rtype in collect.SUPPORTED_TYPES:
        shaped = collect.shape(_probe_row(rtype))
        says_something = (
            shaped["zone_redundant"] is not None
            or str((shaped.get("native_backup") or {}).get("kind") or "unknown") != "unknown"
            or shaped.get("soft_delete") is not None
            or rtype in model.STATELESS_TYPES
            or rtype in model.GLOBAL_TYPES
            or rtype in derive._SELF_HEALING_TYPES
        )
        if not says_something:
            unclassified.append(rtype)
    assert not unclassified, (
        "collected but unclassified — add to a shape() branch, STATELESS_TYPES, "
        f"GLOBAL_TYPES or _SELF_HEALING_TYPES: {unclassified}")


def test_a_type_that_holds_no_data_never_reports_no_recovery_path_for_logical_loss():
    """The false-finding class. A load balancer has nothing to corrupt, so a red
    "no recovery path" against it is noise that buries the resources that do."""
    from app.resiliency import collect, derive

    for rtype in sorted(model.STATELESS_TYPES):
        shaped = collect.shape(_probe_row(rtype))
        verdicts = derive.verdicts_for(shaped)
        for scenario in model.LOGICAL_SCENARIOS:
            v = verdicts[scenario]
            assert not v.applicable, f"{rtype}/{scenario} should not apply"
            assert v.rto_class != model.RTO_NONE, (
                f"{rtype} reports no recovery path for {scenario}"
            )


# ============================================================== Key Vault
KV = "microsoft.keyvault/vaults"


def _kv(**props):
    from app.resiliency import collect

    return collect.shape(_probe_row(KV, props))


def test_a_key_vault_without_soft_delete_has_no_recovery_from_deletion():
    """It was classified stateless, so deletion read "redeployed rather than restored" — but
    a purged vault takes every key with it, and those keys are what other resources are
    encrypted with. Rendering that as not-applicable hid a real, unrecoverable risk."""
    from app.resiliency import derive

    v = derive.verdicts_for(_kv(enableSoftDelete=False))[model.SCENARIO_ACCIDENTAL_DELETE]
    assert v.applicable
    assert v.rto_class == model.RTO_NONE
    assert v.rpo_state == model.RPO_NONE


def test_soft_delete_is_the_control_that_makes_a_key_vault_recoverable():
    from app.resiliency import derive

    v = derive.verdicts_for(_kv(enableSoftDelete=True,
                                softDeleteRetentionInDays=90))[model.SCENARIO_ACCIDENTAL_DELETE]
    assert v.applicable
    assert v.rto_class != model.RTO_NONE
    assert any(e.kind == model.EV_SOFT_DELETE for e in v.basis)


def test_key_vault_corruption_is_answered_by_versioning_not_by_a_backup():
    """Overwriting a secret keeps the previous version, so corruption is not a risk this
    type carries. Saying "no recovery path" here would be a false finding."""
    from app.resiliency import derive

    v = derive.verdicts_for(_kv(enableSoftDelete=True))[model.SCENARIO_DATA_CORRUPTION]
    assert not v.applicable
    assert "previous version" in v.basis[0].detail


# ============================================================== Redis persistence
REDIS = "microsoft.cache/redis"


def _redis(**config):
    from app.resiliency import collect

    return collect.shape(_probe_row(REDIS, {"redisConfiguration": config}))


def test_a_redis_cache_without_persistence_stays_stateless():
    """Non-vacuous guard for the override: the default must still hold."""
    from app.resiliency import derive

    shaped = _redis()
    assert not shaped["holds_data"]
    for scenario in model.LOGICAL_SCENARIOS:
        assert not derive.verdicts_for(shaped)[scenario].applicable


def test_a_redis_with_persistence_is_a_data_store_and_carries_logical_risk():
    """The type alone cannot tell a cache from a data store; the configuration can. Treating
    every Redis as a cache means a persisted one is never asked the question."""
    from app.resiliency import derive

    shaped = _redis(**{"rdb-backup-enabled": "true", "rdb-backup-frequency": "60"})
    assert shaped["holds_data"] is True
    v = derive.verdicts_for(shaped)[model.SCENARIO_DATA_CORRUPTION]
    assert v.applicable
    assert v.rpo_minutes == 60
    assert "RDB persistence" in v.basis[0].detail


def test_append_only_persistence_also_counts_as_durable_state():
    from app.resiliency import derive

    shaped = _redis(**{"aof-backup-enabled": "true"})
    assert shaped["holds_data"] is True
    assert derive.verdicts_for(shaped)[model.SCENARIO_DATA_CORRUPTION].applicable


def test_configuration_can_only_add_a_scenario_never_remove_one():
    """`applies` takes the resource so a default can be overridden upward. It must not be a
    way for a resource to opt OUT of a question its type has to answer."""
    ok, _why = model.applies("microsoft.compute/virtualmachines",
                             model.SCENARIO_DATA_CORRUPTION, {"holds_data": False})
    assert ok is True


# ====================================================== Azure Managed Redis (redisEnterprise)
AMR = "microsoft.cache/redisenterprise"


def _amr(props, zones=()):
    from app.resiliency import collect

    row = _probe_row(AMR, props)
    row["zones"] = list(zones)
    return collect.shape(row)


def test_managed_redis_reads_zone_redundancy_from_redundancy_mode_not_from_zones():
    """Verified against a live cluster: Managed Redis returns `zones: []` even when the
    cluster IS zone redundant, and reports it as `redundancyMode: "ZR"` instead. Trusting
    `zones` calls a zone-redundant cluster single-zone — a false breach on a healthy
    resource, which is the mirror image of the failure this module exists to prevent."""
    assert _amr({"redundancyMode": "ZR"})["zone_redundant"] is True
    assert _amr({"redundancyMode": "LR"})["zone_redundant"] is False


def test_managed_redis_persistence_is_unknown_rather_than_assumed_either_way():
    """Persistence is configured on the child `databases` resource, and Resource Graph does
    not index that type at all (verified: a database with `rdbEnabled` returns Count 0). So
    the cluster row cannot tell a cache from a data store. It must not guess: claiming
    "no recovery path" libels a persisted database, and claiming statelessness hides that
    the data is unprotected."""
    from app.resiliency import derive

    shaped = _amr({"redundancyMode": "ZR"})
    assert shaped["native_backup"]["kind"] == "unknown"
    for scenario in model.LOGICAL_SCENARIOS:
        v = derive.verdicts_for(shaped)[scenario]
        assert v.applicable, f"{scenario} must still be asked"
        assert v.rpo_state == model.RPO_UNKNOWN, f"{scenario} must not claim an answer"
        assert v.rto_class != model.RTO_NONE, f"{scenario} must not claim no recovery path"


def test_managed_redis_is_not_stateless_so_deletion_is_never_reported_as_redeployable():
    """Non-vacuous guard: if the type were added to STATELESS_TYPES the verdict above would
    silently become "redeployed rather than restored" on a persisted database."""
    ok, _why = model.applies(AMR, model.SCENARIO_ACCIDENTAL_DELETE, {})
    assert ok is True


# ============================================================== newly covered types
def test_every_supported_type_has_a_display_name():
    """The fallback pluralises the last ARM segment, so a missing entry ships "Hostpools" to
    the screen and the report."""
    from app.resiliency import collect
    from app.workloads.summarize import _FRIENDLY

    missing = [t for t in collect.SUPPORTED_TYPES if t not in _FRIENDLY]
    assert not missing, f"no friendly name for: {missing}"


def test_a_scale_set_without_data_disks_is_redeployed_not_restored():
    from app.resiliency import collect, derive

    shaped = collect.shape(_probe_row("microsoft.compute/virtualmachinescalesets"))
    assert not shaped["holds_data"]
    assert not derive.verdicts_for(shaped)[model.SCENARIO_ACCIDENTAL_DELETE].applicable


def test_a_scale_set_with_data_disks_carries_logical_risk():
    """Attached data disks are the difference between a web tier you redeploy and state you
    have to restore. Treating every scale set as stateless never asks the question."""
    from app.resiliency import collect, derive

    shaped = collect.shape(_probe_row(
        "microsoft.compute/virtualmachinescalesets",
        {"virtualMachineProfile": {"storageProfile": {"dataDisks": [{"lun": 0}]}}}))
    assert shaped["holds_data"] is True
    assert derive.verdicts_for(shaped)[model.SCENARIO_ACCIDENTAL_DELETE].applicable


def test_a_locally_redundant_vault_cannot_answer_a_region_loss():
    """The estate's last line. A vault whose storage never leaves the region takes every
    recovery point with it, which is exactly the failure the backups existed for."""
    from app.resiliency import collect, derive

    local = collect.shape(_probe_row("microsoft.recoveryservices/vaults", {
        "redundancySettings": {"standardTierStorageRedundancy": "LocallyRedundant"}}))
    geo = collect.shape(_probe_row("microsoft.recoveryservices/vaults", {
        "redundancySettings": {"standardTierStorageRedundancy": "GeoRedundant"}}))
    assert local["replication"] == "LRS"
    assert geo["replication"] == "GRS"

    region = model.SCENARIO_REGION_LOSS
    assert derive.verdicts_for(geo)[region].rto_class != \
        derive.verdicts_for(local)[region].rto_class


def test_a_vault_is_asked_about_deletion_but_not_corruption():
    from app.resiliency import collect, derive

    shaped = collect.shape(_probe_row("microsoft.recoveryservices/vaults", {
        "securitySettings": {"softDeleteSettings": {"softDeleteState": "Enabled"}}}))
    verdicts = derive.verdicts_for(shaped)
    assert not verdicts[model.SCENARIO_DATA_CORRUPTION].applicable
    assert verdicts[model.SCENARIO_ACCIDENTAL_DELETE].applicable
    assert any(e.kind == model.EV_SOFT_DELETE
               for e in verdicts[model.SCENARIO_ACCIDENTAL_DELETE].basis)


def test_a_managed_instance_is_credited_with_point_in_time_restore():
    from app.resiliency import collect, derive

    shaped = collect.shape(_probe_row("microsoft.sql/managedinstances", {
        "zoneRedundant": True, "backupRetentionDays": 14, "storageSizeInGB": 256}))
    assert shaped["zone_redundant"] is True
    assert shaped["size_gb"] == 256
    v = derive.verdicts_for(shaped)[model.SCENARIO_DATA_CORRUPTION]
    assert v.applicable and v.rto_class != model.RTO_NONE


def test_a_netapp_volume_without_a_snapshot_policy_has_no_recovery_path():
    from app.resiliency import collect, derive

    bare = collect.shape(_probe_row("microsoft.netapp/netappaccounts/capacitypools/volumes"))
    protected = collect.shape(_probe_row(
        "microsoft.netapp/netappaccounts/capacitypools/volumes",
        {"dataProtection": {"snapshot": {"snapshotPolicyId": "/subscriptions/x/policy"}}}))
    corruption = model.SCENARIO_DATA_CORRUPTION
    assert derive.verdicts_for(bare)[corruption].rto_class == model.RTO_NONE
    assert derive.verdicts_for(protected)[corruption].rto_class != model.RTO_NONE


def test_a_static_web_app_has_no_zone_to_lose():
    from app.resiliency import collect, derive

    verdicts = derive.verdicts_for(collect.shape(_probe_row("microsoft.web/staticsites")))
    assert not verdicts[model.SCENARIO_ZONE_LOSS].applicable
    assert not verdicts[model.SCENARIO_INSTANCE_LOSS].applicable


def test_a_registry_reports_its_zone_redundancy_and_soft_delete():
    from app.resiliency import collect

    shaped = collect.shape(_probe_row("microsoft.containerregistry/registries", {
        "zoneRedundancy": "Enabled",
        "policies": {"softDeletePolicy": {"status": "enabled"}}}))
    assert shaped["zone_redundant"] is True
    assert shaped["soft_delete"] is True


def test_an_unreadable_property_degrades_to_unknown_rather_than_a_false_claim():
    """Every new branch reads documented property paths. If a path is wrong or absent the
    answer must be `unknown` — never a confident `False` that reads as a finding."""
    from app.resiliency import collect

    for rtype in ("microsoft.apimanagement/service", "microsoft.eventhub/namespaces",
                  "microsoft.network/azurefirewalls"):
        shaped = collect.shape(_probe_row(rtype))
        assert shaped["zone_redundant"] is None, rtype

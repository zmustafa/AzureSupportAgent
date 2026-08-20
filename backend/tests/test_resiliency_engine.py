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

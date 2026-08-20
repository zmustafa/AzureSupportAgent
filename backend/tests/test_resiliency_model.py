"""The scenario model — the primitive every other part of Recovery Readiness projects.

The properties worth protecting are the ones that separate an honest verdict from a
plausible lie: an unmeasured value must not win or lose an aggregate silently, a scenario a
resource cannot experience must not render as a pass, and redundancy must never be credited
against logical loss.
"""
from __future__ import annotations

import pytest

from app.resiliency import model as m


# --------------------------------------------------------------------------- scenarios
def test_five_scenarios_and_they_are_ordered_infrastructure_then_logical():
    assert m.SCENARIOS == (
        m.SCENARIO_INSTANCE_LOSS, m.SCENARIO_ZONE_LOSS, m.SCENARIO_REGION_LOSS,
        m.SCENARIO_DATA_CORRUPTION, m.SCENARIO_ACCIDENTAL_DELETE,
    )
    assert m.LOGICAL_SCENARIOS == {m.SCENARIO_DATA_CORRUPTION, m.SCENARIO_ACCIDENTAL_DELETE}


def test_every_scenario_has_a_label_and_a_description():
    for s in m.SCENARIOS:
        assert m.SCENARIO_LABEL.get(s)
        assert m.SCENARIO_DESCRIPTION.get(s)


# ------------------------------------------------------------- the rule that carries it
def test_redundancy_is_not_a_control_for_logical_loss():
    """ZRS/GRS/multi-region replicate corruption and deletion, usually within seconds. A
    resource can be flawlessly redundant and have no recovery path at all."""
    assert m.redundancy_helps(m.SCENARIO_ZONE_LOSS) is True
    assert m.redundancy_helps(m.SCENARIO_REGION_LOSS) is True
    assert m.redundancy_helps(m.SCENARIO_INSTANCE_LOSS) is True
    assert m.redundancy_helps(m.SCENARIO_DATA_CORRUPTION) is False
    assert m.redundancy_helps(m.SCENARIO_ACCIDENTAL_DELETE) is False


# --------------------------------------------------------------------------- rto ranking
def test_rto_rank_is_worst_first_and_unknown_is_not_on_the_scale():
    assert m.rto_rank(m.RTO_NONE) < m.rto_rank(m.RTO_DAY_PLUS) < m.rto_rank(m.RTO_HOURS)
    assert m.rto_rank(m.RTO_HOURS) < m.rto_rank(m.RTO_MINUTES) < m.rto_rank(m.RTO_AUTOMATIC)
    # Placing `unknown` on the scale would make one unmeasured component silently win or
    # lose every aggregate. The caller has to decide instead.
    assert m.rto_rank(m.RTO_UNKNOWN) is None


def test_worst_rto_picks_the_worst_determined_and_counts_the_rest():
    got, undetermined = m.worst_rto([m.RTO_AUTOMATIC, m.RTO_DAY_PLUS, m.RTO_UNKNOWN])
    assert got == m.RTO_DAY_PLUS
    assert undetermined == 1


def test_worst_rto_of_nothing_measured_is_unknown_not_automatic():
    """The aggregate of nothing measured is not a clean bill of health."""
    got, undetermined = m.worst_rto([m.RTO_UNKNOWN, m.RTO_UNKNOWN])
    assert got == m.RTO_UNKNOWN
    assert undetermined == 2


def test_none_beats_everything_in_an_aggregate():
    got, _ = m.worst_rto([m.RTO_AUTOMATIC, m.RTO_NONE, m.RTO_MINUTES])
    assert got == m.RTO_NONE


# --------------------------------------------------------------------------- confidence
def test_a_composed_verdict_is_only_as_good_as_its_weakest_input():
    assert m.weakest_confidence([m.CONFIDENCE_HIGH, m.CONFIDENCE_LOW]) == m.CONFIDENCE_LOW
    assert m.weakest_confidence([m.CONFIDENCE_HIGH, m.CONFIDENCE_MEDIUM]) == m.CONFIDENCE_MEDIUM
    assert m.weakest_confidence([]) == m.CONFIDENCE_LOW


# --------------------------------------------------------------------------- invariants
def test_an_rpo_in_minutes_only_survives_when_the_state_says_known():
    """`None` alone cannot distinguish 'no recovery point' from 'we could not tell'."""
    v = m.verdict("zone_loss", rpo_minutes=60, rpo_state=m.RPO_NONE,
                  rto_class=m.RTO_HOURS, basis=(m.Evidence("sku", "x"),))
    assert v.rpo_minutes is None
    assert v.rpo_state == m.RPO_NONE


def test_a_known_state_without_a_number_falls_back_to_unknown():
    v = m.verdict("zone_loss", rpo_minutes=None, rpo_state=m.RPO_KNOWN,
                  rto_class=m.RTO_HOURS, basis=(m.Evidence("sku", "x"),))
    assert v.rpo_state == m.RPO_UNKNOWN


def test_a_verdict_with_no_evidence_can_never_be_confident():
    """If we cannot say why, we say unknown. A confident answer with no basis is an
    opinion wearing a number."""
    v = m.verdict("zone_loss", rpo_minutes=5, rpo_state=m.RPO_KNOWN, rto_class=m.RTO_AUTOMATIC,
                  confidence=m.CONFIDENCE_HIGH, basis=())
    assert v.rto_class == m.RTO_UNKNOWN
    assert v.rpo_state == m.RPO_UNKNOWN
    assert v.confidence == m.CONFIDENCE_LOW


def test_evidence_keeps_a_verdict_intact():
    v = m.verdict("zone_loss", rpo_minutes=0, rpo_state=m.RPO_KNOWN, rto_class=m.RTO_AUTOMATIC,
                  confidence=m.CONFIDENCE_HIGH,
                  basis=(m.Evidence(m.EV_ZONE_CONFIG, "3 zones", "Resource Graph"),))
    assert v.rto_class == m.RTO_AUTOMATIC
    assert v.rpo_minutes == 0
    assert v.confidence == m.CONFIDENCE_HIGH


# --------------------------------------------------------------------------- applicability
def test_a_stateless_resource_has_no_logical_loss_and_it_is_explained():
    ok, why = m.applies("microsoft.web/sites", m.SCENARIO_DATA_CORRUPTION)
    assert ok is False
    assert "holds no durable data" in why


def test_a_global_service_has_no_zone_to_lose():
    ok, why = m.applies("microsoft.cdn/profiles", m.SCENARIO_ZONE_LOSS)
    assert ok is False
    assert "global service" in why


def test_a_database_experiences_every_scenario():
    for scenario in m.SCENARIOS:
        ok, _ = m.applies("microsoft.sql/servers/databases", scenario)
        assert ok is True, scenario


def test_not_applicable_is_marked_absent_rather_than_passing():
    """Rendering it green would imply a protection the resource does not have."""
    v = m.not_applicable(m.SCENARIO_DATA_CORRUPTION, "stateless")
    assert v.applicable is False
    assert v.rto_class == m.RTO_UNKNOWN
    assert v.rpo_state == m.RPO_UNKNOWN


@pytest.mark.parametrize("scenario", m.SCENARIOS)
def test_every_scenario_is_either_applicable_or_explained(scenario):
    for rtype in ("microsoft.compute/virtualmachines", "microsoft.web/sites",
                  "microsoft.cdn/profiles", "microsoft.storage/storageaccounts"):
        ok, why = m.applies(rtype, scenario)
        assert ok or why, f"{rtype}/{scenario} is inapplicable with no explanation"


# --------------------------------------------------------------------------- serialisation
def test_a_serialised_verdict_always_carries_its_state_and_basis():
    v = m.verdict("region_loss", rpo_minutes=15, rpo_state=m.RPO_KNOWN, rto_class=m.RTO_HOURS,
                  basis=(m.Evidence(m.EV_SKU, "Standard_GRS", "Resource Graph"),),
                  confidence=m.CONFIDENCE_LOW)
    d = v.as_dict()
    assert d["rpo_state"] == m.RPO_KNOWN
    assert d["basis"][0]["detail"] == "Standard_GRS"
    assert d["rto_band_minutes"] is None
    assert set(d) >= {"scenario", "rpo_minutes", "rpo_state", "rto_class", "basis",
                      "confidence", "applicable"}

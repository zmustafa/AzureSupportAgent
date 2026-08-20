"""Recovery Readiness → Reliability pillar contribution."""
from __future__ import annotations

from app.resiliency import assessment, model


def _row(rid: str, verdicts: dict, *, zone_redundant=None, replication=""):
    return {
        "id": rid, "name": rid.rsplit("/", 1)[-1], "type": "microsoft.compute/virtualmachines",
        "location": "westeurope", "resource_group": "rg", "subscription_id": "sub",
        "redundancy": {"zones": [], "zone_redundant": zone_redundant,
                       "replication": replication, "sku": ""},
        "verdicts": verdicts,
    }


def _verdict(rto_class, *, applicable=True, breach_state=None, detail="because"):
    v = {"rto_class": rto_class, "applicable": applicable,
         "basis": [{"detail": detail}]}
    if breach_state:
        v["breach"] = {"state": breach_state}
    return v


def test_checks_are_reliability_pillar_and_weighted():
    checks = assessment.checks()
    assert {c["id"] for c in checks} == {
        "recovery.no_path", "recovery.logical_unprotected", "recovery.target_breach"}
    assert all(c["pillar"] == "reliability" for c in checks)
    # no_path is the worst outcome there is, so it must outweigh a target miss.
    by_id = {c["id"]: c for c in checks}
    assert by_id["recovery.no_path"]["weight"] > by_id["recovery.target_breach"]["weight"]


def test_no_snapshot_contributes_nothing_to_the_score():
    """Absent analysis must not be reported as the tenant's risk."""
    for snap in (None, {}, {"report_exists": False}):
        out = assessment.findings_from(snap)
        assert len(out) == 3
        assert {f["status"] for f in out} == {"not_applicable"}
        assert all(f["flagged_count"] == 0 for f in out)
        assert all("has not analyzed" in f["ai_rationale"] for f in out)


def test_no_recovery_path_is_flagged_once_per_control_not_per_resource():
    rows = [
        _row(f"/subscriptions/s/rg/r{i}",
             {"region_loss": _verdict(model.RTO_NONE),
              "data_corruption": _verdict(model.RTO_NONE)})
        for i in range(5)
    ]
    out = {f["check_id"]: f for f in assessment.findings_from(
        {"report_exists": True, "resources": rows})}
    no_path = out["recovery.no_path"]
    assert no_path["status"] == "fail"
    # 5 resources x 2 scenarios, but ONE finding.
    assert no_path["flagged_count"] == 10
    assert len(no_path["flagged_resources"]) == 10
    assert no_path["partial"] is False


def test_sample_is_bounded_and_marked_partial():
    rows = [_row(f"/s/r{i}", {"region_loss": _verdict(model.RTO_NONE)}) for i in range(60)]
    out = {f["check_id"]: f for f in assessment.findings_from(
        {"report_exists": True, "resources": rows})}
    no_path = out["recovery.no_path"]
    assert no_path["flagged_count"] == 60
    assert len(no_path["flagged_resources"]) == assessment.MAX_SAMPLE
    assert no_path["partial"] is True


def test_redundant_but_no_pitr_is_its_own_control():
    """Redundancy replicates corruption — it must not be mistaken for protection."""
    rows = [
        # Redundant, but nothing recovers it from corruption/deletion.
        _row("/s/redundant", {
            "region_loss": _verdict(model.RTO_MINUTES),
            "data_corruption": _verdict(model.RTO_NONE),
            "accidental_delete": _verdict(model.RTO_NONE),
        }, zone_redundant=True),
        # Not redundant and not backed up: no_path, but NOT the logical control.
        _row("/s/bare", {"data_corruption": _verdict(model.RTO_NONE)}),
    ]
    out = {f["check_id"]: f for f in assessment.findings_from(
        {"report_exists": True, "resources": rows})}
    logical = out["recovery.logical_unprotected"]
    assert logical["flagged_count"] == 2  # corruption + delete on the redundant one only
    assert all("/s/redundant" == s["id"] for s in logical["flagged_resources"])


def test_infrastructure_scenarios_never_land_in_the_logical_control():
    rows = [_row("/s/x", {"region_loss": _verdict(model.RTO_NONE)}, replication="GRS")]
    out = {f["check_id"]: f for f in assessment.findings_from(
        {"report_exists": True, "resources": rows})}
    assert out["recovery.no_path"]["flagged_count"] == 1
    assert out["recovery.logical_unprotected"]["flagged_count"] == 0


def test_breach_is_separate_from_no_path():
    """A resource with no path is already counted; do not double-charge it as a breach."""
    rows = [
        _row("/s/none", {"region_loss": _verdict(model.RTO_NONE, breach_state="breached")}),
        _row("/s/slow", {"region_loss": _verdict(model.RTO_DAY_PLUS, breach_state="breached")}),
        _row("/s/ok", {"region_loss": _verdict(model.RTO_MINUTES, breach_state="met")}),
    ]
    out = {f["check_id"]: f for f in assessment.findings_from(
        {"report_exists": True, "resources": rows})}
    assert out["recovery.no_path"]["flagged_count"] == 1
    breach = out["recovery.target_breach"]
    assert breach["flagged_count"] == 1
    assert breach["flagged_resources"][0]["id"] == "/s/slow"


def test_inapplicable_scenarios_are_ignored():
    rows = [_row("/s/global", {"region_loss": _verdict(model.RTO_NONE, applicable=False)})]
    out = {f["check_id"]: f for f in assessment.findings_from(
        {"report_exists": True, "resources": rows})}
    assert all(f["status"] == "pass" for f in out.values())


def test_clean_estate_passes_rather_than_disappearing():
    rows = [_row("/s/ok", {"region_loss": _verdict(model.RTO_MINUTES, breach_state="met")})]
    out = assessment.findings_from({"report_exists": True, "resources": rows})
    assert {f["status"] for f in out} == {"pass"}


def test_subject_detail_names_the_scenario():
    rows = [_row("/s/x", {"data_corruption": _verdict(model.RTO_NONE, detail="no backup")})]
    out = {f["check_id"]: f for f in assessment.findings_from(
        {"report_exists": True, "resources": rows})}
    detail = out["recovery.no_path"]["flagged_resources"][0]["detail"]
    assert model.SCENARIO_LABEL["data_corruption"] in detail
    assert "no backup" in detail


def test_contribution_is_off_by_default():
    """Enabling it moves an existing tenant's score — it must be opt-in."""
    assert assessment.enabled({}) is False
    assert assessment.enabled({"assessments_include_recovery": False}) is False
    assert assessment.enabled({"assessments_include_recovery": True}) is True


def test_default_settings_ship_the_flag_off():
    from app.core.app_settings import load_settings

    assert load_settings().get("assessments_include_recovery") is False


def test_contribution_actually_moves_the_reliability_score():
    """A contribution that cannot change the number is decoration, not a control."""
    from app.assessments.runner import _scored

    base_checks = [{"id": "x", "pillar": "reliability", "severity": "warning", "weight": 3}]
    base_findings = [{"check_id": "x", "pillar": "reliability", "severity": "warning",
                      "weight": 3, "status": "pass", "kind": "graph"}]
    before = _scored(base_checks, base_findings)["scores"]["reliability"]["score"]

    rows = [_row("/s/x", {"region_loss": _verdict(model.RTO_NONE)})]
    contributed = assessment.findings_from({"report_exists": True, "resources": rows})
    after = _scored(base_checks + assessment.checks(),
                    base_findings + contributed)["scores"]["reliability"]["score"]

    assert before == 100
    assert after < before


def test_unanalysed_scope_leaves_the_score_untouched():
    from app.assessments.runner import _scored

    base_checks = [{"id": "x", "pillar": "reliability", "severity": "warning", "weight": 3}]
    base_findings = [{"check_id": "x", "pillar": "reliability", "severity": "warning",
                      "weight": 3, "status": "pass", "kind": "graph"}]
    before = _scored(base_checks, base_findings)["scores"]["reliability"]["score"]
    after = _scored(base_checks + assessment.checks(),
                    base_findings + assessment.findings_from(None),
                    )["scores"]["reliability"]["score"]
    assert after == before


def test_admin_settings_accepts_the_flag():
    from app.api.admin import AppSettingsUpdate

    payload = AppSettingsUpdate(assessments_include_recovery=True)
    assert payload.model_dump(exclude_none=True) == {"assessments_include_recovery": True}

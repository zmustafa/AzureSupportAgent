"""The demo estate, through the real pipeline.

These assert SPECIFIC verdicts rather than "some data came back". If they ever change, the
demo narrative has silently broken, and the alternative to catching it here is finding out
in front of an audience.

Everything runs through `collect_demo -> build_rows -> derive`, the same path a live tenant
takes. A demo that diverged from production would hide exactly the bugs it should catch.
"""
from __future__ import annotations

import pytest

from app import demo_catalog
from app.resiliency import collect, join, model

CONTOSO = demo_catalog.CONTOSO_ID
ZAVA_WEB = demo_catalog.ZAVA_WEB_ID
ZAVA_CRM = demo_catalog.ZAVA_CRM_ID


def rows_for(scope_id: str, *, backup_known: bool = True) -> dict[str, dict]:
    config, _meta = collect.collect_demo(scope_id)
    rows = join.build_rows(
        config,
        backup=demo_catalog.resiliency_backup_for(scope_id),
        asr=demo_catalog.resiliency_asr_for(scope_id),
        backup_known=backup_known,
    )
    return {r["name"]: r for r in rows}


def verdict(row: dict, scenario: str) -> dict:
    return row["verdicts"][scenario]


# ============================================================== the money row
def test_a_zone_perfect_resource_can_still_have_no_path_from_corruption():
    """The product's whole thesis, on one resource. Cosmos with multi-region writes and
    zone redundancy is reported resilient by every other tool; its periodic backup means a
    bad deployment costs 24 hours."""
    row = rows_for(CONTOSO)["contoso-guests-cosmos"]

    zone = verdict(row, model.SCENARIO_ZONE_LOSS)
    assert zone["rto_class"] == model.RTO_AUTOMATIC
    assert zone["rpo_minutes"] == 0

    region = verdict(row, model.SCENARIO_REGION_LOSS)
    assert region["rto_class"] == model.RTO_AUTOMATIC

    corruption = verdict(row, model.SCENARIO_DATA_CORRUPTION)
    assert corruption["rpo_minutes"] == 1440, "periodic backup every 1440 minutes"
    assert corruption["rto_class"] == model.RTO_DAY_PLUS
    assert corruption["basis"], "a verdict must always say why"


def test_redundancy_is_never_credited_against_logical_loss():
    """Across the whole demo estate: no resource may earn a corruption or deletion verdict
    from a zone or replication fact."""
    for scope in (CONTOSO, ZAVA_WEB, ZAVA_CRM):
        for name, row in rows_for(scope).items():
            for scenario in model.LOGICAL_SCENARIOS:
                v = verdict(row, scenario)
                if not v["applicable"]:
                    continue
                kinds = {e["kind"] for e in v["basis"]}
                assert model.EV_ZONE_CONFIG not in kinds, f"{name}/{scenario}"
                assert model.EV_REPLICATION not in kinds, f"{name}/{scenario}"


# ============================================================== no recovery path
def test_the_legacy_vm_has_no_recovery_path_at_all():
    """Not slow — none. This is the finding nobody currently gets."""
    row = rows_for(CONTOSO)["contoso-pms-vm"]
    for scenario in model.SCENARIOS:
        v = verdict(row, scenario)
        if v["applicable"]:
            assert v["rto_class"] == model.RTO_NONE, scenario
    assert row["protection"]["state"] == join.PROTECTION_NOT_PROTECTED
    assert row["worst"]["rto_class"] == model.RTO_NONE


def test_a_backed_up_database_can_still_be_lost_with_its_region():
    """Protected, and the backups are in a locally-redundant vault. A coverage detector
    reports this resource as green."""
    row = rows_for(ZAVA_WEB)["zava-web-sql/catalog"]
    assert row["protection"]["state"] == join.PROTECTION_PROTECTED

    region = verdict(row, model.SCENARIO_REGION_LOSS)
    assert region["rto_class"] == model.RTO_NONE
    assert any("locally-redundant" in e["detail"] for e in region["basis"])

    # ...while still being recoverable from corruption.
    corruption = verdict(row, model.SCENARIO_DATA_CORRUPTION)
    assert corruption["rto_class"] != model.RTO_NONE


# ============================================================== unknown is not a verdict
def test_an_unmappable_resource_is_unknown_and_never_unprotected():
    """`unknown` and `not protected` are opposite facts. Conflating them produces a
    full-estate false alarm."""
    row = rows_for(CONTOSO)["contoso-redis"]
    assert row["protection"]["state"] == join.PROTECTION_UNKNOWN
    assert row["protection"]["reason"]
    for scenario in model.SCENARIOS:
        v = verdict(row, scenario)
        if v["applicable"]:
            assert v["rto_class"] != model.RTO_NONE, (
                f"{scenario}: an unmapped resource must not be declared unrecoverable")


def test_a_scope_with_no_backup_analysis_reports_unknown_not_unprotected():
    """Backup Manager's snapshot is user-triggered. Absent analysis is absent knowledge."""
    rows = rows_for(ZAVA_CRM, backup_known=False)
    states = {r["protection"]["state"] for r in rows.values()}
    assert states == {join.PROTECTION_UNKNOWN}
    assert all("has not analyzed" in r["protection"]["reason"] for r in rows.values())
    for row in rows.values():
        assert row["protection"]["policy_name"] == ""


# ============================================================== measured vs derived
def test_replication_gives_a_measured_thirty_second_rpo():
    row = rows_for(ZAVA_CRM)["zava-crm-vm01"]
    region = verdict(row, model.SCENARIO_REGION_LOSS)
    assert region["rpo_minutes"] == 1
    assert row["dr"]["rpo_seconds"] == 30
    assert region["confidence"] == model.CONFIDENCE_HIGH


def test_two_vms_in_one_application_can_differ_by_three_orders_of_magnitude():
    rows = rows_for(ZAVA_CRM)
    replicated = verdict(rows["zava-crm-vm01"], model.SCENARIO_REGION_LOSS)
    ordinary = verdict(rows["zava-crm-vm02"], model.SCENARIO_REGION_LOSS)
    assert replicated["rpo_minutes"] == 1
    assert ordinary["rpo_minutes"] == 1440


# ============================================================== the frequency column
def test_backup_frequency_is_reported_for_both_vault_and_platform_backup():
    contoso = rows_for(CONTOSO)
    assert "Daily" in contoso["contoso-aks"]["protection"]["frequency"]
    # Platform-native backup is invisible to a vault-centric view; it must still show.
    assert contoso["contoso-guests-cosmos"]["protection"]["frequency"] == "Every 1d (platform)"


def test_a_windowed_hourly_policy_reports_its_real_worst_gap():
    row = rows_for(ZAVA_WEB)["zava-web-sql/catalog"]
    assert "worst gap" in row["protection"]["frequency"]


# ============================================================== coverage of the axis
def test_every_profile_appears_somewhere_in_the_demo_estate():
    seen = set()
    for scope in (CONTOSO, ZAVA_WEB, ZAVA_CRM):
        for item in demo_catalog.resiliency_for(scope):
            seen.add(item["demo_profile"])
    assert seen == set(demo_catalog.PROFILE_STORY), sorted(set(demo_catalog.PROFILE_STORY) - seen)


def test_the_resiliency_axis_is_orthogonal_to_the_health_tier():
    """If profile were derived from tier the demo would be one-dimensional and would fail
    to show the thing the product exists to show."""
    pairs = set()
    for scope in (CONTOSO, ZAVA_WEB, ZAVA_CRM):
        tiers = demo_catalog.tier_index(scope)
        for item in demo_catalog.resiliency_for(scope):
            pairs.add((tiers.get(item["id"]), item["demo_profile"]))
    by_tier: dict[str, set[str]] = {}
    for tier, profile in pairs:
        by_tier.setdefault(tier, set()).add(profile)
    assert any(len(v) > 1 for v in by_tier.values()), "each tier must span several profiles"
    green = by_tier.get(demo_catalog.GREEN, set())
    assert demo_catalog.PROFILE_ZONE_REDUNDANT_NO_PITR in green or len(green) > 1


def test_criticality_differs_per_workload_so_targets_are_not_decorative():
    assert demo_catalog.criticality_for(CONTOSO) == "mission_critical"
    assert demo_catalog.criticality_for(ZAVA_WEB) == "business_critical"
    assert demo_catalog.criticality_for(ZAVA_CRM) == "standard"


# ============================================================== summary
def test_the_summary_counts_four_buckets_not_two():
    config, _ = collect.collect_demo(CONTOSO)
    rows = join.build_rows(config, backup=demo_catalog.resiliency_backup_for(CONTOSO),
                           asr=demo_catalog.resiliency_asr_for(CONTOSO))
    summary = join.summarize(rows)
    corruption = summary["by_scenario"][model.SCENARIO_DATA_CORRUPTION]
    assert set(corruption) == {"determined", "no_recovery_path", "undetermined",
                               "not_applicable", "total"}
    assert corruption["no_recovery_path"] >= 1
    assert corruption["not_applicable"] >= 1, "stateless resources must be excluded, not passed"
    assert summary["protection"]["unknown"] >= 1


@pytest.mark.parametrize("scope", [CONTOSO, ZAVA_WEB, ZAVA_CRM])
def test_every_demo_workload_produces_a_complete_matrix(scope):
    rows = rows_for(scope)
    assert rows
    for name, row in rows.items():
        assert set(row["verdicts"]) == set(model.SCENARIOS), name
        for scenario, v in row["verdicts"].items():
            assert v["rpo_state"] in model.RPO_STATES, f"{name}/{scenario}"
            assert v["rto_class"] in model.RTO_CLASSES, f"{name}/{scenario}"
            if v["rpo_minutes"] is not None:
                assert v["rpo_state"] == model.RPO_KNOWN, f"{name}/{scenario}"


# ============================================================== the thesis, on demo data
def test_the_demo_estate_actually_demonstrates_the_redundancy_gap():
    """The demo exists to show the thesis. If this list is empty the narrative is missing
    its single most important row, and the screen and the report both fall flat."""
    from app.resiliency import analysis

    rows = list(rows_for(CONTOSO).values())
    gap = analysis.redundancy_gap(rows)
    assert gap, "the demo estate no longer demonstrates the product's thesis"

    cosmos = next((g for g in gap if "cosmos" in g["name"]), None)
    assert cosmos, [g["name"] for g in gap]
    # Recovers from a region loss automatically; needs a day to recover from a bad
    # deployment. That asymmetry is the finding — it does not need to be unrecoverable.
    assert cosmos["infra_rto_class"] == model.RTO_AUTOMATIC
    assert cosmos["logical_rto_class"] == model.RTO_DAY_PLUS
    assert cosmos["unrecoverable"] is False
    assert sorted(cosmos["worse_for"]) == ["Accidental deletion", "Data corruption"]


def test_the_consistent_demo_resource_is_not_flagged_as_a_gap():
    """`contosohotelsmedia` is hours for everything. Flagging it would make the list noise."""
    from app.resiliency import analysis

    rows = list(rows_for(CONTOSO).values())
    names = {g["name"] for g in analysis.redundancy_gap(rows)}
    assert "contosohotelsmedia" not in names


# ============================================================== the seed hook
def test_the_seed_hook_works_from_inside_a_running_event_loop(tmp_path):
    """`_seed_all` is sync but is called straight from an async endpoint, so the seed runs
    with a loop already on the thread. `asyncio.run()` refuses there — and because the
    caller swallows and logs the failure, the demo estate silently had no recovery data."""
    import asyncio

    from app.api.admin_demo import _seed_recovery
    from app.resiliency import snapshot as store

    store.set_path_for_tests(tmp_path / "snap.json")
    try:
        async def seed_the_way_the_endpoint_does():
            _seed_recovery("t-demo", CONTOSO)

        asyncio.run(seed_the_way_the_endpoint_does())
        snap = store.read("t-demo", "", "workload", CONTOSO)
        assert snap["report_exists"] is True
        assert snap["resources"], "the seed reported success but wrote nothing"
    finally:
        store.clear()


def test_the_seed_hook_still_works_without_a_loop(tmp_path):
    from app.api.admin_demo import _seed_recovery
    from app.resiliency import snapshot as store

    store.set_path_for_tests(tmp_path / "snap.json")
    try:
        _seed_recovery("t-demo", CONTOSO)
        assert store.read("t-demo", "", "workload", CONTOSO)["report_exists"] is True
    finally:
        store.clear()

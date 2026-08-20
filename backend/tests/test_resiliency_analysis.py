"""Aggregate analysis over an analyzed snapshot.

The four rules in `analysis.py`'s docstring each get a named test, because each is a
plausible mistake that produces a confident, wrong, *reassuring* number — the worst kind.
"""
from __future__ import annotations

from app.resiliency import analysis, model


def _v(rto_class, *, applicable=True, rpo_state=model.RPO_KNOWN, rpo=60,
       breach=None, detail="because", kind="backup_policy", source="Backup Manager"):
    out = {
        "applicable": applicable,
        "rto_class": rto_class,
        "rpo_state": rpo_state,
        "rpo_minutes": rpo,
        "basis": [{"kind": kind, "detail": detail, "source": source}] if detail else [],
    }
    if breach:
        out["breach"] = {"state": breach}
    return out


def _row(name, rtype, verdicts, *, zone_redundant=None, replication=""):
    return {
        "id": f"/subscriptions/s/rg/{name}", "name": name, "type": rtype,
        "location": "westeurope",
        "redundancy": {"zone_redundant": zone_redundant, "replication": replication,
                       "zones": [], "sku": ""},
        "verdicts": verdicts,
    }


# ============================================================== rule 1: no average RTO
def test_no_aggregate_ever_reports_an_average_rto():
    """`_RTO_RANK` excludes `unknown` on purpose, so a mean over the scale is undefined.
    An 'average RTO' would also be the single most quotable number in the report."""
    rows = [
        _row("a", "t", {"region_loss": _v(model.RTO_AUTOMATIC)}),
        _row("b", "t", {"region_loss": _v(model.RTO_NONE)}),
    ]
    result = analysis.analyze({"resources": rows})
    text = repr(result)
    assert "average" not in text.lower()
    assert "mean" not in text.lower()
    entry = result["by_type"][0]
    # The honest aggregate is the worst plus the distribution behind it.
    assert entry["worst_rto_class"] == model.RTO_NONE
    assert entry["rto_counts"][model.RTO_AUTOMATIC] == 1
    assert entry["rto_counts"][model.RTO_NONE] == 1


def test_the_worst_class_ignores_unknown_rather_than_letting_it_win():
    rows = [
        _row("a", "t", {"region_loss": _v(model.RTO_UNKNOWN)}),
        _row("b", "t", {"region_loss": _v(model.RTO_HOURS)}),
    ]
    entry = analysis.by_resource_type(rows)[0]
    assert entry["worst_rto_class"] == model.RTO_HOURS
    assert entry["undetermined"] == 1


def test_an_all_unknown_type_is_unknown_not_healthy():
    rows = [_row("a", "t", {"region_loss": _v(model.RTO_UNKNOWN)})]
    entry = analysis.by_resource_type(rows)[0]
    assert entry["worst_rto_class"] == model.RTO_UNKNOWN
    assert entry["undetermined"] == 1


# ============================================================== rule 2: median RPO
def test_the_median_rpo_covers_only_known_values_and_says_what_it_excluded():
    rows = [
        _row("a", "t", {"region_loss": _v(model.RTO_HOURS, rpo=60)}),
        _row("b", "t", {"region_loss": _v(model.RTO_HOURS, rpo=120)}),
        _row("c", "t", {"region_loss": _v(model.RTO_HOURS, rpo=180)}),
        _row("d", "t", {"region_loss": _v(model.RTO_NONE, rpo_state=model.RPO_NONE, rpo=None)}),
        _row("e", "t", {"region_loss": _v(model.RTO_UNKNOWN, rpo_state=model.RPO_UNKNOWN,
                                          rpo=None)}),
    ]
    rpo = analysis.rpo_distribution(rows, "region_loss")
    assert rpo["median_minutes"] == 120
    assert rpo["count_known"] == 3
    # The count it left out travels with the number, so a renderer cannot print the median
    # alone and imply it describes all five.
    assert rpo["excluded"] == 2
    assert rpo["none"] == 1
    assert rpo["unknown"] == 1


def test_no_recovery_point_and_unreadable_are_never_merged():
    """One is a finding about the estate; the other is a gap in our own reading."""
    rows = [
        _row("a", "t", {"region_loss": _v(model.RTO_NONE, rpo_state=model.RPO_NONE, rpo=None)}),
        _row("b", "t", {"region_loss": _v(model.RTO_UNKNOWN, rpo_state=model.RPO_UNKNOWN,
                                          rpo=None)}),
    ]
    rpo = analysis.rpo_distribution(rows, "region_loss")
    assert rpo["none"] == 1 and rpo["unknown"] == 1


def test_an_even_median_rounds_towards_more_data_loss():
    rows = [
        _row("a", "t", {"region_loss": _v(model.RTO_HOURS, rpo=60)}),
        _row("b", "t", {"region_loss": _v(model.RTO_HOURS, rpo=61)}),
    ]
    # Overstating data loss is the safe direction to be wrong in.
    assert analysis.rpo_distribution(rows, "region_loss")["median_minutes"] == 61


def test_median_is_none_rather_than_zero_when_nothing_is_known():
    rows = [_row("a", "t", {"region_loss": _v(model.RTO_UNKNOWN,
                                              rpo_state=model.RPO_UNKNOWN, rpo=None)})]
    rpo = analysis.rpo_distribution(rows, "region_loss")
    assert rpo["median_minutes"] is None, "0 would read as zero data loss"


# ============================================================== rule 3: undetermined
def test_undetermined_is_its_own_bucket_and_never_a_percentage():
    rows = [_row(f"r{i}", "t", {"region_loss": _v(model.RTO_MINUTES)}) for i in range(17)]
    rows += [_row(f"u{i}", "t", {"region_loss": _v(model.RTO_UNKNOWN)}) for i in range(3)]
    entry = analysis.by_resource_type(rows)[0]
    assert entry["resources"] == 20
    assert entry["undetermined"] == 3
    assert entry["rto_counts"][model.RTO_UNKNOWN] == 3


def test_every_rto_class_is_present_even_at_zero():
    """A renderer that iterates the dict must not silently drop the empty `none` bucket
    and produce a chart that looks complete."""
    rows = [_row("a", "t", {"region_loss": _v(model.RTO_MINUTES)})]
    dist = analysis.rto_distribution(rows, "region_loss")
    assert set(model.RTO_CLASSES) <= set(dist)
    assert dist[model.RTO_NONE] == 0


# ============================================================== rule 4: not applicable
def test_a_type_that_cannot_experience_a_failure_is_excluded_not_scored():
    """A stateless front end shown as '100% meets objective' for data corruption implies a
    protection it does not have."""
    rows = [_row("fd", "microsoft.cdn/profiles",
                 {"data_corruption": _v(model.RTO_AUTOMATIC, applicable=False),
                  "region_loss": _v(model.RTO_AUTOMATIC)})]
    entries = analysis.by_resource_type(rows)
    scenarios = {e["scenario"] for e in entries}
    assert "data_corruption" not in scenarios
    assert "region_loss" in scenarios


def test_partially_applicable_types_report_the_excluded_count():
    rows = [
        _row("a", "t", {"data_corruption": _v(model.RTO_NONE)}),
        _row("b", "t", {"data_corruption": _v(model.RTO_AUTOMATIC, applicable=False)}),
    ]
    entry = next(e for e in analysis.by_resource_type(rows) if e["scenario"] == "data_corruption")
    assert entry["resources"] == 1
    assert entry["not_applicable"] == 1


# ============================================================== the dominant reason
def test_the_dominant_reason_turns_a_backlog_into_one_fix():
    rows = [_row(f"sa{i}", "microsoft.storage/storageaccounts",
                 {"region_loss": _v(model.RTO_NONE, detail="vault is locally redundant")})
            for i in range(42)]
    rows += [_row("odd", "microsoft.storage/storageaccounts",
                  {"region_loss": _v(model.RTO_NONE, detail="something else")})]
    entry = next(e for e in analysis.by_resource_type(rows) if e["scenario"] == "region_loss")
    assert entry["dominant_reason"] == "vault is locally redundant"
    assert entry["dominant_reason_count"] == 42


def test_the_reason_index_ranks_by_what_it_explains():
    rows = [_row(f"a{i}", "t", {"region_loss": _v(model.RTO_NONE, detail="LRS vault")})
            for i in range(10)]
    rows += [_row(f"b{i}", "t2", {"region_loss": _v(model.RTO_HOURS, detail="geo vault")})
             for i in range(30)]
    index = analysis.reason_index(rows)
    # No-recovery-path outranks sheer volume: 10 unrecoverable beats 30 merely slow.
    assert index[0]["reason"] == "LRS vault"
    assert index[0]["no_recovery_path"] == 10
    assert index[0]["types"] == ["t"]
    assert len(index[0]["examples"]) == 5


def test_the_reason_index_is_bounded():
    rows = [_row(f"r{i}", "t", {"region_loss": _v(model.RTO_HOURS, detail=f"reason {i}")})
            for i in range(80)]
    assert len(analysis.reason_index(rows, limit=25)) == 25


def test_inapplicable_verdicts_contribute_no_reasons():
    rows = [_row("a", "t", {"data_corruption": _v(model.RTO_AUTOMATIC, applicable=False,
                                                  detail="stateless")})]
    assert analysis.reason_index(rows) == []


# ============================================================== ranking
def test_types_are_ranked_by_consequence_not_alphabetically():
    rows = [
        _row("z", "aaa-type", {"region_loss": _v(model.RTO_MINUTES)}),
        _row("a", "zzz-type", {"region_loss": _v(model.RTO_NONE)}),
    ]
    assert analysis.by_resource_type(rows)[0]["type"] == "zzz-type"


def test_an_unreadable_type_does_not_outrank_a_genuinely_broken_one():
    """A type we could not read is a gap in our reading, not the estate's biggest risk."""
    rows = [
        _row("a", "unknown-type", {"region_loss": _v(model.RTO_UNKNOWN)}),
        _row("b", "broken-type", {"region_loss": _v(model.RTO_NONE)}),
    ]
    assert analysis.by_resource_type(rows)[0]["type"] == "broken-type"


def test_worst_offenders_skips_resources_that_are_fine():
    rows = [
        _row("ok", "t", {"region_loss": _v(model.RTO_MINUTES, breach="met")}),
        _row("bad", "t", {"region_loss": _v(model.RTO_NONE)}),
    ]
    offenders = analysis.worst_offenders(rows)
    assert [o["name"] for o in offenders] == ["bad"]
    assert offenders[0]["no_recovery_path"] == ["Region loss"]


# ============================================================== the thesis
def test_the_redundancy_gap_is_the_row_no_other_tool_flags():
    """A resource that recovers from a region loss automatically and needs a day to recover
    from a bad deployment is reported resilient by every zone-centric tool. That asymmetry
    is the finding, and it does not require the resource to be unrecoverable."""
    rows = [
        _row("cosmos", "microsoft.documentdb/databaseaccounts", {
            "zone_loss": _v(model.RTO_AUTOMATIC),
            "region_loss": _v(model.RTO_AUTOMATIC),
            "data_corruption": _v(model.RTO_DAY_PLUS, detail="periodic backup only"),
            "accidental_delete": _v(model.RTO_DAY_PLUS, detail="periodic backup only"),
        }, zone_redundant=True, replication="multi-region-write"),
        # Not redundant: already obvious to every other tool, so not this list's job.
        _row("bare-vm", "microsoft.compute/virtualmachines", {
            "region_loss": _v(model.RTO_NONE), "data_corruption": _v(model.RTO_NONE),
        }),
    ]
    out = analysis.redundancy_gap(rows)
    assert [r["name"] for r in out] == ["cosmos"]
    assert sorted(out[0]["worse_for"]) == ["Accidental deletion", "Data corruption"]
    assert out[0]["infra_rto_class"] == model.RTO_AUTOMATIC
    assert out[0]["logical_rto_class"] == model.RTO_DAY_PLUS
    assert out[0]["unrecoverable"] is False


def test_a_redundant_resource_with_no_logical_path_is_flagged_and_ranked_first():
    rows = [
        _row("slow", "t", {
            "zone_loss": _v(model.RTO_AUTOMATIC), "region_loss": _v(model.RTO_AUTOMATIC),
            "data_corruption": _v(model.RTO_DAY_PLUS),
        }, zone_redundant=True),
        _row("none", "t", {
            "zone_loss": _v(model.RTO_AUTOMATIC), "region_loss": _v(model.RTO_AUTOMATIC),
            "data_corruption": _v(model.RTO_NONE),
        }, zone_redundant=True),
    ]
    out = analysis.redundancy_gap(rows)
    assert [r["name"] for r in out] == ["none", "slow"]
    assert out[0]["unrecoverable"] is True


def test_the_gap_is_measured_against_the_worst_infrastructure_answer():
    """A storage account that survives a zone loss automatically but needs hours for a
    region loss has not had its infrastructure story solved by redundancy. Comparing
    corruption against the flattering number would manufacture a finding."""
    rows = [_row("media", "microsoft.storage/storageaccounts", {
        "zone_loss": _v(model.RTO_AUTOMATIC),
        "region_loss": _v(model.RTO_HOURS),
        "data_corruption": _v(model.RTO_HOURS),
    }, zone_redundant=True, replication="GZRS")]
    assert analysis.redundancy_gap(rows) == []


def test_a_consistent_resource_is_not_flagged():
    """Hours for a region loss and hours for corruption is not a gap; it is a posture."""
    rows = [_row("media", "microsoft.storage/storageaccounts", {
        "zone_loss": _v(model.RTO_HOURS), "region_loss": _v(model.RTO_HOURS),
        "data_corruption": _v(model.RTO_HOURS), "accidental_delete": _v(model.RTO_HOURS),
    }, zone_redundant=True, replication="GZRS")]
    assert analysis.redundancy_gap(rows) == []


def test_a_one_class_difference_is_not_called_a_gap():
    """Two classes is a category change; one is close to a rounding difference, and a claim
    that cannot survive an argument should not be made."""
    rows = [_row("x", "t", {
        "zone_loss": _v(model.RTO_MINUTES), "region_loss": _v(model.RTO_MINUTES),
        "data_corruption": _v(model.RTO_HOURS),
    }, zone_redundant=True)]
    assert analysis.redundancy_gap(rows) == []


def test_an_unknown_logical_answer_is_not_evidence_of_a_gap():
    """`unknown` is not on the RTO scale. Placing it there would manufacture findings out of
    our own inability to read a source."""
    rows = [_row("x", "t", {
        "zone_loss": _v(model.RTO_AUTOMATIC), "region_loss": _v(model.RTO_AUTOMATIC),
        "data_corruption": _v(model.RTO_UNKNOWN),
    }, zone_redundant=True)]
    assert analysis.redundancy_gap(rows) == []


def test_an_unknown_infrastructure_answer_is_not_a_reassuring_baseline():
    rows = [_row("x", "t", {
        "zone_loss": _v(model.RTO_UNKNOWN), "region_loss": _v(model.RTO_UNKNOWN),
        "data_corruption": _v(model.RTO_NONE),
    }, zone_redundant=True)]
    assert analysis.redundancy_gap(rows) == []


def test_no_recovery_point_counts_as_a_gap_even_when_the_rto_class_looks_fine():
    """An RTO of minutes over nothing is a recovery to an empty resource."""
    rows = [_row("x", "t", {
        "zone_loss": _v(model.RTO_AUTOMATIC), "region_loss": _v(model.RTO_AUTOMATIC),
        "data_corruption": _v(model.RTO_MINUTES, rpo_state=model.RPO_NONE, rpo=None),
    }, zone_redundant=True)]
    assert [r["name"] for r in analysis.redundancy_gap(rows)] == ["x"]


def test_lrs_is_not_read_as_redundancy_by_the_thesis_list():
    """LRS is the ABSENCE of redundancy; crediting it would put un-backed-up single-region
    resources on the one list that is supposed to be counter-intuitive."""
    rows = [_row("sa", "microsoft.storage/storageaccounts", {
        "zone_loss": _v(model.RTO_AUTOMATIC), "region_loss": _v(model.RTO_AUTOMATIC),
        "data_corruption": _v(model.RTO_NONE),
    }, replication="LRS")]
    assert analysis.redundancy_gap(rows) == []


# ============================================================== the whole payload
def test_analyse_returns_every_section_for_an_empty_snapshot():
    result = analysis.analyze({"resources": []})
    assert result["resources"] == 0
    assert result["by_type"] == []
    assert result["reasons"] == []
    assert set(result["rto_distribution"]) == set(model.SCENARIOS)
    assert set(result["rpo_distribution"]) == set(model.SCENARIOS)


def test_analyse_is_pure_and_does_not_mutate_the_snapshot():
    rows = [_row("a", "t", {"region_loss": _v(model.RTO_NONE)})]
    snapshot = {"resources": rows}
    before = repr(snapshot)
    analysis.analyze(snapshot)
    assert repr(snapshot) == before

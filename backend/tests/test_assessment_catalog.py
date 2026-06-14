"""Structural + behavioural tests for the assessment check catalog and the metric-backed
control engine (Phases 1-5 enhancements).

These are pure/offline tests: catalog integrity is validated against the in-memory
``ALL_CHECKS`` and the metric-backed runner path is exercised with the Azure capture
functions monkeypatched, so nothing here touches a live subscription.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from app.assessments import catalog
from app.assessments import runner
from app.exec import command_runner
from app.exec.command_runner import CaptureResult

_REQUIRED_PROJECTION = "id, name, type, resourceGroup, subscriptionId"
_VALID_SEVERITIES = {"critical", "error", "warning", "info"}
_VALID_FRAMEWORKS = set(catalog.FRAMEWORKS)
_METRIC_KEYS = {"metric", "aggregation", "evaluate", "comparison", "threshold", "lookback_days", "interval", "unit"}


# --- catalog structural integrity ----------------------------------------------------
def test_check_ids_unique():
    ids = [c["id"] for c in catalog.ALL_CHECKS]
    assert len(ids) == len(set(ids)), "duplicate check id(s)"


def test_every_check_has_valid_core_fields():
    for c in catalog.ALL_CHECKS:
        kind = c.get("kind") or ("metric" if c.get("metric") else "graph")
        assert c["pillar"] in catalog.PILLARS, c["id"]
        assert c["severity"] in _VALID_SEVERITIES, c["id"]
        # graph/metric controls target resource types; manual/signal controls may be
        # workload-level (no resource_types) and apply whenever any resource is in scope.
        if kind in ("graph", "metric"):
            assert c["resource_types"], f"{c['id']} has no resource_types"
        # resource_types must be lowercased ARM type strings.
        assert all(t == t.lower() for t in c["resource_types"]), c["id"]
        assert c["remediation"].strip(), f"{c['id']} has empty remediation"
        # frameworks keys must be a subset of the supported frameworks.
        assert set(c["frameworks"]).issubset(_VALID_FRAMEWORKS), c["id"]


def test_graph_checks_project_required_columns():
    for c in catalog.ALL_CHECKS:
        kind = c.get("kind") or ("metric" if c.get("metric") else "graph")
        if kind != "graph":
            continue  # metric/manual/signal controls have no standard KQL projection
        assert _REQUIRED_PROJECTION in c["kql"], f"{c['id']} does not project the standard columns"


def test_manual_and_signal_kinds_well_formed():
    manual = [c for c in catalog.ALL_CHECKS if c.get("kind") == "manual"]
    signal = [c for c in catalog.ALL_CHECKS if c.get("kind") == "signal"]
    assert manual, "expected manual attestation controls"
    assert signal, "expected at least one Advisor signal control"
    for c in manual:
        assert c["kql"] == "", f"{c['id']} manual check should have empty kql"
        assert not c.get("metric"), c["id"]
    for c in signal:
        assert c["signal"]["provider"] == "advisor", c["id"]
        assert c["signal"]["category"], c["id"]


def test_metric_checks_well_formed():
    metric_checks = [c for c in catalog.ALL_CHECKS if c.get("metric")]
    assert len(metric_checks) >= 2, "expected the new metric-backed checks"
    for c in metric_checks:
        assert c["kql"] == "", f"{c['id']} metric check should have empty kql"
        mc = c["metric"]
        assert _METRIC_KEYS.issubset(mc), c["id"]
        assert mc["comparison"] in ("lt", "le", "gt", "ge"), c["id"]
        assert mc["evaluate"] in ("avg", "max", "min"), c["id"]
        assert isinstance(mc["threshold"], float)
        assert mc["lookback_days"] >= 1


def test_catalog_breadth_grew():
    by_pillar: dict[str, int] = {}
    for c in catalog.ALL_CHECKS:
        by_pillar[c["pillar"]] = by_pillar.get(c["pillar"], 0) + 1
    assert len(catalog.ALL_CHECKS) >= 60
    assert by_pillar["security"] >= 25
    for pillar in catalog.PILLARS:
        assert by_pillar.get(pillar, 0) >= 5, pillar


# --- Phase 1: CIS pinning ------------------------------------------------------------
def test_cis_version_pinned_and_no_vague_refs():
    assert catalog.CIS_VERSION.startswith("v")
    vague = re.compile(r"\d+\.x", re.IGNORECASE)
    for c in catalog.ALL_CHECKS:
        for cid in c["frameworks"].get("cis", []):
            assert not vague.search(cid), f"{c['id']} still has a vague CIS ref: {cid}"


def test_tls_check_promoted_to_shipped_catalog():
    tls = catalog.get_check("sec_storage_min_tls")
    assert tls is not None
    assert "CIS Azure 3.15" in tls["frameworks"].get("cis", [])
    assert tls["remediation_command"].startswith("az storage account update")


def test_new_security_checks_present():
    expected = {
        "sec_storage_net_default_allow",
        "sec_sql_aad_only",
        "sec_kv_rbac",
        "sec_kv_soft_delete",
        "sec_acr_admin_user",
        "sec_aks_no_rbac",
        "sec_nsg_db_ports_open",
    }
    ids = {c["id"] for c in catalog.ALL_CHECKS}
    assert expected.issubset(ids)


# --- Phase 5: framework coverage -----------------------------------------------------
def test_framework_meta_covers_all_frameworks():
    for fw in catalog.FRAMEWORKS:
        assert fw in catalog.FRAMEWORK_META
        assert catalog.FRAMEWORK_META[fw]["label"]


def test_mcsb_and_pci_applied_centrally():
    # A representative security check should carry MCSB + PCI ids applied centrally.
    https = catalog.get_check("sec_storage_https_only")
    assert https is not None
    assert https["frameworks"].get("mcsb")
    assert https["frameworks"].get("pci")


def test_compliance_coverage_aggregates_worst_status():
    findings = [
        {
            "check_id": "sec_storage_https_only",
            "title": "HTTPS",
            "status": "fail",
            "frameworks": {"cis": ["CIS Azure 3.1"], "mcsb": ["DP-3"], "pci": ["PCI DSS 4"]},
        },
        {
            "check_id": "sec_storage_min_tls",
            "title": "TLS",
            "status": "pass",
            "frameworks": {"mcsb": ["DP-3"], "pci": ["PCI DSS 4"]},
        },
    ]
    cov = catalog.compliance_coverage(findings)
    # Every supported framework appears in the coverage report.
    assert set(cov) == set(catalog.FRAMEWORKS)
    # DP-3 cited by one fail + one pass → worst status wins (fail).
    dp3 = next(c for c in cov["mcsb"]["controls"] if c["control"] == "DP-3")
    assert dp3["status"] == "fail"
    assert cov["pci"]["failed"] >= 1


def test_catalog_markdown_generates_reference():
    md = catalog.catalog_markdown()
    assert "# Assessment check catalog" in md
    assert "Security" in md and "Cost Optimization" in md
    assert "sec_storage_min_tls" in md
    assert catalog.CIS_VERSION in md


# --- detection predicate -------------------------------------------------------------
def test_detection_predicate_graph_vs_metric():
    # Graph checks expose a what-if predicate; metric checks intentionally do not.
    assert catalog.detection_predicate("sec_storage_min_tls") != ""
    assert catalog.detection_predicate("cost_vm_idle") == ""


# --- runner metric helpers -----------------------------------------------------------
def test_reduce_series():
    assert runner._reduce_series([2.0, 4.0, 6.0], "avg") == 4.0
    assert runner._reduce_series([2.0, 4.0, 6.0], "max") == 6.0
    assert runner._reduce_series([2.0, 4.0, 6.0], "min") == 2.0
    assert runner._reduce_series([], "avg") is None


def test_metric_violates():
    assert runner._metric_violates(3.0, "lt", 5.0) is True
    assert runner._metric_violates(7.0, "lt", 5.0) is False
    assert runner._metric_violates(90.0, "gt", 85.0) is True
    assert runner._metric_violates(85.0, "ge", 85.0) is True
    assert runner._metric_violates(84.0, "ge", 85.0) is False


def test_parse_metric_points_prefers_aggregation_then_falls_back():
    blob = json.dumps(
        {"value": [{"timeseries": [{"data": [{"average": 1.0}, {"average": 3.0}, {"maximum": 9.0}]}]}]}
    )
    assert runner._parse_metric_points(blob, "Average") == [1.0, 3.0, 9.0]
    assert runner._parse_metric_points("not json", "Average") == []


# --- runner metric-backed evaluation (mocked Azure) ----------------------------------
_VM_ROWS = [
    {"id": "/subscriptions/s1/rg/vm-idle", "name": "vm-idle", "type": "microsoft.compute/virtualmachines", "resourceGroup": "rg", "subscriptionId": "s1"},
    {"id": "/subscriptions/s1/rg/vm-busy", "name": "vm-busy", "type": "microsoft.compute/virtualmachines", "resourceGroup": "rg", "subscriptionId": "s1"},
]


def _metric_blob(values: list[float]) -> str:
    return json.dumps({"value": [{"timeseries": [{"data": [{"average": v} for v in values]}]}]})


def _patch_capture(monkeypatch, *, metrics_fn):
    async def _fake_kql(kql, connection, output="json", session_config_dir=None):
        return CaptureResult(ok=True, stdout=json.dumps(_VM_ROWS))

    monkeypatch.setattr(runner, "run_kql_capture", _fake_kql)
    monkeypatch.setattr(command_runner, "run_metrics_capture", metrics_fn)


def test_evaluate_metric_check_flags_idle_vm(monkeypatch):
    async def _fake_metrics(resource_id, metrics, connection, **kwargs):
        # Idle VM stays under 5%; busy VM peaks high.
        values = [2.0, 3.0, 4.0] if "idle" in resource_id else [10.0, 95.0]
        return CaptureResult(ok=True, stdout=_metric_blob(values))

    _patch_capture(monkeypatch, metrics_fn=_fake_metrics)
    check = catalog.get_check("cost_vm_idle")
    res = asyncio.run(
        runner._evaluate_metric_check(check, "subscriptionId =~ 's1'", {"microsoft.compute/virtualmachines"}, {})
    )
    assert res["status"] == "fail"
    assert {r["name"] for r in res["rows"]} == {"vm-idle"}
    assert res["rows"][0]["metric_value"] == 4.0


def test_evaluate_metric_check_passes_when_no_violation(monkeypatch):
    async def _fake_metrics(resource_id, metrics, connection, **kwargs):
        return CaptureResult(ok=True, stdout=_metric_blob([40.0, 55.0]))

    _patch_capture(monkeypatch, metrics_fn=_fake_metrics)
    check = catalog.get_check("cost_vm_idle")
    res = asyncio.run(
        runner._evaluate_metric_check(check, "subscriptionId =~ 's1'", {"microsoft.compute/virtualmachines"}, {})
    )
    assert res["status"] == "pass"
    assert res["rows"] == []


def test_evaluate_metric_check_errors_when_no_data(monkeypatch):
    async def _fake_metrics(resource_id, metrics, connection, **kwargs):
        return CaptureResult(ok=False, error="metric unavailable")

    _patch_capture(monkeypatch, metrics_fn=_fake_metrics)
    check = catalog.get_check("cost_vm_idle")
    res = asyncio.run(
        runner._evaluate_metric_check(check, "subscriptionId =~ 's1'", {"microsoft.compute/virtualmachines"}, {})
    )
    assert res["status"] == "error"
    assert res["rows"] == []


def test_evaluate_metric_check_not_applicable_without_type(monkeypatch):
    async def _fake_metrics(resource_id, metrics, connection, **kwargs):  # pragma: no cover - not reached
        return CaptureResult(ok=True, stdout=_metric_blob([1.0]))

    _patch_capture(monkeypatch, metrics_fn=_fake_metrics)
    check = catalog.get_check("cost_vm_idle")
    # No matching resource type present in scope.
    res = asyncio.run(runner._evaluate_metric_check(check, "subscriptionId =~ 's1'", {"microsoft.storage/storageaccounts"}, {}))
    assert res["status"] == "not_applicable"

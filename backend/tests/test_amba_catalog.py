"""Integrity of the vendored AMBA catalog and the report adapters built on top of it.

These pin the defects found while reviewing the rendered PDF against live data:
Markdown link syntax leaking into operator-facing prose, and a substring-based category
classifier that mislabelled alerts (``"Supported"`` contains ``"up"``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.amba.builtin_seed import (
    ALERT_TYPES,
    AMBA_CATEGORIES,
    OPERATORS,
    PATTERNS,
    SEVERITIES,
    TIERS,
    amba_release,
    builtin_reference,
)
from app.core.coverage_pdf import (
    _adapt,
    _amba_condition,
    _amba_remediation,
    _duration,
    _fmt_threshold,
)

CATALOG_PATH = Path(__file__).resolve().parents[1] / "app" / "amba" / "data" / "amba_catalog.json"


@pytest.fixture(scope="module")
def reference() -> dict:
    return builtin_reference()


@pytest.fixture(scope="module")
def alerts(reference: dict) -> list[dict]:
    return [a for spec in reference["types"].values() for a in spec["alerts"]]


# --------------------------------------------------------------------------- catalog
def test_vendored_catalogue_is_present_and_pinned():
    assert CATALOG_PATH.exists(), "run scripts/import_amba_catalog.py"
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}(-\w+)?", catalog["amba_release"])
    assert catalog["type_count"] >= 70
    assert catalog["alert_count"] >= 600
    assert amba_release() == catalog["amba_release"]


def test_every_alert_conforms_to_the_schema(alerts: list[dict]):
    for alert in alerts:
        where = f"{alert.get('key')} ({alert.get('name')})"
        assert alert["key"], where
        assert alert["name"], where
        assert alert["alert_type"] in ALERT_TYPES, where
        assert alert["amba_category"] in AMBA_CATEGORIES, where
        assert alert["severity"] in SEVERITIES, where
        assert alert["severity_num"] in range(5), where
        assert alert["tier"] in TIERS, where
        assert alert["operator"] in OPERATORS, where
        assert all(p in PATTERNS for p in alert["patterns"]), where
        assert alert["source"] in ("amba", "local"), where
        assert isinstance(alert["dimensions"], list), where
        assert isinstance(alert["references"], list), where


def test_alert_keys_are_unique_within_a_type(reference: dict):
    for arm_type, spec in reference["types"].items():
        keys = [a["key"] for a in spec["alerts"]]
        assert len(keys) == len(set(keys)), f"duplicate alert keys in {arm_type}"


def test_no_markdown_leaks_into_operator_facing_prose(alerts: list[dict]):
    """Upstream descriptions embed Markdown links; a PDF table cell renders them literally."""
    offenders = [
        a["name"]
        for a in alerts
        if "](" in (a["description"] or "") or "](" in (a["why"] or "")
    ]
    assert offenders == [], f"markdown link syntax survived the import: {offenders[:5]}"


def test_every_alert_has_operator_facing_copy(alerts: list[dict]):
    assert all(a["why"].strip() for a in alerts)


def test_category_classifier_is_not_fooled_by_prose(reference: dict):
    """Regression: substring matching put 'Supported for: Linux' alerts in *availability*
    (via "up" in "Supported") and every Key Vault metric in *security* (via "vault")."""
    by_metric = {
        a["metric"]: a["amba_category"]
        for spec in reference["types"].values()
        for a in spec["alerts"]
        if a["metric"]
    }
    expected = {
        "Availability": "availability",
        "UnhealthyHostCount": "availability",
        "HealthyHostCount": "availability",
        "FailedRequests": "availability",
        "Percentage5XX": "availability",
        "Heartbeat": "availability",
        "IfUnderDDoSAttack": "security",
        "CpuUtilization": "performance",
        "ServiceApiLatency": "performance",
    }
    for metric, category in expected.items():
        assert by_metric.get(metric) == category, f"{metric} classified as {by_metric.get(metric)}"


def test_service_health_and_activity_log_baselines_exist(reference: dict):
    subs = reference["types"]["microsoft.resources/subscriptions"]["alerts"]
    categories = {(a.get("activity_log") or {}).get("category") for a in subs}
    assert "ServiceHealth" in categories
    assert "ResourceHealth" in categories
    assert all(a["alert_type"] == "activitylog" for a in subs)


def test_log_search_baselines_carry_a_query(alerts: list[dict]):
    log_alerts = [a for a in alerts if a["alert_type"] == "log"]
    assert log_alerts
    assert all(a["log_query"].strip() for a in log_alerts)


def test_dynamic_threshold_baselines_have_no_static_threshold(alerts: list[dict]):
    dynamic = [a for a in alerts if a["criterion_type"] == "DynamicThresholdCriterion"]
    assert dynamic
    for alert in dynamic:
        assert alert["threshold"] is None, alert["name"]
        assert alert["deployable"] is True, alert["name"]


def test_threshold_override_tags_follow_the_amba_convention(alerts: list[dict]):
    tagged = [a for a in alerts if a["threshold_override_tag"]]
    assert tagged
    for alert in tagged:
        tag = alert["threshold_override_tag"]
        assert tag.startswith("_amba-") and tag.endswith("-threshold-Override_"), tag


# --------------------------------------------------------------------------- formatting
@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (50.0, "count", "50 count"),
        (90.0, "%", "90%"),
        (99.9, "%", "99.9%"),
        (1073741824.0, "bytes", "1 GB"),
        (10000000.0, "bytes", "9.5 MB"),
        (60000000.0, "bytes/s", "57 MB/s"),
        (512.0, "bytes", "512 bytes"),
        (0.04, "s", "0.04 s"),
        (None, "count", "Nonecount"),
    ],
)
def test_threshold_formatting(value, unit, expected):
    assert _fmt_threshold(value, unit) == expected


@pytest.mark.parametrize(
    ("iso", "expected"),
    [("PT5M", "5m"), ("PT1H", "1h"), ("PT15M", "15m"), ("P1D", "1d"), ("", ""), ("weird", "WEIRD")],
)
def test_duration_formatting(iso, expected):
    assert _duration(iso) == expected


def test_condition_reads_as_a_sentence_for_each_alert_class():
    metric = {
        "metric": "Percentage CPU", "operator": "GreaterThan", "threshold": 90.0, "unit": "%",
        "window_size": "PT5M", "evaluation_frequency": "PT1M", "criterion_type": "StaticThresholdCriterion",
    }
    assert _amba_condition(metric, "metric") == "Percentage CPU > 90% over 5m, checked every 1m"

    dynamic = {**metric, "criterion_type": "DynamicThresholdCriterion", "threshold": None,
               "alert_sensitivity": "Medium"}
    assert "dynamic threshold (Medium sensitivity)" in _amba_condition(dynamic, "metric")

    log = {
        "log_query": "Heartbeat\n| summarize x=count()", "operator": "LessThan", "threshold": 10.0,
        "unit": "count", "window_size": "PT15M", "evaluation_frequency": "PT5M",
    }
    assert _amba_condition(log, "log") == "log search on Heartbeat < 10 count over 15m, checked every 5m"

    activity = {"activity_log": {"category": "ServiceHealth", "incidentType": "Incident"}}
    assert _amba_condition(activity, "activitylog") == "ServiceHealth · Incident"


def test_condition_includes_dimensions():
    rec = {
        "metric": "ResponseStatus", "operator": "GreaterThan", "threshold": 10.0, "unit": "count",
        "window_size": "PT5M", "evaluation_frequency": "PT5M",
        "dimensions": [{"name": "HttpStatusGroup", "operator": "Include", "values": ["5xx"]}],
    }
    assert "where HttpStatusGroup=5xx" in _amba_condition(rec, "metric")


# --------------------------------------------------------------------------- remediation
def _gap(status: str, issues: list[str], **observed) -> dict:
    return {"status": status, "observed": {"issues": issues, **observed}}


def test_remediation_is_specific_per_failure_mode():
    condition = "Percentage CPU > 90% over 5m"

    missing = _gap("missing", [])
    assert _amba_remediation(missing, "CPU high", "metric", condition) == (
        "Create the 'CPU high' metric alert (Percentage CPU > 90% over 5m) and wire it to an action group."
    )

    disabled = _gap("misconfigured", ["disabled"], rule_name="cpu-rule")
    assert _amba_remediation(disabled, "CPU high", "metric", condition) == (
        "Re-enable the existing 'cpu-rule' rule."
    )

    no_ag = _gap("misconfigured", ["no action group"], rule_name="cpu-rule")
    assert "Wire an action group" in _amba_remediation(no_ag, "CPU high", "metric", condition)

    empty_ag = _gap("misconfigured", ["action group has no receivers"], rule_name="cpu-rule")
    assert "Add a receiver" in _amba_remediation(empty_ag, "CPU high", "metric", condition)

    drift = _gap("misconfigured", ["threshold differs from baseline"], rule_name="cpu-rule")
    assert condition in _amba_remediation(drift, "CPU high", "metric", condition)

    suppressed = _gap(
        "suppressed", [], rule_name="cpu-rule", suppressed_by=[{"name": "mute-all"}]
    )
    fix = _amba_remediation(suppressed, "CPU high", "metric", condition)
    assert "mute-all" in fix and "narrow or disable" in fix


def test_remediation_names_the_right_alert_class():
    for alert_type, phrase in (
        ("metric", "metric alert"),
        ("log", "log search alert"),
        ("activitylog", "activity log alert"),
    ):
        fix = _amba_remediation(_gap("missing", []), "X", alert_type, "")
        assert phrase in fix


# --------------------------------------------------------------------------- PDF adapter
def test_adapter_surfaces_notification_health_and_baseline_provenance():
    snap = {
        "scope_name": "wl", "scope_kind": "workload", "coverage_pct": 50,
        "generated_at": "2026-07-28T00:00:00Z", "source": "azure_resource_graph",
        "connection_configured": True, "demo": False, "all_resources": [],
        "baseline": {"amba_release": "2026-06-03", "version": 3, "tiers": ["core"], "patterns": []},
        "kpis": {
            "total_resources_in_baseline": 4, "alerts_present": 2, "alerts_missing": 1,
            "alerts_misconfigured": 1, "alerts_suppressed": 3, "recommended_total": 7,
            "action_groups": 2, "action_groups_usable": 1, "suppression_rules": 1,
            "resources_excluded": 2,
        },
        "groups": [
            {"resource_type": "microsoft.storage/storageaccounts", "display": "Storage Account"}
        ],
        "gaps": [
            {
                "resource_id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/a",
                "resource_name": "a", "resource_type": "microsoft.storage/storageaccounts",
                "resource_group": "rg", "subscription_id": "s", "location": "westeurope",
                "alert_key": "availability", "alert_name": "Availability", "alert_type": "metric",
                "amba_category": "availability", "severity": "critical", "severity_num": 0,
                "tier": "core", "status": "missing", "why": "Storage availability matters.",
                "recommended": {
                    "metric": "Availability", "operator": "LessThan", "threshold": 100.0,
                    "unit": "%", "window_size": "PT5M", "evaluation_frequency": "PT1M",
                    "criterion_type": "StaticThresholdCriterion",
                },
                "observed": {},
            }
        ],
    }
    model = _adapt("amba", snap)

    labels = {label for label, _, _ in model["kpis"]}
    assert "Suppressed" in labels
    assert model["notification_health"] == {
        "action_groups": 2, "action_groups_usable": 1, "suppression_rules": 1, "excluded": 2,
    }
    assert model["baseline"]["amba_release"] == "2026-06-03"
    # Friendly display name, not the ARM type's last segment.
    assert model["gap_type_counts"] == [("Storage Account", 1)]
    assert model["gaps"][0]["type_display"] == "Storage Account"
    assert "Availability < 100% over 5m, checked every 1m" in model["gaps"][0]["detail"]
    # Notification plumbing is called out on the summary line.
    assert "muted by an alert processing rule" in model["summary_line"]
    assert "no enabled receivers" in model["summary_line"]
    assert "MonitorDisable" in model["summary_line"]

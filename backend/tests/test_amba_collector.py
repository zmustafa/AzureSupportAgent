"""AMBA coverage collector: detection correctness for every alert class.

Each test pins a behavior that was previously wrong or unrepresentable:
false-PRESENT on log alerts, multi-resource metric rules reading as missing, dynamic
thresholds, alert processing rule suppression, AMBA-ALZ threshold-override tags,
action groups with no receivers, Service Health / Activity Log matching, and the
MonitorDisable opt-out.
"""
from __future__ import annotations

import pytest

from app.amba import collector
from app.amba.collector import (
    STATUS_MISCONFIGURED,
    STATUS_MISSING,
    STATUS_PRESENT,
    STATUS_SUPPRESSED,
    CoverageOptions,
    compute_coverage,
)

SUB = "11111111-1111-1111-1111-111111111111"
RG = "rg-amba"
VM_TYPE = "microsoft.compute/virtualmachines"
VM_ID = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Compute/virtualMachines/vm1"
AG_ID = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/microsoft.insights/actionGroups/oncall"


# --------------------------------------------------------------------------- fixtures
def _vm(**overrides):
    row = {
        "id": VM_ID,
        "name": "vm1",
        "type": VM_TYPE,
        "resourceGroup": RG,
        "subscriptionId": SUB,
        "location": "westeurope",
        "tags": {},
    }
    row.update(overrides)
    return row


def _action_group(*, receivers: bool = True, enabled: bool = True):
    props = {"enabled": enabled, "groupShortName": "oncall"}
    if receivers:
        props["emailReceivers"] = [{"name": "sre", "emailAddress": "sre@example.com"}]
    return {"id": AG_ID, "name": "oncall", "type": "microsoft.insights/actionGroups", "properties": props}


def _metric_rule(
    name="r1",
    *,
    metric="Percentage CPU",
    threshold=90,
    scopes=None,
    enabled=True,
    action_groups=(AG_ID,),
    dynamic=False,
    sensitivity="Medium",
    dimensions=None,
    target_type="",
    target_region="",
    severity=None,
):
    clause = {"name": "c1", "metricName": metric, "operator": "GreaterThan", "timeAggregation": "Average"}
    if dynamic:
        clause["criterionType"] = "DynamicThresholdCriterion"
        clause["alertSensitivity"] = sensitivity
    else:
        clause["criterionType"] = "StaticThresholdCriterion"
        clause["threshold"] = threshold
    if dimensions:
        clause["dimensions"] = dimensions
    props = {
        "enabled": enabled,
        "scopes": list(scopes or [VM_ID]),
        "criteria": {"allOf": [clause]},
        "actions": [{"actionGroupId": g} for g in action_groups],
    }
    if target_type:
        props["targetResourceType"] = target_type
    if target_region:
        props["targetResourceRegion"] = target_region
    if severity is not None:
        props["severity"] = severity
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/microsoft.insights/metricAlerts/{name}",
        "name": name,
        "type": "microsoft.insights/metricAlerts",
        "properties": props,
    }


def _log_rule(name="log1", *, query="Heartbeat | summarize x=count()", scopes=None, threshold=10):
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/microsoft.insights/scheduledQueryRules/{name}",
        "name": name,
        "type": "microsoft.insights/scheduledQueryRules",
        "properties": {
            "enabled": True,
            "scopes": list(scopes or [VM_ID]),
            "criteria": {"allOf": [{"query": query, "operator": "GreaterThan", "threshold": threshold}]},
            "actions": {"actionGroups": [AG_ID]},
        },
    }


def _activity_rule(name="act1", *, category="ServiceHealth", incident_type=None, operation=None, scopes=None):
    conditions = [{"field": "category", "equals": category}]
    if incident_type:
        conditions.append({"field": "properties.incidentType", "equals": incident_type})
    if operation:
        conditions.append({"field": "operationName", "equals": operation})
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/microsoft.insights/activityLogAlerts/{name}",
        "name": name,
        "type": "microsoft.insights/activityLogAlerts",
        "properties": {
            "enabled": True,
            "scopes": list(scopes or [f"/subscriptions/{SUB}"]),
            "condition": {"allOf": conditions},
            "actions": {"actionGroups": [{"actionGroupId": AG_ID}]},
        },
    }


def _suppression(name="mute", *, scopes=None, conditions=None):
    props = {
        "enabled": True,
        "scopes": list(scopes or [f"/subscriptions/{SUB}"]),
        "actions": [{"actionType": "RemoveAllActionGroups"}],
    }
    if conditions:
        props["conditions"] = conditions
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.AlertsManagement/actionRules/{name}",
        "name": name,
        "type": "microsoft.alertsmanagement/actionRules",
        "properties": props,
    }


def _reference(alerts, arm_type=VM_TYPE, display="Virtual Machine"):
    """A minimal single-type reference document."""
    normalized = []
    for alert in alerts:
        base = {
            "key": alert.get("key", "a1"),
            "guid": "",
            "name": alert.get("name", "Alert"),
            "description": "",
            "why": "",
            "alert_type": "metric",
            "amba_category": "performance",
            "severity": "warning",
            "severity_num": 2,
            "tier": "core",
            "patterns": [],
            "metric": "",
            "metric_namespace": "",
            "counter_name": "",
            "operator": "GreaterThan",
            "threshold": None,
            "unit": "%",
            "criterion_type": "",
            "alert_sensitivity": None,
            "failing_periods": None,
            "auto_mitigate": None,
            "time_aggregation": "Average",
            "window_size": "PT5M",
            "evaluation_frequency": "PT5M",
            "dimensions": [],
            "dimension_filter": "",
            "activity_log": {},
            "log_query": "",
            "visible": True,
            "verified": False,
            "default_enabled": True,
            "requires_action_group": True,
            "deployable": True,
            "references": [],
            "deployments": [],
            "policy_alert_name": "",
            "policy_scope": "",
            "threshold_override_tag": "",
            "amba_tags": [],
            "source": "amba",
        }
        base.update(alert)
        normalized.append(base)
    return {
        "version": 1,
        "amba_release": "test",
        "types": {arm_type: {"display": display, "category": "compute", "source": "amba", "alerts": normalized}},
    }


def _status(snapshot, alert_key="a1"):
    for group in snapshot["groups"]:
        for row in group["rows"]:
            for cell in row["cells"]:
                if cell["alert_key"] == alert_key:
                    return cell["status"], cell["observed"]
    return None, {}


CPU = {"key": "a1", "name": "CPU high", "metric": "Percentage CPU", "threshold": 90.0,
       "criterion_type": "StaticThresholdCriterion"}


# --------------------------------------------------------------------------- metric alerts
def test_directly_scoped_metric_alert_is_present():
    snap = compute_coverage([_vm()], [_action_group(), _metric_rule()], reference=_reference([CPU]))
    status, observed = _status(snap)
    assert status == STATUS_PRESENT
    assert observed["rule_name"] == "r1"
    assert observed["action_group_usable"] is True


def test_missing_metric_alert_is_missing():
    snap = compute_coverage([_vm()], [_action_group()], reference=_reference([CPU]))
    assert _status(snap)[0] == STATUS_MISSING


def test_multi_resource_alert_scoped_to_resource_group_counts_as_present():
    """A rule scoped to the RG with targetResourceType covers every child VM."""
    rule = _metric_rule(
        scopes=[f"/subscriptions/{SUB}/resourceGroups/{RG}"],
        target_type=VM_TYPE,
        target_region="westeurope",
    )
    snap = compute_coverage([_vm()], [_action_group(), rule], reference=_reference([CPU]))
    assert _status(snap)[0] == STATUS_PRESENT


def test_multi_resource_alert_scoped_to_subscription_counts_as_present():
    rule = _metric_rule(scopes=[f"/subscriptions/{SUB}"], target_type=VM_TYPE)
    snap = compute_coverage([_vm()], [_action_group(), rule], reference=_reference([CPU]))
    assert _status(snap)[0] == STATUS_PRESENT


def test_multi_resource_alert_for_a_different_type_does_not_match():
    rule = _metric_rule(scopes=[f"/subscriptions/{SUB}"], target_type="microsoft.storage/storageaccounts")
    snap = compute_coverage([_vm()], [_action_group(), rule], reference=_reference([CPU]))
    assert _status(snap)[0] == STATUS_MISSING


def test_multi_resource_alert_in_another_region_does_not_match():
    rule = _metric_rule(scopes=[f"/subscriptions/{SUB}"], target_type=VM_TYPE, target_region="eastus")
    snap = compute_coverage([_vm()], [_action_group(), rule], reference=_reference([CPU]))
    assert _status(snap)[0] == STATUS_MISSING


def test_container_scoped_rule_without_target_type_is_ignored():
    rule = _metric_rule(scopes=[f"/subscriptions/{SUB}/resourceGroups/{RG}"])
    snap = compute_coverage([_vm()], [_action_group(), rule], reference=_reference([CPU]))
    assert _status(snap)[0] == STATUS_MISSING


def test_disabled_rule_is_misconfigured():
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(enabled=False)], reference=_reference([CPU])
    )
    status, observed = _status(snap)
    assert status == STATUS_MISCONFIGURED
    assert "disabled" in observed["issues"]


def test_rule_without_action_group_is_misconfigured():
    snap = compute_coverage(
        [_vm()], [_metric_rule(action_groups=())], reference=_reference([CPU])
    )
    status, observed = _status(snap)
    assert status == STATUS_MISCONFIGURED
    assert "no action group" in observed["issues"]


def test_action_group_without_receivers_is_misconfigured():
    snap = compute_coverage(
        [_vm()], [_action_group(receivers=False), _metric_rule()], reference=_reference([CPU])
    )
    status, observed = _status(snap)
    assert status == STATUS_MISCONFIGURED
    assert "action group has no receivers" in observed["issues"]


def test_threshold_outside_tolerance_is_misconfigured():
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(threshold=40)], reference=_reference([CPU])
    )
    status, observed = _status(snap)
    assert status == STATUS_MISCONFIGURED
    assert "threshold differs from baseline" in observed["issues"]


def test_threshold_inside_tolerance_is_present():
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(threshold=85)], reference=_reference([CPU]),
        options=CoverageOptions(tolerance_pct=10),
    )
    assert _status(snap)[0] == STATUS_PRESENT


def test_dimension_filtered_recommendation_requires_matching_dimension():
    rec = {**CPU, "key": "d1", "metric": "ServiceApiResult",
           "dimensions": [{"name": "StatusCode", "operator": "Include", "values": ["403"]}]}
    without = _metric_rule(metric="ServiceApiResult")
    snap = compute_coverage([_vm()], [_action_group(), without], reference=_reference([rec]))
    assert _status(snap, "d1")[0] == STATUS_MISSING

    with_dim = _metric_rule(
        metric="ServiceApiResult",
        dimensions=[{"name": "StatusCode", "operator": "Include", "values": ["403"]}],
    )
    snap = compute_coverage([_vm()], [_action_group(), with_dim], reference=_reference([rec]))
    assert _status(snap, "d1")[0] == STATUS_PRESENT


# --------------------------------------------------------------------------- dynamic thresholds
def test_dynamic_threshold_recommendation_matched_by_dynamic_rule():
    rec = {**CPU, "criterion_type": "DynamicThresholdCriterion", "threshold": None,
           "alert_sensitivity": "Medium",
           "failing_periods": {"number_of_evaluation_periods": 2, "min_failing_periods_to_alert": 2}}
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(dynamic=True)], reference=_reference([rec])
    )
    status, observed = _status(snap)
    assert status == STATUS_PRESENT
    assert observed["observed_criterion_types"] == ["DynamicThresholdCriterion"]
    assert observed["observed_sensitivities"] == ["Medium"]


def test_static_rule_where_baseline_wants_dynamic_is_misconfigured():
    rec = {**CPU, "criterion_type": "DynamicThresholdCriterion", "threshold": None}
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(dynamic=False)], reference=_reference([rec])
    )
    status, observed = _status(snap)
    assert status == STATUS_MISCONFIGURED
    assert "dynamic threshold recommended" in observed["issues"]


# --------------------------------------------------------------------------- log alerts
LOG_REC = {
    "key": "L1",
    "name": "Low free disk space",
    "alert_type": "log",
    "threshold": 10.0,
    "log_query": (
        'InsightsMetrics\n| where Origin == "vm.azm.ms"\n'
        '| where Namespace == "LogicalDisk" and Name == "FreeSpacePercentage"\n'
        "| summarize AggregatedValue = avg(Val) by bin(TimeGenerated,15m), Computer, _ResourceId"
    ),
}


def test_unrelated_metric_rule_does_not_satisfy_a_log_recommendation():
    """Regression: an empty metric name previously made *any* rule count as a match."""
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule()], reference=_reference([LOG_REC])
    )
    assert _status(snap, "L1")[0] == STATUS_MISSING


def test_unrelated_log_rule_on_the_same_table_does_not_match():
    other = _log_rule(
        query='InsightsMetrics | where Namespace == "Memory" and Name == "AvailableMB"'
    )
    snap = compute_coverage([_vm()], [_action_group(), other], reference=_reference([LOG_REC]))
    assert _status(snap, "L1")[0] == STATUS_MISSING


def test_matching_log_rule_is_present():
    match = _log_rule(query=LOG_REC["log_query"])
    snap = compute_coverage([_vm()], [_action_group(), match], reference=_reference([LOG_REC]))
    status, observed = _status(snap, "L1")
    assert status == STATUS_PRESENT
    assert observed["rule_type"] == "microsoft.insights/scheduledqueryrules"


# --------------------------------------------------------------------------- activity log
SUB_TYPE = collector.SUBSCRIPTION_TYPE
SH_REC = {
    "key": "sh1",
    "name": "Service Health Incident",
    "alert_type": "activitylog",
    "threshold": None,
    "activity_log": {"category": "ServiceHealth", "incidentType": "Incident"},
}


def test_service_health_alert_scored_against_synthetic_subscription_row():
    ref = _reference([SH_REC], arm_type=SUB_TYPE, display="Subscription (platform)")
    rule = _activity_rule(category="ServiceHealth", incident_type="Incident")
    snap = compute_coverage([], [_action_group(), rule], reference=ref, subscriptions=[SUB])
    assert snap["kpis"]["total_resources_in_baseline"] == 1
    assert _status(snap, "sh1")[0] == STATUS_PRESENT


def test_service_health_alert_for_a_different_incident_type_does_not_match():
    ref = _reference([SH_REC], arm_type=SUB_TYPE)
    rule = _activity_rule(category="ServiceHealth", incident_type="Maintenance")
    snap = compute_coverage([], [_action_group(), rule], reference=ref, subscriptions=[SUB])
    assert _status(snap, "sh1")[0] == STATUS_MISSING


def test_missing_service_health_alert_is_reported():
    ref = _reference([SH_REC], arm_type=SUB_TYPE)
    snap = compute_coverage([], [_action_group()], reference=ref, subscriptions=[SUB])
    assert _status(snap, "sh1")[0] == STATUS_MISSING


def test_administrative_delete_alert_matches_on_operation_name():
    rec = {
        "key": "del1",
        "name": "Key Vault deleted",
        "alert_type": "activitylog",
        "threshold": None,
        "activity_log": {"category": "Administrative", "operationName": "Microsoft.KeyVault/vaults/delete"},
    }
    ref = _reference([rec], arm_type=VM_TYPE)
    rule = _activity_rule(category="Administrative", operation="Microsoft.KeyVault/vaults/delete")
    snap = compute_coverage([_vm()], [_action_group(), rule], reference=ref)
    assert _status(snap, "del1")[0] == STATUS_PRESENT


# --------------------------------------------------------------------------- suppression
def test_unconditional_suppression_rule_marks_alerts_suppressed():
    alerts = [_action_group(), _metric_rule(), _suppression()]
    snap = compute_coverage([_vm()], alerts, reference=_reference([CPU]))
    status, observed = _status(snap)
    assert status == STATUS_SUPPRESSED
    assert observed["suppressed_by"][0]["name"] == "mute"
    assert snap["kpis"]["alerts_suppressed"] == 1
    assert snap["suppression_rules"][0]["unconditional"] is True


def test_conditional_suppression_downgrades_to_misconfigured():
    rule = _suppression(conditions=[{"field": "Severity", "operator": "Equals", "values": ["Sev4"]}])
    snap = compute_coverage([_vm()], [_action_group(), _metric_rule(), rule], reference=_reference([CPU]))
    status, observed = _status(snap)
    assert status == STATUS_MISCONFIGURED
    assert "may be muted by an alert processing rule" in observed["issues"]


def test_disabled_suppression_rule_is_ignored():
    rule = _suppression()
    rule["properties"]["enabled"] = False
    snap = compute_coverage([_vm()], [_action_group(), _metric_rule(), rule], reference=_reference([CPU]))
    assert _status(snap)[0] == STATUS_PRESENT


def test_non_suppressing_action_rule_is_ignored():
    rule = _suppression()
    rule["properties"]["actions"] = [{"actionType": "AddActionGroups", "actionGroupIds": [AG_ID]}]
    snap = compute_coverage([_vm()], [_action_group(), _metric_rule(), rule], reference=_reference([CPU]))
    assert _status(snap)[0] == STATUS_PRESENT
    assert snap["kpis"]["suppression_rules"] == 0


# --------------------------------------------------------------------------- tags
def test_threshold_override_tag_changes_the_expected_threshold():
    rec = {**CPU, "threshold_override_tag": "_amba-Percentage CPU-threshold-Override_"}
    vm = _vm(tags={"_amba-Percentage CPU-threshold-Override_": "40"})
    snap = compute_coverage(
        [vm], [_action_group(), _metric_rule(threshold=40)], reference=_reference([rec])
    )
    status, observed = _status(snap)
    assert status == STATUS_PRESENT
    assert observed["effective_threshold"] == 40.0
    assert observed["threshold_override_tag"] == "_amba-Percentage CPU-threshold-Override_"


def test_threshold_override_tag_is_case_insensitive():
    rec = {**CPU, "threshold_override_tag": "_amba-Percentage CPU-threshold-Override_"}
    vm = _vm(tags={"_AMBA-percentage cpu-THRESHOLD-override_": "40"})
    snap = compute_coverage(
        [vm], [_action_group(), _metric_rule(threshold=40)], reference=_reference([rec])
    )
    assert _status(snap)[0] == STATUS_PRESENT


def test_monitor_disable_tag_excludes_the_resource():
    vm = _vm(tags={"MonitorDisable": "true"})
    snap = compute_coverage([vm], [_action_group()], reference=_reference([CPU]))
    assert snap["kpis"]["recommended_total"] == 0
    assert snap["kpis"]["resources_excluded"] == 1
    assert snap["excluded_resources"][0]["resource_name"] == "vm1"


def test_monitor_disable_tag_can_be_ignored():
    vm = _vm(tags={"MonitorDisable": "true"})
    snap = compute_coverage(
        [vm], [_action_group()], reference=_reference([CPU]),
        options=CoverageOptions(honor_monitor_disable_tag=False),
    )
    assert snap["kpis"]["resources_excluded"] == 0
    assert _status(snap)[0] == STATUS_MISSING


# --------------------------------------------------------------------------- tiers / patterns
def test_optional_tier_alerts_are_not_scored_by_default():
    ref = _reference([{**CPU, "tier": "optional"}])
    snap = compute_coverage([_vm()], [_action_group()], reference=ref)
    assert snap["kpis"]["recommended_total"] == 0


def test_optional_tier_alerts_can_be_enabled():
    ref = _reference([{**CPU, "tier": "optional"}])
    snap = compute_coverage(
        [_vm()], [_action_group()], reference=ref,
        options=CoverageOptions(tiers=("core", "recommended", "optional")),
    )
    assert snap["kpis"]["recommended_total"] == 1


def test_pattern_filter_restricts_scoring():
    ref = _reference([{**CPU, "patterns": ["hpc"]}])
    snap = compute_coverage(
        [_vm()], [_action_group()], reference=ref, options=CoverageOptions(patterns=("alz",))
    )
    assert snap["kpis"]["recommended_total"] == 0

    snap = compute_coverage(
        [_vm()], [_action_group()], reference=ref, options=CoverageOptions(patterns=("hpc",))
    )
    assert snap["kpis"]["recommended_total"] == 1


def test_non_deployable_alerts_are_not_scored():
    ref = _reference([{**CPU, "deployable": False}])
    snap = compute_coverage([_vm()], [_action_group()], reference=ref)
    assert snap["kpis"]["recommended_total"] == 0


# --------------------------------------------------------------------------- severity
def test_severity_mismatch_is_ignored_by_default():
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(severity=4)], reference=_reference([CPU])
    )
    assert _status(snap)[0] == STATUS_PRESENT


def test_severity_mismatch_can_be_flagged():
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(severity=4)], reference=_reference([CPU]),
        options=CoverageOptions(severity_counts_as_gap=True),
    )
    status, observed = _status(snap)
    assert status == STATUS_MISCONFIGURED
    assert any("severity 4 differs" in issue for issue in observed["issues"])


# --------------------------------------------------------------------------- scoring
def test_coverage_percentage_and_kpis():
    ref = _reference([CPU, {**CPU, "key": "a2", "metric": "Available Memory Bytes", "threshold": 1000.0}])
    snap = compute_coverage([_vm()], [_action_group(), _metric_rule()], reference=ref)
    assert snap["kpis"]["recommended_total"] == 2
    assert snap["kpis"]["alerts_present"] == 1
    assert snap["kpis"]["alerts_missing"] == 1
    assert snap["coverage_pct"] == 50
    assert snap["kpis"]["action_groups"] == 1
    assert snap["kpis"]["action_groups_usable"] == 1


def test_misconfigured_half_credit_mode():
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(enabled=False)], reference=_reference([CPU]),
        options=CoverageOptions(misconfig_counts_as_gap=False),
    )
    assert snap["coverage_pct"] == 50


@pytest.mark.parametrize("scope", [f"/subscriptions/{SUB}", f"/subscriptions/{SUB}/resourceGroups/{RG}"])
def test_suppression_scope_covers_child_resources(scope):
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(), _suppression(scopes=[scope])],
        reference=_reference([CPU]),
    )
    assert _status(snap)[0] == STATUS_SUPPRESSED


def test_suppression_scoped_elsewhere_does_not_apply():
    other = f"/subscriptions/{SUB}/resourceGroups/rg-other"
    snap = compute_coverage(
        [_vm()], [_action_group(), _metric_rule(), _suppression(scopes=[other])],
        reference=_reference([CPU]),
    )
    assert _status(snap)[0] == STATUS_PRESENT

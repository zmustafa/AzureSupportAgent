"""Alerts Manager Phase 3/4 metric, log-query, and Activity Log rule tests."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.alerts_manager import rules, service


METRIC = {
    "name": "cpu-high",
    "subscription_id": "sub-1",
    "resource_group": "rg-monitoring",
    "location": "Global",
    "enabled": False,
    "severity": 2,
    "description": "CPU high",
    "scopes": ["/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm1"],
    "action_group_ids": [],
    "evaluation_frequency": "PT5M",
    "window_size": "PT15M",
    "auto_mitigate": True,
    "target_resource_type": "",
    "target_resource_region": "",
    "conditions": [
        {
            "name": "cpu",
            "metric_name": "Percentage CPU",
            "metric_namespace": "Microsoft.Compute/virtualMachines",
            "threshold_type": "static",
            "operator": "GreaterThan",
            "threshold": 80,
            "aggregation": "Average",
            "dimensions": [],
            "min_failing_periods": 1,
            "evaluation_periods": 1,
        }
    ],
    "tags": {},
}


def test_metric_rule_validation_and_disabled_by_default_body() -> None:
    assert rules.validate_metric_payload(METRIC, create=True) == []
    body = rules.build_metric_body(METRIC)
    assert body["properties"]["enabled"] is False
    assert body["properties"]["criteria"]["allOf"][0]["threshold"] == 80.0
    assert body["properties"]["criteria"]["allOf"][0]["metricNamespace"] == "Microsoft.Compute/virtualMachines"


def test_dynamic_metric_constraints_are_enforced() -> None:
    payload = json.loads(json.dumps(METRIC))
    payload["conditions"][0].update({"threshold_type": "dynamic", "operator": "GreaterOrLessThan"})
    payload["conditions"].append({**payload["conditions"][0], "name": "another"})
    assert any("multiple conditions" in error for error in rules.validate_metric_payload(payload, create=True))


def test_metric_dimensions_are_emitted() -> None:
    payload = json.loads(json.dumps(METRIC))
    payload["conditions"][0]["dimensions"] = [{"name": "ApiName", "operator": "Include", "values": ["GetBlob"]}]
    body = rules.build_metric_body(payload)
    assert body["properties"]["criteria"]["allOf"][0]["dimensions"] == [{"name": "ApiName", "operator": "Include", "values": ["GetBlob"]}]


def test_log_alert_validation_body_and_cost_warning() -> None:
    payload = {
        **METRIC,
        "name": "container-errors",
        "location": "southcentralus",
        "scopes": ["/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/ws"],
        "conditions": [{"query": "ContainerAppConsoleLogs_CL | where Log_s contains 'error' | summarize count()", "aggregation": "Count", "operator": "GreaterThan", "threshold": 0, "min_failing_periods": 1, "evaluation_periods": 1, "dimensions": []}],
    }
    assert rules.validate_log_payload(payload, create=True) == []
    body = rules.build_log_body(payload)
    assert body["kind"] == "LogAlert"
    assert body["properties"]["skipQueryValidation"] is False
    assert body["properties"]["criteria"]["allOf"][0]["query"].startswith("ContainerApp")
    assert rules.cost_advisory("log", payload)["warnings"]


def test_kql_external_sources_and_control_commands_are_rejected() -> None:
    assert rules.validate_kql(".show tables")
    assert rules.validate_kql("externaldata(x:string)[h'https://example.test/data']")
    assert rules.validate_kql("Heartbeat | take 10") == []


def test_activity_alert_body_supports_service_and_resource_health_conditions() -> None:
    payload = {
        **METRIC,
        "name": "resource-health",
        "severity": None,
        "evaluation_frequency": "",
        "window_size": "",
        "scopes": ["/subscriptions/sub-1"],
        "activity_conditions": [
            {"field": "category", "equals": "ResourceHealth"},
            {"field": "properties.currentHealthStatus", "equals": "Unavailable"},
        ],
    }
    assert rules.validate_activity_payload(payload, create=True) == []
    body = rules.build_activity_body(payload)
    assert body["location"] == "Global"
    assert body["properties"]["condition"]["allOf"][0]["equals"] == "ResourceHealth"


def test_public_rule_exposes_activity_category_only_for_activity_rules() -> None:
    activity = rules.public_rule({
        "id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Insights/activityLogAlerts/health",
        "name": "health", "type": "microsoft.insights/activitylogalerts", "location": "Global",
        "properties": {"enabled": True, "scopes": ["/subscriptions/sub-1"], "condition": {"allOf": [
            {"field": "level", "equals": "Error"}, {"field": "category", "equals": "ResourceHealth"},
        ]}},
    })
    metric = rules.public_rule({
        "id": METRIC["scopes"][0], "name": "cpu", "type": "microsoft.insights/metricalerts",
        "properties": {"enabled": True, "severity": 2, "scopes": METRIC["scopes"], "criteria": {"allOf": []}},
    })
    assert activity["category"] == "ResourceHealth"
    assert metric["category"] == ""


def test_multi_resource_metric_requires_type_and_region() -> None:
    payload = json.loads(json.dumps(METRIC))
    payload["scopes"].append("/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm2")
    errors = rules.validate_metric_payload(payload, create=True)
    assert any("target resource type" in error for error in errors)
    assert any("target resource region" in error for error in errors)


def test_log_rule_uses_supported_stable_api_version() -> None:
    assert rules.api_for_family("log") == ("microsoft.insights/scheduledqueryrules", "2022-06-15")


def test_log_rule_requires_exactly_one_workspace_scope() -> None:
    payload = {
        **json.loads(json.dumps(METRIC)),
        "location": "southcentralus",
        "conditions": [{"query": "AzureDiagnostics | take 1", "aggregation": "Count", "operator": "GreaterThan", "threshold": 0, "dimensions": []}],
    }
    payload["scopes"] = ["/subscriptions/sub-1/resourceGroups/rg-monitoring"]
    assert "Log alerts must target exactly one Log Analytics workspace." in rules.validate_log_payload(payload, create=True)
    payload["scopes"] = [
        "/subscriptions/sub-1/resourceGroups/rg-monitoring/providers/Microsoft.OperationalInsights/workspaces/law-one",
        "/subscriptions/sub-1/resourceGroups/rg-monitoring/providers/Microsoft.OperationalInsights/workspaces/law-two",
    ]
    assert "Log alerts must target exactly one Log Analytics workspace." in rules.validate_log_payload(payload, create=True)


@pytest.mark.asyncio
async def test_log_preview_rejects_non_workspace_arm_scope() -> None:
    with pytest.raises(ValueError, match="Select a Log Analytics workspace"):
        await rules.log_preview(
            {"id": "connection"},
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Resources/resourceGroups/rg",
            "AzureDiagnostics | take 1",
        )

@pytest.mark.asyncio
async def test_rule_apply_blocks_stale_state(monkeypatch) -> None:
    before = {
        "id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/cpu",
        "type": "microsoft.insights/metricalerts",
        "location": "Global",
        "properties": {"enabled": True, "severity": 2, "scopes": METRIC["scopes"], "criteria": {"allOf": []}},
    }
    change = SimpleNamespace(
        target_type="metric_rule",
        target_id=before["id"],
        operation="update",
        expected_state_hash=service.canonical_hash(service._resource_body(before)),
        desired_encrypted=service.encrypted_json({"body": service._resource_body(before)}),
    )
    live = json.loads(json.dumps(before))
    live["properties"]["enabled"] = False

    async def get_live(*_args, **_kwargs):
        return live, 200, ""

    monkeypatch.setattr(rules, "get_rule", get_live)
    resource, status, error = await rules.apply_rule_change({"read_only": False}, change)
    assert resource is None
    assert status == 409
    assert "changed after" in error


@pytest.mark.asyncio
async def test_metric_catalog_normalizes_azure_definitions(monkeypatch) -> None:
    raw = [{
        "name": {"value": "Transactions", "localizedValue": "Transactions"},
        "namespace": "Microsoft.Storage/storageAccounts",
        "unit": "Count",
        "primaryAggregationType": "Total",
        "supportedAggregationTypes": ["Total", "Average"],
        "metricAvailabilities": [{"timeGrain": "PT1M"}, {"timeGrain": "PT5M"}],
        "dimensions": [
            {"name": {"value": "ApiName", "localizedValue": "API name"}},
            {"value": "StatusCode", "localizedValue": "Status code"},
        ],
    }]

    async def token(_connection):
        return "token"

    async def definitions(_token, _resource_id):
        return json.dumps(raw), None

    monkeypatch.setattr(service, "_token", token)
    monkeypatch.setattr("app.azure.arm.get_metric_definitions", definitions)
    result = await rules.metric_definitions({}, "/subscriptions/s/resource")
    assert result[0]["name"] == "Transactions"
    assert result[0]["primary_aggregation"] == "Total"
    assert result[0]["dimensions"][0]["name"] == "ApiName"
    assert result[0]["dimensions"][1]["name"] == "StatusCode"

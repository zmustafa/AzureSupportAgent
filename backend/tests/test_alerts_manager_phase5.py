"""Alerts Manager advanced-rule, bulk, authoring, and advisory contracts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alerts_manager import advisory, rules, service
from app.api import alerts_manager as alerts_api
from app.api.alerts_manager import router
from app.core.db import Base
from app.core.security import Principal


SMART = {
    "name": "smart-failure", "subscription_id": "sub-1", "resource_group": "rg-monitoring", "location": "global",
    "enabled": False, "severity": 2, "description": "Failure anomalies",
    "scopes": ["/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.Insights/components/appi"],
    "action_group_ids": [], "detector_id": "FailureAnomaliesDetector", "detector_parameters": {},
    "frequency": "PT1M", "throttling_duration": "PT15M", "tags": {},
}


def test_smart_detector_validation_and_body() -> None:
    assert rules.validate_smart_payload(SMART, create=True) == []
    body = rules.build_smart_body(SMART)
    assert body["properties"]["state"] == "Disabled"
    assert body["properties"]["severity"] == "Sev2"
    assert body["properties"]["detector"]["id"] == "FailureAnomaliesDetector"
    assert body["properties"]["throttling"]["duration"] == "PT15M"


PROMETHEUS = {
    "name": "prometheus-platform", "subscription_id": "sub-1", "resource_group": "rg-monitoring", "location": "eastus",
    "enabled": False, "description": "Managed Prometheus alerts",
    "scopes": ["/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Monitor/accounts/amw"],
    "action_group_ids": [], "interval": "PT5M", "cluster_name": "aks-prod", "tags": {},
    "prometheus_rules": [{
        "alert": "HighErrorRate", "expression": "sum(rate(http_requests_total{code=~\"5..\"}[5m])) by (job) > 10",
        "enabled": True, "for": "PT5M", "severity": 2,
        "labels": {"team": "platform"}, "annotations": {"summary": "High error rate"},
        "actions": [{"actionGroupId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/ops"}],
    }],
}


def test_prometheus_validation_and_body() -> None:
    assert rules.validate_prometheus_payload(PROMETHEUS, create=True) == []
    body = rules.build_prometheus_body(PROMETHEUS)
    assert body["properties"]["enabled"] is False
    assert body["properties"]["interval"] == "PT5M"
    assert body["properties"]["rules"][0]["expression"].startswith("sum(rate")
    assert body["properties"]["rules"][0]["actions"][0]["actionGroupId"].endswith("/ops")


def test_promql_defensive_validation() -> None:
    assert rules.validate_promql("sum(rate(requests_total[5m])) > 10") == []
    assert any("unbalanced" in item for item in rules.validate_promql("sum(rate(requests_total[5m])"))
    bad = json.loads(json.dumps(PROMETHEUS))
    bad["interval"] = "PT30M"
    bad["scopes"].append("/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Monitor/accounts/other")
    errors = rules.validate_prometheus_payload(bad, create=True)
    assert any("exactly one" in item for item in errors)
    assert any("1 and 15" in item for item in errors)


def test_phase5_api_routes_are_registered() -> None:
    paths = {route.path for route in router.routes}
    assert "/alerts-manager/alert-rules/bulk-changes" in paths
    assert "/alerts-manager/authoring/options" in paths
    assert "/alerts-manager/authoring/resolve" in paths
    assert "/alerts-manager/notifications/simulate" in paths
    assert "/alerts-manager/action-groups/suggestions" in paths
    assert "/alerts-manager/alert-rules/noise-guard" in paths


@pytest.mark.asyncio
async def test_bulk_rule_preparation_reuses_token_caps_six_and_preserves_order(monkeypatch, tmp_path: Path) -> None:
    targets = [
        f"/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/rule-{index:02d}"
        for index in range(12)
    ]
    token_calls = 0
    active = 0
    max_active = 0
    first_wave = asyncio.Event()

    async def token(_connection):
        nonlocal token_calls
        token_calls += 1
        return "shared-token"

    async def arm_write(received_token, method, path, **_kwargs):
        nonlocal active, max_active
        assert received_token == "shared-token"
        assert method == "GET"
        active += 1
        max_active = max(max_active, active)
        if active == 6:
            first_wave.set()
        await first_wave.wait()
        active -= 1
        return {
            "id": path, "name": path.rsplit("/", 1)[-1], "type": "microsoft.insights/metricalerts",
            "location": "global", "properties": {"enabled": True, "severity": 2, "scopes": []},
        }, None, 200

    monkeypatch.setattr(alerts_api, "_connection", lambda *_args, **_kwargs: {"id": "connection-1", "read_only": False})
    monkeypatch.setattr(service, "_token", token)
    monkeypatch.setattr("app.azure.arm.arm_write", arm_write)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bulk.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    principal = Principal("operator", "operator@example.test", "tenant-bulk", "admin", frozenset({"alerts_manager.bulk_write"}))
    try:
        async with Session() as db:
            result = await alerts_api.request_bulk_rule_changes(
                alerts_api.BulkRuleChangeRequest(
                    connection_id="connection-1", action="delete", reason="Bulk cleanup",
                    targets=[alerts_api.BulkRuleTarget(target_id=target, family="metric") for target in targets],
                ),
                principal,
                db,
            )
        assert token_calls == 1
        assert max_active == 6
        assert [change["target_id"] for change in result["changes"]] == targets
        async with Session() as db:
            second_page = await alerts_api.list_changes(
                connection_id="connection-1", status="", page=2, page_size=5, principal=principal, db=db,
            )
        assert second_page["total"] == 12
        assert second_page["page"] == 2
        assert second_page["page_size"] == 5
        assert len(second_page["changes"]) == 5
        assert second_page["pending_count"] == 12
        assert second_page["approved_count"] == 0
        assert second_page["actionable_count"] == 12
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_authoring_scope_resolver_returns_friendly_azure_metadata(monkeypatch) -> None:
    resource_id = "/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-one"

    async def fake_arg(_connection, query, _subscriptions, **_kwargs):
        if query.startswith("resources"):
            return [{
                "id": resource_id,
                "name": "vm-one",
                "type": "microsoft.compute/virtualmachines",
                "kind": "",
                "subscriptionId": "sub-1",
                "resourceGroup": "rg-app",
                "location": "eastus",
            }]
        return []

    async def fake_token(_connection):
        return "token"

    async def fake_subscriptions(_token):
        return [{"id": "sub-1", "name": "Production Subscription", "state": "Enabled", "is_default": False}], None

    monkeypatch.setattr(alerts_api, "_connection", lambda _connection_id: {"id": "connection-1"})
    monkeypatch.setattr(service, "_arg", fake_arg)
    monkeypatch.setattr(service, "_token", fake_token)
    monkeypatch.setattr("app.azure.arm.list_subscriptions", fake_subscriptions)

    result = await alerts_api.resolve_authoring_scopes(
        alerts_api.AuthoringResolveRequest(connection_id="connection-1", resource_ids=[resource_id, "sub-1"]),
        None,
    )

    assert result["resources"] == [{
        "kind": "resource",
        "id": resource_id,
        "name": "vm-one",
        "subscription_id": "sub-1",
        "subscription_name": "Production Subscription",
        "resource_group": "rg-app",
        "resource_type": "microsoft.compute/virtualmachines",
        "location": "eastus",
    }, {
        "kind": "subscription",
        "id": "sub-1",
        "name": "Production Subscription",
        "subscription_id": "sub-1",
        "subscription_name": "Production Subscription",
        "resource_group": "",
        "resource_type": "",
        "location": "",
    }]


def test_dimension_overlap_distinguishes_disjoint_partial_and_exact() -> None:
    prod = [{"name": "environment", "operator": "Include", "values": ["prod"]}]
    stage = [{"name": "environment", "operator": "Include", "values": ["stage"]}]
    mixed = [{"name": "environment", "operator": "Include", "values": ["prod", "stage"]}]
    assert advisory.classify_dimension_overlap(prod, stage) == "disjoint"
    assert advisory.classify_dimension_overlap(prod, mixed) == "partial"
    assert advisory.classify_dimension_overlap(prod, prod) == "exact"


def test_kql_and_promql_semantic_keys_ignore_format_and_time_windows() -> None:
    assert advisory._kql_semantic_key("Heartbeat | where TimeGenerated > ago(5m) | summarize count()") == advisory._kql_semantic_key("Heartbeat\n| where TimeGenerated > ago(15m)\n| summarize count()")
    assert advisory._promql_semantic_key('sum(rate(http_requests_total{job="api",code="500"}[5m])) > 10') == advisory._promql_semantic_key('sum(rate(http_requests_total{code="500", job="api"}[15m])) > 20')


@pytest.mark.asyncio
async def test_notification_simulator_direct_group_override_and_delivery_metadata(monkeypatch) -> None:
    rule_id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/cpu"
    inherited = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/inherited"
    selected = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/selected"

    async def list_rules(*_args, **_kwargs):
        return [{"id": rule_id, "name": "cpu", "family": "metric", "severity": 2, "enabled": True, "scopes": ["/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"], "action_group_ids": [inherited], "description": ""}]

    async def get_rule(*_args, **_kwargs):
        return {"id": rule_id, "name": "cpu", "type": "microsoft.insights/metricalerts", "properties": {"enabled": True, "severity": 2, "scopes": ["/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"], "criteria": {"allOf": []}, "autoMitigate": True}}, 200, ""

    async def list_fired(*_args, **_kwargs): return []
    async def list_groups(*_args, **_kwargs):
        return [{"id": selected, "name": "selected", "enabled": True, "receivers": [{"type": "webhook", "name": "hook", "masked": "hooks.example.com", "fingerprint": "fp", "enabled": True, "use_common_alert_schema": True}]}]

    monkeypatch.setattr(rules, "list_rules", list_rules)
    monkeypatch.setattr(rules, "get_rule", get_rule)
    monkeypatch.setattr(service, "list_fired_alerts", list_fired)
    monkeypatch.setattr(service, "list_action_groups", list_groups)
    monkeypatch.setattr("app.alerts_manager.delivery_history.for_groups", lambda *_args, **_kwargs: [])
    result = await advisory.simulate_notification_path({}, {"rule_id": rule_id, "resource_id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm", "selected_action_group_ids": [selected], "use_selected_only": True, "monitor_condition": "Resolved"})
    assert result["final_action_group_ids"] == [selected]
    assert result["paths"][0]["receivers"][0]["payload_schema"] == "common"
    assert result["paths"][0]["receivers"][0]["payload_preview"]["essentials"]["monitorCondition"] == "Resolved"


def test_bulk_notification_simulator_builds_sankey_routes_and_diagnostics() -> None:
    resource = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
    group_a = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/a"
    group_b = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/b"
    rules_inventory = [{
        "id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/cpu",
        "name": "cpu", "family": "metric", "severity": 1, "enabled": True,
        "scopes": [resource], "action_group_ids": [group_a, group_b],
    }, {
        "id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/disabled",
        "name": "disabled", "family": "metric", "severity": 3, "enabled": False,
        "scopes": [resource], "action_group_ids": [group_a],
    }]
    groups = [
        {"id": group_a, "name": "a", "enabled": True, "receivers": [{"type": "email", "name": "ops", "destination": "ops@example.com", "masked": "ops@example.com", "fingerprint": "same", "enabled": True, "use_common_alert_schema": True}]},
        {"id": group_b, "name": "b", "enabled": True, "receivers": [{"type": "email", "name": "ops", "destination": "ops@example.com", "masked": "ops@example.com", "fingerprint": "same", "enabled": True, "use_common_alert_schema": True}]},
    ]
    result = advisory.build_bulk_notification_simulation(rules_inventory, groups)
    assert result["summary"]["rules"] == 2
    assert result["summary"]["would_deliver"] == 2
    assert result["summary"]["blocked"] == 1
    assert {node["kind"] for node in result["nodes"]} == {"resource", "alert", "action_group", "receiver", "outcome"}
    assert any(item["code"] == "duplicate_receiver_path" for item in result["diagnostics"])
    assert any(route["outcome"] == "disabled" for route in result["routes"])


@pytest.mark.asyncio
async def test_noise_guard_detects_exact_overlap(monkeypatch) -> None:
    metric = {
        "name": "cpu-high", "subscription_id": "sub-1", "resource_group": "rg-monitoring", "location": "Global",
        "enabled": False, "severity": 2, "description": "CPU high",
        "scopes": ["/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm1"],
        "action_group_ids": [], "evaluation_frequency": "PT5M", "window_size": "PT15M", "auto_mitigate": True,
        "target_resource_type": "", "target_resource_region": "", "tags": {},
        "conditions": [{"name": "cpu", "metric_name": "Percentage CPU", "metric_namespace": "Microsoft.Compute/virtualMachines", "threshold_type": "static", "operator": "GreaterThan", "threshold": 80, "aggregation": "Average", "dimensions": [], "min_failing_periods": 1, "evaluation_periods": 1}],
    }
    existing = {**metric, "id": "/subscriptions/sub-1/resourceGroups/rg-monitoring/providers/Microsoft.Insights/metricAlerts/existing", "family": "metric", "type": "microsoft.insights/metricalerts", "state_hash": "x", "condition_count": 1}

    async def list_rules(*_args, **_kwargs):
        return [existing]

    async def get_rule(*_args, **_kwargs):
        return {"id": existing["id"], "type": existing["type"], "name": "existing", **rules.build_metric_body(metric)}, 200, ""

    monkeypatch.setattr(rules, "list_rules", list_rules)
    monkeypatch.setattr(rules, "get_rule", get_rule)
    result = await advisory.noise_guard({}, "metric", metric)
    assert result["overlap"] is True
    assert result["findings"][0]["type"] == "exact"


@pytest.mark.asyncio
async def test_ownership_suggestions_never_return_owner_or_receiver_email(monkeypatch) -> None:
    group_id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/platform"

    monkeypatch.setattr("app.ownership.resolve.build_context", lambda _tenant: {})
    monkeypatch.setattr("app.ownership.resolve.resolve_owner", lambda *_args, **_kwargs: {"source": "direct", "owners": [{"display_name": "Platform Team", "email": "platform@example.com", "role": "technical", "primary": True}]})

    async def groups(*_args, **_kwargs):
        return [{"id": group_id, "name": "platform", "short_name": "plat", "subscription_id": "s", "receiver_count": 1, "tags": {}}]

    async def arg(*_args, **_kwargs):
        return [{"id": group_id, "properties": {"emailReceivers": [{"emailAddress": "platform@example.com"}]}}]

    monkeypatch.setattr(service, "list_action_groups", groups)
    monkeypatch.setattr(service, "_arg", arg)
    result = await advisory.suggest_action_groups({}, "tenant", subject_kind="resource", subject_id="/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm")
    assert result["suggestions"][0]["confidence"] == 0.95
    serialized = json.dumps(result)
    assert "platform@example.com" not in serialized

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.alerts_manager import activity_planner, advisory, rules, service, service_health
from app.api import alerts_manager as alerts_api


def _activity_rule(name: str, category: str, conditions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": f"/subscriptions/sub-1/resourceGroups/monitor/providers/Microsoft.Insights/activityLogAlerts/{name}",
        "name": name,
        "family": "activity",
        "category": category,
        "severity": None,
        "enabled": True,
        "scopes": ["/subscriptions/sub-1"],
        "action_group_ids": [],
        "activity_conditions": [{"field": "category", "equals": category}, *conditions],
    }


def test_canonical_service_health_taxonomy_matches_azure_activity_log_values() -> None:
    assert service_health.SERVICE_HEALTH_EVENT_TYPE_INCIDENT_TYPES == {
        "service_issue": ("Incident",),
        "planned_maintenance": ("Maintenance",),
        "health_advisory": ("Informational", "ActionRequired"),
        "security_advisory": ("Security",),
    }
    assert set(service_health.SERVICE_HEALTH_INCIDENT_TYPES) == {
        "Incident", "Maintenance", "Informational", "ActionRequired", "Security",
    }


@pytest.mark.parametrize(
    ("conditions", "expected"),
    [
        ([{"field": "PROPERTIES.INCIDENTTYPE", "equals": "incident"}], ("service_issue",)),
        ([{"field": "properties.incidentType", "containsAny": ["Informational", "ActionRequired"]}], ("health_advisory",)),
        ([{"anyOf": [
            {"field": "properties.incidentType", "equals": "Maintenance"},
            {"field": "properties.incidentType", "equals": "Security"},
        ]}], ("planned_maintenance", "security_advisory")),
    ],
)
def test_condition_projection_supports_equals_contains_any_any_of_and_case(
    conditions: list[dict[str, Any]], expected: tuple[str, ...],
) -> None:
    projection = service_health.project_service_health_conditions(conditions)
    assert projection.event_types == expected
    assert projection.unrestricted is False
    assert projection.unmapped is False


def test_condition_projection_honors_all_of_and_unrestricted_rules() -> None:
    restricted = service_health.project_service_health_conditions([
        {"field": "properties.incidentType", "containsAny": ["Incident", "Maintenance"]},
        {"field": "properties.incidentType", "equals": "Maintenance"},
        {"field": "properties.region", "equals": "eastus"},
    ])
    assert restricted.event_types == ("planned_maintenance",)

    unrestricted = service_health.project_service_health_conditions([
        {"field": "category", "equals": "ServiceHealth"},
        {"field": "properties.region", "equals": "eastus"},
    ])
    assert unrestricted.event_types == service_health.SERVICE_HEALTH_EVENT_TYPES
    assert unrestricted.unrestricted is True


def test_unknown_incident_values_are_explicitly_unmapped() -> None:
    projection = service_health.project_service_health_conditions([
        {"field": "properties.incidentType", "containsAny": ["Incident", "FutureNotice"]},
    ])
    assert projection.event_types == ("service_issue",)
    assert projection.unmapped is True
    assert projection.unmapped_values == ("FutureNotice",)


def test_resource_health_metadata_projects_all_portal_dimensions() -> None:
    rule = _activity_rule("resource", "ResourceHealth", [
        {"field": "status", "containsAny": ["Active", "In Progress", "Updated"]},
        {"field": "properties.currentHealthStatus", "containsAny": ["Available", "Unavailable"]},
        {"field": "properties.previousHealthStatus", "equals": "Unknown"},
        {"anyOf": [
            {"field": "properties.cause", "equals": "Platform Initiated"},
            {"field": "properties.cause", "equals": "UserInitiated"},
        ]},
    ])

    metadata = service_health.rule_activity_metadata(rule)

    assert metadata["resource_health_event_statuses"] == ["active", "in_progress", "updated"]
    assert metadata["resource_health_current_statuses"] == ["available", "unavailable"]
    assert metadata["resource_health_previous_statuses"] == ["unknown"]
    assert metadata["resource_health_reason_types"] == ["platform_initiated", "user_initiated"]
    assert metadata["activity_unmapped_values"] == {}


def test_resource_health_missing_dimensions_are_unrestricted() -> None:
    metadata = service_health.rule_activity_metadata(
        _activity_rule("resource", "ResourceHealth", [
            {"field": "properties.currentHealthStatus", "equals": "Degraded"},
        ])
    )

    assert metadata["resource_health_current_statuses"] == ["degraded"]
    assert set(metadata["activity_unrestricted_fields"]) == {
        "resource_health_event_statuses",
        "resource_health_previous_statuses",
        "resource_health_reason_types",
    }


def test_recommendation_metadata_supports_current_advisor_categories_and_impacts() -> None:
    rule = _activity_rule("recommendation", "Recommendation", [
        {"field": "properties.recommendationCategory", "containsAny": [
            "Cost", "Performance", "High Availability", "OperationalExcellence", "Security",
        ]},
        {"field": "properties.recommendationImpact", "containsAny": ["High", "Medium", "Low"]},
    ])

    metadata = service_health.rule_activity_metadata(rule)

    assert metadata["recommendation_categories"] == [
        "cost", "performance", "high_availability", "operational_excellence", "security",
    ]
    assert metadata["recommendation_impacts"] == ["high", "medium", "low"]
    assert metadata["activity_unmapped_values"] == {}


def test_activity_metadata_preserves_unknown_values_by_dimension() -> None:
    metadata = service_health.rule_activity_metadata(
        _activity_rule("future", "Recommendation", [
            {"field": "properties.recommendationCategory", "equals": "FuturePillar"},
        ])
    )

    assert metadata["recommendation_categories"] == []
    assert metadata["activity_unmapped_values"] == {
        "recommendation_categories": ["FuturePillar"],
    }


def test_bulk_graph_filters_service_health_rules_and_exports_normalized_metadata() -> None:
    inventory = [
        _activity_rule("incident", "ServiceHealth", [{"field": "properties.incidentType", "equals": "Incident"}]),
        _activity_rule("maintenance", "ServiceHealth", [{"field": "properties.incidentType", "equals": "Maintenance"}]),
        _activity_rule("health-info", "ServiceHealth", [{"field": "properties.incidentType", "equals": "Informational"}]),
        _activity_rule("health-action", "ServiceHealth", [{"field": "properties.incidentType", "equals": "ActionRequired"}]),
        _activity_rule("security", "ServiceHealth", [{"field": "properties.incidentType", "equals": "Security"}]),
        _activity_rule("all-service-health", "ServiceHealth", []),
        _activity_rule("resource-health", "ResourceHealth", [{"field": "properties.currentHealthStatus", "equals": "Unavailable"}]),
    ]

    result = advisory.build_bulk_notification_simulation(
        inventory, [], families={"activity"}, activity_categories={"ServiceHealth"},
        service_health_event_types={"health_advisory"},
    )

    assert result["summary"]["rules"] == 3
    assert {rule["name"] for rule in result["rules"]} == {
        "health-info", "health-action", "all-service-health",
    }
    assert {route["rule_name"] for route in result["routes"]} == {
        "health-info", "health-action", "all-service-health",
    }
    unrestricted = next(route for route in result["routes"] if route["rule_name"] == "all-service-health")
    assert unrestricted["service_health_event_types"] == list(service_health.SERVICE_HEALTH_EVENT_TYPES)
    assert unrestricted["service_health_unrestricted"] is True
    assert all(route["activity_category"] == "ServiceHealth" for route in result["routes"])


@pytest.mark.asyncio
async def test_bulk_simulation_facets_and_resource_states_use_the_same_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = [
        _activity_rule("incident", "ServiceHealth", [{"field": "properties.incidentType", "equals": "Incident"}]),
        _activity_rule("maintenance", "ServiceHealth", [{"field": "properties.incidentType", "equals": "Maintenance"}]),
        _activity_rule("future", "ServiceHealth", [{"field": "properties.incidentType", "equals": "FutureNotice"}]),
        _activity_rule("all", "ServiceHealth", []),
    ]

    async def list_rules(*_args, **kwargs):
        return (inventory, {}) if kwargs.get("with_metadata") else inventory

    async def list_groups(*_args, **kwargs):
        return ([], {}) if kwargs.get("with_metadata") else []

    async def scope_context(*_args, **_kwargs):
        return {
            "scope": {"kind": "subscription", "id": "sub-1", "name": "sub-1"},
            "resources": [{
                "id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
                "name": "vm", "resource_type": "microsoft.compute/virtualmachines",
                "resource_group": "rg", "subscription_id": "sub-1", "subscription_name": "sub-1",
                "workload_ids": [], "accessible": True,
            }],
            "workloads": [], "subscriptions": [],
            "completeness": {"complete": True, "partial": False, "warnings": []},
        }

    monkeypatch.setattr(rules, "list_rules", list_rules)
    monkeypatch.setattr(service, "list_action_groups", list_groups)
    monkeypatch.setattr(advisory, "_scope_resource_context", scope_context)

    result = await advisory.bulk_simulate_notification_paths(
        {}, subscription_id="sub-1", families={"activity"},
        activity_categories={"ServiceHealth"}, service_health_event_types={"service_issue"},
    )

    assert result["summary"]["rules"] == 2
    assert result["facets"]["service_health_event_types"] == {
        "service_issue": 2,
        "planned_maintenance": 2,
        "health_advisory": 1,
        "security_advisory": 1,
    }
    assert result["facets"]["service_health_unrestricted"] == 1
    assert result["facets"]["service_health_unmapped"] == 1
    assert result["facets"]["applied"]["service_health_event_types"] == ["service_issue"]
    assert result["resources"][0]["alert_rule_ids"] == sorted([inventory[0]["id"], inventory[3]["id"]])


@pytest.mark.asyncio
async def test_bulk_api_validates_filter_combinations_and_forwards_empty_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def simulate(_connection, **kwargs):
        captured.update(kwargs)
        return {"summary": {}, "nodes": [], "links": [], "routes": [], "diagnostics": [], "warning": ""}

    monkeypatch.setattr(alerts_api, "_connection", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(advisory, "bulk_simulate_notification_paths", simulate)

    payload = alerts_api.BulkNotificationSimulationRequest(
        subscription_id="sub-1", families=["activity"], activity_categories=["ServiceHealth"],
        service_health_event_types=[],
    )
    await alerts_api.bulk_simulate_notification_paths(payload, None)
    assert captured["activity_categories"] == {"ServiceHealth"}
    assert captured["service_health_event_types"] == set()

    invalid = alerts_api.BulkNotificationSimulationRequest(
        subscription_id="sub-1", families=["activity"],
        activity_categories=["ResourceHealth"], service_health_event_types=["service_issue"],
    )
    with pytest.raises(alerts_api.HTTPException) as exc:
        await alerts_api.bulk_simulate_notification_paths(invalid, None)
    assert exc.value.status_code == 422

    with pytest.raises(ValidationError):
        alerts_api.BulkNotificationSimulationRequest(
            subscription_id="sub-1", unexpected_filter=True,
        )


def test_service_health_authoring_defaults_include_both_health_advisory_values() -> None:
    desired = activity_planner.build_desired(
        subscription_id="sub-1", category="ServiceHealth", resource_group="monitor",
        action_group_ids=["/subscriptions/sub-1/resourceGroups/monitor/providers/Microsoft.Insights/actionGroups/ops"],
    )
    incident_condition = next(
        condition for condition in desired["activity_conditions"]
        if condition["field"] == "properties.incidentType"
    )
    assert incident_condition["containsAny"] == list(service_health.SERVICE_HEALTH_INCIDENT_TYPES)


def test_resource_health_authoring_defaults_match_portal_condition_values() -> None:
    desired = activity_planner.build_desired(
        subscription_id="sub-1", category="ResourceHealth", resource_group="monitor",
        action_group_ids=["/subscriptions/sub-1/resourceGroups/monitor/providers/Microsoft.Insights/actionGroups/ops"],
    )
    by_field = {condition["field"]: condition for condition in desired["activity_conditions"]}

    assert by_field["status"]["containsAny"] == ["Active", "InProgress", "Resolved", "Updated"]
    assert by_field["properties.currentHealthStatus"]["containsAny"] == ["Available", "Degraded", "Unavailable"]
    assert by_field["properties.previousHealthStatus"]["containsAny"] == ["Available", "Degraded", "Unavailable", "Unknown"]
    assert by_field["properties.cause"]["containsAny"] == ["PlatformInitiated", "Unknown", "UserInitiated"]

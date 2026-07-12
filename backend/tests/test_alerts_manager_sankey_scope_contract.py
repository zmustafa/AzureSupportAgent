"""Backend-only scope and compatibility contracts for the bulk notification Sankey."""
from __future__ import annotations

from typing import Any

import pytest

from app.alerts_manager import advisory, rules, service
from app.api import alerts_manager as alerts_api


def _resource(subscription: str, index: int) -> str:
    return f"/subscriptions/{subscription}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-{index:03d}"


def _rule(resource_id: str, index: int, *, enabled: bool = True, group_id: str = "") -> dict[str, Any]:
    subscription = service._subscription_from_id(resource_id)
    return {
        "id": f"/subscriptions/{subscription}/resourceGroups/monitor/providers/Microsoft.Insights/metricAlerts/rule-{index:03d}",
        "name": f"rule-{index:03d}",
        "family": "metric",
        "severity": 2,
        "enabled": enabled,
        "scopes": [resource_id],
        "action_group_ids": [group_id] if group_id else [],
    }


def _raw_rule(resource_id: str, index: int) -> dict[str, Any]:
    public = _rule(resource_id, index)
    return {
        "id": public["id"],
        "name": public["name"],
        "type": "microsoft.insights/metricalerts",
        "subscriptionId": service._subscription_from_id(resource_id),
        "resourceGroup": "monitor",
        "location": "global",
        "properties": {
            "enabled": True,
            "severity": 2,
            "scopes": [resource_id],
            "actions": [],
            "criteria": {"allOf": []},
        },
    }


def _group(subscription: str = "sub-1", *, enabled: bool = True) -> dict[str, Any]:
    group_id = f"/subscriptions/{subscription}/resourceGroups/monitor/providers/Microsoft.Insights/actionGroups/ops"
    return {
        "id": group_id,
        "name": "ops",
        "enabled": enabled,
        "receivers": [{
            "type": "email",
            "name": "on-call",
            "destination": "o***@e***",
            "masked": "o***@e***",
            "fingerprint": "mail-on-call",
            "enabled": True,
            "use_common_alert_schema": True,
        }],
    }


def test_monitoring_control_plane_types_are_not_monitored_resources() -> None:
    excluded = {
        *[resource_type for resource_type, _api_version in rules.RULE_APIS.values()],
        "microsoft.insights/actiongroups",
    }

    assert excluded == {
        "microsoft.insights/metricalerts",
        "microsoft.insights/scheduledqueryrules",
        "microsoft.insights/activitylogalerts",
        "microsoft.alertsmanagement/smartdetectoralertrules",
        "microsoft.alertsmanagement/prometheusrulegroups",
        "microsoft.insights/actiongroups",
    }
    assert all(not advisory._is_monitored_resource({"type": resource_type.upper()}) for resource_type in excluded)
    assert advisory._is_monitored_resource({"type": "microsoft.compute/virtualmachines"})


@pytest.mark.asyncio
async def test_scope_resource_context_excludes_monitoring_control_plane_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitored = {
        "id": _resource("sub-1", 1), "name": "vm-001",
        "type": "microsoft.compute/virtualmachines", "resourceGroup": "rg",
        "subscriptionId": "sub-1",
    }
    control_plane_rows = [
        {
            "id": f"/subscriptions/sub-1/resourceGroups/monitor/providers/{resource_type}/item-{index}",
            "name": f"item-{index}", "type": resource_type, "resourceGroup": "monitor",
            "subscriptionId": "sub-1",
        }
        for index, resource_type in enumerate(advisory._MONITORING_CONTROL_PLANE_RESOURCE_TYPES)
    ]

    async def query_resources(predicates, _connection):
        assert predicates == ["subscriptionId =~ 'sub-1'"]
        return [monitored, *control_plane_rows]

    async def list_subscriptions(_token):
        return [{"id": "sub-1", "name": "Subscription One"}], ""

    async def token(_connection):
        return "token"

    monkeypatch.setattr("app.amba.collector._query_resources", query_resources)
    monkeypatch.setattr("app.workloads.registry.list_workloads", lambda: [])
    monkeypatch.setattr(service, "_token", token)
    monkeypatch.setattr("app.azure.arm.list_subscriptions", list_subscriptions)

    context = await advisory._scope_resource_context(
        {}, workload_id=None, subscription_id="sub-1", management_group_id=None,
    )

    assert [resource["id"] for resource in context["resources"]] == [monitored["id"]]
    assert context["resources"][0]["resource_type"] == "microsoft.compute/virtualmachines"


def test_excluded_control_plane_objects_remain_in_downstream_graph_stages() -> None:
    resource_id = _resource("sub-1", 1)
    group = _group()
    inventory = [
        {
            **_rule(resource_id, index, group_id=group["id"]),
            "family": family,
            "id": (
                f"/subscriptions/sub-1/resourceGroups/monitor/providers/"
                f"{resource_type}/rule-{index:03d}"
            ),
        }
        for index, (family, (resource_type, _api_version)) in enumerate(rules.RULE_APIS.items())
    ]

    result = advisory.build_bulk_notification_simulation(inventory, [group])

    assert {node["family"] for node in result["nodes"] if node["kind"] == "alert"} == set(rules.RULE_APIS)
    assert {node["resource_id"].lower() for node in result["nodes"] if node["kind"] == "action_group"} == {group["id"].lower()}
    assert {node["resource_id"].lower() for node in result["nodes"] if node["kind"] == "resource"} == {resource_id.lower()}
    assert len(result["routes"]) == len(rules.RULE_APIS)
    assert all(route["outcome"] == "deliver" for route in result["routes"])


@pytest.mark.asyncio
async def test_workload_rule_scope_contains_only_exact_members_with_alert_and_no_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member_with_alert = _resource("sub-1", 1)
    member_without_alert = _resource("sub-1", 2)
    nonmember = _resource("sub-1", 3)
    workload = {
        "id": "wl-1",
        "nodes": [
            {"kind": "resource", "id": member_with_alert, "subscription_id": "sub-1"},
            {"kind": "resource", "id": member_without_alert, "subscription_id": "sub-1"},
        ],
    }

    monkeypatch.setattr("app.workloads.registry.get_workload", lambda workload_id: workload if workload_id == "wl-1" else None)

    async def arg(_connection, _query, subscriptions, **kwargs):
        assert subscriptions == {"sub-1"}
        assert kwargs["with_metadata"] is True
        return [_raw_rule(member_with_alert, 1), _raw_rule(nonmember, 2)]

    monkeypatch.setattr(service, "_arg", arg)
    found, metadata = await rules._list_rules_uncached(
        {}, workload_id="wl-1", subscription_id=None, management_group_id=None, family="",
    )

    assert [item["scopes"] for item in found] == [[member_with_alert]]
    assert member_without_alert not in {scope for item in found for scope in item["scopes"]}
    assert nonmember not in {scope for item in found for scope in item["scopes"]}
    assert metadata["normalized_count"] == 1


@pytest.mark.asyncio
async def test_shared_resource_membership_is_visible_in_each_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _resource("sub-shared", 1)
    only_a = _resource("sub-shared", 2)
    workloads = {
        "wl-a": {"id": "wl-a", "nodes": [{"id": shared}, {"id": only_a}]},
        "wl-b": {"id": "wl-b", "nodes": [{"id": shared}]},
    }
    monkeypatch.setattr("app.workloads.registry.get_workload", workloads.get)

    async def arg(*_args, **_kwargs):
        return [_raw_rule(shared, 1), _raw_rule(only_a, 2)]

    monkeypatch.setattr(service, "_arg", arg)
    a, _ = await rules._list_rules_uncached(
        {}, workload_id="wl-a", subscription_id=None, management_group_id=None, family="",
    )
    b, _ = await rules._list_rules_uncached(
        {}, workload_id="wl-b", subscription_id=None, management_group_id=None, family="",
    )

    assert {item["scopes"][0] for item in a} == {shared, only_a}
    assert {item["scopes"][0] for item in b} == {shared}


def test_subscription_inventory_partitions_100_resources_50_20_30_without_loss() -> None:
    workload_1 = {_resource("sub-1", index) for index in range(50)}
    workload_2 = {_resource("sub-1", index) for index in range(50, 70)}
    unmapped = {_resource("sub-1", index) for index in range(70, 100)}
    inventory = [
        _rule(resource_id, index)
        for index, resource_id in enumerate(sorted(workload_1 | workload_2 | unmapped))
    ]

    result = advisory.build_bulk_notification_simulation(inventory, [])
    graph_resources = {
        str(node["resource_id"])
        for node in result["nodes"]
        if node["kind"] == "resource"
    }

    assert (len(workload_1), len(workload_2), len(unmapped)) == (50, 20, 30)
    assert workload_1 | workload_2 | unmapped == graph_resources
    assert result["summary"]["resources"] == 100
    assert result["summary"]["rules"] == 100


@pytest.mark.asyncio
async def test_bulk_response_enriches_100_resources_and_preserves_70_without_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = []
    for index in range(100):
        memberships = ["wl-1"] if index < 50 else ["wl-2"] if index < 70 else []
        resources.append({
            "id": _resource("sub-1", index), "name": f"vm-{index:03d}",
            "resource_type": "microsoft.compute/virtualmachines", "resource_group": "rg",
            "subscription_id": "sub-1", "subscription_name": "Subscription One",
            "workload_ids": memberships, "membership_status": "single" if memberships else "unmapped",
            "accessible": True,
        })
    group = _group()
    inventory = [_rule(resources[index]["id"], index, group_id=group["id"]) for index in range(30)]

    async def list_rules(*_args, **_kwargs): return inventory, {"partial": False}
    async def list_groups(*_args, **_kwargs): return [group], {"partial": False}
    async def context(*_args, **_kwargs):
        return {
            "scope": {"kind": "subscription", "id": "sub-1", "name": "Subscription One"},
            "resources": resources,
            "workloads": [
                {"id": "wl-1", "name": "Workload 1", "resource_ids": [item["id"] for item in resources[:50]], "subscription_ids": ["sub-1"], "accessible": True},
                {"id": "wl-2", "name": "Workload 2", "resource_ids": [item["id"] for item in resources[50:70]], "subscription_ids": ["sub-1"], "accessible": True},
            ],
            "subscriptions": [{"id": "sub-1", "name": "Subscription One", "accessible": True, "partial": False}],
            "completeness": {"complete": True, "partial": False, "inaccessible_subscription_ids": [], "warnings": []},
        }

    monkeypatch.setattr(rules, "list_rules", list_rules)
    monkeypatch.setattr(service, "list_action_groups", list_groups)
    monkeypatch.setattr(advisory, "_scope_resource_context", context)
    result = await advisory.bulk_simulate_notification_paths({}, subscription_id="sub-1")
    assert result["summary"]["resources"] == 100
    assert result["summary"]["mapped_resources"] == 70
    assert result["summary"]["unmapped_resources"] == 30
    assert result["summary"]["alerted_resources"] == 30
    assert result["summary"]["no_alert_resources"] == 70
    assert sum(item["coverage_state"] == "no_alert" for item in result["resources"]) == 70


@pytest.mark.asyncio
async def test_bulk_simulation_facets_only_report_existing_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = [{
        "id": _resource("sub-1", 1), "name": "vm-001",
        "resource_type": "microsoft.compute/virtualmachines", "resource_group": "rg",
        "subscription_id": "sub-1", "subscription_name": "Subscription One",
        "workload_ids": [], "membership_status": "unmapped", "accessible": True,
    }]
    inventory = [
        {**_rule(resources[0]["id"], 1), "severity": 1},
        {**_rule(resources[0]["id"], 2), "severity": 2},
    ]

    async def list_rules(*_args, **_kwargs): return inventory, {"partial": False}
    async def list_groups(*_args, **_kwargs): return [], {"partial": False}
    async def context(*_args, **_kwargs):
        return {
            "scope": {"kind": "subscription", "id": "sub-1", "name": "Subscription One"},
            "resources": resources, "workloads": [],
            "subscriptions": [{"id": "sub-1", "name": "Subscription One"}],
            "completeness": {"complete": True, "partial": False, "inaccessible_subscription_ids": [], "warnings": []},
        }

    monkeypatch.setattr(rules, "list_rules", list_rules)
    monkeypatch.setattr(service, "list_action_groups", list_groups)
    monkeypatch.setattr(advisory, "_scope_resource_context", context)
    result = await advisory.bulk_simulate_notification_paths(
        {}, subscription_id="sub-1", families={"metric"}, severities={1},
    )

    assert result["summary"]["rules"] == 1
    assert result["facets"]["total_rules"] == 2
    assert result["facets"]["families"] == {
        "metric": 2, "log": 0, "activity": 0, "smart": 0, "prometheus": 0,
    }
    assert result["facets"]["severities"] == {0: 0, 1: 1, 2: 1, 3: 0, 4: 0}


@pytest.mark.asyncio
async def test_management_group_scope_reaches_rules_and_groups_with_all_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, dict[str, Any]] = {}

    async def list_rules(_connection, **kwargs):
        calls["rules"] = kwargs
        return []

    async def list_groups(_connection, **kwargs):
        calls["groups"] = kwargs
        return []

    monkeypatch.setattr(rules, "list_rules", list_rules)
    monkeypatch.setattr(service, "list_action_groups", list_groups)

    result = await advisory.bulk_simulate_notification_paths({}, management_group_id="mg-root")

    assert calls["rules"] == {
        "workload_id": None, "subscription_id": None, "management_group_id": "mg-root",
        "with_metadata": True,
    }
    assert calls["groups"] == {
        "workload_id": None, "subscription_id": None,
        "management_group_id": "mg-root", "all_visible": True, "with_metadata": True,
    }
    assert result["summary"]["rules"] == 0


@pytest.mark.asyncio
async def test_management_group_rule_query_uses_every_resolved_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[set[str]] = []

    async def subscriptions_under_mg(_connection, management_group_id):
        assert management_group_id == "mg-root"
        return ["sub-a", "sub-b", "sub-c"]

    async def arg(_connection, _query, subscriptions, **_kwargs):
        captured.append(set(subscriptions))
        return []

    monkeypatch.setattr("app.workloads.discovery.subscriptions_under_mg", subscriptions_under_mg)
    monkeypatch.setattr(service, "_arg", arg)

    found, _ = await rules._list_rules_uncached(
        {}, workload_id=None, subscription_id=None, management_group_id="mg-root", family="",
    )

    assert found == []
    assert captured == [{"sub-a", "sub-b", "sub-c"}]


def test_inaccessible_subscription_route_is_partial_not_missing() -> None:
    resource_id = _resource("source", 1)
    inaccessible_group = _group("inaccessible")["id"]

    result = advisory.build_bulk_notification_simulation(
        [_rule(resource_id, 1, group_id=inaccessible_group)], [],
    )

    assert result["routes"][0]["outcome"] == "unresolved_group"
    assert result["routes"][0]["issues"] == [
        "cross-subscription Action Group is outside the readable scope",
    ]
    assert {item["code"] for item in result["diagnostics"]} == {"unresolved_action_group_access"}


def test_unique_resource_count_is_unchanged_by_multiple_rules() -> None:
    resource_id = _resource("sub-1", 1)
    group = _group()
    inventory = [
        _rule(resource_id, 1, group_id=group["id"]),
        _rule(resource_id.upper(), 2, group_id=group["id"]),
        _rule(resource_id, 3, group_id=group["id"]),
    ]

    result = advisory.build_bulk_notification_simulation(inventory, [group])

    assert result["summary"]["rules"] == 3
    assert result["summary"]["resources"] == 1
    assert len([node for node in result["nodes"] if node["kind"] == "resource"]) == 1
    assert result["summary"]["would_deliver"] == 3


def test_healthy_and_gap_routes_have_consistent_summary_classification() -> None:
    healthy_resource = _resource("sub-1", 1)
    disabled_resource = _resource("sub-1", 2)
    no_route_resource = _resource("sub-1", 3)
    group = _group()
    result = advisory.build_bulk_notification_simulation([
        _rule(healthy_resource, 1, group_id=group["id"]),
        _rule(disabled_resource, 2, enabled=False, group_id=group["id"]),
        _rule(no_route_resource, 3),
    ], [group])

    healthy = [route for route in result["routes"] if route.get("would_run")]
    gaps = [route for route in result["routes"] if not route.get("would_run")]

    assert [route["outcome"] for route in healthy] == ["deliver"]
    assert {route["outcome"] for route in gaps} == {"disabled", "no_receiver"}
    assert result["summary"]["would_deliver"] == len(healthy) == 1
    assert result["summary"]["blocked"] == len(gaps) == 2


@pytest.mark.asyncio
async def test_bulk_api_retains_legacy_routes_while_forwarding_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = {
        "summary": {"rules": 1, "resources": 1, "action_groups": 0, "receiver_paths": 0, "would_deliver": 0, "blocked": 1, "diagnostics": 1},
        "nodes": [],
        "links": [],
        "routes": [{"rule_id": "legacy-rule", "outcome": "no_receiver"}],
        "diagnostics": [],
        "warning": "Dry-run only.",
    }
    captured: dict[str, Any] = {}

    async def simulate(_connection, **kwargs):
        captured.update(kwargs)
        return legacy

    monkeypatch.setattr(alerts_api, "_connection", lambda connection_id, workload_id=None: {"id": connection_id, "workload_id": workload_id})
    monkeypatch.setattr(advisory, "bulk_simulate_notification_paths", simulate)
    payload = alerts_api.BulkNotificationSimulationRequest(
        connection_id="conn-1", workload_id="wl-1", families=["metric"], severities=[2],
    )

    result = await alerts_api.bulk_simulate_notification_paths(payload, None)

    assert result is legacy
    assert result["routes"] == [{"rule_id": "legacy-rule", "outcome": "no_receiver"}]
    assert captured == {
        "workload_id": "wl-1", "subscription_id": None, "management_group_id": None,
        "monitor_condition": "Fired", "include_disabled": True,
        "families": {"metric"}, "severities": {2},
    }
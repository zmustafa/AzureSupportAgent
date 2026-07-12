"""Isolated live-Azure certification for Alerts Manager resource adapters.

Creates only disabled resources in a uniquely named temporary resource group, exercises
create/read/update/stale-write/delete, and deletes the resource group in ``finally``.
Execution is refused unless the explicit confirmation phrase is supplied.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from app.alerts_manager import rules, service
from app.azure.arm import arm_write

CONFIRMATION = "CREATE_AND_DELETE_TEMP_ALERT_RESOURCES"


@dataclass
class Certification:
    family: str
    target_id: str
    body: dict[str, Any]
    apply: Callable[[dict[str, Any], Any], Awaitable[tuple[dict[str, Any] | None, int, str]]]
    get: Callable[[dict[str, Any], str], Awaitable[tuple[dict[str, Any] | None, int, str]]]
    target_type: str


def _change(item: Certification, operation: str, body: dict[str, Any], expected: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        target_type=item.target_type, target_id=item.target_id, operation=operation,
        desired_encrypted=service.encrypted_json({"body": body}), before_encrypted=service.encrypted_json({}),
        expected_state_hash=expected,
    )


async def _certify(connection: dict[str, Any], item: Certification) -> dict[str, Any]:
    result: dict[str, Any] = {"family": item.family, "target_id": item.target_id, "steps": []}
    created, status, error = await item.apply(connection, _change(item, "create", item.body))
    if error or not created:
        raise RuntimeError(f"{item.family} create failed ({status}): {error}")
    result["steps"].append({"step": "create", "status": status})
    live, status, error = await item.get(connection, item.target_id)
    if error or not live:
        raise RuntimeError(f"{item.family} read failed ({status}): {error}")
    before_hash = service.canonical_hash(service._resource_body(live))
    original_body = service._resource_body(live)
    result["steps"].append({"step": "read", "status": status})
    update_body = json.loads(json.dumps(service._resource_body(live)))
    update_body.setdefault("tags", {})["certification-step"] = "updated"
    updated, status, error = await item.apply(connection, _change(item, "update", update_body, before_hash))
    if error or not updated:
        raise RuntimeError(f"{item.family} update failed ({status}): {error}")
    result["steps"].append({"step": "update", "status": status})
    updated_live, status, error = await item.get(connection, item.target_id)
    if error or not updated_live:
        raise RuntimeError(f"{item.family} refresh before rollback failed ({status}): {error}")
    updated_hash = service.canonical_hash(service._resource_body(updated_live))
    rolled_back, status, error = await item.apply(connection, _change(item, "update", original_body, updated_hash))
    if error or not rolled_back:
        raise RuntimeError(f"{item.family} rollback failed ({status}): {error}")
    result["steps"].append({"step": "rollback", "status": status})
    _stale, stale_status, stale_error = await item.apply(connection, _change(item, "update", item.body, updated_hash))
    if stale_status != 409 or not stale_error:
        raise RuntimeError(f"{item.family} stale-write guard did not return 409")
    result["steps"].append({"step": "stale_guard", "status": stale_status})
    current, status, error = await item.get(connection, item.target_id)
    if error or not current:
        raise RuntimeError(f"{item.family} refresh before delete failed ({status}): {error}")
    current_hash = service.canonical_hash(service._resource_body(current))
    _deleted, status, error = await item.apply(connection, _change(item, "delete", {}, current_hash))
    if error:
        raise RuntimeError(f"{item.family} delete failed ({status}): {error}")
    result["steps"].append({"step": "delete", "status": status})
    return result


async def _discover(connection: dict[str, Any], subscription_id: str) -> dict[str, str]:
    rows = await service._arg(
        connection,
        "resources | where type in~ ('microsoft.storage/storageaccounts','microsoft.operationalinsights/workspaces','microsoft.insights/components','microsoft.monitor/accounts') | project id,name,type,location,resourceGroup | order by type asc",
        {subscription_id}, max_rows=200,
    )
    found: dict[str, str] = {}
    for row in rows:
        kind = str(row.get("type") or "").lower()
        found.setdefault(kind, str(row.get("id") or ""))
    return found


async def _provider_state(token: str, subscription_id: str, namespace: str) -> str:
    data, _error, _status = await arm_write(token, "GET", f"/subscriptions/{subscription_id}/providers/{namespace}", api_version="2021-04-01")
    return str((data or {}).get("registrationState") or "")


async def _set_provider(token: str, subscription_id: str, namespace: str, action: str, wanted: str) -> tuple[bool, str]:
    _data, error, status = await arm_write(token, "POST", f"/subscriptions/{subscription_id}/providers/{namespace}/{action}", api_version="2021-04-01")
    if error:
        return False, f"ARM {status}: {service.safe_error(error)}"
    for _attempt in range(90):
        state = await _provider_state(token, subscription_id, namespace)
        if state.lower() == wanted.lower() or (wanted.lower() in {"unregistered", "notregistered"} and state.lower() in {"unregistered", "notregistered"}):
            return True, ""
        await asyncio.sleep(2)
    return False, f"Provider remained in state {await _provider_state(token, subscription_id, namespace) or 'unknown'}."


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"Refusing Azure mutation. Pass --confirm {CONFIRMATION}")
    connection = service.resolve_selected_connection(args.connection_id or None)
    service.assert_writable(connection)
    token = await service._token(connection)
    suffix = uuid.uuid4().hex[:10]
    group_name = f"rg-alerts-manager-cert-{suffix}"
    group_id = f"/subscriptions/{args.subscription_id}/resourceGroups/{group_name}"
    report: dict[str, Any] = {"resource_group": group_id, "location": args.location, "results": [], "skipped": [], "cleanup": {}}
    monitor_provider_initial = await _provider_state(token, args.subscription_id, "Microsoft.Monitor")
    monitor_registered_by_test = False
    temporary_monitor_workspace = ""
    report["provider_restore"] = {"initial_state": monitor_provider_initial, "registered_temporarily": False}
    if monitor_provider_initial.lower() != "registered" and args.register_missing_providers:
        registered, register_error = await _set_provider(token, args.subscription_id, "Microsoft.Monitor", "register", "Registered")
        report["provider_restore"].update({"registered_temporarily": registered, "registration_error": register_error})
        monitor_registered_by_test = registered
    _created, error, status = await arm_write(token, "PUT", group_id, body={"location": args.location, "tags": {"purpose": "alerts-manager-live-certification", "temporary": "true"}}, api_version="2024-03-01")
    if error:
        if monitor_registered_by_test:
            await _set_provider(token, args.subscription_id, "Microsoft.Monitor", "unregister", "Unregistered")
        raise RuntimeError(f"Temporary resource-group creation failed ({status}): {error}")
    try:
        discovered = await _discover(connection, args.subscription_id)
        action_group_id = f"{group_id}/providers/Microsoft.Insights/actionGroups/cert-{suffix}"
        action_group_body = service.build_action_group_body({
            "name": f"cert-{suffix}", "subscription_id": args.subscription_id, "resource_group": group_name,
            "location": "Global", "short_name": f"cert{suffix[:6]}", "enabled": False,
            "email_receivers": [], "sms_receivers": [], "webhook_receivers": [], "arm_role_receivers": [],
            "voice_receivers": [], "azure_app_push_receivers": [], "azure_function_receivers": [],
            "logic_app_receivers": [], "event_hub_receivers": [], "automation_runbook_receivers": [], "itsm_receivers": [],
            "tags": {"temporary": "true"},
        })
        certifications: list[Certification] = [
            Certification("action_group", action_group_id, action_group_body, service.apply_action_group_change, lambda c, rid: service.get_arm_resource(c, rid), "action_group"),
        ]

        activity = {
            "name": f"activity-{suffix}", "subscription_id": args.subscription_id, "resource_group": group_name,
            "location": "Global", "enabled": False, "description": "Alerts Manager isolated live certification",
            "scopes": [f"/subscriptions/{args.subscription_id}"], "action_group_ids": [], "tags": {"temporary": "true"},
            "activity_conditions": [
                {"field": "category", "equals": "Administrative"},
                {"field": "operationName", "equals": "Microsoft.Resources/subscriptions/resourceGroups/write"},
            ],
        }
        activity_id = f"{group_id}/providers/Microsoft.Insights/activityLogAlerts/{activity['name']}"
        certifications.append(Certification("activity", activity_id, rules.build_activity_body(activity), lambda c, ch: rules.apply_rule_change(c, ch), lambda c, rid: rules.get_rule(c, rid, "activity"), "activity_rule"))

        metric_target = args.metric_target_id or discovered.get("microsoft.storage/storageaccounts", "")
        if metric_target:
            try:
                definitions = await rules.metric_definitions(connection, metric_target)
            except ValueError:
                definitions = []
            metric = next((item for item in definitions if item.get("name") and item.get("namespace")), None)
            if metric:
                metric_payload = {
                    "name": f"metric-{suffix}", "subscription_id": args.subscription_id, "resource_group": group_name,
                    "location": "Global", "enabled": False, "severity": 4, "description": "Alerts Manager isolated live certification",
                    "scopes": [metric_target], "action_group_ids": [], "evaluation_frequency": "PT5M", "window_size": "PT15M",
                    "auto_mitigate": True, "target_resource_type": "", "target_resource_region": "", "tags": {"temporary": "true"},
                    "conditions": [{"name": "cert", "metric_name": metric["name"], "metric_namespace": metric["namespace"], "threshold_type": "static", "operator": "GreaterThan", "threshold": 1e15, "aggregation": metric.get("primary_aggregation") or "Average", "dimensions": [], "min_failing_periods": 1, "evaluation_periods": 1, "skip_metric_validation": False}],
                }
                metric_id = f"{group_id}/providers/Microsoft.Insights/metricAlerts/{metric_payload['name']}"
                certifications.append(Certification("metric", metric_id, rules.build_metric_body(metric_payload), lambda c, ch: rules.apply_rule_change(c, ch), lambda c, rid: rules.get_rule(c, rid, "metric"), "metric_rule"))
            else:
                report["skipped"].append({"family": "metric", "reason": "No metric definition discovered for a safe target."})
        else:
            report["skipped"].append({"family": "metric", "reason": "No metric-capable target discovered."})

        workspace = args.log_workspace_id or discovered.get("microsoft.operationalinsights/workspaces", "")
        if workspace:
            log_payload = {
                "name": f"log-{suffix}", "subscription_id": args.subscription_id, "resource_group": group_name,
                "location": args.location, "enabled": False, "severity": 4, "description": "Alerts Manager isolated live certification",
                "scopes": [workspace], "action_group_ids": [], "evaluation_frequency": "PT15M", "window_size": "PT15M",
                "auto_mitigate": True, "identity": {}, "tags": {"temporary": "true"}, "skip_query_validation": True,
                "conditions": [{"query": "Heartbeat | count", "aggregation": "Count", "operator": "GreaterThan", "threshold": 1e15, "min_failing_periods": 1, "evaluation_periods": 1, "dimensions": []}],
            }
            log_id = f"{group_id}/providers/Microsoft.Insights/scheduledQueryRules/{log_payload['name']}"
            certifications.append(Certification("log", log_id, rules.build_log_body(log_payload), lambda c, ch: rules.apply_rule_change(c, ch), lambda c, rid: rules.get_rule(c, rid, "log"), "log_rule"))
        else:
            report["skipped"].append({"family": "log", "reason": "No Log Analytics workspace discovered."})

        component = args.app_insights_id or discovered.get("microsoft.insights/components", "")
        if not component:
            component = f"{group_id}/providers/Microsoft.Insights/components/appi-{suffix}"
            _component, component_error, component_status = await arm_write(
                token, "PUT", component,
                body={"location": args.location, "kind": "web", "tags": {"temporary": "true"}, "properties": {"Application_Type": "web", "Flow_Type": "Bluefield", "Request_Source": "rest"}},
                api_version="2020-02-02",
            )
            if component_error:
                report["skipped"].append({"family": "smart", "reason": f"Temporary Application Insights dependency failed ({component_status}): {service.safe_error(component_error)}"})
                component = ""
        if component:
            smart_payload = {
                "name": f"smart-{suffix}", "subscription_id": args.subscription_id, "resource_group": group_name,
                "location": "global", "enabled": False, "severity": 4, "description": "Alerts Manager isolated live certification",
                "scopes": [component], "action_group_ids": [], "detector_id": args.detector_id,
                "detector_parameters": {}, "frequency": "PT1M", "throttling_duration": "PT15M", "tags": {"temporary": "true"},
            }
            smart_id = f"{group_id}/providers/Microsoft.AlertsManagement/smartDetectorAlertRules/{smart_payload['name']}"
            certifications.append(Certification("smart", smart_id, rules.build_smart_body(smart_payload), lambda c, ch: rules.apply_rule_change(c, ch), lambda c, rid: rules.get_rule(c, rid, "smart"), "smart_rule"))
        else:
            report["skipped"].append({"family": "smart", "reason": "No Application Insights component discovered."})

        monitor_workspace = args.azure_monitor_workspace_id or discovered.get("microsoft.monitor/accounts", "")
        if not monitor_workspace and (monitor_provider_initial.lower() == "registered" or monitor_registered_by_test):
            monitor_workspace = f"{group_id}/providers/Microsoft.Monitor/accounts/amw-{suffix}"
            _workspace, workspace_error, workspace_status = await arm_write(
                token, "PUT", monitor_workspace,
                body={"location": args.location, "tags": {"temporary": "true"}, "properties": {}},
                api_version="2023-04-03",
            )
            if workspace_error:
                report["skipped"].append({"family": "prometheus", "reason": f"Temporary Azure Monitor workspace dependency failed ({workspace_status}): {service.safe_error(workspace_error)}"})
                monitor_workspace = ""
            else:
                temporary_monitor_workspace = monitor_workspace
        if monitor_workspace:
            prometheus_payload = {
                "name": f"prometheus-{suffix}", "subscription_id": args.subscription_id, "resource_group": group_name,
                "location": args.location, "enabled": False, "description": "Alerts Manager isolated live certification",
                "scopes": [monitor_workspace], "interval": "PT5M", "cluster_name": "", "tags": {"temporary": "true"},
                "action_group_ids": [], "prometheus_rules": [{"record": "alerts_manager_certification", "expression": "vector(1)", "enabled": True, "labels": {"temporary": "true"}}],
            }
            prometheus_id = f"{group_id}/providers/Microsoft.AlertsManagement/prometheusRuleGroups/{prometheus_payload['name']}"
            certifications.append(Certification("prometheus", prometheus_id, rules.build_prometheus_body(prometheus_payload), lambda c, ch: rules.apply_rule_change(c, ch), lambda c, rid: rules.get_rule(c, rid, "prometheus"), "prometheus_rule"))
        else:
            report["skipped"].append({"family": "prometheus", "reason": "No Azure Monitor workspace discovered."})

        for item in certifications:
            try:
                report["results"].append(await _certify(connection, item))
            except Exception as exc:  # noqa: BLE001 - certification must continue to cleanup/report
                report["results"].append({"family": item.family, "target_id": item.target_id, "error": service.safe_error(str(exc))})
    finally:
        if temporary_monitor_workspace:
            _amw_data, amw_error, amw_status = await arm_write(token, "DELETE", temporary_monitor_workspace, api_version="2023-04-03")
            amw_deleted = False
            if not amw_error:
                for _attempt in range(90):
                    _value, get_error, get_status = await arm_write(token, "GET", temporary_monitor_workspace, api_version="2023-04-03")
                    if get_status == 404:
                        amw_deleted = True
                        break
                    if get_error and get_status not in {0, 404, 409}:
                        break
                    await asyncio.sleep(2)
            report["monitor_workspace_cleanup"] = {"status": amw_status, "error": service.safe_error(amw_error), "verified_deleted": amw_deleted}
        _data, cleanup_error, cleanup_status = await arm_write(token, "DELETE", group_id, api_version="2024-03-01")
        cleanup_verified = False
        if not cleanup_error:
            for _attempt in range(45):
                _value, get_error, get_status = await arm_write(token, "GET", group_id, api_version="2024-03-01")
                if get_status == 404:
                    cleanup_verified = True
                    break
                if get_error and get_status not in {0, 404, 409}:
                    break
                await asyncio.sleep(2)
        report["cleanup"] = {"status": cleanup_status, "error": service.safe_error(cleanup_error), "verified_deleted": cleanup_verified}
        if monitor_registered_by_test:
            restored, restore_error = await _set_provider(token, args.subscription_id, "Microsoft.Monitor", "unregister", "Unregistered")
            report["provider_restore"].update({"restored": restored, "restore_error": restore_error, "final_state": await _provider_state(token, args.subscription_id, "Microsoft.Monitor")})
    provider_restored = not monitor_registered_by_test or bool(report["provider_restore"].get("restored"))
    workspace_clean = not temporary_monitor_workspace or bool(report.get("monitor_workspace_cleanup", {}).get("verified_deleted"))
    report["passed"] = all("error" not in item for item in report["results"]) and not report["cleanup"]["error"] and report["cleanup"]["verified_deleted"] and provider_restored and workspace_clean
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Create, update, stale-check, delete, and clean up isolated Alerts Manager resources.")
    value.add_argument("--connection-id", default="")
    value.add_argument("--subscription-id", required=True)
    value.add_argument("--location", default="southcentralus")
    value.add_argument("--metric-target-id", default="")
    value.add_argument("--log-workspace-id", default="")
    value.add_argument("--app-insights-id", default="")
    value.add_argument("--azure-monitor-workspace-id", default="")
    value.add_argument("--detector-id", default="FailureAnomaliesDetector")
    value.add_argument("--register-missing-providers", action="store_true", help="Temporarily register Microsoft.Monitor for Prometheus certification and restore its original state.")
    value.add_argument("--confirm", required=True)
    return value


if __name__ == "__main__":
    report = asyncio.run(run(parser().parse_args()))
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report.get("passed") else 1)

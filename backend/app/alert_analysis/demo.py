"""Deterministic Alerts Manager data for the shared demo workloads."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from app.alert_analysis.collector import compute_analysis
from app.amba.demo import demo_alerts, demo_resources, demo_scope_name
from app.demo_catalog import DEMO_SUB, DEMO_WORKLOAD_IDS, rg_for


def is_demo_scope(scope_kind: str, scope_id: str) -> bool:
    return scope_kind == "workload" and scope_id in DEMO_WORKLOAD_IDS


def build_demo_snapshot(scope_id: str) -> dict[str, Any]:
    resources = demo_resources(scope_id)
    alerts = deepcopy(demo_alerts(scope_id))
    rg = rg_for(scope_id)
    action_group_id = f"/subscriptions/{DEMO_SUB}/resourceGroups/{rg}/providers/microsoft.insights/actionGroups/oncall"
    secondary_group_id = f"/subscriptions/{DEMO_SUB}/resourceGroups/{rg}/providers/microsoft.insights/actionGroups/platform-email"

    # Create a real duplicate-notification example and a broken-routing example while
    # retaining AMBA's varied present/misconfigured/missing baseline states.
    if alerts:
        duplicate = deepcopy(alerts[0])
        duplicate["id"] = str(duplicate["id"]) + "-duplicate"
        duplicate["name"] = str(duplicate["name"]) + " duplicate"
        duplicate["properties"]["actions"] = [{"actionGroupId": secondary_group_id}]
        alerts.append(duplicate)
    if len(alerts) > 1:
        alerts[1]["properties"]["actions"] = []

    groups = [
        {
            "id": action_group_id,
            "name": "oncall",
            "type": "microsoft.insights/actiongroups",
            "subscriptionId": DEMO_SUB,
            "resourceGroup": rg,
            "properties": {
                "enabled": True,
                "emailReceivers": [
                    {"name": "primary", "emailAddress": "oncall@contoso.example"},
                    {"name": "platform", "emailAddress": "platform@contoso.example"},
                ],
                "webhookReceivers": [
                    {"name": "teams", "serviceUri": "https://hooks.contoso.example/monitor/demo-secret"}
                ],
            },
        },
        {
            "id": secondary_group_id,
            "name": "platform-email",
            "type": "microsoft.insights/actiongroups",
            "subscriptionId": DEMO_SUB,
            "resourceGroup": rg,
            "properties": {
                "enabled": True,
                "emailReceivers": [
                    {"name": "same-platform", "emailAddress": "platform@contoso.example"}
                ],
            },
        },
    ]
    if resources:
        alerts.append(
            {
                "id": f"/subscriptions/{DEMO_SUB}/resourceGroups/{rg}/providers/microsoft.alertsmanagement/smartdetectoralertrules/failure-anomalies",
                "name": "Failure anomaly detector",
                "type": "microsoft.alertsmanagement/smartdetectoralertrules",
                "subscriptionId": DEMO_SUB,
                "resourceGroup": rg,
                "properties": {
                    "enabled": True,
                    "severity": 1,
                    "frequency": "PT5M",
                    "scope": [resources[0]["id"]],
                    "detector": {"id": "providers/Microsoft.AlertsManagement/smartDetectorRuleTemplates/FailureAnomaliesDetector"},
                    "actionGroups": [{"actionGroupId": action_group_id}],
                },
            }
        )
        alerts.append(
            {
                "id": f"/subscriptions/{DEMO_SUB}/resourceGroups/{rg}/providers/microsoft.alertsmanagement/prometheusrulegroups/platform-prometheus",
                "name": "platform-prometheus",
                "type": "microsoft.alertsmanagement/prometheusrulegroups",
                "subscriptionId": DEMO_SUB,
                "resourceGroup": rg,
                "properties": {
                    "enabled": True,
                    "scopes": [resources[0]["id"]],
                    "interval": "PT1M",
                    "rules": [
                        {
                            "alert": "HighRequestRate",
                            "expr": "rate(http_requests_total[5m]) > 100",
                            "for": "PT5M",
                            "labels": {"severity": "warning"},
                            "actions": [{"actionGroupId": action_group_id}],
                        }
                    ],
                },
            }
        )
    now = datetime.now(timezone.utc)
    firings: list[dict[str, Any]] = []
    for index, alert in enumerate(alerts[:3]):
        for days_ago in range(index + 1):
            firings.append(
                {
                    "id": f"demo-fire-{index}-{days_ago}",
                    "essentials": {
                        "alertRule": alert.get("id", ""),
                        "firedDateTime": (now - timedelta(days=days_ago + 1)).isoformat(),
                        "monitorCondition": "Resolved",
                    },
                }
            )
    snapshot = compute_analysis(
        resources,
        alerts,
        groups,
        firings,
        scope_kind="workload",
        scope_id=scope_id,
        scope_name=demo_scope_name(scope_id),
    )
    snapshot.update(
        {
            "source": "demo_dummy_data",
            "demo": True,
            "connection_configured": False,
        }
    )
    return snapshot

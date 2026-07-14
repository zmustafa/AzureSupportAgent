"""Metric, log-query, and activity-log alert-rule management for Alerts Manager."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.alerts_manager import cache as inventory_cache
from app.alerts_manager import service

RULE_APIS = {
    "metric": ("microsoft.insights/metricalerts", "2018-03-01"),
    "log": ("microsoft.insights/scheduledqueryrules", "2022-06-15"),
    "activity": ("microsoft.insights/activitylogalerts", "2020-10-01"),
    "smart": ("microsoft.alertsmanagement/smartdetectoralertrules", "2021-04-01"),
    "prometheus": ("microsoft.alertsmanagement/prometheusrulegroups", "2023-03-01"),
}
_RESOURCE_TYPE_TO_FAMILY = {resource_type: family for family, (resource_type, _api) in RULE_APIS.items()}
_FREQUENCIES = ("PT1M", "PT5M", "PT10M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H")
_WINDOWS = ("PT1M", "PT5M", "PT10M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H", "P1D", "P2D")
_OPERATORS = {"Equals", "GreaterThan", "GreaterThanOrEqual", "LessThan", "LessThanOrEqual"}
_DYNAMIC_OPERATORS = {"GreaterThan", "LessThan", "GreaterOrLessThan"}
_AGGREGATIONS = {"Average", "Count", "Minimum", "Maximum", "Total"}
_RULE_NAME_RE = re.compile(r"^[^*#&+:<>?@%{}\\/]+$")
_DISALLOWED_KQL = re.compile(r"(^|[;\n]\s*)\.|\b(externaldata|external_table|http_request|evaluate\s+python)\b", re.I)


def family_for_type(resource_type: str) -> str:
    return _RESOURCE_TYPE_TO_FAMILY.get((resource_type or "").lower(), "")


def api_for_family(family: str) -> tuple[str, str]:
    value = RULE_APIS.get(family)
    if not value:
        raise ValueError("Unsupported alert rule family.")
    return value


def _duration_minutes(value: str) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", value or "")
    if not match:
        return 0
    days, hours, minutes = (int(part or 0) for part in match.groups())
    return days * 1440 + hours * 60 + minutes


def _scope_matches_any(scope: str, wanted: set[str]) -> bool:
    normalized = scope.lower().rstrip("/")
    return any(
        normalized == item.rstrip("/")
        or normalized.startswith(item.rstrip("/") + "/")
        or item.rstrip("/").startswith(normalized + "/")
        for item in wanted
    )


def _rule_actions(props: dict[str, Any], family: str) -> list[str]:
    if family == "smart":
        actions = (props.get("actionGroups") or {}).get("groupIds") or []
    elif family == "prometheus":
        actions = [
            action.get("actionGroupId")
            for rule in props.get("rules") or [] if isinstance(rule, dict)
            for action in rule.get("actions") or [] if isinstance(action, dict)
        ]
    elif family == "log":
        actions = (props.get("actions") or {}).get("actionGroups") or []
    elif family == "activity":
        actions = [item.get("actionGroupId") for item in (props.get("actions") or {}).get("actionGroups") or [] if isinstance(item, dict)]
    else:
        actions = [item.get("actionGroupId") for item in props.get("actions") or [] if isinstance(item, dict)]
    return list(dict.fromkeys(str(value).strip().lower().rstrip("/") for value in actions if str(value).strip()))


def _condition_count(props: dict[str, Any], family: str) -> int:
    if family == "smart":
        return 1
    if family == "prometheus":
        return len(props.get("rules") or [])
    root = props.get("condition") if family == "activity" else props.get("criteria")
    return len((root or {}).get("allOf") or []) if isinstance(root, dict) else 0


def _activity_category(props: dict[str, Any]) -> str:
    condition = props.get("condition") if isinstance(props.get("condition"), dict) else {}
    for clause in condition.get("allOf") or []:
        if isinstance(clause, dict) and str(clause.get("field") or "").lower() == "category":
            return str(clause.get("equals") or "")
    return ""


def public_rule(resource: dict[str, Any]) -> dict[str, Any]:
    resource_type = str(resource.get("type") or "").lower()
    family = family_for_type(resource_type)
    props = resource.get("properties") if isinstance(resource.get("properties"), dict) else {}
    resource_id = str(resource.get("id") or "")
    result = {
        "id": resource_id,
        "name": str(resource.get("name") or service._name_from_id(resource_id)),
        "type": resource_type,
        "family": family,
        "category": _activity_category(props) if family == "activity" else "",
        "subscription_id": str(resource.get("subscriptionId") or service._subscription_from_id(resource_id)),
        "resource_group": str(resource.get("resourceGroup") or service._resource_group_from_id(resource_id)),
        "location": str(resource.get("location") or "Global"),
        "enabled": str(props.get("state") or "Enabled").lower() == "enabled" if family == "smart" else bool(props.get("enabled", True)),
        "severity": int(str(props.get("severity") or "Sev3").removeprefix("Sev")) if family == "smart" else (None if family in {"activity", "prometheus"} else int(props.get("severity", 3) or 0)),
        "description": str(props.get("description") or ""),
        "scopes": [str(value) for value in (props.get("scope") if family == "smart" else props.get("scopes")) or []],
        "action_group_ids": _rule_actions(props, family),
        "condition_count": _condition_count(props, family),
        "evaluation_frequency": str(props.get("frequency") if family == "smart" else props.get("interval") if family == "prometheus" else props.get("evaluationFrequency") or ""),
        "window_size": str(props.get("windowSize") or ""),
        "state_hash": service.canonical_hash(service._resource_body(resource)),
        "tags": resource.get("tags") or {},
    }
    if family == "activity":
        condition = props.get("condition") if isinstance(props.get("condition"), dict) else {}
        result["activity_conditions"] = json.loads(json.dumps(condition.get("allOf") or []))
    return result


async def _subscriptions(
    connection: dict[str, Any], workload_id: str | None, subscription_id: str | None,
    management_group_id: str | None,
) -> tuple[set[str], set[str]]:
    _workload, workload_ids, workload_subs = service._workload_context(workload_id)
    subscriptions = {subscription_id} if subscription_id else workload_subs
    if management_group_id:
        from app.workloads.discovery import subscriptions_under_mg

        subscriptions = set(await subscriptions_under_mg(connection, management_group_id))
    return subscriptions, workload_ids


async def list_rules(
    connection: dict[str, Any], *, workload_id: str | None = None, subscription_id: str | None = None,
    management_group_id: str | None = None, family: str = "", tenant_id: str = "",
    with_metadata: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized_family = family.strip().lower()
    key = inventory_cache.inventory_key(
        "rules", connection, tenant_id=tenant_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
        dimensions=(normalized_family,),
    )

    async def load() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return await _list_rules_uncached(
            connection, workload_id=workload_id, subscription_id=subscription_id,
            management_group_id=management_group_id, family=normalized_family,
        )

    rows, metadata = await inventory_cache.get_or_create(key, load)
    return (rows, metadata) if with_metadata else rows


async def _list_rules_uncached(
    connection: dict[str, Any], *, workload_id: str | None, subscription_id: str | None,
    management_group_id: str | None, family: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subscriptions, workload_ids = await _subscriptions(connection, workload_id, subscription_id, management_group_id)
    families = [family] if family else list(RULE_APIS)
    types = [api_for_family(item)[0] for item in families]
    quoted = ",".join(f"'{value}'" for value in types)
    rows, metadata = service._arg_rows_and_metadata(
        await service._arg(
            connection,
            f"resources | where type in~ ({quoted}) | project id, name, type, subscriptionId, resourceGroup, location, tags, properties",
            subscriptions,
            max_rows=10000,
            with_metadata=True,
        ),
        max_rows=10000,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        row_family = family_for_type(str(row.get("type") or ""))
        scopes = [str(value) for value in (props.get("scope") if row_family == "smart" else props.get("scopes")) or []]
        if workload_ids and scopes and not any(_scope_matches_any(scope, workload_ids) for scope in scopes):
            continue
        out.append(public_rule(row))
    out.sort(key=lambda item: (item["family"], item["name"].lower()))
    metadata["normalized_count"] = len(out)
    return out, metadata


async def get_rule(connection: dict[str, Any], resource_id: str, family: str) -> tuple[dict[str, Any] | None, int, str]:
    _resource_type, api_version = api_for_family(family)
    return await service.get_arm_resource(connection, resource_id, api_version)


def _dimensions_from_criteria(criteria: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or ""),
            "operator": str(item.get("operator") or "Include"),
            "values": [str(value) for value in item.get("values") or []],
        }
        for item in criteria.get("dimensions") or [] if isinstance(item, dict)
    ]


def editable_rule(resource: dict[str, Any]) -> dict[str, Any]:
    summary = public_rule(resource)
    family = summary["family"]
    props = resource.get("properties") if isinstance(resource.get("properties"), dict) else {}
    base: dict[str, Any] = {
        **summary,
        "auto_mitigate": bool(props.get("autoMitigate", True)),
        "target_resource_type": str(props.get("targetResourceType") or ""),
        "target_resource_region": str(props.get("targetResourceRegion") or ""),
        "identity": resource.get("identity") or {},
    }
    if family == "smart":
        action_groups = props.get("actionGroups") if isinstance(props.get("actionGroups"), dict) else {}
        detector = props.get("detector") if isinstance(props.get("detector"), dict) else {}
        base.update(
            {
                "detector_id": str(detector.get("id") or ""),
                "detector_parameters": detector.get("parameters") or {},
                "frequency": str(props.get("frequency") or "PT5M"),
                "throttling_duration": str((props.get("throttling") or {}).get("duration") or "PT0M"),
                "action_group_ids": _rule_actions(props, family),
                "conditions": [],
            }
        )
    elif family == "prometheus":
        base.update(
            {
                "interval": str(props.get("interval") or "PT1M"),
                "cluster_name": str(props.get("clusterName") or ""),
                "prometheus_rules": json.loads(json.dumps(props.get("rules") or [])),
                "conditions": [],
            }
        )
    elif family == "metric":
        conditions = []
        criteria = props.get("criteria") if isinstance(props.get("criteria"), dict) else {}
        for index, item in enumerate(criteria.get("allOf") or []):
            if not isinstance(item, dict):
                continue
            dynamic = str(item.get("criterionType") or "").lower().startswith("dynamic")
            conditions.append(
                {
                    "name": str(item.get("name") or f"condition-{index + 1}"),
                    "metric_name": str(item.get("metricName") or ""),
                    "metric_namespace": str(item.get("metricNamespace") or ""),
                    "threshold_type": "dynamic" if dynamic else "static",
                    "operator": str(item.get("operator") or ("GreaterOrLessThan" if dynamic else "GreaterThan")),
                    "threshold": item.get("threshold"),
                    "aggregation": str(item.get("timeAggregation") or "Average"),
                    "sensitivity": str(item.get("alertSensitivity") or "Medium"),
                    "min_failing_periods": int((item.get("failingPeriods") or {}).get("minFailingPeriodsToAlert", 1)),
                    "evaluation_periods": int((item.get("failingPeriods") or {}).get("numberOfEvaluationPeriods", 1)),
                    "dimensions": _dimensions_from_criteria(item),
                    "skip_metric_validation": bool(item.get("skipMetricValidation", False)),
                }
            )
        base["conditions"] = conditions
    elif family == "log":
        conditions = []
        for item in (props.get("criteria") or {}).get("allOf") or []:
            if not isinstance(item, dict):
                continue
            conditions.append(
                {
                    "query": str(item.get("query") or ""),
                    "aggregation": str(item.get("timeAggregation") or "Count"),
                    "metric_measure_column": str(item.get("metricMeasureColumn") or ""),
                    "resource_id_column": str(item.get("resourceIdColumn") or ""),
                    "operator": str(item.get("operator") or "GreaterThan"),
                    "threshold": item.get("threshold", 0),
                    "min_failing_periods": int((item.get("failingPeriods") or {}).get("minFailingPeriodsToAlert", 1)),
                    "evaluation_periods": int((item.get("failingPeriods") or {}).get("numberOfEvaluationPeriods", 1)),
                    "dimensions": _dimensions_from_criteria(item),
                }
            )
        base.update(
            {
                "display_name": str(props.get("displayName") or summary["name"]),
                "conditions": conditions,
                "mute_actions_duration": str(props.get("muteActionsDuration") or ""),
                "override_query_time_range": str(props.get("overrideQueryTimeRange") or ""),
                "target_resource_types": [str(value) for value in props.get("targetResourceTypes") or []],
                "skip_query_validation": bool(props.get("skipQueryValidation", False)),
            }
        )
    else:
        condition = props.get("condition") if isinstance(props.get("condition"), dict) else {}
        base["activity_conditions"] = json.loads(json.dumps(condition.get("allOf") or []))
    return base


async def metric_definitions(connection: dict[str, Any], resource_id: str) -> list[dict[str, Any]]:
    from app.azure.arm import get_metric_definitions

    token = await service._token(connection)
    text, error = await get_metric_definitions(token, resource_id)
    if error:
        raise ValueError(service.safe_error(error))
    try:
        values = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Azure returned an invalid metric catalog.") from exc
    out: list[dict[str, Any]] = []
    for item in values if isinstance(values, list) else []:
        name = item.get("name") if isinstance(item.get("name"), dict) else {}
        namespace = item.get("namespace") or item.get("metricNamespace") or ""
        out.append(
            {
                "name": str(name.get("value") or item.get("name") or ""),
                "display_name": str(name.get("localizedValue") or name.get("value") or ""),
                "namespace": str(namespace),
                "unit": str(item.get("unit") or ""),
                "primary_aggregation": str(item.get("primaryAggregationType") or "Average"),
                "supported_aggregations": [str(value) for value in item.get("supportedAggregationTypes") or []],
                "time_grains": [str(value.get("timeGrain") or "") for value in item.get("metricAvailabilities") or [] if isinstance(value, dict)],
                "dimensions": [
                    {
                        "name": str(
                            (value.get("name") or {}).get("value")
                            if isinstance(value.get("name"), dict)
                            else value.get("value") or value.get("name") or ""
                        ),
                        "display_name": str(
                            (value.get("name") or {}).get("localizedValue")
                            if isinstance(value.get("name"), dict)
                            else value.get("localizedValue") or value.get("value") or value.get("name") or ""
                        ),
                    }
                    for value in item.get("dimensions") or [] if isinstance(value, dict)
                ],
            }
        )
    out.sort(key=lambda item: item["display_name"].lower())
    return out


async def metric_preview(
    connection: dict[str, Any], resource_id: str, metric_name: str, aggregation: str, interval: str,
) -> dict[str, Any]:
    from app.exec.command_runner import run_metrics_capture

    start = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    capture = await run_metrics_capture(
        resource_id, [metric_name], connection, aggregation=aggregation, interval=interval, timespan=start
    )
    if not capture.ok:
        raise ValueError(service.safe_error(capture.error or capture.stderr))
    try:
        payload = json.loads(capture.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Azure returned invalid metric preview data.") from exc
    points: list[dict[str, Any]] = []
    for metric in payload.get("value") or []:
        for timeseries in metric.get("timeseries") or []:
            for point in timeseries.get("data") or []:
                if not isinstance(point, dict):
                    continue
                value = point.get(aggregation.lower())
                if value is None:
                    value = next((point.get(key) for key in ("average", "total", "maximum", "minimum", "count") if point.get(key) is not None), None)
                points.append({"time": point.get("timeStamp") or point.get("timestamp"), "value": value})
    numeric = [float(item["value"]) for item in points if isinstance(item.get("value"), (int, float))]
    return {
        "points": points[-200:],
        "count": len(points),
        "minimum": min(numeric) if numeric else None,
        "maximum": max(numeric) if numeric else None,
        "average": sum(numeric) / len(numeric) if numeric else None,
    }


def _validate_common(payload: dict[str, Any], *, create: bool) -> list[str]:
    errors: list[str] = []
    name = str(payload.get("name") or "").strip()
    if create and not name:
        errors.append("Rule name is required.")
    if name and (not _RULE_NAME_RE.fullmatch(name) or name.endswith((" ", "."))):
        errors.append("Rule name contains characters Azure Monitor doesn't allow.")
    if create and not str(payload.get("subscription_id") or "").strip():
        errors.append("Subscription is required.")
    if create and not str(payload.get("resource_group") or "").strip():
        errors.append("Resource group is required.")
    scopes = [str(value).strip() for value in payload.get("scopes") or [] if str(value).strip()]
    if not scopes:
        errors.append("At least one Azure scope is required.")
    if any(not value.startswith("/subscriptions/") for value in scopes):
        errors.append("Every scope must be a full subscription resource ID.")
    action_groups = [str(value) for value in payload.get("action_group_ids") or [] if value]
    if len(action_groups) > 5:
        errors.append("Azure Monitor supports at most five Action Groups per alert rule.")
    if any("/providers/microsoft.insights/actiongroups/" not in value.lower() for value in action_groups):
        errors.append("Every Action Group must be a full Microsoft.Insights/actionGroups resource ID.")
    return errors


def validate_metric_payload(payload: dict[str, Any], *, create: bool) -> list[str]:
    errors = _validate_common(payload, create=create)
    if payload.get("severity") not in {0, 1, 2, 3, 4}:
        errors.append("Metric alert severity must be between 0 and 4.")
    frequency = str(payload.get("evaluation_frequency") or "")
    window = str(payload.get("window_size") or "")
    if frequency not in _FREQUENCIES[:6]:
        errors.append("Metric evaluation frequency must be PT1M, PT5M, PT10M, PT15M, PT30M, or PT1H.")
    if window not in _WINDOWS:
        errors.append("Unsupported metric lookback window.")
    if _duration_minutes(window) < _duration_minutes(frequency):
        errors.append("Lookback window must be greater than or equal to evaluation frequency.")
    conditions = payload.get("conditions") or []
    if not conditions:
        errors.append("At least one metric condition is required.")
    dynamic_count = sum(1 for item in conditions if item.get("threshold_type") == "dynamic")
    if dynamic_count and len(conditions) > 1:
        errors.append("Dynamic thresholds can't be combined with multiple conditions.")
    if dynamic_count and len(payload.get("scopes") or []) > 5:
        errors.append("Dynamic threshold rules support at most five explicitly selected resources.")
    for index, item in enumerate(conditions):
        label = f"Condition {index + 1}"
        if not str(item.get("metric_name") or "").strip():
            errors.append(f"{label} needs a metric name.")
        if not str(item.get("metric_namespace") or "").strip():
            errors.append(f"{label} needs a metric namespace.")
        aggregation = str(item.get("aggregation") or "")
        if aggregation not in _AGGREGATIONS:
            errors.append(f"{label} has an unsupported aggregation.")
        dynamic = item.get("threshold_type") == "dynamic"
        operator = str(item.get("operator") or "")
        if operator not in (_DYNAMIC_OPERATORS if dynamic else _OPERATORS):
            errors.append(f"{label} has an unsupported operator.")
        if not dynamic and not isinstance(item.get("threshold"), (int, float)):
            errors.append(f"{label} needs a numeric threshold.")
        if dynamic:
            failing = int(item.get("min_failing_periods") or 1)
            periods = int(item.get("evaluation_periods") or 1)
            if failing < 1 or periods < failing:
                errors.append(f"{label} has invalid dynamic failing periods.")
        for dimension in item.get("dimensions") or []:
            if not dimension.get("name") or dimension.get("operator") not in {"Include", "Exclude"} or not dimension.get("values"):
                errors.append(f"{label} has an invalid dimension filter.")
    if len(payload.get("scopes") or []) > 1 and not payload.get("target_resource_type"):
        errors.append("Multi-resource rules require a target resource type.")
    if len(payload.get("scopes") or []) > 1 and not payload.get("target_resource_region"):
        errors.append("Multi-resource rules require a target resource region.")
    return list(dict.fromkeys(errors))


def validate_kql(query: str) -> list[str]:
    text = (query or "").strip()
    errors: list[str] = []
    if not text:
        errors.append("KQL query is required.")
    if len(text) > 8000:
        errors.append("KQL query must be at most 8,000 characters.")
    if _DISALLOWED_KQL.search(text):
        errors.append("KQL control commands and external data functions aren't allowed in alert previews.")
    return errors


def validate_log_payload(payload: dict[str, Any], *, create: bool) -> list[str]:
    errors = _validate_common(payload, create=create)
    scopes = [str(scope or "").strip() for scope in payload.get("scopes") or [] if str(scope or "").strip()]
    if len(scopes) != 1 or "/providers/microsoft.operationalinsights/workspaces/" not in scopes[0].lower():
        errors.append("Log alerts must target exactly one Log Analytics workspace.")
    if payload.get("severity") not in {0, 1, 2, 3, 4}:
        errors.append("Log alert severity must be between 0 and 4.")
    if not str(payload.get("location") or "").strip():
        errors.append("Log alert location must match the Log Analytics workspace region.")
    frequency = str(payload.get("evaluation_frequency") or "")
    window = str(payload.get("window_size") or "")
    if frequency not in _FREQUENCIES:
        errors.append("Unsupported log-alert evaluation frequency.")
    if window not in _WINDOWS:
        errors.append("Unsupported log-alert lookback window.")
    if _duration_minutes(window) < _duration_minutes(frequency):
        errors.append("Lookback window must be greater than or equal to evaluation frequency.")
    conditions = payload.get("conditions") or []
    if not conditions:
        errors.append("At least one log condition is required.")
    for index, item in enumerate(conditions):
        errors.extend(validate_kql(str(item.get("query") or "")))
        if str(item.get("aggregation") or "") not in _AGGREGATIONS:
            errors.append(f"Condition {index + 1} has an unsupported aggregation.")
        if str(item.get("operator") or "") not in _OPERATORS:
            errors.append(f"Condition {index + 1} has an unsupported operator.")
        if not isinstance(item.get("threshold"), (int, float)):
            errors.append(f"Condition {index + 1} needs a numeric threshold.")
    identity = payload.get("identity") or {}
    if identity.get("type") == "UserAssigned" and not (identity.get("userAssignedIdentities") or {}):
        errors.append("A user-assigned rule identity requires its full resource ID.")
    return list(dict.fromkeys(errors))


def validate_activity_payload(payload: dict[str, Any], *, create: bool) -> list[str]:
    errors = _validate_common(payload, create=create)
    if str(payload.get("location") or "Global").lower() not in {"global", "west europe", "westeurope", "north europe", "northeurope"}:
        errors.append("Activity Log alert location must be Global, West Europe, or North Europe.")
    for scope in payload.get("scopes") or []:
        parts = str(scope).strip("/").split("/")
        if len(parts) not in {2, 4} or parts[0].lower() != "subscriptions" or (len(parts) == 4 and parts[2].lower() != "resourcegroups"):
            errors.append("Activity Log alert scopes must be a subscription or resource-group resource ID.")
    conditions = payload.get("activity_conditions") or []
    if not conditions:
        errors.append("At least one Activity Log condition is required.")
    category = next((str(item.get("equals") or "") for item in conditions if isinstance(item, dict) and str(item.get("field") or "").lower() == "category"), "")
    if category not in {"Administrative", "ServiceHealth", "ResourceHealth", "Security", "Recommendation"}:
        errors.append("Select a supported Activity Log category.")
    for item in conditions:
        if not isinstance(item, dict):
            errors.append("Invalid Activity Log condition.")
            continue
        if "anyOf" in item:
            if not isinstance(item["anyOf"], list) or not item["anyOf"]:
                errors.append("Activity Log anyOf must contain at least one condition.")
        elif not item.get("field") or (not item.get("equals") and not item.get("containsAny")):
            errors.append("Each Activity Log condition needs a field and equals/containsAny value.")
    return list(dict.fromkeys(errors))


def validate_smart_payload(payload: dict[str, Any], *, create: bool) -> list[str]:
    errors = _validate_common(payload, create=create)
    if not str(payload.get("detector_id") or "").strip():
        errors.append("Smart Detector ID is required.")
    if payload.get("severity") not in {0, 1, 2, 3, 4}:
        errors.append("Smart Detector severity must be between 0 and 4.")
    if not re.fullmatch(r"PT(?:[1-9]|[1-5]\d)M", str(payload.get("frequency") or "")):
        errors.append("Smart Detector frequency must be an ISO-8601 minute duration.")
    if str(payload.get("detector_id") or "").lower() == "failureanomaliesdetector" and payload.get("frequency") != "PT1M":
        errors.append("FailureAnomaliesDetector requires PT1M evaluation frequency.")
    if not re.fullmatch(r"PT(?:0|[1-9]|[1-5]\d)M", str(payload.get("throttling_duration") or "")):
        errors.append("Smart Detector throttling must be an ISO-8601 minute duration.")
    if not isinstance(payload.get("detector_parameters") or {}, dict):
        errors.append("Detector parameters must be a JSON object.")
    return list(dict.fromkeys(errors))


def validate_promql(expression: str) -> list[str]:
    value = (expression or "").strip()
    errors: list[str] = []
    if not value:
        errors.append("PromQL expression is required.")
    if len(value) > 8000:
        errors.append("PromQL expression must be at most 8,000 characters.")
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        if value.count(opening) != value.count(closing):
            errors.append(f"PromQL has unbalanced {opening}{closing} delimiters.")
    if len(re.findall(r"\b(on|ignoring|group_left|group_right)\b", value)) > 10:
        errors.append("PromQL contains too many vector matching/join modifiers.")
    if re.search(r"\b(label_replace|label_join)\s*\([^)]{4000,}", value):
        errors.append("PromQL label manipulation is too complex.")
    return errors


def validate_prometheus_payload(payload: dict[str, Any], *, create: bool) -> list[str]:
    errors = _validate_common(payload, create=create)
    scopes = payload.get("scopes") or []
    if len(scopes) != 1:
        errors.append("This Prometheus API version requires exactly one Azure Monitor workspace scope.")
    minutes = _duration_minutes(str(payload.get("interval") or ""))
    if minutes < 1 or minutes > 15:
        errors.append("Prometheus rule-group interval must be between 1 and 15 minutes.")
    rule_values = payload.get("prometheus_rules") or []
    if not rule_values:
        errors.append("At least one Prometheus alert or recording rule is required.")
    for index, item in enumerate(rule_values):
        if not isinstance(item, dict):
            errors.append(f"Prometheus rule {index + 1} is invalid.")
            continue
        errors.extend(validate_promql(str(item.get("expression") or item.get("expr") or "")))
        if not item.get("alert") and not item.get("record"):
            errors.append(f"Prometheus rule {index + 1} needs an alert or record name.")
        if item.get("alert") and item.get("record"):
            errors.append(f"Prometheus rule {index + 1} can't be both an alert and recording rule.")
        if item.get("severity") is not None and item.get("severity") not in {0, 1, 2, 3, 4}:
            errors.append(f"Prometheus rule {index + 1} severity must be between 0 and 4.")
        if len(item.get("actions") or []) > 5:
            errors.append(f"Prometheus rule {index + 1} has more than five Action Groups.")
    return list(dict.fromkeys(errors))


def validate_rule_payload(family: str, payload: dict[str, Any], *, create: bool) -> list[str]:
    if family == "metric":
        return validate_metric_payload(payload, create=create)
    if family == "log":
        return validate_log_payload(payload, create=create)
    if family == "activity":
        return validate_activity_payload(payload, create=create)
    if family == "smart":
        return validate_smart_payload(payload, create=create)
    if family == "prometheus":
        return validate_prometheus_payload(payload, create=create)
    return ["Unsupported alert rule family."]


def _dimension_body(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or ""),
            "operator": str(item.get("operator") or "Include"),
            "values": [str(value) for value in item.get("values") or []],
        }
        for item in values
    ]


def build_metric_body(payload: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
    conditions: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("conditions") or []):
        dynamic = item.get("threshold_type") == "dynamic"
        condition: dict[str, Any] = {
            "name": str(item.get("name") or f"condition-{index + 1}"),
            "criterionType": "DynamicThresholdCriterion" if dynamic else "StaticThresholdCriterion",
            "metricName": str(item.get("metric_name") or ""),
            "metricNamespace": str(item.get("metric_namespace") or ""),
            "operator": str(item.get("operator") or ("GreaterOrLessThan" if dynamic else "GreaterThan")),
            "timeAggregation": str(item.get("aggregation") or "Average"),
            "dimensions": _dimension_body(item.get("dimensions") or []),
        }
        if dynamic:
            condition.update(
                {
                    "alertSensitivity": str(item.get("sensitivity") or "Medium"),
                    "failingPeriods": {
                        "minFailingPeriodsToAlert": int(item.get("min_failing_periods") or 1),
                        "numberOfEvaluationPeriods": int(item.get("evaluation_periods") or 1),
                    },
                }
            )
        else:
            condition["threshold"] = float(item.get("threshold"))
        if item.get("skip_metric_validation"):
            condition["skipMetricValidation"] = True
        conditions.append(condition)
    scopes = [str(value) for value in payload.get("scopes") or []]
    criteria_type = "Microsoft.Azure.Monitor.MultipleResourceMultipleMetricCriteria"
    props = {
        "description": str(payload.get("description") or "")[:2048],
        "severity": int(payload.get("severity", 3)),
        "enabled": bool(payload.get("enabled", False)),
        "scopes": scopes,
        "evaluationFrequency": str(payload.get("evaluation_frequency") or "PT5M"),
        "windowSize": str(payload.get("window_size") or "PT15M"),
        "criteria": {"odata.type": criteria_type, "allOf": conditions},
        "actions": [{"actionGroupId": value} for value in payload.get("action_group_ids") or []],
        "autoMitigate": bool(payload.get("auto_mitigate", True)),
    }
    if payload.get("target_resource_type"):
        props["targetResourceType"] = payload["target_resource_type"]
    if payload.get("target_resource_region"):
        props["targetResourceRegion"] = payload["target_resource_region"]
    return {
        "location": str(payload.get("location") or (before or {}).get("location") or "global"),
        "tags": payload.get("tags") if isinstance(payload.get("tags"), dict) else (before or {}).get("tags") or {},
        "properties": props,
    }


def build_log_body(payload: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
    conditions = []
    for item in payload.get("conditions") or []:
        condition: dict[str, Any] = {
            "query": str(item.get("query") or ""),
            "timeAggregation": str(item.get("aggregation") or "Count"),
            "operator": str(item.get("operator") or "GreaterThan"),
            "threshold": float(item.get("threshold", 0)),
            "failingPeriods": {
                "minFailingPeriodsToAlert": int(item.get("min_failing_periods") or 1),
                "numberOfEvaluationPeriods": int(item.get("evaluation_periods") or 1),
            },
            "dimensions": _dimension_body(item.get("dimensions") or []),
        }
        if item.get("metric_measure_column"):
            condition["metricMeasureColumn"] = item["metric_measure_column"]
        if item.get("resource_id_column"):
            condition["resourceIdColumn"] = item["resource_id_column"]
        conditions.append(condition)
    props: dict[str, Any] = {
        "displayName": str(payload.get("display_name") or payload.get("name") or ""),
        "description": str(payload.get("description") or "")[:4096],
        "severity": int(payload.get("severity", 3)),
        "enabled": bool(payload.get("enabled", False)),
        "scopes": [str(value) for value in payload.get("scopes") or []],
        "evaluationFrequency": str(payload.get("evaluation_frequency") or "PT5M"),
        "windowSize": str(payload.get("window_size") or "PT15M"),
        "criteria": {"allOf": conditions},
        "actions": {"actionGroups": [str(value) for value in payload.get("action_group_ids") or []], "customProperties": payload.get("custom_properties") or {}},
        "autoMitigate": bool(payload.get("auto_mitigate", True)),
        "skipQueryValidation": False,
    }
    if payload.get("mute_actions_duration"):
        props["muteActionsDuration"] = payload["mute_actions_duration"]
    if payload.get("override_query_time_range"):
        props["overrideQueryTimeRange"] = payload["override_query_time_range"]
    if payload.get("target_resource_types"):
        props["targetResourceTypes"] = payload["target_resource_types"]
    body: dict[str, Any] = {
        "kind": "LogAlert",
        "location": str(payload.get("location") or (before or {}).get("location") or ""),
        "tags": payload.get("tags") if isinstance(payload.get("tags"), dict) else (before or {}).get("tags") or {},
        "properties": props,
    }
    identity = payload.get("identity") or (before or {}).get("identity")
    if identity:
        body["identity"] = identity
    return body


def build_activity_body(payload: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "location": str(payload.get("location") or (before or {}).get("location") or "Global"),
        "tags": payload.get("tags") if isinstance(payload.get("tags"), dict) else (before or {}).get("tags") or {},
        "properties": {
            "scopes": [str(value) for value in payload.get("scopes") or []],
            "condition": {"allOf": payload.get("activity_conditions") or []},
            "actions": {"actionGroups": [{"actionGroupId": value} for value in payload.get("action_group_ids") or []]},
            "enabled": bool(payload.get("enabled", False)),
            "description": str(payload.get("description") or "")[:2048],
        },
    }


def build_smart_body(payload: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "location": str(payload.get("location") or (before or {}).get("location") or "global"),
        "tags": payload.get("tags") if isinstance(payload.get("tags"), dict) else (before or {}).get("tags") or {},
        "properties": {
            "description": str(payload.get("description") or "")[:2048],
            "state": "Enabled" if payload.get("enabled", False) else "Disabled",
            "severity": f"Sev{int(payload.get('severity', 3))}",
            "frequency": str(payload.get("frequency") or "PT5M"),
            "scope": [str(value) for value in payload.get("scopes") or []],
            "detector": {"id": str(payload.get("detector_id") or ""), "parameters": payload.get("detector_parameters") or {}},
            "actionGroups": {"groupIds": [str(value) for value in payload.get("action_group_ids") or []]},
            "throttling": {"duration": str(payload.get("throttling_duration") or "PT0M")},
        },
    }


def build_prometheus_body(payload: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_rules = []
    for item in payload.get("prometheus_rules") or []:
        rule: dict[str, Any] = {
            "expression": str(item.get("expression") or item.get("expr") or ""),
            "enabled": bool(item.get("enabled", True)),
        }
        for key in ("alert", "record", "for", "severity"):
            if item.get(key) not in (None, ""):
                rule[key] = item[key]
        for key in ("labels", "annotations", "resolveConfiguration"):
            if isinstance(item.get(key), dict) and item[key]:
                rule[key] = item[key]
        actions = []
        for action in item.get("actions") or []:
            if isinstance(action, str):
                actions.append({"actionGroupId": action})
            elif isinstance(action, dict) and action.get("actionGroupId"):
                actions.append({"actionGroupId": action["actionGroupId"], **({"actionProperties": action["actionProperties"]} if action.get("actionProperties") else {})})
        if actions:
            rule["actions"] = actions
        normalized_rules.append(rule)
    props: dict[str, Any] = {
        "description": str(payload.get("description") or "")[:2048],
        "enabled": bool(payload.get("enabled", False)),
        "interval": str(payload.get("interval") or "PT1M"),
        "scopes": [str(value) for value in payload.get("scopes") or []],
        "rules": normalized_rules,
    }
    if payload.get("cluster_name"):
        props["clusterName"] = payload["cluster_name"]
    return {
        "location": str(payload.get("location") or (before or {}).get("location") or ""),
        "tags": payload.get("tags") if isinstance(payload.get("tags"), dict) else (before or {}).get("tags") or {},
        "properties": props,
    }


def build_rule_body(family: str, payload: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
    if family == "metric":
        return build_metric_body(payload, before)
    if family == "log":
        return build_log_body(payload, before)
    if family == "activity":
        return build_activity_body(payload, before)
    if family == "smart":
        return build_smart_body(payload, before)
    if family == "prometheus":
        return build_prometheus_body(payload, before)
    raise ValueError("Unsupported alert rule family.")


def summarize_rule_body(family: str, body: dict[str, Any]) -> dict[str, Any]:
    props = body.get("properties") if isinstance(body.get("properties"), dict) else {}
    actions = _rule_actions(props, family)
    scopes = props.get("scope") if family == "smart" else props.get("scopes")
    return {
        "family": family,
        "enabled": str(props.get("state") or "Disabled").lower() == "enabled" if family == "smart" else bool(props.get("enabled", False)),
        "severity": props.get("severity"),
        "scope_count": len(scopes or []),
        "condition_count": _condition_count(props, family),
        "action_group_count": len(actions),
        "evaluation_frequency": str(props.get("frequency") if family == "smart" else props.get("interval") if family == "prometheus" else props.get("evaluationFrequency") or ""),
        "window_size": str(props.get("windowSize") or ""),
    }


def cost_advisory(family: str, payload: dict[str, Any]) -> dict[str, Any]:
    frequency = max(1, _duration_minutes(str(payload.get("evaluation_frequency") or "PT5M")))
    evaluations_per_day = round(1440 / frequency)
    scopes = len(payload.get("scopes") or [])
    dimensions = sum(len(item.get("dimensions") or []) for item in payload.get("conditions") or [])
    score = evaluations_per_day * max(1, scopes) * max(1, dimensions)
    warnings: list[str] = []
    if frequency <= 1:
        warnings.append("One-minute evaluation has a higher Azure Monitor cost and a subscription quota.")
    if scopes > 20:
        warnings.append("This rule targets many resources; verify time-series and notification volume.")
    if dimensions:
        warnings.append("Dimension splitting can create multiple independently billed time series or alert instances.")
    if family == "log":
        warnings.append("Log alert cost depends on evaluation frequency and the amount of data scanned by the KQL query.")
    return {"evaluations_per_day": evaluations_per_day, "relative_evaluation_units": score, "warnings": warnings}


async def log_preview(
    connection: dict[str, Any], workspace_id: str, query: str, timespan: str = "PT1H"
) -> dict[str, Any]:
    errors = validate_kql(query)
    if errors:
        raise ValueError("; ".join(errors))
    workspace = workspace_id.strip()
    if workspace.startswith("/subscriptions/"):
        if "/providers/microsoft.operationalinsights/workspaces/" not in workspace.lower():
            raise ValueError("Select a Log Analytics workspace before previewing the query.")
        resource, _status, error = await service.get_arm_resource(connection, workspace, "2022-10-01")
        if error or not resource:
            raise ValueError(error or "Log Analytics workspace not found.")
        workspace = str((resource.get("properties") or {}).get("customerId") or "")
    if not workspace:
        raise ValueError("A Log Analytics workspace ID is required.")
    from app.exec.command_runner import run_la_capture

    capture = await run_la_capture(query, workspace, connection, timespan=timespan)
    if not capture.ok:
        raise ValueError(service.safe_error(capture.error or capture.stderr))
    try:
        rows = json.loads(capture.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Log Analytics returned invalid preview data.") from exc
    if isinstance(rows, dict):
        rows = rows.get("tables") or rows.get("value") or []
    if not isinstance(rows, list):
        rows = []
    return {"rows": rows[:100], "row_count": len(rows), "truncated": len(rows) > 100, "timespan": timespan}


async def apply_rule_change(connection: dict[str, Any], change: Any) -> tuple[dict[str, Any] | None, int, str]:
    from app.azure.arm import arm_write

    service.assert_writable(connection)
    family = str(change.target_type).removesuffix("_rule")
    _resource_type, api_version = api_for_family(family)
    token = await service._token(connection)
    desired = service.decrypted_json(change.desired_encrypted)
    if change.operation != "create":
        live, status, error = await get_rule(connection, change.target_id, family)
        if error or not live:
            return None, status, error or "The alert rule no longer exists."
        if service.canonical_hash(service._resource_body(live)) != change.expected_state_hash:
            return None, 409, "Azure state changed after this request was reviewed. Refresh and create a new change."
    else:
        live, status, _error = await get_rule(connection, change.target_id, family)
        if live:
            return None, 409, "An alert rule with this name already exists."
        if status not in (0, 404):
            return None, status, "Could not verify that the alert-rule name is available."
    if change.operation == "delete":
        _data, error, status = await arm_write(token, "DELETE", change.target_id, api_version=api_version)
        return ({} if not error else None), status, service.safe_error(error)
    body = desired.get("body") if isinstance(desired.get("body"), dict) else {}
    data, error, status = await arm_write(token, "PUT", change.target_id, body=body, api_version=api_version)
    return (data if isinstance(data, dict) else {} if not error else None), status, service.safe_error(error)

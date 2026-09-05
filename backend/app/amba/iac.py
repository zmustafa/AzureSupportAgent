"""Generate Bicep / Terraform / Azure Policy artifacts for AMBA alert-coverage gaps.

Unlike the frontend architecture skeleton exporters, these emit real, parameterized alert
resources from the recommended-alert spec + the target resource. Every AMBA alert class is
covered:

* metric (static)   ``Microsoft.Insights/metricAlerts`` with ``StaticThresholdCriterion``
* metric (dynamic)  ``Microsoft.Insights/metricAlerts`` with ``DynamicThresholdCriterion``,
                    carrying ``alertSensitivity`` and ``failingPeriods``
* log-search        ``Microsoft.Insights/scheduledQueryRules`` with the baseline KQL
* activity log      ``Microsoft.Insights/activityLogAlerts`` (Service Health, Resource
                    Health, and Administrative delete/change alerts)

A fourth format, ``policy``, mirrors how AMBA-ALZ actually deploys at scale: an Azure Policy
assignment of the published AMBA initiative at management-group scope, rather than
per-resource templates.

Output is read-only artifact text for download/review — it is NEVER applied by the app."""
from __future__ import annotations

import json
import re
from typing import Any

_SEVERITY_NUM = {"critical": 0, "error": 1, "warning": 2, "info": 3}

# Terraform's azurerm provider accepts the same operator spellings as ARM.
_OPERATORS = {
    "GreaterThan", "GreaterThanOrEqual", "LessThan", "LessThanOrEqual",
    "GreaterOrLessThan", "Equals",
}


def _ident(s: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9]+", "_", s or "").strip("_")
    if not out:
        out = "alert"
    if out[0].isdigit():
        out = f"a_{out}"
    return out


def _severity(gap: dict[str, Any]) -> int:
    num = gap.get("severity_num")
    if isinstance(num, int) and 0 <= num <= 4:
        return num
    return _SEVERITY_NUM.get(str(gap.get("severity") or "warning"), 2)


def _operator(rec: dict[str, Any]) -> str:
    op = str(rec.get("operator") or "GreaterThan")
    return op if op in _OPERATORS else "GreaterThan"


def _rule_name(gap: dict[str, Any]) -> str:
    return f"{gap.get('resource_name', 'res')}-{gap.get('alert_key', 'alert')}"[:128]


def _gap_symbol(gap: dict[str, Any]) -> str:
    return _ident(f"{gap.get('resource_name','res')}_{gap.get('alert_key','alert')}")


def _description(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended") or {}
    text = gap.get("why") or rec.get("metric") or gap.get("alert_name") or ""
    return re.sub(r"\s+", " ", str(text)).strip()[:400]


def _q(value: str) -> str:
    """Single-quoted Bicep string literal."""
    return "'" + str(value).replace("'", "\\'") + "'"


def _dq(value: str) -> str:
    """Double-quoted Terraform/JSON string literal."""
    return json.dumps(str(value))


def _alert_kind(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended") or {}
    kind = str(gap.get("alert_type") or rec.get("alert_type") or "metric").lower()
    return kind if kind in ("metric", "log", "activitylog") else "metric"


# --------------------------------------------------------------------------- Bicep
def _bicep_metric(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended", {})
    sym = _gap_symbol(gap)
    metric = rec.get("metric", "")
    dynamic = str(rec.get("criterion_type") or "") == "DynamicThresholdCriterion"
    window = rec.get("window_size") or rec.get("window") or "PT5M"
    frequency = rec.get("evaluation_frequency") or window
    aggregation = rec.get("aggregation") or "Average"
    namespace = rec.get("metric_namespace") or gap.get("resource_type", "")

    criterion: list[str] = [
        "        {",
        f"          name: {_q(_ident(metric) or 'criterion')}",
        f"          metricName: {_q(metric)}",
        f"          metricNamespace: {_q(namespace)}",
        f"          operator: {_q(_operator(rec))}",
        f"          timeAggregation: {_q(aggregation)}",
    ]
    dimensions = rec.get("dimensions") or []
    if dimensions:
        criterion.append("          dimensions: [")
        for dim in dimensions:
            values = ", ".join(_q(v) for v in dim.get("values") or [])
            criterion.append("            {")
            criterion.append(f"              name: {_q(dim.get('name', ''))}")
            criterion.append(f"              operator: {_q(dim.get('operator') or 'Include')}")
            criterion.append(f"              values: [{values}]")
            criterion.append("            }")
        criterion.append("          ]")

    if dynamic:
        failing = rec.get("failing_periods") or {}
        criterion += [
            "          criterionType: 'DynamicThresholdCriterion'",
            f"          alertSensitivity: {_q(rec.get('alert_sensitivity') or 'Medium')}",
            "          failingPeriods: {",
            f"            numberOfEvaluationPeriods: {failing.get('number_of_evaluation_periods') or 4}",
            f"            minFailingPeriodsToAlert: {failing.get('min_failing_periods_to_alert') or 4}",
            "          }",
        ]
        odata = "Microsoft.Azure.Monitor.MultipleResourceMultipleMetricCriteria"
    else:
        threshold = rec.get("threshold")
        criterion += [
            "          criterionType: 'StaticThresholdCriterion'",
            f"          threshold: {threshold if threshold is not None else 0}",
        ]
        odata = "Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria"
    criterion.append("        }")

    return "\n".join(
        [
            f"resource {sym} 'Microsoft.Insights/metricAlerts@2018-03-01' = {{",
            f"  name: {_q(_rule_name(gap))}",
            "  location: 'global'",
            "  properties: {",
            f"    description: {_q(_description(gap))}",
            f"    severity: {_severity(gap)}",
            "    enabled: true",
            "    scopes: [",
            f"      {_q(gap.get('resource_id', ''))}",
            "    ]",
            f"    evaluationFrequency: {_q(frequency)}",
            f"    windowSize: {_q(window)}",
            "    criteria: {",
            f"      'odata.type': {_q(odata)}",
            "      allOf: [",
            *criterion,
            "      ]",
            "    }",
            "    // TODO: wire an action group below before deploying.",
            "    actions: [",
            "      // { actionGroupId: actionGroupId }",
            "    ]",
            "  }",
            "}",
        ]
    )


def _bicep_log(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended", {})
    sym = _gap_symbol(gap)
    window = rec.get("window_size") or "PT15M"
    frequency = rec.get("evaluation_frequency") or window
    query = str(rec.get("log_query") or "").rstrip()
    threshold = rec.get("threshold")
    indented = "\n".join(f"          {line}" for line in query.splitlines()) or "          // query unavailable"
    return "\n".join(
        [
            f"resource {sym} 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {{",
            f"  name: {_q(_rule_name(gap))}",
            "  location: resourceGroup().location",
            "  properties: {",
            f"    description: {_q(_description(gap))}",
            f"    severity: {_severity(gap)}",
            "    enabled: true",
            "    scopes: [",
            f"      {_q(gap.get('resource_id', ''))}",
            "    ]",
            f"    evaluationFrequency: {_q(frequency)}",
            f"    windowSize: {_q(window)}",
            "    criteria: {",
            "      allOf: [",
            "        {",
            "          query: '''",
            indented,
            "          '''",
            f"          operator: {_q(_operator(rec))}",
            f"          threshold: {threshold if threshold is not None else 0}",
            "          timeAggregation: 'Average'",
            "          metricMeasureColumn: 'AggregatedValue'",
            "          resourceIdColumn: '_ResourceId'",
            "          failingPeriods: {",
            "            numberOfEvaluationPeriods: 1",
            "            minFailingPeriodsToAlert: 1",
            "          }",
            "        }",
            "      ]",
            "    }",
            "    // TODO: wire an action group below before deploying.",
            "    actions: {",
            "      actionGroups: []",
            "    }",
            "  }",
            "}",
        ]
    )


def _bicep_activity(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended", {})
    sym = _gap_symbol(gap)
    activity = rec.get("activity_log") or {}
    conditions = [
        "        {",
        "          field: 'category'",
        f"          equals: {_q(activity.get('category', 'ServiceHealth'))}",
        "        }",
    ]
    if activity.get("operationName"):
        conditions += [
            "        {",
            "          field: 'operationName'",
            f"          equals: {_q(activity['operationName'])}",
            "        }",
        ]
    if activity.get("incidentType"):
        conditions += [
            "        {",
            "          field: 'properties.incidentType'",
            f"          equals: {_q(activity['incidentType'])}",
            "        }",
        ]
    for field_name, key in (("status", "status"), ("properties.currentHealthStatus", "currentHealthStatus")):
        values = activity.get(key)
        if isinstance(values, list) and values:
            joined = ", ".join(_q(v) for v in values)
            conditions += [
                "        {",
                f"          field: {_q(field_name)}",
                f"          containsAny: [{joined}]",
                "        }",
            ]

    scope = gap.get("subscription_id") or ""
    scope_id = f"/subscriptions/{scope}" if scope else gap.get("resource_id", "")
    return "\n".join(
        [
            f"resource {sym} 'Microsoft.Insights/activityLogAlerts@2020-10-01' = {{",
            f"  name: {_q(_rule_name(gap))}",
            "  location: 'global'",
            "  properties: {",
            f"    description: {_q(_description(gap))}",
            "    enabled: true",
            "    scopes: [",
            f"      {_q(scope_id)}",
            "    ]",
            "    condition: {",
            "      allOf: [",
            *conditions,
            "      ]",
            "    }",
            "    // TODO: wire an action group below before deploying.",
            "    actions: {",
            "      actionGroups: []",
            "    }",
            "  }",
            "}",
        ]
    )


def _bicep_block(gap: dict[str, Any]) -> str:
    kind = _alert_kind(gap)
    if kind == "log":
        return _bicep_log(gap)
    if kind == "activitylog":
        return _bicep_activity(gap)
    return _bicep_metric(gap)


# --------------------------------------------------------------------------- Terraform
def _tf_metric(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended", {})
    sym = _gap_symbol(gap)
    metric = rec.get("metric", "")
    dynamic = str(rec.get("criterion_type") or "") == "DynamicThresholdCriterion"
    window = rec.get("window_size") or rec.get("window") or "PT5M"
    frequency = rec.get("evaluation_frequency") or window
    namespace = rec.get("metric_namespace") or gap.get("resource_type", "")
    aggregation = rec.get("aggregation") or "Average"

    dimension_lines: list[str] = []
    for dim in rec.get("dimensions") or []:
        values = ", ".join(_dq(v) for v in dim.get("values") or [])
        dimension_lines += [
            "    dimension {",
            f"      name     = {_dq(dim.get('name', ''))}",
            f"      operator = {_dq(dim.get('operator') or 'Include')}",
            f"      values   = [{values}]",
            "    }",
        ]

    if dynamic:
        failing = rec.get("failing_periods") or {}
        criteria = [
            "  dynamic_criteria {",
            f"    metric_namespace  = {_dq(namespace)}",
            f"    metric_name       = {_dq(metric)}",
            f"    aggregation       = {_dq(aggregation)}",
            f"    operator          = {_dq(_operator(rec))}",
            f"    alert_sensitivity = {_dq(rec.get('alert_sensitivity') or 'Medium')}",
            f"    evaluation_total_count   = {failing.get('number_of_evaluation_periods') or 4}",
            f"    evaluation_failure_count = {failing.get('min_failing_periods_to_alert') or 4}",
            *dimension_lines,
            "  }",
        ]
    else:
        threshold = rec.get("threshold")
        criteria = [
            "  criteria {",
            f"    metric_namespace = {_dq(namespace)}",
            f"    metric_name      = {_dq(metric)}",
            f"    aggregation      = {_dq(aggregation)}",
            f"    operator         = {_dq(_operator(rec))}",
            f"    threshold        = {threshold if threshold is not None else 0}",
            *dimension_lines,
            "  }",
        ]

    return "\n".join(
        [
            f'resource "azurerm_monitor_metric_alert" "{sym}" {{',
            f"  name                = {_dq(_rule_name(gap))}",
            f"  resource_group_name = {_dq(gap.get('resource_group', ''))}",
            f"  scopes              = [{_dq(gap.get('resource_id', ''))}]",
            f"  description         = {_dq(_description(gap))}",
            f"  severity            = {_severity(gap)}",
            f"  frequency           = {_dq(frequency)}",
            f"  window_size         = {_dq(window)}",
            *criteria,
            "  # TODO: wire an action group before applying.",
            "  # action {",
            '  #   action_group_id = var.action_group_id',
            "  # }",
            "}",
        ]
    )


def _tf_log(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended", {})
    sym = _gap_symbol(gap)
    window = rec.get("window_size") or "PT15M"
    frequency = rec.get("evaluation_frequency") or window
    threshold = rec.get("threshold")
    query = str(rec.get("log_query") or "").rstrip()
    indented = "\n".join(f"    {line}" for line in query.splitlines()) or "    // query unavailable"
    return "\n".join(
        [
            f'resource "azurerm_monitor_scheduled_query_rules_alert_v2" "{sym}" {{',
            f"  name                 = {_dq(_rule_name(gap))}",
            f"  resource_group_name  = {_dq(gap.get('resource_group', ''))}",
            "  location             = var.location",
            f"  scopes               = [{_dq(gap.get('resource_id', ''))}]",
            f"  description          = {_dq(_description(gap))}",
            f"  severity             = {_severity(gap)}",
            f"  evaluation_frequency = {_dq(frequency)}",
            f"  window_duration      = {_dq(window)}",
            "  criteria {",
            "    query = <<-KQL",
            indented,
            "    KQL",
            f"    operator                = {_dq(_operator(rec))}",
            f"    threshold               = {threshold if threshold is not None else 0}",
            '    time_aggregation_method = "Average"',
            '    metric_measure_column   = "AggregatedValue"',
            '    resource_id_column      = "_ResourceId"',
            "    failing_periods {",
            "      minimum_failing_periods_to_trigger_alert = 1",
            "      number_of_evaluation_periods             = 1",
            "    }",
            "  }",
            "  # TODO: wire an action group before applying.",
            "  # action { action_groups = [var.action_group_id] }",
            "}",
        ]
    )


def _tf_activity(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended", {})
    sym = _gap_symbol(gap)
    activity = rec.get("activity_log") or {}
    scope = gap.get("subscription_id") or ""
    scope_id = f"/subscriptions/{scope}" if scope else gap.get("resource_id", "")

    criteria = [f"    category = {_dq(activity.get('category', 'ServiceHealth'))}"]
    if activity.get("operationName"):
        criteria.append(f"    operation_name = {_dq(activity['operationName'])}")
    if activity.get("status"):
        statuses = activity["status"]
        first = statuses[0] if isinstance(statuses, list) and statuses else str(statuses)
        criteria.append(f"    status = {_dq(first)}")
    if activity.get("incidentType"):
        criteria += [
            "    service_health {",
            f"      events = [{_dq(activity['incidentType'])}]",
            "    }",
        ]

    return "\n".join(
        [
            f'resource "azurerm_monitor_activity_log_alert" "{sym}" {{',
            f"  name                = {_dq(_rule_name(gap))}",
            f"  resource_group_name = {_dq(gap.get('resource_group') or 'rg-monitoring')}",
            "  location            = \"global\"",
            f"  scopes              = [{_dq(scope_id)}]",
            f"  description         = {_dq(_description(gap))}",
            "  criteria {",
            *criteria,
            "  }",
            "  # TODO: wire an action group before applying.",
            "  # action { action_group_id = var.action_group_id }",
            "}",
        ]
    )


def _tf_block(gap: dict[str, Any]) -> str:
    kind = _alert_kind(gap)
    if kind == "log":
        return _tf_log(gap)
    if kind == "activitylog":
        return _tf_activity(gap)
    return _tf_metric(gap)


# --------------------------------------------------------------------------- Azure Policy
_AMBA_POLICY_DOCS = "https://azure.github.io/azure-monitor-baseline-alerts/patterns/alz/"


def _policy_document(gaps: list[dict[str, Any]]) -> str:
    """A management-group Policy assignment plan — how AMBA-ALZ actually deploys at scale.

    Per-resource templates do not survive new resources being created; the upstream pattern
    assigns policy initiatives at a management group so every current *and future* resource
    is remediated. This emits the assignment skeleton plus the exact set of AMBA policy
    definitions implicated by the detected gaps."""
    policies: dict[str, dict[str, Any]] = {}
    unmapped: list[str] = []
    for gap in gaps:
        rec = gap.get("recommended") or {}
        deployments = rec.get("deployments") or gap.get("deployments") or []
        alert_name = rec.get("policy_alert_name") or gap.get("policy_alert_name") or ""
        template = ""
        for dep in deployments:
            if isinstance(dep, dict) and dep.get("template"):
                template = str(dep["template"])
                break
        if not template and not alert_name:
            unmapped.append(f"{gap.get('resource_type', '?')} / {gap.get('alert_name', '?')}")
            continue
        key = template or alert_name
        entry = policies.setdefault(
            key,
            {
                "template": template,
                "alertName": alert_name,
                "resourceType": gap.get("resource_type", ""),
                "alert": gap.get("alert_name", ""),
                "affectedResources": 0,
                "thresholdOverrideTag": rec.get("threshold_override_tag", ""),
            },
        )
        entry["affectedResources"] += 1

    plan = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "metadata": {
            "generatedBy": "aznetagent — AMBA coverage",
            "readOnly": "Review before deploying. This app never applies changes.",
            "guidance": _AMBA_POLICY_DOCS,
            "note": (
                "Assign the AMBA-ALZ policy initiatives at a management group so future "
                "resources are covered automatically, then run the remediation tasks."
            ),
        },
        "parameters": {
            "managementGroupId": {"type": "string", "metadata": {"description": "Target management group."}},
            "actionGroupResourceId": {"type": "string", "metadata": {"description": "Notification action group."}},
            "userAssignedManagedIdentityResourceId": {
                "type": "string",
                "defaultValue": "",
                "metadata": {"description": "Optional BYO identity for policy remediation."},
            },
        },
        "ambaPolicies": sorted(policies.values(), key=lambda p: (-p["affectedResources"], p["alert"])),
        "thresholdOverrideTags": sorted(
            {p["thresholdOverrideTag"] for p in policies.values() if p["thresholdOverrideTag"]}
        ),
        "unmappedGaps": unmapped[:50],
    }
    header = [
        "// Azure Policy deployment plan generated from AMBA coverage gaps.",
        "// Read-only artifact; this app does not apply changes.",
        f"// Deployment guidance: {_AMBA_POLICY_DOCS}",
        "//",
        "// 1. Assign the AMBA-ALZ initiative(s) below at your management group.",
        "// 2. Create the remediation tasks so existing resources are brought into compliance.",
        "// 3. Use the listed threshold-override tags to tune individual resources.",
        "",
    ]
    return "\n".join(header) + json.dumps(plan, indent=2)


# --------------------------------------------------------------------------- entry point
def generate_iac(gaps: list[dict[str, Any]], fmt: str) -> str:
    """Return a single IaC document covering every gap."""
    fmt = (fmt or "bicep").lower()
    if fmt == "policy":
        return _policy_document(gaps)

    blocks: list[str] = []
    skipped: list[str] = []
    for gap in gaps:
        rec = gap.get("recommended") or {}
        kind = _alert_kind(gap)
        if kind == "metric" and not rec.get("metric"):
            skipped.append(f"{gap.get('resource_name','?')} / {gap.get('alert_name','?')}")
            continue
        if kind == "log" and not rec.get("log_query"):
            skipped.append(f"{gap.get('resource_name','?')} / {gap.get('alert_name','?')}")
            continue
        if kind == "activitylog" and not (rec.get("activity_log") or {}).get("category"):
            skipped.append(f"{gap.get('resource_name','?')} / {gap.get('alert_name','?')}")
            continue
        blocks.append(_bicep_block(gap) if fmt == "bicep" else _tf_block(gap))

    if fmt == "terraform":
        header = [
            "# Terraform generated from AMBA coverage gaps — review & wire action groups before apply.",
            "# Read-only artifact; this app does not apply changes.",
            "terraform {",
            '  required_providers { azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" } }',
            "}",
            'provider "azurerm" { features {} }',
            'variable "location" { type = string, default = "westeurope" }',
            'variable "action_group_id" { type = string, default = "" }',
            "",
        ]
    else:
        header = [
            "// Bicep generated from AMBA coverage gaps — review & wire action groups before deploy.",
            "// Read-only artifact; this app does not apply changes.",
            "",
            "@description('Action group to notify. Wire this into each rule before deploying.')",
            "param actionGroupId string = ''",
            "",
        ]
    if skipped:
        header.append(
            ("# " if fmt == "terraform" else "// ")
            + f"Skipped {len(skipped)} alert(s) with insufficient baseline detail: "
            + "; ".join(skipped[:10])
            + ("…" if len(skipped) > 10 else "")
        )
        header.append("")
    body = "\n\n".join(blocks)
    return "\n".join(header) + ("\n" + body if body else "")

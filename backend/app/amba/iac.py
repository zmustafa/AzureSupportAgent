"""Generate Bicep / Terraform for AMBA alert-coverage gaps.

Unlike the frontend architecture skeleton exporters, these emit real, parameterized
``Microsoft.Insights/metricAlerts`` (Bicep) and ``azurerm_monitor_metric_alert``
(Terraform) blocks from the recommended-alert spec + the target resource. Output is
read-only artifact text for download/review — it is NEVER applied by the app."""
from __future__ import annotations

import re
from typing import Any

_OP_BICEP = {
    "GreaterThan": "GreaterThan",
    "LessThan": "LessThan",
    "GreaterOrLessThan": "GreaterOrLessThan",
    "Equals": "Equals",
}
_OP_TF = {
    "GreaterThan": "GreaterThan",
    "LessThan": "LessThan",
    "GreaterOrLessThan": "GreaterOrLessThan",
    "Equals": "Equals",
}


def _ident(s: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9]+", "_", s or "").strip("_")
    if not out:
        out = "alert"
    if out[0].isdigit():
        out = f"a_{out}"
    return out


def _namespace_for(resource_type: str) -> str:
    """Metric namespace = the resource provider/type (ARM expects the full type)."""
    return resource_type


def _gap_symbol(gap: dict[str, Any]) -> str:
    return _ident(f"{gap.get('resource_name','res')}_{gap.get('alert_key','alert')}")


def _bicep_block(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended", {})
    sym = _gap_symbol(gap)
    metric = rec.get("metric", "")
    op = _OP_BICEP.get(rec.get("operator", "GreaterThan"), "GreaterThan")
    threshold = rec.get("threshold")
    threshold = threshold if threshold is not None else 0
    window = rec.get("window", "PT5M")
    sev = {"critical": 0, "error": 1, "warning": 2, "info": 3}.get(gap.get("severity", "warning"), 2)
    name = f"{gap.get('resource_name','res')}-{gap.get('alert_key','alert')}"[:128]
    rid = gap.get("resource_id", "")
    ns = _namespace_for(gap.get("resource_type", ""))
    return "\n".join(
        [
            f"resource {sym} 'Microsoft.Insights/metricAlerts@2018-03-01' = {{",
            f"  name: '{name}'",
            "  location: 'global'",
            "  properties: {",
            f"    description: '{(gap.get('why','') or rec.get('metric','')).replace(chr(39), '')[:200]}'",
            f"    severity: {sev}",
            "    enabled: true",
            "    scopes: [",
            f"      '{rid}'",
            "    ]",
            f"    evaluationFrequency: '{window}'",
            f"    windowSize: '{window}'",
            f"    targetResourceType: '{ns}'",
            "    criteria: {",
            "      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'",
            "      allOf: [",
            "        {",
            f"          name: '{_ident(metric) or 'criterion'}'",
            f"          metricName: '{metric}'",
            f"          operator: '{op}'",
            f"          threshold: {threshold}",
            "          timeAggregation: 'Average'",
            "          criterionType: 'StaticThresholdCriterion'",
            "        }",
            "      ]",
            "    }",
            "    // TODO: wire an action group below before deploying.",
            "    actions: [",
            "      // { actionGroupId: '<action-group-resource-id>' }",
            "    ]",
            "  }",
            "}",
        ]
    )


def _tf_block(gap: dict[str, Any]) -> str:
    rec = gap.get("recommended", {})
    sym = _gap_symbol(gap)
    metric = rec.get("metric", "")
    op = _OP_TF.get(rec.get("operator", "GreaterThan"), "GreaterThan")
    threshold = rec.get("threshold")
    threshold = threshold if threshold is not None else 0
    window = rec.get("window", "PT5M")
    sev = {"critical": 0, "error": 1, "warning": 2, "info": 3}.get(gap.get("severity", "warning"), 2)
    name = f"{gap.get('resource_name','res')}-{gap.get('alert_key','alert')}"[:128]
    rid = gap.get("resource_id", "")
    ns = _namespace_for(gap.get("resource_type", ""))
    rg = gap.get("resource_group", "")
    return "\n".join(
        [
            f'resource "azurerm_monitor_metric_alert" "{sym}" {{',
            f'  name                = "{name}"',
            f'  resource_group_name = "{rg}"',
            f'  scopes              = ["{rid}"]',
            f'  description         = "{(gap.get("why","") or metric).replace(chr(34), "")[:200]}"',
            f"  severity            = {sev}",
            f'  frequency           = "{window}"',
            f'  window_size         = "{window}"',
            f'  target_resource_type = "{ns}"',
            "  criteria {",
            f'    metric_namespace = "{ns}"',
            f'    metric_name      = "{metric}"',
            '    aggregation      = "Average"',
            f'    operator         = "{op}"',
            f"    threshold        = {threshold}",
            "  }",
            "  # TODO: wire an action group before applying.",
            "  # action {",
            '  #   action_group_id = "<action-group-resource-id>"',
            "  # }",
            "}",
        ]
    )


def generate_iac(gaps: list[dict[str, Any]], fmt: str) -> str:
    """Return a single IaC document covering every gap (skips non-metric/log gaps with a note)."""
    fmt = (fmt or "bicep").lower()
    blocks: list[str] = []
    skipped: list[str] = []
    for g in gaps:
        rec = g.get("recommended", {})
        if not rec.get("metric"):
            skipped.append(f"{g.get('resource_name','?')} / {g.get('alert_name','?')}")
            continue
        blocks.append(_bicep_block(g) if fmt == "bicep" else _tf_block(g))

    if fmt == "terraform":
        header = [
            "# Terraform generated from AMBA coverage gaps — review & wire action groups before apply.",
            "# Read-only artifact; this app does not apply changes.",
            'terraform {',
            '  required_providers { azurerm = { source = "hashicorp/azurerm", version = "~> 3.0" } }',
            "}",
            'provider "azurerm" { features {} }',
            "",
        ]
    else:
        header = [
            "// Bicep generated from AMBA coverage gaps — review & wire action groups before deploy.",
            "// Read-only artifact; this app does not apply changes.",
            "",
        ]
    if skipped:
        header.append(
            ("# " if fmt == "terraform" else "// ")
            + f"Skipped {len(skipped)} log/query-based alert(s) requiring manual scheduledQueryRules: "
            + "; ".join(skipped[:10])
            + ("…" if len(skipped) > 10 else "")
        )
        header.append("")
    body = "\n\n".join(blocks)
    return "\n".join(header) + ("\n" + body if body else "")

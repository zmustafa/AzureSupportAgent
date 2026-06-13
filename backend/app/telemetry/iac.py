"""Generate Bicep / Azure Policy remediation artifacts for telemetry-coverage gaps.

Two formats, both read-only download artifacts (never applied by the app):
    bicep   — a ``Microsoft.Insights/diagnosticSettings`` resource per gap, enabling the
              recommended categories and pointing at the approved workspace.
    policy  — an Azure Policy assignment skeleton (DeployIfNotExists) that enforces
              diagnostic settings for the resource type, parameterized with the workspace.
"""
from __future__ import annotations

import json
import re
from typing import Any


def _ident(s: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9]+", "_", s or "").strip("_")
    if not out:
        out = "res"
    if out[0].isdigit():
        out = f"r_{out}"
    return out


def _recommended_log_keys(gap: dict[str, Any]) -> list[str]:
    """Log categories to enable for this gap (missing set, else recommended set)."""
    cats = gap.get("missing_categories") or gap.get("recommended_categories") or []
    return [c for c in cats if c and c != "AllMetrics"]


def _bicep_block(gap: dict[str, Any], workspace_id: str) -> str:
    sym = _ident(f"{gap.get('resource_name','res')}_diag")
    rid = gap.get("resource_id", "")
    logs = _recommended_log_keys(gap)
    want_metrics = "AllMetrics" in (gap.get("missing_categories") or gap.get("recommended_categories") or [])
    lines = [
        f"resource {sym} 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {{",
        f"  name: '{(gap.get('resource_name','res'))[:60]}-diag'",
        f"  scope: <existing resource: {rid}>  // TODO: replace with a symbolic reference or existing() ref",
        "  properties: {",
        f"    workspaceId: '{workspace_id or '<approved-log-analytics-workspace-resource-id>'}'",
        "    logs: [",
    ]
    for cat in logs:
        lines += [
            "      {",
            f"        category: '{cat}'",
            "        enabled: true",
            "      }",
        ]
    lines += [
        "    ]",
        "    metrics: [",
    ]
    if want_metrics or not logs:
        lines += [
            "      {",
            "        category: 'AllMetrics'",
            "        enabled: true",
            "      }",
        ]
    lines += [
        "    ]",
        "  }",
        "}",
    ]
    return "\n".join(lines)


def _policy_assignment(resource_type: str, log_keys: list[str], workspace_id: str) -> str:
    """An Azure Policy assignment skeleton enforcing diagnostic settings for a type."""
    assignment = {
        "type": "Microsoft.Authorization/policyAssignments",
        "apiVersion": "2022-06-01",
        "name": f"enforce-diag-{_ident(resource_type)}"[:64],
        "properties": {
            "displayName": f"Deploy diagnostic settings for {resource_type}",
            "description": (
                "DeployIfNotExists assignment that ensures diagnostic settings shipping the "
                "recommended categories to the approved Log Analytics workspace exist for all "
                f"{resource_type} resources."
            ),
            "policyDefinitionId": "<built-in or custom DINE policy definition resource id>",
            "parameters": {
                "logAnalytics": {"value": workspace_id or "<approved-workspace-resource-id>"},
                "recommendedCategories": {"value": log_keys},
            },
            "identity_note": "DeployIfNotExists assignments require a managed identity + role assignment.",
        },
    }
    return json.dumps(assignment, indent=2)


def generate_iac(gaps: list[dict[str, Any]], fmt: str, *, workspace_id: str = "") -> str:
    fmt = (fmt or "bicep").lower()

    if fmt == "policy":
        # One assignment per distinct resource type across the gaps.
        by_type: dict[str, list[str]] = {}
        for g in gaps:
            t = g.get("resource_type", "")
            keys = [c for c in (g.get("missing_categories") or []) if c and c != "AllMetrics"]
            cur = set(by_type.get(t, []))
            cur |= set(keys)
            by_type[t] = sorted(cur)
        header = [
            "// Azure Policy assignments generated from telemetry-coverage gaps.",
            "// Read-only artifacts; review, attach a DINE policy definition + managed identity, then deploy via your pipeline.",
            "",
        ]
        blocks = [_policy_assignment(t, keys, workspace_id) for t, keys in sorted(by_type.items())]
        body = "\n\n".join(blocks)
        return "\n".join(header) + ("\n" + body if body else "")

    header = [
        "// Bicep generated from telemetry-coverage gaps — review the scope refs + workspace, then deploy.",
        "// Read-only artifact; this app does not apply changes.",
        "",
    ]
    blocks = [_bicep_block(g, workspace_id) for g in gaps]
    body = "\n\n".join(blocks)
    return "\n".join(header) + ("\n" + body if body else "")

"""Generate a valid but deliberately no-op Bicep review artifact for remediation plans.

The artifact records existing rule references and commented operator commands. Deploying it
changes nothing; approval is human sign-off for exporting the plan to a controlled pipeline.
"""
from __future__ import annotations

import re
from typing import Any


def _symbol(name: str, index: int) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or f"rule_{index}"
    if value[0].isdigit():
        value = "r_" + value
    return f"existing_{value[:45]}_{index}"


def _resource_parts(resource_id: str) -> tuple[str, str, str]:
    match = re.search(r"/providers/([^/]+/[^/]+(?:/[^/]+)?)/(.+)$", resource_id, re.I)
    if not match:
        return "", "", ""
    resource_type = match.group(1)
    names = match.group(2).split("/")
    name = "/".join(names)
    api = (
        "2018-03-01" if "metricalerts" in resource_type.lower()
        else "2021-08-01" if "scheduledqueryrules" in resource_type.lower()
        else "2020-10-01" if "activitylogalerts" in resource_type.lower()
        else "2021-04-01"
    )
    return resource_type, name, api


def generate_review_artifact(snapshot: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    rules = {rule["id"]: rule for rule in snapshot.get("rules", [])}
    actions: list[dict[str, Any]] = []
    lines = [
        "// Alerts Manager remediation review artifact",
        "// SAFE BY DESIGN: every resource below is declared `existing`; deploying this file changes nothing.",
        "// Approval records human sign-off only. Copy reviewed actions into your controlled pipeline.",
        "targetScope = 'resourceGroup'",
        "",
    ]
    seen: set[str] = set()
    for overlap in snapshot.get("active_overlaps", snapshot.get("overlaps", [])):
        ids = [rule_id for rule_id in overlap.get("rule_ids", []) if rule_id in rules]
        if not ids:
            continue
        keep = ids[0]
        for duplicate in ids[1:]:
            actions.append(
                {
                    "action": "consolidate_rule",
                    "keep_rule_id": keep,
                    "candidate_rule_id": duplicate,
                    "overlap_id": overlap.get("id", ""),
                    "reason": overlap.get("explanation", ""),
                }
            )
    for gap in snapshot.get("active_gaps", snapshot.get("gaps", [])):
        actions.append(
            {
                "action": "review_gap",
                "gap_type": gap.get("type", ""),
                "resource_id": gap.get("resource_id", ""),
                "rule_id": gap.get("rule_id", ""),
                "recommendation": gap.get("recommendation", ""),
            }
        )
    for index, rule in enumerate(rules.values(), start=1):
        rid = str(rule.get("id", ""))
        if not rid or rid in seen:
            continue
        seen.add(rid)
        resource_type, name, api = _resource_parts(rid)
        if not resource_type:
            continue
        escaped_name = name.replace("'", "''")
        lines += [
            f"resource {_symbol(rule.get('name','rule'), index)} '{resource_type}@{api}' existing = {{",
            f"  name: '{escaped_name}'",
            "}",
            "",
        ]
    lines += [
        "// REVIEW QUEUE (comments only — never executed by this application)",
    ]
    for index, action in enumerate(actions, start=1):
        lines.append(f"// {index}. {action}")
    return "\n".join(lines) + "\n", actions

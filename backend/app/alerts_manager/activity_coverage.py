"""Essential Azure Activity Log alert coverage evaluation.

Coverage is intentionally based on the existing cached rule and Action Group inventories.  An
Activity Log alert counts as covered only when it is enabled, subscription-scoped, and routes to
an enabled Action Group with at least one active receiver.
"""
from __future__ import annotations

import json
from typing import Any

ESSENTIAL_CATEGORIES = ("ServiceHealth", "ResourceHealth", "Security", "Recommendation")
RECOGNIZED_CATEGORIES = ("Administrative", *ESSENTIAL_CATEGORIES)

_SIEM_GUIDANCE = (
    "Activity Log alerts notify through Action Groups; they do not send Activity Log records to "
    "a Log Analytics workspace or SIEM. Configure a diagnostic setting or the SIEM connector "
    "separately when log ingestion and investigation are required."
)


def normalize_category(value: str) -> str:
    wanted = str(value or "").replace("_", "").replace("-", "").lower()
    return next(
        (category for category in RECOGNIZED_CATEGORIES if category.lower() == wanted),
        "",
    )


def subscription_scope(subscription_id: str) -> str:
    return f"/subscriptions/{str(subscription_id).strip()}"


def _exact_subscription_scope(rule: dict[str, Any], scope_id: str) -> bool:
    wanted = scope_id.lower().rstrip("/")
    return any(str(value).lower().rstrip("/") == wanted for value in rule.get("scopes") or [])


def _routing(rule: dict[str, Any], action_groups: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ids = [str(value) for value in rule.get("action_group_ids") or [] if str(value)]
    found = [action_groups[value.lower().rstrip("/")] for value in ids if value.lower().rstrip("/") in action_groups]
    healthy = [
        group for group in found
        if bool(group.get("enabled", True)) and int(group.get("active_receiver_count") or 0) > 0
    ]
    return {
        "action_group_ids": ids,
        "configured_count": len(ids),
        "existing_count": len(found),
        "healthy_count": len(healthy),
        "missing_action_group_ids": [
            value for value in ids if value.lower().rstrip("/") not in action_groups
        ],
        "action_groups": [
            {
                "id": str(group.get("id") or ""),
                "name": str(group.get("name") or ""),
                "enabled": bool(group.get("enabled", True)),
                "active_receiver_count": int(group.get("active_receiver_count") or 0),
            }
            for group in found
        ],
        "active": bool(healthy),
    }


def _condition_values(condition: dict[str, Any]) -> set[str]:
    if isinstance(condition.get("containsAny"), list):
        return {str(value).strip().lower() for value in condition["containsAny"] if str(value).strip()}
    value = str(condition.get("equals") or "").strip().lower()
    return {value} if value else set()


def condition_completeness(rule: dict[str, Any], category: str) -> dict[str, Any]:
    """Classify whether a rule covers the full essential-category event set.

    A missing non-category filter is intentionally broad and therefore complete. A narrower
    allowlisted filter is partial; an unrelated filter is treated as no coverage. Unknown or
    malformed filters fail closed as partial instead of being credited as complete.
    """
    from app.alerts_manager.activity_planner import _CONDITION_FIELDS, _DEFAULTS

    conditions = rule.get("activity_conditions") or []
    if not isinstance(conditions, list):
        return {"status": "partial", "issues": ["Activity Log conditions are not readable."], "missing_values": {}}
    category_conditions = [
        item for item in conditions if isinstance(item, dict) and str(item.get("field") or "").lower() == "category"
    ]
    if category_conditions and not any(category.lower() in _condition_values(item) for item in category_conditions):
        return {"status": "none", "issues": [f"The category condition does not include {category}."], "missing_values": {}}

    required = {
        str(item.get("field") or ""): _condition_values(item)
        for item in _DEFAULTS[category]["conditions"]
        if str(item.get("field") or "") != "category"
    }
    supplied = {
        str(item.get("field") or ""): _condition_values(item)
        for item in conditions if isinstance(item, dict) and str(item.get("field") or "") != "category"
    }
    issues: list[str] = []
    missing_values: dict[str, list[str]] = {}
    for field, values in supplied.items():
        if not values or field not in _CONDITION_FIELDS[category]:
            issues.append(f"Condition field '{field or 'unknown'}' cannot be proven complete.")
            continue
        expected = required.get(field)
        if expected is None:
            issues.append(f"Additional filter '{field}' narrows category coverage.")
        elif not values & expected:
            return {
                "status": "none",
                "issues": [f"Condition '{field}' excludes all required values."],
                "missing_values": {field: sorted(expected)},
            }
        elif not expected.issubset(values):
            missing_values[field] = sorted(expected - values)
            issues.append(f"Condition '{field}' omits required values: {', '.join(sorted(expected - values))}.")
    return {
        "status": "partial" if issues else "complete",
        "issues": issues,
        "missing_values": missing_values,
    }


def _overlap_details(
    matches: list[dict[str, Any]], routed: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    routes = {str(rule.get("id") or ""): route for rule, route in routed}
    details: list[dict[str, Any]] = []
    for index, left in enumerate(matches):
        for right in matches[index + 1:]:
            left_id, right_id = str(left.get("id") or ""), str(right.get("id") or "")
            left_conditions = json.dumps(left.get("activity_conditions") or [], sort_keys=True, separators=(",", ":"))
            right_conditions = json.dumps(right.get("activity_conditions") or [], sort_keys=True, separators=(",", ":"))
            left_groups = set(routes.get(left_id, {}).get("action_group_ids") or [])
            right_groups = set(routes.get(right_id, {}).get("action_group_ids") or [])
            same_conditions = left_conditions == right_conditions
            shared_groups = sorted(left_groups & right_groups)
            details.append({
                "rule_ids": [left_id, right_id],
                "rule_names": [str(left.get("name") or ""), str(right.get("name") or "")],
                "type": "exact_duplicate" if same_conditions and left_groups == right_groups else "notification_overlap",
                "same_conditions": same_conditions,
                "same_routing": left_groups == right_groups,
                "shared_action_group_ids": shared_groups,
                "notification_risk": "high" if same_conditions and (shared_groups or left_groups == right_groups) else "medium",
            })
    return details


def evaluate_coverage(
    subscription_ids: set[str],
    rules: list[dict[str, Any]],
    action_groups: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
    blockers: list[dict[str, Any]] | None = None,
    subscription_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return frontend-ready per-subscription and aggregate essential-category coverage."""
    source = dict(metadata or {})
    partial_source = bool(source.get("partial") or source.get("truncated"))
    groups_by_id = {
        str(group.get("id") or "").lower().rstrip("/"): group
        for group in action_groups if group.get("id")
    }
    activity_rules = [rule for rule in rules if str(rule.get("family") or "").lower() == "activity"]
    pending = list(blockers or [])
    names = {str(key).lower(): str(value) for key, value in (subscription_names or {}).items() if value}
    scopes: list[dict[str, Any]] = []
    status_counts = {status: 0 for status in ("covered", "partial", "disabled", "no_routing", "missing", "unknown")}

    for subscription_id in sorted(value for value in subscription_ids if value):
        scope_id = subscription_scope(subscription_id)
        categories: list[dict[str, Any]] = []
        for category in ESSENTIAL_CATEGORIES:
            matches = [
                rule for rule in activity_rules
                if normalize_category(str(rule.get("category") or "")) == category
                and _exact_subscription_scope(rule, scope_id)
            ]
            routed: list[tuple[dict[str, Any], dict[str, Any]]] = [
                (rule, _routing(rule, groups_by_id)) for rule in matches
            ]
            completeness = {str(rule.get("id") or ""): condition_completeness(rule, category) for rule in matches}
            complete_healthy_count = sum(
                1 for rule, route in routed
                if bool(rule.get("enabled")) and route["active"]
                and completeness[str(rule.get("id") or "")]["status"] == "complete"
            )
            partial_healthy_count = sum(
                1 for rule, route in routed
                if bool(rule.get("enabled")) and route["active"]
                and completeness[str(rule.get("id") or "")]["status"] == "partial"
            )
            healthy_count = complete_healthy_count + partial_healthy_count
            unhealthy_count = len(matches) - healthy_count
            category_blockers = [
                blocker for blocker in pending
                if str(blocker.get("subscription_id") or "").lower() == subscription_id.lower()
                and normalize_category(str(blocker.get("category") or "")) == category
            ]
            if complete_healthy_count and unhealthy_count:
                status = "partial"
            elif complete_healthy_count:
                status = "covered"
            elif partial_healthy_count:
                status = "partial"
            elif matches and not any(bool(rule.get("enabled")) for rule in matches):
                status = "disabled"
            elif matches:
                status = "no_routing"
            elif partial_source:
                status = "unknown"
            else:
                status = "missing"
            status_counts[status] += 1
            condition_groups: dict[str, list[dict[str, Any]]] = {}
            for rule in matches:
                signature = json.dumps(rule.get("activity_conditions") or [], sort_keys=True, separators=(",", ":"))
                condition_groups.setdefault(signature, []).append(rule)
            issues: list[dict[str, Any]] = []
            for rule in matches:
                detail = completeness[str(rule.get("id") or "")]
                if detail["status"] != "complete":
                    issues.append({
                        "type": "condition_partial" if detail["status"] == "partial" else "condition_mismatch",
                        "severity": "warning", "rule_id": str(rule.get("id") or ""),
                        "message": "; ".join(detail["issues"]), "missing_values": detail["missing_values"],
                    })
            overlap_details = _overlap_details(matches, routed)
            if len(matches) > 1:
                duplicate = any(item["type"] == "exact_duplicate" for item in overlap_details)
                issues.append({
                    "type": "duplicate" if duplicate else "overlap",
                    "severity": "warning",
                    "message": f"{len(matches)} rules cover {category}; duplicate notifications may be generated.",
                    "rule_ids": [str(rule.get("id") or "") for rule in matches],
                    "overlaps": overlap_details,
                })
            if category_blockers:
                operations = {str(blocker.get("operation") or "update") for blocker in category_blockers}
                issues.append({
                    "type": "pending_change",
                    "pending_effect": "deletion" if "delete" in operations else "replacement",
                    "severity": "warning" if "delete" in operations else "info",
                    "message": "An approved or pending deletion may remove this coverage." if "delete" in operations else "A pending or approved replacement may change this coverage.",
                    "change_ids": [str(blocker.get("change_id") or "") for blocker in category_blockers],
                })
            pending_effect = "deletion" if any(str(item.get("operation") or "") == "delete" for item in category_blockers) else "replacement" if category_blockers else "none"
            categories.append({
                "category": category,
                "status": status,
                "rule_count": len(matches),
                "enabled_rule_count": sum(1 for rule in matches if rule.get("enabled")),
                "rules": [
                    {
                        "id": str(rule.get("id") or ""),
                        "name": str(rule.get("name") or ""),
                        "enabled": bool(rule.get("enabled")),
                        "routing": route,
                        "condition_completeness": completeness[str(rule.get("id") or "")],
                    }
                    for rule, route in routed
                ],
                "healthy_rule_count": healthy_count,
                "complete_healthy_rule_count": complete_healthy_count,
                "partial_healthy_rule_count": partial_healthy_count,
                "condition_complete": bool(complete_healthy_count),
                "overlap_details": overlap_details,
                "issues": issues,
                "pending_changes": category_blockers,
                "pending_effect": pending_effect,
                "projected_status": "missing" if pending_effect == "deletion" and len(category_blockers) >= len(matches) else "covered" if pending_effect == "replacement" and not matches else status,
                "blocked": bool(category_blockers),
                "remediation_required": status != "covered",
                "recommended_action": "Review and resolve the pending deletion before remediation." if pending_effect == "deletion" else "Review overlapping rules and consolidate routing." if overlap_details else "Create a complete enabled rule with healthy routing." if status == "missing" else "Complete the condition filters and verify routing." if status == "partial" else "Enable the rule and verify routing." if status in {"disabled", "no_routing"} else "No action required.",
            })
        scopes.append({
            "scope_id": scope_id,
            "scope_type": "subscription",
            "subscription_id": subscription_id,
            "subscription_display_name": names.get(subscription_id.lower(), subscription_id),
            "categories": categories,
            "counts": {
                status: sum(1 for item in categories if item["status"] == status)
                for status in status_counts
            },
            "covered": all(item["status"] == "covered" for item in categories),
        })

    category_summary: list[dict[str, Any]] = []
    for category in ESSENTIAL_CATEGORIES:
        values = [
            item for scope in scopes for item in scope["categories"] if item["category"] == category
        ]
        statuses = {item["status"] for item in values}
        if not values or statuses == {"unknown"}:
            status = "unknown"
        elif statuses == {"covered"}:
            status = "covered"
        elif "covered" in statuses:
            status = "partial"
        elif len(statuses) == 1:
            status = next(iter(statuses))
        else:
            status = "partial"
        category_summary.append({
            "category": category,
            "status": status,
            "covered_subscriptions": sum(1 for item in values if item["status"] == "covered"),
            "subscription_count": len(values),
        })

    total = len(scopes) * len(ESSENTIAL_CATEGORIES)
    covered = status_counts["covered"]
    partial_categories = sum(1 for item in category_summary if item["status"] == "partial")
    return {
        "essential_categories": list(ESSENTIAL_CATEGORIES),
        "recognized_optional_categories": ["Administrative"],
        "scopes": scopes,
        "categories": category_summary,
        "counts": {
            **status_counts, "partial_categories": partial_categories,
            "total": total, "covered": covered, "gaps": total - covered,
        },
        "coverage_percent": round(covered * 100 / total) if total else 0,
        "complete": bool(scopes) and not partial_source,
        "partial": partial_source,
        "metadata": source,
        "pending_change_count": len(pending),
        "security_guidance": _SIEM_GUIDANCE,
    }

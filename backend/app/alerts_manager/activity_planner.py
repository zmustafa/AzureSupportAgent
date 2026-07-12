"""Deterministic, stateless planner for essential Activity Log alert rules."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.alerts_manager import rules
from app.alerts_manager.activity_coverage import ESSENTIAL_CATEGORIES, normalize_category, subscription_scope

_DEFAULTS: dict[str, dict[str, Any]] = {
    "ServiceHealth": {
        "slug": "service-health",
        "label": "Service Health",
        "description": "Notify responders about Azure Service Health incidents for this subscription.",
        "conditions": [
            {"field": "category", "equals": "ServiceHealth"},
            {"field": "properties.incidentType", "containsAny": ["Incident", "Maintenance", "Security", "ActionRequired"]},
        ],
    },
    "ResourceHealth": {
        "slug": "resource-health",
        "label": "Resource Health",
        "description": "Notify responders when Azure reports an unhealthy resource state.",
        "conditions": [
            {"field": "category", "equals": "ResourceHealth"},
            {"field": "properties.currentHealthStatus", "containsAny": ["Degraded", "Unavailable", "Unknown"]},
        ],
    },
    "Security": {
        "slug": "security",
        "label": "Security",
        "description": "Notify responders about subscription Activity Log Security events.",
        "conditions": [{"field": "category", "equals": "Security"}],
    },
    "Recommendation": {
        "slug": "recommendation",
        "label": "Recommendation",
        "description": "Notify responders about subscription Activity Log Recommendation events.",
        "conditions": [{"field": "category", "equals": "Recommendation"}],
    },
}

_CONDITION_FIELDS: dict[str, set[str]] = {
    "ServiceHealth": {"properties.incidentType", "properties.service", "properties.region"},
    "ResourceHealth": {
        "properties.currentHealthStatus", "properties.previousHealthStatus", "properties.cause",
    },
    "Security": {"level", "operationName", "resourceType", "resourceGroup"},
    "Recommendation": {"level", "operationName", "resourceType", "resourceGroup"},
}
_CONDITION_OPERATORS = {"equals", "containsAny"}


def _clean_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.()-]+", "-", value).strip("-. ")
    return cleaned[:180] or "essential-activity"


def target_id(desired: dict[str, Any]) -> str:
    return (
        f"/subscriptions/{desired['subscription_id']}/resourceGroups/{desired['resource_group']}"
        f"/providers/microsoft.insights/activityLogAlerts/{desired['name']}"
    )


def equivalent(rule: dict[str, Any], category: str, subscription_id: str) -> bool:
    wanted_scope = subscription_scope(subscription_id).lower().rstrip("/")
    return (
        str(rule.get("family") or "").lower() == "activity"
        and normalize_category(str(rule.get("category") or "")) == category
        and any(str(scope).lower().rstrip("/") == wanted_scope for scope in rule.get("scopes") or [])
    )


def _conditions(category: str, supplied: Any) -> list[dict[str, Any]]:
    """Validate and copy the deliberately small condition language accepted by the wizard."""
    raw = supplied if supplied is not None else _DEFAULTS[category]["conditions"]
    if not isinstance(raw, list) or not raw or len(raw) > 12:
        raise ValueError(f"{category} conditions must contain between 1 and 12 entries.")
    allowed_fields = _CONDITION_FIELDS[category]
    result: list[dict[str, Any]] = []
    category_conditions = 0
    for index, condition in enumerate(raw, 1):
        if not isinstance(condition, dict):
            raise ValueError(f"{category} condition {index} must be an object.")
        field = str(condition.get("field") or "").strip()
        operators = [operator for operator in _CONDITION_OPERATORS if operator in condition]
        if len(operators) != 1 or set(condition) != {"field", operators[0]}:
            raise ValueError(f"{category} condition {index} must use exactly one allowed operator: equals or containsAny.")
        operator = operators[0]
        if field == "category":
            category_conditions += 1
            if operator != "equals" or condition.get(operator) != category:
                raise ValueError(f"{category} conditions must include category equals {category}.")
        elif field not in allowed_fields:
            raise ValueError(f"Field '{field}' is not allowed for {category} Activity Log alerts.")
        value = condition.get(operator)
        if operator == "equals":
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{category} condition {index} needs a non-empty equals value.")
            normalized: Any = value.strip()
        else:
            if not isinstance(value, list) or not value or len(value) > 50:
                raise ValueError(f"{category} condition {index} containsAny must have 1 to 50 values.")
            normalized = []
            for entry in value:
                if not isinstance(entry, str) or not entry.strip() or len(entry) > 256:
                    raise ValueError(f"{category} condition {index} containsAny values must be non-empty strings.")
                if entry.strip() not in normalized:
                    normalized.append(entry.strip())
        result.append({"field": field, operator: normalized})
    if category_conditions != 1:
        raise ValueError(f"{category} conditions must include exactly one mandatory category condition.")
    return result


def build_desired(
    *, subscription_id: str, category: str, resource_group: str,
    action_group_ids: list[str], name_prefix: str = "essential-activity",
    conditions: Any = None,
) -> dict[str, Any]:
    definition = _DEFAULTS[category]
    name = _clean_name(f"{name_prefix}-{definition['slug']}-{subscription_id[:8]}")
    return {
        "name": name,
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "location": "Global",
        "enabled": True,
        "description": definition["description"],
        "scopes": [subscription_scope(subscription_id)],
        "action_group_ids": list(dict.fromkeys(action_group_ids)),
        "activity_conditions": _conditions(category, conditions),
        "tags": {"aznetagent-managed": "essential-activity-log", "activity-category": category},
    }


def _subscription_from_id(resource_id: str) -> str:
    parts = str(resource_id or "").strip("/").split("/")
    return parts[1] if len(parts) >= 2 and parts[0].lower() == "subscriptions" else ""


def _routing_errors(
    action_group_ids: list[str], action_groups: list[dict[str, Any]], subscription_id: str,
) -> list[str]:
    by_id = {str(item.get("id") or "").lower().rstrip("/"): item for item in action_groups}
    errors: list[str] = []
    if not action_group_ids:
        return ["Select at least one Action Group."]
    for action_group_id in action_group_ids:
        group = by_id.get(action_group_id.lower().rstrip("/"))
        if not group:
            errors.append(f"Action Group does not exist in the selected management scope: {action_group_id}")
        elif (_subscription_from_id(str(group.get("id") or action_group_id)) or str(group.get("subscription_id") or "")).lower() != subscription_id.lower():
            errors.append(f"Action Group must be in subscription {subscription_id}: {group.get('name') or action_group_id}")
        elif not bool(group.get("enabled", True)):
            errors.append(f"Action Group is disabled: {group.get('name') or action_group_id}")
        elif int(group.get("active_receiver_count") or 0) < 1:
            errors.append(f"Action Group has no active receivers: {group.get('name') or action_group_id}")
    return errors


def plan_fingerprint(inputs: dict[str, Any]) -> str:
    text = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def preview_plan(
    request: dict[str, Any], *, subscription_ids: set[str], rules_inventory: list[dict[str, Any]],
    action_groups: list[dict[str, Any]], blockers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    categories = list(dict.fromkeys(normalize_category(value) for value in request.get("categories") or ESSENTIAL_CATEGORIES))
    invalid_categories = [value for value in categories if value not in ESSENTIAL_CATEGORIES]
    if invalid_categories or not categories:
        raise ValueError("Select one or more essential Activity Log categories.")
    requested_subscriptions = set(str(value) for value in request.get("subscription_ids") or [] if str(value))
    selected_subscriptions = requested_subscriptions or subscription_ids
    if not selected_subscriptions:
        raise ValueError("The selected management scope contains no subscriptions.")
    outside = requested_subscriptions - subscription_ids
    if outside:
        raise ValueError("One or more requested subscriptions are outside the selected management scope.")
    resource_group = str(request.get("resource_group") or "").strip()
    if not resource_group:
        raise ValueError("A monitoring resource group is required.")
    common = str(request.get("common_action_group_id") or "").strip()
    per_category = request.get("action_group_ids_by_category") or {}
    raw_conditions_by_category = request.get("conditions_by_category") or {}
    if not isinstance(raw_conditions_by_category, dict):
        raise ValueError("conditions_by_category must be an object keyed by essential category.")
    invalid_condition_categories = [key for key in raw_conditions_by_category if normalize_category(key) not in categories]
    if invalid_condition_categories:
        raise ValueError("Conditions may only be supplied for selected essential categories.")
    conditions_by_category = {
        normalize_category(key): value for key, value in raw_conditions_by_category.items()
    }
    routing_mode = str(request.get("routing_mode") or "common")
    if routing_mode not in {"common", "per_category"}:
        raise ValueError("Routing mode must be common or per_category.")
    blockers = blockers or {}
    items: list[dict[str, Any]] = []
    counts = {"create": 0, "update": 0, "enable": 0, "equivalent": 0, "blocked": 0, "invalid": 0}

    for subscription_id in sorted(selected_subscriptions):
        for category in categories:
            action_group_ids = [common] if routing_mode == "common" and common else [
                str(value) for value in per_category.get(category, []) if str(value)
            ]
            desired = build_desired(
                subscription_id=subscription_id, category=category, resource_group=resource_group,
                action_group_ids=action_group_ids, name_prefix=str(request.get("name_prefix") or "essential-activity"),
                conditions=conditions_by_category.get(category),
            )
            resource_id = target_id(desired)
            matches = [item for item in rules_inventory if equivalent(item, category, subscription_id)]
            enabled = [item for item in matches if item.get("enabled")]
            healthy_enabled = [
                item for item in enabled
                if set(str(value).lower().rstrip("/") for value in item.get("action_group_ids") or [])
                & set(str(value).lower().rstrip("/") for value in action_group_ids)
                and (
                    not item.get("activity_conditions")
                    or item.get("activity_conditions") == desired["activity_conditions"]
                )
            ]
            errors = _routing_errors(action_group_ids, action_groups, subscription_id)
            blocker = blockers.get(resource_id.lower().rstrip("/")) or next((
                blockers.get(str(match.get("id") or "").lower().rstrip("/"))
                for match in matches
                if blockers.get(str(match.get("id") or "").lower().rstrip("/"))
            ), None)
            if blocker:
                classification, actionable = "blocked", False
                errors.append(f"A {blocker['status']} managed change already targets this rule.")
            elif healthy_enabled:
                classification, actionable = "equivalent", False
            elif enabled and not errors:
                classification, actionable = "update", True
                desired["name"] = str(enabled[0].get("name") or desired["name"])
                resource_id = str(enabled[0].get("id") or resource_id)
            elif matches:
                classification, actionable = ("enable", True) if not errors else ("invalid", False)
                desired["name"] = str(matches[0].get("name") or desired["name"])
                resource_id = str(matches[0].get("id") or resource_id)
            elif errors:
                classification, actionable = "invalid", False
            else:
                classification, actionable = "create", True
            counts[classification] += 1
            item_operation = "create" if classification == "create" else "update" if classification in {"update", "enable"} else "none"
            body = rules.build_rule_body("activity", desired)
            validation_errors = rules.validate_rule_payload("activity", desired, create=classification == "create")
            if validation_errors and classification in {"create", "update", "enable"}:
                classification, actionable = "invalid", False
                counts["create" if item_operation == "create" else "enable" if item_operation == "enable" else "update"] -= 1
                counts["invalid"] += 1
            errors.extend(error for error in validation_errors if error not in errors)
            selected_group_rows = [
                group for group in action_groups
                if str(group.get("id") or "").lower().rstrip("/") in {
                    value.lower().rstrip("/") for value in action_group_ids
                }
            ]
            validation_status = "blocked" if classification == "blocked" else "invalid" if errors else "valid"
            issues = []
            overlap_details = []
            if len(matches) > 1:
                for index, left in enumerate(matches):
                    for right in matches[index + 1:]:
                        left_conditions = json.dumps(left.get("activity_conditions") or [], sort_keys=True, separators=(",", ":"))
                        right_conditions = json.dumps(right.get("activity_conditions") or [], sort_keys=True, separators=(",", ":"))
                        left_groups = {str(value).lower().rstrip("/") for value in left.get("action_group_ids") or []}
                        right_groups = {str(value).lower().rstrip("/") for value in right.get("action_group_ids") or []}
                        overlap_details.append({
                            "rule_ids": [str(left.get("id") or ""), str(right.get("id") or "")],
                            "type": "exact_duplicate" if left_conditions == right_conditions and left_groups == right_groups else "notification_overlap",
                            "same_conditions": left_conditions == right_conditions,
                            "same_routing": left_groups == right_groups,
                            "shared_action_group_ids": sorted(left_groups & right_groups),
                        })
                issues.append({
                    "type": "duplicate" if any(item["type"] == "exact_duplicate" for item in overlap_details) else "overlap",
                    "severity": "warning",
                    "message": f"{len(matches)} existing rules cover this subscription and category; review duplicate notifications.",
                    "rule_ids": [str(match.get("id") or "") for match in matches],
                    "overlaps": overlap_details,
                })
            existing_details = [
                {
                    "id": str(match.get("id") or ""), "name": str(match.get("name") or ""),
                    "enabled": bool(match.get("enabled")),
                    "action_group_ids": list(match.get("action_group_ids") or []),
                    "activity_conditions": list(match.get("activity_conditions") or []),
                    "reason": "same subscription and Activity Log category",
                }
                for match in matches
            ]
            reason = {
                "create": "No existing rule covers this subscription and category.",
                "update": "An enabled rule exists but does not use the selected healthy routing.",
                "enable": "An existing matching rule is disabled and will be enabled with the selected routing.",
                "equivalent": "An enabled existing rule already uses the selected routing.",
                "blocked": "A pending or approved managed change already targets this rule.",
                "invalid": "The proposed rule failed safe validation.",
            }[classification]
            items.append({
                "order": len(items) + 1,
                "key": f"{subscription_id}:{category}",
                "subscription_id": subscription_id,
                "scope_id": subscription_scope(subscription_id),
                "category": category,
                "category_label": _DEFAULTS[category]["label"],
                "classification": classification,
                "operation": item_operation if classification != "invalid" else "none",
                "actionable": actionable,
                "target_id": resource_id,
                "desired": desired,
                "body": body,
                "errors": errors,
                "validation_status": validation_status,
                "reason": reason,
                "risk": "low" if classification in {"enable", "equivalent"} else "medium",
                "cost": {"classification": "free", "estimated_monthly_cost": 0, "currency": "USD"},
                "receiver_count": sum(int(group.get("active_receiver_count") or 0) for group in selected_group_rows),
                "selected_action_groups": [
                    {"id": str(group.get("id") or ""), "name": str(group.get("name") or ""),
                     "enabled": bool(group.get("enabled", True)),
                     "receiver_count": int(group.get("receiver_count") or 0),
                     "active_receiver_count": int(group.get("active_receiver_count") or 0)}
                    for group in selected_group_rows
                ],
                "ownership": {
                    "source": "action_group_tags",
                    "owners": sorted({
                        str((group.get("tags") or {}).get(key) or "").strip()
                        for group in selected_group_rows for key in ("owner", "Owner", "team", "Team", "serviceOwner")
                        if str((group.get("tags") or {}).get(key) or "").strip()
                    }),
                    "ai_used": False,
                },
                "noise": {
                    "existing_rule_count": len(matches), "overlap_pair_count": len(overlap_details),
                    "exact_duplicate_count": sum(1 for detail in overlap_details if detail["type"] == "exact_duplicate"),
                    "receiver_fanout": sum(int(group.get("active_receiver_count") or 0) for group in selected_group_rows),
                    "ai_used": False,
                },
                "issues": issues,
                "existing_rule_details": existing_details,
                "equivalent_rules": [
                    {"id": str(item.get("id") or ""), "name": str(item.get("name") or ""), "enabled": bool(item.get("enabled"))}
                    for item in matches
                ],
                "blocker": blocker or None,
            })
    token_inputs = {
        "connection_id": str(request.get("connection_id") or ""),
        "workload_id": request.get("workload_id"),
        "subscription_id": request.get("subscription_id"),
        "management_group_id": request.get("management_group_id"),
        "subscription_ids": sorted(selected_subscriptions),
        "categories": categories,
        "resource_group": resource_group,
        "routing_mode": routing_mode,
        "common_action_group_id": common,
        "action_group_ids_by_category": per_category,
        "conditions_by_category": conditions_by_category,
        "name_prefix": str(request.get("name_prefix") or "essential-activity"),
    }
    return {
        "plan_version": 1,
        "plan_token": plan_fingerprint(token_inputs),
        "inputs": token_inputs,
        "items": items,
        "counts": {**counts, "total": len(items), "actionable": sum(1 for item in items if item["actionable"])},
        "valid": all(not item["errors"] for item in items if item["actionable"]) and any(item["actionable"] for item in items),
        "warnings": [
            "Security Activity Log alerts notify responders only. Configure diagnostic settings or a SIEM connector separately for log ingestion."
        ] if "Security" in categories else [],
    }

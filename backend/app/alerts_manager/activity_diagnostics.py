"""Subscription Activity Log diagnostic-settings inventory and safe change planning."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from app.alerts_manager import cache as inventory_cache
from app.alerts_manager import service

API_VERSION = "2021-05-01-preview"
REQUIRED_CATEGORIES = ("Administrative", "Alert", "Policy", "Security")
_DESTINATION_FIELDS = {
    "workspace": "workspaceId",
    "storage": "storageAccountId",
    "event_hub": "eventHubAuthorizationRuleId",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9_.()-]{1,260}$")
_RESOURCE_PATTERNS = {
    "workspace": re.compile(r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.OperationalInsights/workspaces/[^/]+$", re.I),
    "storage": re.compile(r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Storage/storageAccounts/[^/]+$", re.I),
    "event_hub": re.compile(r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.EventHub/namespaces/[^/]+/authorizationRules/[^/]+$", re.I),
}


def subscription_id(value: str) -> str:
    match = re.match(r"^/subscriptions/([^/]+)", str(value or ""), re.I)
    return match.group(1) if match else ""


def collection_path(subscription: str) -> str:
    return f"/subscriptions/{subscription}/providers/Microsoft.Insights/diagnosticSettings"


def setting_path(subscription: str, name: str) -> str:
    return f"{collection_path(subscription)}/{name}"


def _properties(setting: dict[str, Any]) -> dict[str, Any]:
    value = setting.get("properties")
    return value if isinstance(value, dict) else {}


def enabled_categories(setting: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in _properties(setting).get("logs") or []:
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        if str(item.get("categoryGroup") or "").lower() == "alllogs":
            return set(REQUIRED_CATEGORIES)
        category = str(item.get("category") or "")
        match = next((value for value in REQUIRED_CATEGORIES if value.lower() == category.lower()), "")
        if match:
            result.add(match)
    return result


def destinations(setting: dict[str, Any]) -> list[dict[str, str]]:
    props = _properties(setting)
    result: list[dict[str, str]] = []
    for kind, field in _DESTINATION_FIELDS.items():
        resource_id = str(props.get(field) or "")
        if resource_id:
            row = {"kind": kind, "resource_id": resource_id}
            if kind == "event_hub":
                row["event_hub_name"] = str(props.get("eventHubName") or "")
            result.append(row)
    return result


def destination_key(value: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(value.get("kind") or "").lower(),
        str(value.get("resource_id") or "").lower().rstrip("/"),
        str(value.get("event_hub_name") or "").lower(),
    )


def validate_destination(value: dict[str, Any]) -> list[str]:
    kind = str(value.get("kind") or "")
    resource_id = str(value.get("resource_id") or "").strip()
    errors: list[str] = []
    if kind not in _DESTINATION_FIELDS:
        errors.append("Destination kind must be workspace, event_hub, or storage.")
        return errors
    if not _RESOURCE_PATTERNS[kind].fullmatch(resource_id):
        errors.append(f"A valid Azure {kind.replace('_', ' ')} resource ID is required.")
    event_hub_name = str(value.get("event_hub_name") or "").strip()
    if kind == "event_hub" and (not event_hub_name or len(event_hub_name) > 256):
        errors.append("An Event Hub name is required for an event_hub destination.")
    if kind != "event_hub" and event_hub_name:
        errors.append("event_hub_name is only valid for an event_hub destination.")
    return errors


def classify_subscription(subscription: str, settings: list[dict[str, Any]], *, error: str = "", partial: bool = False) -> dict[str, Any]:
    if error:
        return {
            "subscription_id": subscription, "status": "unknown", "complete": False,
            "error": service.safe_error(error), "settings": [],
            "categories": {category: "unknown" for category in REQUIRED_CATEGORIES},
            "destinations": {kind: "unknown" for kind in _DESTINATION_FIELDS},
        }
    normalized: list[dict[str, Any]] = []
    all_categories: set[str] = set()
    destination_kinds: set[str] = set()
    malformed = partial
    for raw in settings:
        if not isinstance(raw, dict) or not str(raw.get("name") or "") or not isinstance(raw.get("properties"), dict):
            malformed = True
            continue
        categories = enabled_categories(raw)
        setting_destinations = destinations(raw)
        all_categories.update(categories)
        destination_kinds.update(item["kind"] for item in setting_destinations)
        normalized.append({
            "id": str(raw.get("id") or setting_path(subscription, str(raw.get("name") or ""))),
            "name": str(raw.get("name") or ""),
            "categories": sorted(categories),
            "destinations": setting_destinations,
            "properties": _properties(raw),
            "state_hash": service.canonical_hash(service._resource_body(raw)),
        })
    if malformed:
        status = "partial"
    elif not normalized:
        status = "missing"
    elif set(REQUIRED_CATEGORIES).issubset(all_categories) and destination_kinds:
        status = "covered"
    else:
        status = "partial"
    return {
        "subscription_id": subscription, "status": status, "complete": not malformed,
        "error": "Inventory contained malformed or incomplete diagnostic-setting data." if malformed else "",
        "settings": normalized,
        "categories": {category: "covered" if category in all_categories else "missing" for category in REQUIRED_CATEGORIES},
        "destinations": {kind: "configured" if kind in destination_kinds else "missing" for kind in _DESTINATION_FIELDS},
    }


async def inventory(connection: dict[str, Any], subscriptions: set[str], *, tenant_id: str = "") -> dict[str, Any]:
    selected = tuple(sorted(str(value) for value in subscriptions if str(value)))
    key = inventory_cache.inventory_key(
        "activity_log_diagnostic_settings", connection, tenant_id=tenant_id, dimensions=(selected,),
    )

    async def load() -> dict[str, Any]:
        from app.azure.arm import arm_write

        token = await service._token(connection)
        semaphore = asyncio.Semaphore(6)

        async def read_one(sub: str) -> dict[str, Any]:
            async with semaphore:
                data, error, status = await arm_write(
                    token, "GET", collection_path(sub), api_version=API_VERSION,
                )
            if error:
                return classify_subscription(sub, [], error=error or f"ARM {status}")
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                return classify_subscription(sub, [], partial=True)
            return classify_subscription(sub, data["value"])

        rows = await asyncio.gather(*(read_one(sub) for sub in selected))
        counts = {status: sum(1 for row in rows if row["status"] == status) for status in ("covered", "partial", "missing", "unknown")}
        return {
            "api_version": API_VERSION, "subscriptions": rows, "counts": counts,
            "partial": any(not row["complete"] for row in rows),
        }

    return await inventory_cache.get_or_create(key, load)


def _normalize_categories(values: list[str]) -> list[str]:
    categories: list[str] = []
    for raw in values:
        match = next((value for value in REQUIRED_CATEGORIES if value.lower() == str(raw).lower()), "")
        if not match:
            raise ValueError("Categories may contain only Administrative, Alert, Policy, and Security.")
        if match not in categories:
            categories.append(match)
    if not categories:
        raise ValueError("Select at least one required Activity Log category.")
    return categories


def _desired_properties(before: dict[str, Any], destination: dict[str, Any], categories: list[str]) -> dict[str, Any]:
    props = json.loads(json.dumps(before or {}))
    logs = [item for item in props.get("logs") or [] if isinstance(item, dict)]
    by_category = {str(item.get("category") or "").lower(): item for item in logs if item.get("category")}
    for category in categories:
        existing = by_category.get(category.lower())
        if existing is None:
            logs.append({"category": category, "enabled": True})
        else:
            existing["enabled"] = True
    props["logs"] = logs
    for field in _DESTINATION_FIELDS.values():
        props.pop(field, None)
    props.pop("eventHubName", None)
    kind = str(destination["kind"])
    props[_DESTINATION_FIELDS[kind]] = str(destination["resource_id"])
    if kind == "event_hub":
        props["eventHubName"] = str(destination["event_hub_name"])
    return props


def fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def preview_plan(
    request: dict[str, Any], inventory_result: dict[str, Any], *, allowed_subscriptions: set[str],
    blockers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    requested = {str(value) for value in request.get("subscription_ids") or [] if str(value)}
    selected = requested or set(allowed_subscriptions)
    if not selected:
        raise ValueError("The selected management scope contains no subscriptions.")
    if requested - allowed_subscriptions:
        raise ValueError("One or more requested subscriptions are outside the selected management scope.")
    categories = _normalize_categories(list(request.get("categories") or REQUIRED_CATEGORIES))
    destination = dict(request.get("destination") or {})
    destination_errors = validate_destination(destination)
    name = str(request.get("setting_name") or "aznetagent-activity-log").strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError("Diagnostic-setting name contains unsupported characters.")
    rows = {str(row.get("subscription_id") or ""): row for row in inventory_result.get("subscriptions") or []}
    blockers = blockers or {}
    items: list[dict[str, Any]] = []
    selected_key = destination_key(destination)
    for sub in sorted(selected):
        row = rows.get(sub)
        errors = list(destination_errors)
        if not row or not row.get("complete") or row.get("status") == "unknown":
            errors.append("Activity Log diagnostic-setting inventory is incomplete; planning failed closed.")
        settings = list((row or {}).get("settings") or [])
        named = next((item for item in settings if str(item.get("name") or "").lower() == name.lower()), None)
        matching = next((item for item in settings if selected_key in {destination_key(value) for value in item.get("destinations") or []}), None)
        before = named or matching
        target_name = str((before or {}).get("name") or name)
        before_props = dict((before or {}).get("properties") or {})
        desired_props = _desired_properties(before_props, destination, categories) if not destination_errors else {}
        already_categories = set((before or {}).get("categories") or [])
        already_destination = bool(before and selected_key in {destination_key(value) for value in before.get("destinations") or []})
        equivalent = set(categories).issubset(already_categories) and already_destination
        target = setting_path(sub, target_name)
        blocker = blockers.get(target.lower().rstrip("/"))
        if blocker:
            errors.append(f"A {blocker['status']} managed change already targets this diagnostic setting.")
        classification = "blocked" if errors else "equivalent" if equivalent else "update" if before else "create"
        operation = classification if classification in {"create", "update"} else "none"
        items.append({
            "order": len(items) + 1, "subscription_id": sub, "classification": classification,
            "operation": operation, "actionable": classification in {"create", "update"},
            "target_id": target, "setting_name": target_name, "categories": categories,
            "destination": destination, "before": before or {},
            "desired": {"properties": desired_props}, "errors": errors, "blocker": blocker,
        })
    token_inputs = {
        "connection_id": str(request.get("connection_id") or ""),
        "workload_id": request.get("workload_id"), "subscription_id": request.get("subscription_id"),
        "management_group_id": request.get("management_group_id"), "subscription_ids": sorted(selected),
        "categories": categories, "destination": destination, "setting_name": name,
        "inventory_hashes": {item["subscription_id"]: str(item["before"].get("state_hash") or "") for item in items},
    }
    counts = {value: sum(1 for item in items if item["classification"] == value) for value in ("create", "update", "equivalent", "blocked")}
    return {
        "plan_version": 1, "plan_token": fingerprint(token_inputs), "inputs": token_inputs,
        "items": items, "counts": {**counts, "total": len(items), "actionable": sum(1 for item in items if item["actionable"])},
        "valid": not any(item["errors"] for item in items) and any(item["actionable"] for item in items),
    }


async def get_setting(connection: dict[str, Any], target_id: str) -> tuple[dict[str, Any] | None, int, str]:
    return await service.get_arm_resource(connection, target_id, API_VERSION)


async def apply_change(connection: dict[str, Any], change: Any) -> tuple[dict[str, Any] | None, int, str]:
    from app.azure.arm import arm_write

    service.assert_writable(connection)
    token = await service._token(connection)
    live, status, error = await get_setting(connection, change.target_id)
    if change.operation == "create":
        if live:
            return None, 409, "A diagnostic setting with this name already exists."
        if status not in (0, 404):
            return None, status, error or "Could not verify that the diagnostic-setting name is available."
    elif change.operation == "update":
        if error or not live:
            return None, status, error or "The diagnostic setting no longer exists."
        if service.canonical_hash(service._resource_body(live)) != change.expected_state_hash:
            return None, 409, "Azure state changed after this request was reviewed. Refresh and create a new change."
    else:
        return None, 422, "Unsupported diagnostic-setting operation."
    desired = service.decrypted_json(change.desired_encrypted)
    body = desired.get("body") if isinstance(desired.get("body"), dict) else {}
    data, write_error, write_status = await arm_write(token, "PUT", change.target_id, body=body, api_version=API_VERSION)
    return (data if isinstance(data, dict) else {} if not write_error else None), write_status, service.safe_error(write_error)

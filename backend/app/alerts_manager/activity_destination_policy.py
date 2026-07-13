"""Tenant and Azure-connection scoped Activity Log destination defaults."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "activity_log_destination_policies.json"
_RESOURCE_GROUP_RE = re.compile(r"^[A-Za-z0-9_.()-]{1,90}$")
_LOCATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$")
_ACTION_GROUP_ID_RE = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Insights/actionGroups/[^/]+$",
    re.IGNORECASE,
)
_ACTION_GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9_.()-]{1,260}$")


def _key(value: str) -> str:
    return str(value or "").strip().lower()


def _read() -> dict[str, Any]:
    value = jsonstore.read_json(_PATH, {"version": 1, "tenants": {}})
    return value if isinstance(value, dict) else {"version": 1, "tenants": {}}


def validate_resource_group(value: str, *, required: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized and not required:
        return ""
    if not _RESOURCE_GROUP_RE.fullmatch(normalized) or normalized.endswith("."):
        raise ValueError("Resource group names must be 1-90 Azure-safe characters and must not end with a period.")
    return normalized


def validate_location(value: str, *, required: bool = False) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if not normalized and not required:
        return ""
    if not _LOCATION_RE.fullmatch(normalized):
        raise ValueError("Azure location must be a non-empty valid location name of at most 128 characters.")
    return normalized


def validate_action_group_id(value: str, *, required: bool = False) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized and not required:
        return ""
    if not _ACTION_GROUP_ID_RE.fullmatch(normalized) or "\x00" in normalized:
        raise ValueError("Action Group IDs must be complete Azure resource IDs.")
    return normalized


def validate_action_group_name_prefix(value: str) -> str:
    normalized = str(value or "").strip()
    if not _ACTION_GROUP_NAME_RE.fullmatch(normalized):
        raise ValueError("Action Group clone name prefixes must contain only Azure-safe characters.")
    return normalized


def clone_action_group_name(prefix: str, subscription_id: str) -> str:
    return f"{validate_action_group_name_prefix(prefix)}-{str(subscription_id)[:8]}"[:260]


def receiver_count(resource: dict[str, Any]) -> int:
    if "receiver_count" in resource:
        return int(resource.get("active_receiver_count") or resource.get("receiver_count") or 0)
    props = resource.get("properties") if isinstance(resource.get("properties"), dict) else {}
    return sum(
        len(value) for key, value in props.items()
        if key.lower().endswith("receivers") and isinstance(value, list)
    )


def normalize_policy(payload: dict[str, Any]) -> dict[str, Any]:
    mappings = payload.get("resource_groups_by_subscription") or {}
    if not isinstance(mappings, dict):
        raise ValueError("resource_groups_by_subscription must be an object.")
    normalized_mappings: dict[str, str] = {}
    for subscription_id, resource_group in mappings.items():
        subscription = _key(subscription_id)
        if not subscription or len(subscription) > 128 or "/" in subscription or "\x00" in subscription:
            raise ValueError("Resource group mappings require valid normalized subscription IDs.")
        normalized_mappings[subscription] = validate_resource_group(str(resource_group), required=True)
    action_group_mappings = payload.get("action_groups_by_subscription") or {}
    if not isinstance(action_group_mappings, dict):
        raise ValueError("action_groups_by_subscription must be an object.")
    normalized_action_groups: dict[str, str] = {}
    for subscription_id, action_group_id in action_group_mappings.items():
        subscription = _key(subscription_id)
        if not subscription or len(subscription) > 128 or "/" in subscription or "\x00" in subscription:
            raise ValueError("Action Group mappings require valid normalized subscription IDs.")
        normalized_action_groups[subscription] = validate_action_group_id(str(action_group_id), required=True)
    return {
        "preferred_resource_group_name": validate_resource_group(
            str(payload.get("preferred_resource_group_name") or "")
        ),
        "default_location": validate_location(str(payload.get("default_location") or "")),
        "resource_groups_by_subscription": dict(sorted(normalized_mappings.items())),
        "preferred_action_group_id": validate_action_group_id(str(payload.get("preferred_action_group_id") or "")),
        "action_groups_by_subscription": dict(sorted(normalized_action_groups.items())),
    }


def get_policy(tenant_id: str, connection_id: str) -> dict[str, Any]:
    data = _read()
    value = (
        data.get("tenants", {}).get(_key(tenant_id) or "default", {})
        .get("connections", {}).get(_key(connection_id), {})
    )
    return {
        "preferred_resource_group_name": str(value.get("preferred_resource_group_name") or ""),
        "default_location": str(value.get("default_location") or ""),
        "resource_groups_by_subscription": dict(value.get("resource_groups_by_subscription") or {}),
        "preferred_action_group_id": str(value.get("preferred_action_group_id") or ""),
        "action_groups_by_subscription": dict(value.get("action_groups_by_subscription") or {}),
        "updated_at": str(value.get("updated_at") or ""),
    }


def put_policy(tenant_id: str, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    policy = normalize_policy(payload)
    policy["updated_at"] = datetime.now(timezone.utc).isoformat()
    data = _read()
    tenant = data.setdefault("tenants", {}).setdefault(_key(tenant_id) or "default", {})
    tenant.setdefault("connections", {})[_key(connection_id)] = policy
    jsonstore.write_json(_PATH, data)
    return dict(policy)

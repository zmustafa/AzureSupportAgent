"""Read-only Azure inventory for Activity Log diagnostic destinations."""
from __future__ import annotations

from typing import Any

from app.alerts_manager import service

_EVENT_HUB_API = "2024-01-01"
_DESTINATION_TYPES = {
    "workspace": "microsoft.operationalinsights/workspaces",
    "storage": "microsoft.storage/storageaccounts",
    "event_hub": "microsoft.eventhub/namespaces",
}


def _option(row: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or row.get("id") or ""),
        "subscription_id": str(row.get("subscriptionId") or row.get("subscription_id") or ""),
        "resource_group": str(row.get("resourceGroup") or row.get("resource_group") or ""),
        "location": str(row.get("location") or ""),
    }


def _sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "").lower()))


async def _subscriptions_below(token: str, management_group_id: str) -> list[str]:
    from app.azure.arm import get_management_group_children

    subscriptions: list[str] = []
    seen: set[str] = set()
    stack = [management_group_id]
    while stack:
        current = stack.pop()
        if current.lower() in seen:
            continue
        seen.add(current.lower())
        children, error = await get_management_group_children(token, current)
        if error:
            raise ValueError(service.safe_error(error))
        for child in children:
            if child.get("kind") == "mg":
                stack.append(str(child.get("id") or ""))
            elif child.get("kind") == "subscription" and child.get("id"):
                subscriptions.append(str(child["id"]))
    return list(dict.fromkeys(subscriptions))


async def options(
    connection: dict[str, Any], *, management_group_id: str = "", subscription_id: str = "",
    resource_group: str = "", kind: str = "workspace", namespace_id: str = "",
) -> dict[str, Any]:
    """Return one progressively filtered MG -> subscription -> RG -> resource catalog."""
    from app.azure.arm import arm_write, list_all_management_groups, list_subscriptions

    if kind not in _DESTINATION_TYPES:
        raise ValueError("Unsupported diagnostic destination kind.")

    token = await service._token(connection)
    management_groups, management_group_error = await list_all_management_groups(token)
    if management_group_error:
        raise ValueError(service.safe_error(management_group_error))
    visible_subscriptions, subscription_error = await list_subscriptions(token)
    if subscription_error:
        raise ValueError(service.safe_error(subscription_error))

    allowed_ids = None
    if management_group_id:
        visible_mg_ids = {str(item.get("id") or "").lower() for item in management_groups}
        if management_group_id.lower() not in visible_mg_ids:
            raise ValueError("The selected management group is not visible to this connection.")
        allowed_ids = {value.lower() for value in await _subscriptions_below(token, management_group_id)}
    subscriptions = [
        item for item in visible_subscriptions
        if allowed_ids is None or str(item.get("id") or "").lower() in allowed_ids
    ]
    visible_subscription_ids = {str(item.get("id") or "").lower() for item in subscriptions}
    if subscription_id and subscription_id.lower() not in visible_subscription_ids:
        raise ValueError("The selected subscription is not visible in the selected management-group scope.")

    resource_groups: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    event_hubs: list[dict[str, Any]] = []
    authorization_rules: list[dict[str, Any]] = []
    authorization_rule_error = ""
    if subscription_id:
        resource_groups = await service._arg(
            connection,
            "resourcecontainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
            "| project id,name,subscriptionId,resourceGroup,location",
            {subscription_id}, max_rows=5000,
        )
    if subscription_id and resource_group:
        escaped_group = resource_group.replace("'", "''")
        resource_type = _DESTINATION_TYPES[kind]
        resources = await service._arg(
            connection,
            f"resources | where resourceGroup =~ '{escaped_group}' | where type =~ '{resource_type}' "
            "| project id,name,type,subscriptionId,resourceGroup,location",
            {subscription_id}, max_rows=5000,
        )
        if not resources and namespace_id:
            namespace_id = ""

    namespace_ids = {str(item.get("id") or "").lower() for item in resources}
    if kind == "event_hub" and namespace_id:
        if namespace_id.lower().rstrip("/") not in {value.rstrip("/") for value in namespace_ids}:
            raise ValueError("The selected Event Hubs namespace is not in the selected resource group.")
        event_hubs = await service._arg(
            connection,
            "resources | where type =~ 'microsoft.eventhub/namespaces/eventhubs' "
            f"| where id startswith '{namespace_id.replace(chr(39), chr(39) * 2)}/eventhubs/' "
            "| project id,name,type,subscriptionId,resourceGroup,location",
            {subscription_id}, max_rows=5000,
        )
        data, error, status = await arm_write(
            token, "GET", f"{namespace_id.rstrip('/')}/authorizationRules", api_version=_EVENT_HUB_API,
        )
        if error or status != 200 or not isinstance(data, dict) or not isinstance(data.get("value"), list):
            authorization_rule_error = service.safe_error(error or "Azure did not return namespace authorization rules.")
        else:
            authorization_rules = [
                {"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}
                for item in data["value"] if item.get("id") and item.get("name")
            ]
            if not authorization_rules:
                authorization_rule_error = "Azure returned no visible namespace authorization rules."

    return {
        "connection_id": str(connection.get("id") or ""),
        "management_groups": _sorted(management_groups),
        "subscriptions": _sorted(subscriptions),
        "resource_groups": _sorted([_option(item) for item in resource_groups]),
        "resources": _sorted([_option(item) for item in resources]),
        "event_hubs": _sorted([_option(item) for item in event_hubs]),
        "authorization_rules": _sorted(authorization_rules),
        "authorization_rules_complete": not authorization_rule_error,
        "authorization_rule_error": authorization_rule_error,
    }

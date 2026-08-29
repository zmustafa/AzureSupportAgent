"""Read-only Azure Cost Management tool for chat."""
from __future__ import annotations

import json
import re
from typing import Any

from app.connectors.base import ConnectorTool, err, ok
from app.inventory import cost, service

TOOL_NAME = "azure_cost_query"
_SUBSCRIPTION_IN_ID = re.compile(r"/subscriptions/([^/]+)", re.IGNORECASE)


def subscription_ids_from_workload(workload: dict[str, Any] | None) -> list[str]:
    """Return every subscription explicitly represented by a workload."""
    found: list[str] = []
    seen: set[str] = set()
    for node in (workload or {}).get("nodes") or []:
        candidates: list[str] = []
        if node.get("kind") == "subscription" and node.get("id"):
            candidates.append(str(node["id"]))
        match = _SUBSCRIPTION_IN_ID.search(str(node.get("id") or ""))
        if match:
            candidates.append(match.group(1))
        for candidate in candidates:
            lowered = candidate.lower()
            if lowered not in seen:
                seen.add(lowered)
                found.append(candidate)
    return found


async def _visible_subscriptions(connection: dict[str, Any] | None) -> tuple[list[dict[str, str]], str]:
    from app.azure.arm import list_subscriptions
    from app.azure.credentials import get_arm_token

    token, token_error = await get_arm_token(connection or {})
    if not token:
        return [], (token_error or "No Azure token is available for this connection.")[:300]
    subscriptions, error = await list_subscriptions(token)
    if error:
        return [], error[:300]
    enabled = [s for s in subscriptions if not s.get("state") or s.get("state") == "Enabled"]
    return enabled, ""


def make_cost_query(
    tenant_id: str,
    principal: Any,
    connection: dict[str, Any] | None,
    *,
    scope: str = "",
    allowed_subscription_ids: list[str] | None = None,
):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not (getattr(principal, "is_admin", False) or principal.has("cost.read")):
            return err("You do not have the 'cost.read' permission.")

        visible, visible_error = await _visible_subscriptions(connection)
        if visible_error:
            return err(visible_error)
        visible_by_id = {str(s["id"]).lower(): s for s in visible if s.get("id")}
        visible_ids = [str(s["id"]) for s in visible if s.get("id")]

        if allowed_subscription_ids:
            base_ids = [
                visible_by_id[s.lower()]["id"]
                for s in allowed_subscription_ids
                if s.lower() in visible_by_id
            ]
        else:
            base_ids = visible_ids
        if scope:
            scoped_ids, scope_error = await service.resolve_scope_sub_ids(connection, scope, visible_ids)
            if scope_error:
                return err(scope_error)
            scoped = {s.lower() for s in scoped_ids}
            base_ids = [s for s in base_ids if s.lower() in scoped]

        requested = [
            str(value).strip()
            for value in (args.get("subscription_ids") or [])
            if str(value).strip()
        ]
        if requested:
            permitted = {s.lower(): s for s in base_ids}
            outside = [s for s in requested if s.lower() not in permitted]
            if outside:
                return err("One or more requested subscriptions are outside the selected chat scope.")
            base_ids = list(dict.fromkeys(permitted[s.lower()] for s in requested))

        filters = {
            key: str(args.get(key) or "").strip()[:512]
            for key in ("resource_id", "resource_group", "service_name")
            if str(args.get(key) or "").strip()
        }
        cost_type = {
            "actual": "ActualCost",
            "amortized": "AmortizedCost",
            "ActualCost": "ActualCost",
            "AmortizedCost": "AmortizedCost",
        }.get(str(args.get("cost_type") or "actual"), "")
        if not cost_type:
            return err("cost_type must be 'actual' or 'amortized'.")
        try:
            payload = await cost.query_cost_breakdown(
                connection,
                base_ids,
                tenant_id,
                str((connection or {}).get("id") or ""),
                timeframe=str(args.get("timeframe") or "last_7_days"),
                start_date=str(args.get("start_date") or ""),
                end_date=str(args.get("end_date") or ""),
                cost_type=cost_type,
                group_by=str(args.get("group_by") or "resource"),
                filters=filters,
                top=int(args.get("top") or 20),
                force=bool(args.get("force_refresh", False)),
            )
        except (TypeError, ValueError) as exc:
            return err(str(exc))
        if not payload.get("available"):
            return err(str(payload.get("reason") or "Azure Cost Management returned no usable result."))

        names = {str(s["id"]).lower(): str(s.get("name") or s["id"]) for s in visible}
        payload["subscription_names"] = {
            subscription_id: names.get(subscription_id.lower(), subscription_id)
            for subscription_id in base_ids[:25]
        }
        summary = (
            f"Azure {payload['cost_type']} for {payload['period']['label']} "
            f"across {payload['subscriptions_succeeded']} subscription(s)"
        )
        return ok(json.dumps(payload, ensure_ascii=False, default=str), summary)

    return _handler


def tool_specs(
    tenant_id: str,
    principal: Any,
    connection: dict[str, Any] | None,
    *,
    scope: str = "",
    allowed_subscription_ids: list[str] | None = None,
) -> list[tuple[str, str, dict[str, Any], Any]]:
    description = (
        "Query authoritative billed Azure spend from the Cost Management Query API. Use this "
        "for actual or amortized historical cost, charges, spend, and cost by resource, "
        "subscription, resource group, service, meter category, or day. This is NOT retail SKU "
        "pricing: use the Azure MCP pricing tool only for list prices or forward estimates. "
        "Dates are resolved by the server clock; never invent a date range. Read-only."
    )
    schema = {
        "type": "object",
        "properties": {
            "timeframe": {
                "type": "string",
                "enum": ["last_7_days", "last_30_days", "current_month", "previous_month", "custom"],
                "description": "Historical billing period; defaults to last_7_days.",
            },
            "start_date": {"type": "string", "description": "YYYY-MM-DD; required for custom."},
            "end_date": {"type": "string", "description": "YYYY-MM-DD; required for custom."},
            "cost_type": {
                "type": "string", "enum": ["actual", "amortized"],
                "description": "Actual charges or reservation/savings-plan amortized cost.",
            },
            "group_by": {
                "type": "string",
                "enum": ["resource", "subscription", "resource_group", "service", "meter_category", "day"],
                "description": "Breakdown dimension; defaults to resource.",
            },
            "subscription_ids": {
                "type": "array", "items": {"type": "string"},
                "description": "Optional subset of subscriptions inside the selected chat scope.",
            },
            "resource_id": {"type": "string", "description": "Optional exact resource-id filter."},
            "resource_group": {"type": "string", "description": "Optional exact resource-group filter."},
            "service_name": {"type": "string", "description": "Optional exact Cost Management service filter."},
            "top": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Rows to return; defaults to 20."},
            "force_refresh": {"type": "boolean", "description": "Bypass the six-hour query cache."},
        },
    }
    return [(
        TOOL_NAME,
        description,
        schema,
        make_cost_query(
            tenant_id,
            principal,
            connection,
            scope=scope,
            allowed_subscription_ids=allowed_subscription_ids,
        ),
    )]


def build_cost_tools(
    tenant_id: str,
    principal: Any,
    connection: dict[str, Any] | None,
    *,
    scope: str = "",
    allowed_subscription_ids: list[str] | None = None,
) -> list[ConnectorTool]:
    from app.core.app_settings import load_settings

    if not bool(load_settings().get("cost_tools_enabled", True)):
        return []
    return [
        ConnectorTool(name=name, description=description, parameters=schema, kind="read", handler=handler)
        for name, description, schema, handler in tool_specs(
            tenant_id,
            principal,
            connection,
            scope=scope,
            allowed_subscription_ids=allowed_subscription_ids,
        )
    ]


def register_cost_tools(
    toolset: Any,
    *,
    tenant_id: str,
    principal: Any,
    connection: dict[str, Any] | None,
    scope: str = "",
    workload: dict[str, Any] | None = None,
) -> None:
    if not (getattr(principal, "is_admin", False) or principal.has("cost.read")):
        return
    try:
        workload_subscriptions = subscription_ids_from_workload(workload)
        tools = build_cost_tools(
            tenant_id,
            principal,
            connection,
            scope=scope,
            allowed_subscription_ids=workload_subscriptions or None,
        )
        if tools:
            toolset.add_connector({"tenant_id": tenant_id}, tools)
    except Exception:  # noqa: BLE001 - optional tools must not break a chat turn
        pass


__all__ = [
    "TOOL_NAME",
    "build_cost_tools",
    "make_cost_query",
    "register_cost_tools",
    "subscription_ids_from_workload",
    "tool_specs",
]

"""Scope-safe, cross-subscription Azure Advisor recommendations for chat."""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from typing import Any

from app.connectors.base import ConnectorTool, err, ok
from app.inventory import service

TOOL_NAME = "azure_advisor_recommendations"
_CONCURRENCY = 4
_MAX_SUBSCRIPTIONS = 25
_MAX_PAGES = 5
_MAX_ROWS_PER_SUBSCRIPTION = 500
_SUBSCRIPTION_IN_ID = re.compile(r"/subscriptions/([^/]+)", re.IGNORECASE)
_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
_NO_FILTER = {"", "all", "any", "*", "none"}


def subscription_ids_from_workload(workload: dict[str, Any] | None) -> list[str]:
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
    return [
        subscription for subscription in subscriptions
        if not subscription.get("state") or subscription.get("state") == "Enabled"
    ], ""


async def _subscription_recommendations(
    token: str, subscription_id: str,
) -> tuple[list[dict[str, Any]], str]:
    from app.azure.arm import arm_rest

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        "/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01"
    )
    rows: list[dict[str, Any]] = []
    for _page in range(_MAX_PAGES):
        text, error = "", ""
        for attempt in range(4):
            text, error = await arm_rest(token, "GET", url)
            transient = error and any(
                token in error.lower()
                for token in ("429", "throttl", "500", "502", "503", "504", "timeout")
            )
            if not transient or attempt == 3:
                break
            await asyncio.sleep(1 + attempt * 2)
        if error:
            return [], error[:300]
        try:
            payload = json.loads(text or "{}")
        except json.JSONDecodeError:
            return [], "Azure Advisor returned an unreadable response."
        for raw in payload.get("value") or []:
            if not isinstance(raw, dict):
                continue
            properties = raw.get("properties") or {}
            description = properties.get("shortDescription") or {}
            metadata = properties.get("resourceMetadata") or {}
            rows.append({
                "id": str(raw.get("id") or ""),
                "recommendation_type_id": str(properties.get("recommendationTypeId") or ""),
                "category": str(properties.get("category") or "Unknown"),
                "impact": str(properties.get("impact") or "Unknown"),
                "problem": str(description.get("problem") or ""),
                "solution": str(description.get("solution") or ""),
                "resource_id": str(metadata.get("resourceId") or raw.get("id") or ""),
                "impacted_field": str(properties.get("impactedField") or ""),
                "impacted_value": str(properties.get("impactedValue") or ""),
                "last_updated": str(properties.get("lastUpdated") or ""),
                "subscription_id": subscription_id,
            })
            if len(rows) >= _MAX_ROWS_PER_SUBSCRIPTION:
                return rows, ""
        next_link = str(payload.get("nextLink") or "")
        if not next_link:
            break
        url = next_link
    return rows, ""


def make_advisor_query(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    scope: str = "",
    allowed_subscription_ids: list[str] | None = None,
):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        visible, visible_error = await _visible_subscriptions(connection)
        if visible_error:
            return err(visible_error)
        visible_by_id = {str(s["id"]).lower(): s for s in visible if s.get("id")}
        visible_ids = [str(s["id"]) for s in visible if s.get("id")]
        base_ids = visible_ids
        if allowed_subscription_ids:
            base_ids = [
                str(visible_by_id[s.lower()]["id"])
                for s in allowed_subscription_ids if s.lower() in visible_by_id
            ]
        if scope:
            scoped_ids, scope_error = await service.resolve_scope_sub_ids(connection, scope, visible_ids)
            if scope_error:
                return err(scope_error)
            scoped = {s.lower() for s in scoped_ids}
            base_ids = [s for s in base_ids if s.lower() in scoped]

        requested = [
            str(value).strip() for value in (args.get("subscription_ids") or [])
            if str(value).strip()
        ]
        if requested:
            permitted = {s.lower(): s for s in base_ids}
            if any(s.lower() not in permitted for s in requested):
                return err("One or more requested subscriptions are outside the selected chat scope.")
            base_ids = list(dict.fromkeys(permitted[s.lower()] for s in requested))
        if not base_ids:
            return err("No visible subscription is in the selected chat scope.")

        category = str(args.get("category") or "").strip().lower()
        impact = str(args.get("impact") or "").strip().lower()
        category = "" if category in _NO_FILTER else category
        impact = "" if impact in _NO_FILTER else impact
        search = str(args.get("search") or "").strip().lower()[:200]
        omitted = base_ids[_MAX_SUBSCRIPTIONS:]
        targets = base_ids[:_MAX_SUBSCRIPTIONS]

        from app.azure.credentials import get_arm_token

        token, token_error = await get_arm_token(connection or {})
        if not token:
            return err((token_error or "No Azure token is available for this connection.")[:300])
        semaphore = asyncio.Semaphore(_CONCURRENCY)

        async def run_one(subscription_id: str):
            async with semaphore:
                rows, error = await _subscription_recommendations(token, subscription_id)
                return subscription_id, rows, error

        results = await asyncio.gather(*(run_one(s) for s in targets))
        names = {str(s["id"]).lower(): str(s.get("name") or s["id"]) for s in visible}
        recommendations: list[dict[str, Any]] = []
        seen_recommendations: set[tuple[str, str, str, str]] = set()
        errors: list[dict[str, str]] = []
        for subscription_id, rows, error in results:
            if error:
                errors.append({"subscription_id": subscription_id, "error": error})
                continue
            subscription_name = names.get(subscription_id.lower(), subscription_id)
            for row in rows:
                row["subscription_name"] = subscription_name
                if category and row["category"].lower() != category:
                    continue
                if impact and row["impact"].lower() != impact:
                    continue
                haystack = " ".join(
                    str(row.get(key) or "")
                    for key in ("problem", "solution", "resource_id", "category", "impact")
                ).lower()
                if search and search not in haystack:
                    continue
                identity = (
                    subscription_id.lower(),
                    str(row.get("recommendation_type_id") or "").lower(),
                    str(row.get("resource_id") or "").lower(),
                    str(row.get("problem") or "").lower(),
                )
                if identity in seen_recommendations:
                    continue
                seen_recommendations.add(identity)
                recommendations.append(row)

        recommendations.sort(key=lambda row: (
            _IMPACT_ORDER.get(str(row.get("impact") or "").lower(), 9),
            str(row.get("category") or ""),
            str(row.get("problem") or ""),
        ))
        category_counts = Counter(str(row["category"]) for row in recommendations)
        impact_counts = Counter(str(row["impact"]) for row in recommendations)
        subscription_counts = Counter({
            names.get(subscription_id.lower(), subscription_id): 0
            for subscription_id in targets
        })
        subscription_counts.update(str(row["subscription_name"]) for row in recommendations)
        top = max(1, min(int(args.get("top") or 25), 100))
        inventory_complete = not errors and not omitted
        payload = {
            "status": "complete" if inventory_complete else "partial",
            "inventory_complete": inventory_complete,
            "total_recommendations": len(recommendations),
            "returned_recommendations": min(top, len(recommendations)),
            "truncated": len(recommendations) > top,
            "counts_by_category": dict(category_counts.most_common()),
            "counts_by_impact": dict(impact_counts.most_common()),
            "counts_by_subscription": dict(subscription_counts.most_common()),
            "recommendations": recommendations[:top],
            "subscriptions_queried": len(targets),
            "subscriptions_succeeded": len(targets) - len(errors),
            "subscriptions_failed": len(errors),
            "subscriptions_omitted": len(omitted),
            "errors": errors,
            "scope_note": "Every accessible subscription in the selected chat scope was queried.",
        }
        return ok(
            json.dumps(payload, ensure_ascii=False, default=str),
            f"Found {len(recommendations)} Advisor recommendation(s) across {len(targets)} subscription(s)",
        )

    return _handler


def build_advisor_tools(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    scope: str = "",
    allowed_subscription_ids: list[str] | None = None,
) -> list[ConnectorTool]:
    description = (
        "List and summarize open Azure Advisor recommendations across EVERY subscription in the "
        "selected chat scope in one read-only call. Use this ONE tool directly; it discovers the "
        "subscriptions itself. Do not load Advisor or search for subscriptions first. Use this for "
        "plural, tenant-wide, or cross-subscription recommendation questions instead of calling "
        "the Advisor MCP namespace without a subscription, which only uses the default subscription."
    )
    schema = {
        "type": "object",
        "properties": {
            "subscription_ids": {
                "type": "array", "items": {"type": "string"},
                "description": "Optional subset inside the selected chat scope.",
            },
            "category": {
                "type": "string",
                "enum": ["All", "Cost", "HighAvailability", "Security", "Performance", "OperationalExcellence"],
                "description": "Optional exact Advisor category. All applies no category filter.",
            },
            "impact": {"type": "string", "enum": ["All", "High", "Medium", "Low"]},
            "search": {"type": "string", "description": "Optional text filter."},
            "top": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    }
    return [ConnectorTool(
        name=TOOL_NAME,
        description=description,
        parameters=schema,
        kind="read",
        handler=make_advisor_query(
            tenant_id,
            connection,
            scope=scope,
            allowed_subscription_ids=allowed_subscription_ids,
        ),
    )]


def register_advisor_tools(
    toolset: Any,
    *,
    tenant_id: str,
    connection: dict[str, Any] | None,
    scope: str = "",
    workload: dict[str, Any] | None = None,
) -> None:
    try:
        tools = build_advisor_tools(
            tenant_id,
            connection,
            scope=scope,
            allowed_subscription_ids=subscription_ids_from_workload(workload) or None,
        )
        toolset.add_connector({"tenant_id": tenant_id}, tools)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["TOOL_NAME", "build_advisor_tools", "make_advisor_query", "register_advisor_tools"]

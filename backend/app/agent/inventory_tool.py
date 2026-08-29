"""Validated natural-language Azure Resource Graph inventory for chat."""
from __future__ import annotations

import json
import asyncio
import re
from typing import Any

from app.connectors.base import ConnectorTool, err, ok

TOOL_NAME = "azure_resource_inventory"


def _transient(result: dict[str, Any]) -> bool:
    if not result.get("isError"):
        return False
    text = " ".join(str(item) for item in (result.get("content") or [])).lower()
    return any(token in text for token in (
        "gatewaytimeout", "gateway timeout", "timed out", "timeout", "throttl", "429",
        'code "500"', 'code "502"', 'code "503"', 'code "504"',
    ))


async def _call_with_retry(
    mcp_client: Any, name: str, arguments: dict[str, Any], *, attempts: int = 3,
) -> dict[str, Any]:
    result: dict[str, Any] = {"isError": True, "content": ["Tool call did not run."]}
    for attempt in range(attempts):
        result = await mcp_client.call_tool(name, arguments)
        if not _transient(result) or attempt == attempts - 1:
            return result
        await asyncio.sleep(1 + attempt * 2)
    return result


def _scope_query(query: str, subscription_ids: list[str]) -> str:
    if not subscription_ids:
        return query
    allowed = ", ".join(json.dumps(value) for value in subscription_ids)
    table = re.compile(
        r"(?im)^(\s*)(resources|resourcecontainers|advisorresources|authorizationresources|"
        r"healthresources|maintenanceresources|networkresources)(\s*)(\||$)",
    )
    match = table.search(query)
    if match is None:
        raise ValueError("Generated query does not begin from a scope-safe Resource Graph table.")
    replacement = (
        f"{match.group(1)}{match.group(2)}\n"
        f"| where subscriptionId in~ ({allowed})\n"
        f"{match.group(1)}{match.group(4)}"
    )
    return query[:match.start()] + replacement + query[match.end():]


def _json_block(result: dict[str, Any]) -> dict[str, Any]:
    for block in result.get("content") or []:
        if not isinstance(block, str):
            continue
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def make_inventory_query(
    mcp_client: Any,
    *,
    scope_hint: str = "",
    allowed_subscription_ids: list[str] | None = None,
):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        request = str(args.get("request") or "").strip()[:4000]
        if not request:
            return err("Describe the resource inventory to retrieve.")
        scoped_request = f"{request}\n\n{scope_hint}" if scope_hint else request
        generated = await _call_with_retry(
            mcp_client,
            "arm", {"command": "generate_query", "parameters": {"prompt": scoped_request}},
        )
        if generated.get("isError"):
            return generated
        query = str(_json_block(generated).get("query") or "").strip()
        if not query:
            return err("Resource Graph query generation returned no query.")
        try:
            query = _scope_query(query, allowed_subscription_ids or [])
        except ValueError as exc:
            return err(str(exc))

        validated = await _call_with_retry(
            mcp_client,
            "arm", {"command": "validate_query", "parameters": {"query": query}},
        )
        validation = _json_block(validated)
        if validated.get("isError") or validation.get("isValid") is not True:
            detail = str(validation.get("syntaxErrors") or "The generated query did not validate.")
            return err(f"Resource Graph query validation failed: {detail[:500]}")

        page_size = max(1, min(int(args.get("top") or 100), 1000))
        max_pages = max(1, min(int(args.get("max_pages") or 5), 10))
        skip_token = ""
        rows: list[Any] = []
        total_records: int | None = None
        pages_read = 0
        for _page in range(max_pages):
            options: dict[str, Any] = {"$top": page_size, "resultFormat": "objectArray"}
            if skip_token:
                options["$skipToken"] = skip_token
            executed = await _call_with_retry(
                mcp_client,
                "arm",
                {"command": "execute_query", "parameters": {"query": query, "options": options}},
            )
            if executed.get("isError"):
                return executed
            pages_read += 1
            payload = _json_block(executed)
            results = payload.get("results") or {}
            data = results.get("data") if isinstance(results, dict) else None
            if data is None:
                data = payload.get("data") or []
            if isinstance(data, list):
                rows.extend(data)
            if payload.get("totalRecords") is not None:
                try:
                    total_records = int(payload["totalRecords"])
                except (TypeError, ValueError):
                    pass
            skip_token = str(payload.get("skipToken") or "")
            if not skip_token:
                break

        result = {
            "status": "complete" if not skip_token else "truncated",
            "request": request,
            "generated_query": query,
            "query_validated": True,
            "rows": rows,
            "returned_rows": len(rows),
            "total_records": total_records if total_records is not None else len(rows),
            "pagination_complete": not skip_token,
            "pages_read": pages_read,
        }
        return ok(
            json.dumps(result, ensure_ascii=False, default=str),
            f"Found {len(rows)} validated Resource Graph row(s)",
        )

    return _handler


def register_inventory_tool(
    toolset: Any,
    *,
    mcp_client: Any,
    scope_hint: str = "",
    allowed_subscription_ids: list[str] | None = None,
) -> None:
    toolset.add_connector(
        {},
        [ConnectorTool(
            name=TOOL_NAME,
            description=(
                "Generate, validate, execute, and page an Azure Resource Graph query from a "
                "natural-language inventory request. Use this first for cross-resource, public "
                "endpoint, internet exposure, or broad Azure inventory questions. The query is "
                "never executed unless Azure validates it. Make one comprehensive call before "
                "requesting narrower follow-ups. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "request": {"type": "string", "description": "The Azure resource inventory to retrieve."},
                    "top": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "max_pages": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["request"],
            },
            kind="read",
            handler=make_inventory_query(
                mcp_client,
                scope_hint=scope_hint,
                allowed_subscription_ids=allowed_subscription_ids,
            ),
        )],
    )


__all__ = ["TOOL_NAME", "make_inventory_query", "register_inventory_tool"]

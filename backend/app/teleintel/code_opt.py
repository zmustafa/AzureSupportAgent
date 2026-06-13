"""Code Optimizations (Profiler-based .NET suggestions), best-effort.

Surfaces Application Insights Code Optimizations inline when available, via a gated
``az rest`` call. Degrades gracefully (empty + note) when command execution is off or the
API returns nothing. Demo data always populates a sample so the UI is reviewable."""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("app.teleintel.code_opt")


async def code_optimizations(component: dict[str, Any], connection: dict[str, Any] | None) -> dict[str, Any]:
    from app.core.app_settings import load_settings
    from app.exec.command_runner import run_az_json_capture

    if not load_settings().get("command_execution_enabled"):
        return {"items": [], "note": "Code Optimizations require command execution to be enabled."}
    comp_id = component.get("id") or ""
    if not comp_id:
        return {"items": [], "note": "No component id."}
    url = f"https://management.azure.com{comp_id}/providers/Microsoft.CodeOptimizations/issues?api-version=2024-10-01-preview"
    cap = await run_az_json_capture(["rest", "--method", "get", "--url", url, "--output", "json"], connection, label="az rest code-optimizations")
    if not cap.ok:
        return {"items": [], "note": "Code Optimizations unavailable for this resource (no Profiler data or feature off)."}
    try:
        data = json.loads(cap.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"items": [], "note": "Could not parse Code Optimizations response."}
    raw = data.get("value", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    items = []
    for it in raw[:25]:
        p = it.get("properties", it) if isinstance(it, dict) else {}
        items.append(
            {
                "type": p.get("type", ""),
                "issue": p.get("issueSummary") or p.get("name", ""),
                "impact": p.get("impact", ""),
                "function": p.get("function", ""),
            }
        )
    return {"items": items, "note": "" if items else "No Code Optimizations issues found."}

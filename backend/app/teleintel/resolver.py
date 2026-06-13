"""Resolve a workload's Application Insights components and dispatch KQL to the right path.

Each App Insights component (``microsoft.insights/components``) is discovered via Resource
Graph from the workload scope. Workspace-based components (the modern default) carry a
``WorkspaceResourceId`` and are queried via Log Analytics; classic components are queried
via the App Insights query API by their AppId. Both paths are read-only ``az`` commands."""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("app.teleintel.resolver")


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("value") or []
    return data if isinstance(data, list) else []


def parse_la_rows(stdout: str) -> list[dict[str, Any]]:
    """Normalize an ``az monitor log-analytics/app-insights query`` JSON result into a
    flat list of row dicts (both CLIs already flatten to a list of dicts in our usage)."""
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("tables"), list) and data["tables"]:
            t = data["tables"][0]
            cols = [c.get("name", f"c{i}") for i, c in enumerate(t.get("columns", []) or [])]
            return [dict(zip(cols, r)) for r in (t.get("rows") or [])]
        if isinstance(data.get("data"), list):
            return data["data"]
    return []


async def resolve_components(
    connection: dict[str, Any] | None,
    *,
    scope_kind: str,
    scope_id: str,
    workload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return {components, predicate, subscriptions, error}. Each component:
    {id, name, app_id, resource_group, subscription_id, location, workspace_id, mode}."""
    from app.assessments.runner import _resolve_scope
    from app.exec.command_runner import run_kql_capture

    subscriptions: list[str] = []
    predicate = ""
    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        predicate = scope.get("predicate") or ""
        subscriptions = list(scope.get("subscriptions") or [])
        for sub, _rg in scope.get("rg_pairs") or []:
            if sub not in subscriptions:
                subscriptions.append(sub)
        if scope.get("error") and not predicate:
            return {"components": [], "predicate": "", "subscriptions": [], "error": scope["error"]}
    elif scope_kind == "subscription" and scope_id:
        predicate = f"subscriptionId =~ '{_esc(scope_id)}'"
        subscriptions = [scope_id]
    else:
        return {"components": [], "predicate": "", "subscriptions": [], "error": "No resolvable scope."}

    kql = (
        "Resources "
        "| where type =~ 'microsoft.insights/components' "
        f"| where {predicate} "
        "| extend wsid = tostring(properties.WorkspaceResourceId), appId = tostring(properties.AppId) "
        "| project id, name, appId, resourceGroup, subscriptionId, location, wsid "
        "| take 50"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    if not cap.ok:
        return {"components": [], "predicate": predicate, "subscriptions": subscriptions, "error": (cap.error or "")[:200]}

    components: list[dict[str, Any]] = []
    for r in _parse_rows(cap.stdout):
        wsid = r.get("wsid") or ""
        components.append(
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "app_id": r.get("appId", ""),
                "resource_group": r.get("resourceGroup", ""),
                "subscription_id": r.get("subscriptionId", ""),
                "location": r.get("location", ""),
                "workspace_id": wsid,
                "mode": "workspace" if wsid else "classic",
            }
        )
    return {"components": components, "predicate": predicate, "subscriptions": subscriptions, "error": ""}


async def run_component_kql(
    component: dict[str, Any],
    kql: str,
    connection: dict[str, Any] | None,
    *,
    timespan: str = "P1D",
) -> dict[str, Any]:
    """Run a KQL query against one component via the appropriate path. Returns
    {ok, rows, error, path}."""
    from app.exec.command_runner import run_app_insights_capture, run_la_capture

    mode = component.get("mode")
    if mode == "workspace" and component.get("workspace_id"):
        cap = await run_la_capture(kql, component["workspace_id"], connection, timespan=timespan)
        path = "log_analytics"
    elif component.get("app_id"):
        cap = await run_app_insights_capture(kql, component["app_id"], connection, timespan=timespan)
        path = "app_insights"
    else:
        return {"ok": False, "rows": [], "error": "Component has no workspace id or app id.", "path": ""}

    if not cap.ok:
        return {"ok": False, "rows": [], "error": (cap.error or "")[:300], "path": path}
    return {"ok": True, "rows": parse_la_rows(cap.stdout), "error": "", "path": path}


def sli_context_for_workload(workload_id: str, tenant_id: str | None = None) -> str:
    """Ground 'what normal looks like' from Architecture Memory: critical_thresholds (SLIs),
    dependencies, diagnostic_hints, expected_flow of the architecture linked to the workload.
    Returns a compact text block (empty when no memory exists)."""
    if not workload_id:
        return ""
    try:
        from app.architectures.memory import get_memory
        from app.architectures.registry import list_architectures
    except Exception:  # noqa: BLE001
        return ""
    arch_id = ""
    for a in list_architectures(tenant_id):
        if (a.get("workload_id") or "") == workload_id:
            arch_id = a.get("id", "")
            break
    if not arch_id:
        return ""
    mem = get_memory(arch_id)
    if not mem:
        return ""
    wanted = {"critical_thresholds", "dependencies", "diagnostic_hints", "expected_flow"}
    parts: list[str] = []
    for s in mem.get("sections", []) or []:
        if s.get("key") in wanted and s.get("content"):
            parts.append(f"## {s.get('label') or s['key']}\n{str(s['content'])[:800]}")
    return "\n\n".join(parts)


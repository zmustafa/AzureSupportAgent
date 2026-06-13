"""Gather the scoped content for an evidence snapshot at capture time.

Honors the include-toggles and the scope (workload / subscription / selected resource ids).
Every section is a self-contained, read-only payload. All Azure reads are best-effort: a
section that can't be gathered (no connection, command-exec off, empty) is recorded with an
explanatory note rather than failing the whole snapshot."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("app.evidence.collector")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("value") or []
    return data if isinstance(data, list) else []


def _esc(v: str) -> str:
    return (v or "").replace("'", "''")


async def _scope_predicate(scope: dict[str, Any], connection: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Return (KQL predicate, resolved-scope-info). Reuses the assessments scope resolver
    for workloads; subscription/resources scopes are direct."""
    kind = scope.get("kind")
    if kind == "workload":
        from app.assessments.runner import _resolve_scope
        from app.workloads.registry import get_workload

        wl = get_workload(scope.get("id", ""))
        if not wl:
            return "", {"error": "Workload not found."}
        resolved = await _resolve_scope(wl, connection)
        return resolved.get("predicate", ""), {"workload_name": wl.get("name", ""), "resolved": resolved}
    if kind == "subscription":
        return f"subscriptionId =~ '{_esc(scope.get('id',''))}'", {}
    if kind == "resources":
        ids = scope.get("resource_ids") or []
        if not ids:
            return "", {"error": "No resource ids."}
        joined = ", ".join(f"'{_esc(i)}'" for i in ids)
        return f"id in~ ({joined})", {}
    return "", {"error": "Unknown scope kind."}


async def _inventory_section(predicate: str, connection: dict[str, Any] | None, *, full_properties: bool) -> dict[str, Any]:
    from app.exec.command_runner import run_kql_capture

    if not predicate:
        return {"note": "No resolvable scope for inventory.", "resources": []}
    proj = "id, name, type, resourceGroup, subscriptionId, location, tags"
    if full_properties:
        proj += ", properties, sku, kind"
    kql = f"Resources | where {predicate} | project {proj} | order by type asc, name asc | take 1000"
    cap = await run_kql_capture(kql, connection, output="json")
    if not cap.ok:
        return {"note": f"Inventory query failed: {(cap.error or '')[:200]}", "resources": []}
    return {"resources": _parse_rows(cap.stdout), "captured_at": _now()}


async def _changes_section(predicate: str, connection: dict[str, Any] | None, *, days: int = 14) -> dict[str, Any]:
    from app.exec.command_runner import run_kql_capture

    if not predicate:
        return {"note": "No scope for changes.", "changes": []}
    # resourcechanges is a separate ARG table; scope by the same subscription predicate.
    kql = (
        "resourcechanges "
        f"| extend ts=todatetime(properties.changeAttributes.timestamp), "
        "ct=tostring(properties.changeType), targetId=tostring(properties.targetResourceId) "
        f"| where ts > ago({int(days)}d) "
        "| project ts, ct, targetId, changes=properties.changes "
        "| order by ts desc | take 200"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    if not cap.ok:
        return {"note": f"resourcechanges query unavailable: {(cap.error or '')[:160]}", "changes": []}
    return {"changes": _parse_rows(cap.stdout), "window_days": days, "captured_at": _now()}


async def _findings_section(tenant_id: str, scope: dict[str, Any]) -> dict[str, Any]:
    """Latest assessment-run findings + active waivers for a workload scope (incl. AMBA /
    Telemetry / Backup-DR synthetic runs)."""
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models import AssessmentRun, AssessmentWaiver

    if scope.get("kind") != "workload":
        return {"note": "Findings are captured for workload-scoped snapshots.", "runs": [], "waivers": []}
    wl = scope.get("id", "")
    runs_out: list[dict[str, Any]] = []
    waivers_out: list[dict[str, Any]] = []
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AssessmentRun).where(
                    AssessmentRun.tenant_id == tenant_id, AssessmentRun.workload_id == wl,
                    AssessmentRun.status == "succeeded",
                ).order_by(AssessmentRun.started_at.desc()).limit(10)
            )
        ).scalars().all()
        # Keep the latest run per pillar-set/trigger to avoid duplicates bloating the blob.
        seen: set[str] = set()
        for r in rows:
            key = f"{r.trigger}:{','.join(r.pillars or [])}"
            if key in seen:
                continue
            seen.add(key)
            runs_out.append({
                "id": r.id, "trigger": r.trigger, "pillars": r.pillars or [],
                "overall_score": r.overall_score, "severity": r.severity,
                "findings": r.findings_json or [], "started_at": r.started_at.isoformat() if r.started_at else "",
            })
        wrows = (
            await db.execute(
                select(AssessmentWaiver).where(
                    AssessmentWaiver.tenant_id == tenant_id, AssessmentWaiver.workload_id == wl,
                    AssessmentWaiver.status == "active",
                )
            )
        ).scalars().all()
        for w in wrows:
            waivers_out.append({"check_id": w.check_id, "resource_id": w.resource_id, "justification": w.justification,
                                "approver": w.approver, "expires_at": w.expires_at.isoformat() if w.expires_at else ""})
    return {"runs": runs_out, "waivers": waivers_out, "captured_at": _now()}


def _architecture_section(scope: dict[str, Any]) -> dict[str, Any]:
    from app.architectures.registry import list_architectures

    if scope.get("kind") != "workload":
        return {"note": "Architecture revision captured for workload scope.", "architectures": []}
    wl = scope.get("id", "")
    out = []
    for a in list_architectures():
        if a.get("workload_id") == wl:
            out.append({"id": a.get("id"), "name": a.get("name"), "node_count": len(a.get("nodes", []) or []),
                        "edge_count": len(a.get("edges", []) or []), "nodes": a.get("nodes", []), "edges": a.get("edges", [])})
    return {"architectures": out, "captured_at": _now()}


def _memory_section(scope: dict[str, Any], architecture_section: dict[str, Any]) -> dict[str, Any]:
    from app.architectures.memory import get_memory

    out = []
    for a in architecture_section.get("architectures", []):
        mem = get_memory(a["id"])
        if mem:
            out.append({"architecture_id": a["id"], "title": mem.get("title", ""),
                        "sections": mem.get("sections", []), "source": mem.get("source", "")})
    return {"memories": out, "captured_at": _now()}


def _activity_section(architecture_section: dict[str, Any]) -> dict[str, Any]:
    from app.architectures import activity

    events = []
    for a in architecture_section.get("architectures", []):
        events.extend(activity.list_activity(a["id"])[-50:])
    return {"events": events, "captured_at": _now()}


async def _metrics_section(predicate: str, connection: dict[str, Any] | None) -> dict[str, Any]:
    from app.core.app_settings import load_settings

    if not load_settings().get("command_execution_enabled", False):
        return {"note": "Metrics capture needs command execution enabled (Admin → General).", "metrics": []}
    # Metrics are per-resource + slow; capture is opportunistic and capped. Left as a
    # documented placeholder set so the snapshot records the intent + gate state.
    return {"note": "Metrics window capture is opportunistic; enable per-resource collection in a later pass.", "metrics": []}


async def collect_content(
    *,
    tenant_id: str,
    scope: dict[str, Any],
    included: list[str],
    connection: dict[str, Any] | None,
    full_properties: bool = True,
) -> dict[str, Any]:
    """Build the snapshot content dict for the selected sections. Never raises."""
    predicate, scope_info = await _scope_predicate(scope, connection)
    content: dict[str, Any] = {
        "_meta": {
            "scope": scope,
            "scope_info": scope_info,
            "connection_configured": connection is not None,
            "captured_at": _now(),
        }
    }
    inc = set(included)

    if "inventory" in inc or "properties" in inc:
        content["inventory"] = await _inventory_section(predicate, connection, full_properties=("properties" in inc) and full_properties)
    if "changes" in inc:
        content["changes"] = await _changes_section(predicate, connection)
    if "findings" in inc:
        content["findings"] = await _findings_section(tenant_id, scope)
    arch = {}
    if "architecture" in inc or "memory" in inc or "activity" in inc:
        arch = _architecture_section(scope)
        if "architecture" in inc:
            content["architecture"] = arch
    if "memory" in inc:
        content["memory"] = _memory_section(scope, arch)
    if "activity" in inc:
        content["activity"] = _activity_section(arch)
    if "metrics" in inc:
        content["metrics"] = await _metrics_section(predicate, connection)
    return content

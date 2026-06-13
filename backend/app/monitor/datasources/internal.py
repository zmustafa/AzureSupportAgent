"""Internal datasources: app telemetry, workbook references, and static/inline data.

- ``app_telemetry`` — exposes the existing Monitor overview (messages, tool calls,
  automations, posture, …) as queryable series so app-health widgets are first-class.
- ``workbook_ref`` — binds a widget to a saved Workbook's latest run (bridges the
  Workbooks system into Monitor; reuses its AI'fied severity/number/structured output).
- ``static`` — hand-entered rows for mockups / markdown tables.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import desc, select

from app.core.db import SessionLocal
from app.models import WorkbookRun

from .base import Column, TableResult, table_from_records

# Short-TTL cache of the (heavy) Monitor overview, keyed by tenant. Several app_telemetry
# widgets on one dashboard refreshing together then share a single aggregation pass.
_OVERVIEW_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_OVERVIEW_TTL = 10.0
# Single-flight: concurrent callers for the same tenant await one in-flight computation
# instead of each running the heavy aggregation (widgets all refresh on the same cadence).
_OVERVIEW_INFLIGHT: dict[str, asyncio.Task] = {}


async def _compute_overview(tenant_id: str) -> dict[str, Any]:
    from app.api.admin import build_monitor_overview

    async with SessionLocal() as db:
        overview = await build_monitor_overview(db, tenant_id)
    _OVERVIEW_CACHE[tenant_id] = (time.time() + _OVERVIEW_TTL, overview)
    return overview


async def _get_overview(tenant_id: str) -> dict[str, Any]:
    hit = _OVERVIEW_CACHE.get(tenant_id)
    if hit and time.time() < hit[0]:
        return hit[1]
    inflight = _OVERVIEW_INFLIGHT.get(tenant_id)
    if inflight is not None and not inflight.done():
        return await inflight
    task = asyncio.ensure_future(_compute_overview(tenant_id))
    _OVERVIEW_INFLIGHT[tenant_id] = task
    try:
        return await task
    finally:
        if _OVERVIEW_INFLIGHT.get(tenant_id) is task:
            _OVERVIEW_INFLIGHT.pop(tenant_id, None)


async def resolve_app_telemetry(
    cfg: dict[str, Any], conn, params, *, tenant_id: str = ""
) -> TableResult:
    """Slice the Monitor overview snapshot into a table chosen by ``telemetry_key``.

    Supported keys: activity_24h, activity_14d, tool_status, providers, top_tools,
    top_chats, tokens_by_model, totals, posture_pillars, automations_status.
    """
    key = str(cfg.get("telemetry_key") or "activity_24h").strip()
    try:
        overview = await _get_overview(tenant_id)
    except Exception as exc:  # noqa: BLE001
        return TableResult.from_error(f"Telemetry unavailable: {exc}")

    if key == "activity_24h":
        rows = [{"hour": r["hour"], "messages": r["messages"], "tool_calls": r["tool_calls"]} for r in overview.get("activity_24h", [])]
        return table_from_records(rows)
    if key == "activity_14d":
        rows = [{"date": r["date"], "messages": r["messages"], "tool_calls": r["tool_calls"], "runs": r["runs"]} for r in overview.get("activity_14d", [])]
        return table_from_records(rows)
    if key == "tool_status":
        by = (overview.get("tool_calls") or {}).get("by_status", {})
        return table_from_records([{"status": k, "count": v} for k, v in by.items()])
    if key == "providers":
        return table_from_records([{"provider": p["provider"], "count": p["count"]} for p in overview.get("providers", [])])
    if key == "top_tools":
        return table_from_records((overview.get("tool_calls") or {}).get("top_tools", []))
    if key == "top_chats":
        rows = [{"title": c["title"], "messages": c["messages"], "tool_calls": c["tool_calls"]} for c in overview.get("top_chats", [])]
        return table_from_records(rows)
    if key == "tokens_by_model":
        return table_from_records((overview.get("tokens") or {}).get("by_model", []))
    if key == "posture_pillars":
        pa = (overview.get("azure_posture") or {}).get("pillar_avgs", {})
        return table_from_records([{"pillar": k, "score": v} for k, v in pa.items()])
    if key == "automations_status":
        by = (overview.get("automations") or {}).get("runs_by_status", {})
        return table_from_records([{"status": k, "count": v} for k, v in by.items()])
    if key == "totals":
        t = overview.get("totals", {})
        return table_from_records([{"metric": k, "value": v} for k, v in t.items()])
    return TableResult.from_error(f"Unknown telemetry key '{key}'.")


async def resolve_workbook_ref(
    cfg: dict[str, Any], conn, params, *, tenant_id: str = ""
) -> TableResult:
    """Latest run of a saved Workbook: its structured extract (as a table) + severity/narrative."""
    workbook_id = str(cfg.get("workbook_id") or "").strip()
    if not workbook_id:
        return TableResult.from_error("No workbook selected.")
    async with SessionLocal() as db:
        q = (
            select(WorkbookRun)
            .where(WorkbookRun.workbook_id == workbook_id)
            .order_by(desc(WorkbookRun.started_at))
            .limit(1)
        )
        run = (await db.execute(q)).scalars().first()
    if run is None:
        return TableResult(columns=[], rows=[], meta={"source": "workbook_ref", "status": "never"})
    meta = {
        "source": "workbook_ref",
        "severity": run.severity,
        "narrative": run.narrative or "",
        "status": run.status,
        "ran_at": run.started_at.isoformat() if run.started_at else None,
        "structured": run.structured_json or {},
    }
    structured = run.structured_json or {}
    # If the extract is a list of rows, present it as a table; if a dict, key/value rows.
    if isinstance(structured, dict) and isinstance(structured.get("rows"), list):
        return TableResult(meta=meta, **_records(structured["rows"]))
    if isinstance(structured, list):
        res = table_from_records(structured)
        res.meta.update(meta)
        return res
    if isinstance(structured, dict) and structured:
        res = table_from_records([{"field": k, "value": v} for k, v in structured.items()])
        res.meta.update(meta)
        return res
    # No structured data: a single severity/narrative row.
    return TableResult(
        columns=[Column("severity", "string"), Column("narrative", "string")],
        rows=[[run.severity, run.narrative or ""]],
        meta=meta,
    )


def _records(rows: list[Any]) -> dict[str, Any]:
    res = table_from_records(rows if isinstance(rows, list) else [])
    return {"columns": res.columns, "rows": res.rows}


async def resolve_static(cfg: dict[str, Any], conn, params) -> TableResult:
    """Inline data the author typed: {columns:[{name,type}], rows:[[...]]} or [records]."""
    cols = cfg.get("columns")
    rows = cfg.get("rows")
    if isinstance(cols, list) and isinstance(rows, list):
        columns = [Column(name=str(c.get("name", f"c{i}")), type=str(c.get("type", "string"))) for i, c in enumerate(cols)]
        return TableResult(columns=columns, rows=[list(r) for r in rows], meta={"source": "static"})
    if isinstance(rows, list):
        res = table_from_records(rows)
        res.meta["source"] = "static"
        return res
    return TableResult(meta={"source": "static"})

"""Execute a workbook: render → run → AI'fy → persist → (maybe) emit an event."""
from __future__ import annotations

import logging
import re
import time as _time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select

from app.core.db import SessionLocal
from app.exec.command_runner import run_command_capture, run_kql_capture
from app.models import WorkbookRun
from app.workbooks import aify as aify_mod
from app.workbooks import registry as wb_registry

logger = logging.getLogger("app.workbooks.executor")

_SEV_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def render_body(body: str, params: list[dict[str, Any]], values: dict[str, Any]) -> str:
    """Interpolate {{key}} placeholders using provided values (or param defaults)."""
    resolved: dict[str, str] = {}
    for p in params or []:
        key = p.get("key", "")
        if not key:
            continue
        val = values.get(key, p.get("default", ""))
        resolved[key] = "" if val is None else str(val)
    # Also allow ad-hoc values not declared as params.
    for k, v in (values or {}).items():
        resolved.setdefault(k, "" if v is None else str(v))

    def _sub(m: re.Match[str]) -> str:
        return resolved.get(m.group(1).strip(), "")

    return re.sub(r"\{\{\s*([\w.-]+)\s*\}\}", _sub, body or "")


async def _execute_and_aify(
    wb: dict[str, Any],
    *,
    params: dict[str, Any] | None,
    connection_id: str | None,
    confirm: bool,
) -> dict[str, Any]:
    """Render → run → AI'fy a workbook dict. Returns a result dict; persists nothing."""
    from app.core.azure_connections import resolve_connection

    conn_id = connection_id or wb.get("connection_id") or ""
    azure_conn = resolve_connection(conn_id or None)
    read_only = bool(azure_conn.get("read_only")) if azure_conn else True

    rendered = render_body(wb.get("body", ""), wb.get("params", []), params or {})
    runtime = wb.get("runtime", "az")

    started = _time.perf_counter()
    if runtime == "kql":
        cap = await run_kql_capture(rendered, azure_conn, output="json")
    elif runtime == "powershell":
        cmd = rendered if rendered.strip().lower().startswith(("powershell", "pwsh")) else (
            f'pwsh -NoProfile -Command "{rendered}"'
        )
        cap = await run_command_capture(cmd, azure_conn, read_only=read_only, confirm=confirm)
    else:  # az (or any allowlisted CLI)
        cap = await run_command_capture(rendered, azure_conn, read_only=read_only, confirm=confirm)
    duration_ms = int((_time.perf_counter() - started) * 1000)

    aify_cfg = wb.get("aify", {}) or {}
    narrative = ""
    structured: dict[str, Any] | None = None
    severity = "error" if not cap.ok else "info"
    if aify_cfg.get("enabled", True):
        modes = aify_cfg.get("modes", ["summary", "severity"])
        result = await aify_mod.aify_output(
            workbook_name=wb.get("name", ""),
            description=wb.get("description", ""),
            runtime=runtime,
            raw_output=cap.stdout,
            modes=modes,
            schema_hint=aify_cfg.get("schema", ""),
            error=cap.error if not cap.ok else "",
        )
        narrative = result.get("narrative", "")
        structured = result.get("structured")
        severity = result.get("severity", severity)
    else:
        narrative = cap.error if not cap.ok else (cap.stdout[:500] or "Completed.")

    return {
        "cap": cap,
        "azure_conn": azure_conn,
        "rendered": rendered,
        "runtime": runtime,
        "duration_ms": duration_ms,
        "narrative": narrative,
        "structured": structured,
        "severity": severity,
        "aify_cfg": aify_cfg,
    }


async def preview_workbook(
    wb: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    connection_id: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Execute an (unsaved) workbook draft and return its result WITHOUT persisting.

    Used by the editor's "Test run" so an author can see the output before saving.
    Diff vs previous run is computed only when the draft already has an id."""
    exec_res = await _execute_and_aify(
        wb, params=params, connection_id=connection_id, confirm=confirm
    )
    cap = exec_res["cap"]
    structured = exec_res["structured"]

    diff = None
    wb_id = wb.get("id")
    if wb_id and "diff" in (exec_res["aify_cfg"].get("modes", []) or []) and structured is not None:
        prev = await _previous_structured(wb_id)
        diff = aify_mod.compute_diff(prev, structured)

    return {
        "id": "",
        "workbook_id": wb_id or "",
        "workbook_name": wb.get("name", ""),
        "runtime": exec_res["runtime"],
        "command": exec_res["rendered"][:4000],
        "params": params or {},
        "trigger": "preview",
        "status": "succeeded" if cap.ok else "failed",
        "exit_code": cap.exit_code,
        "output": (cap.stdout or "")[:60_000],
        "structured": structured,
        "narrative": exec_res["narrative"],
        "severity": exec_res["severity"],
        "diff": diff,
        "error": None if cap.ok else (cap.error or "Run failed."),
        "duration_ms": exec_res["duration_ms"],
        "started_at": None,
        "ended_at": None,
    }


async def _previous_structured(workbook_id: str) -> dict[str, Any] | None:
    """The most recent successful run's structured extraction (for diffing)."""
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(WorkbookRun)
                .where(WorkbookRun.workbook_id == workbook_id, WorkbookRun.status == "succeeded")
                .order_by(desc(WorkbookRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        return row.structured_json if row else None


async def run_workbook(
    workbook_id: str,
    *,
    tenant_id: str,
    actor: str = "",
    params: dict[str, Any] | None = None,
    connection_id: str | None = None,
    trigger: str = "manual",
    confirm: bool = False,
) -> dict[str, Any]:
    """Run a workbook end-to-end and return the persisted run as a dict."""
    wb = wb_registry.get_workbook(workbook_id)
    if wb is None:
        raise ValueError("Workbook not found")

    exec_res = await _execute_and_aify(
        wb, params=params, connection_id=connection_id, confirm=confirm
    )
    cap = exec_res["cap"]
    azure_conn = exec_res["azure_conn"]
    rendered = exec_res["rendered"]
    runtime = exec_res["runtime"]
    duration_ms = exec_res["duration_ms"]
    narrative = exec_res["narrative"]
    structured = exec_res["structured"]
    severity = exec_res["severity"]
    aify_cfg = exec_res["aify_cfg"]

    # --- Diff vs previous run --------------------------------------------------
    diff = None
    if "diff" in (aify_cfg.get("modes", []) or []) and structured is not None:
        prev = await _previous_structured(workbook_id)
        diff = aify_mod.compute_diff(prev, structured)

    # --- Persist ---------------------------------------------------------------
    run = WorkbookRun(
        workbook_id=workbook_id,
        workbook_name=wb.get("name", ""),
        tenant_id=tenant_id,
        connection_id=azure_conn["id"] if azure_conn else None,
        runtime=runtime,
        command=rendered[:4000],
        params_json=params or {},
        trigger=trigger,
        status="succeeded" if cap.ok else "failed",
        exit_code=cap.exit_code,
        output=(cap.stdout or "")[:60_000],
        structured_json=structured,
        narrative=narrative,
        severity=severity,
        diff_json=diff,
        error=None if cap.ok else (cap.error or "Run failed."),
        duration_ms=duration_ms,
        triggered_by=actor,
        ended_at=_now(),
    )
    async with SessionLocal() as db:
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    run_dict = _run_to_dict(run)

    # --- Emit a notification event if the alert threshold is crossed -----------
    alert = wb.get("alert", {}) or {}
    if alert.get("enabled"):
        floor = _SEV_RANK.get(alert.get("min_severity", "warning"), 1)
        if _SEV_RANK.get(severity, 0) >= floor:
            try:
                from app.notifications.engine import publish

                await publish(
                    tenant_id=tenant_id,
                    type="workbook.failed" if not cap.ok else "workbook.severity",
                    source="workbook",
                    severity=severity,
                    title=f"{wb.get('name', 'Workbook')}: {severity}",
                    body=narrative or "Workbook threshold crossed.",
                    facts=structured if isinstance(structured, dict) else {},
                    links={"workbook_run": run_id, "workbook_id": workbook_id},
                    fingerprint=f"workbook:{workbook_id}:{severity}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Workbook %s event publish failed: %s", workbook_id, exc)

    return run_dict


def _run_to_dict(run: WorkbookRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "workbook_id": run.workbook_id,
        "workbook_name": run.workbook_name,
        "runtime": run.runtime,
        "command": run.command,
        "params": run.params_json,
        "trigger": run.trigger,
        "status": run.status,
        "exit_code": run.exit_code,
        "output": run.output,
        "structured": run.structured_json,
        "narrative": run.narrative,
        "severity": run.severity,
        "diff": run.diff_json,
        "error": run.error,
        "duration_ms": run.duration_ms,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }

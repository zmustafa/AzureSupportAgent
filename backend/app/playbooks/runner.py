"""Execute a playbook: run its steps in order with conditions + param mapping."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone
from typing import Any

from app.core.db import SessionLocal
from app.models import PlaybookRun
from app.playbooks import registry as pb_registry
from app.workbooks.executor import run_workbook

logger = logging.getLogger("app.playbooks.runner")

_SEV_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_params(
    step: dict[str, Any], results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Build a step's params from static values + mappings from prior step outputs."""
    params = dict(step.get("params") or {})
    for param_key, ref in (step.get("param_map") or {}).items():
        # ref form: "<stepId>.<structuredKey>"
        if not isinstance(ref, str) or "." not in ref:
            continue
        step_id, _, key = ref.partition(".")
        prev = results.get(step_id) or {}
        structured = prev.get("structured") or {}
        if isinstance(structured, dict) and key in structured:
            params[param_key] = structured[key]
    return params


async def run_playbook(
    playbook_id: str,
    *,
    tenant_id: str,
    actor: str = "",
    trigger: str = "manual",
) -> dict[str, Any]:
    """Run a playbook end-to-end. Returns {playbook, steps:[...], severity, status}."""
    pb = pb_registry.get_playbook(playbook_id)
    if pb is None:
        raise ValueError("Playbook not found")

    started = _time.perf_counter()
    results: dict[str, dict[str, Any]] = {}
    step_outcomes: list[dict[str, Any]] = []
    running_rank = 0
    overall_status = "succeeded"
    run_error: str | None = None

    for idx, step in enumerate(pb.get("steps", [])):
        step_id = step.get("id") or f"s{idx}"
        run_if = step.get("run_if", "always")
        # Conditional gating against the running severity.
        if run_if != "always":
            floor = _SEV_RANK.get(run_if, 0)
            if running_rank < floor:
                step_outcomes.append(
                    {
                        "step_id": step_id,
                        "name": step.get("name", ""),
                        "skipped": True,
                        "reason": f"running severity below '{run_if}'",
                    }
                )
                continue

        wb_id = step.get("workbook_id", "")
        if not wb_id:
            step_outcomes.append(
                {"step_id": step_id, "name": step.get("name", ""), "skipped": True, "reason": "no workbook"}
            )
            continue

        params = _resolve_params(step, results)
        try:
            run = await run_workbook(
                wb_id,
                tenant_id=tenant_id,
                actor=actor,
                params=params,
                connection_id=pb.get("connection_id") or None,
                trigger="playbook",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Playbook %s step %s failed: %s", playbook_id, step_id, exc)
            overall_status = "failed"
            run_error = str(exc)[:300]
            step_outcomes.append(
                {"step_id": step_id, "name": step.get("name", ""), "error": str(exc)[:300]}
            )
            break

        results[step_id] = run
        running_rank = max(running_rank, _SEV_RANK.get(run.get("severity", "info"), 0))
        if run.get("status") == "failed":
            overall_status = "failed"
        step_outcomes.append(
            {
                "step_id": step_id,
                "name": step.get("name", "") or run.get("workbook_name"),
                "workbook_id": wb_id,
                "run_id": run.get("id"),
                "severity": run.get("severity"),
                "status": run.get("status"),
                "narrative": run.get("narrative"),
            }
        )

    rank_to_sev = {v: k for k, v in _SEV_RANK.items()}
    severity = rank_to_sev.get(running_rank, "info")
    duration_ms = int((_time.perf_counter() - started) * 1000)

    # Persist the run for history (never overwritten).
    run_id = ""
    try:
        async with SessionLocal() as db:
            row = PlaybookRun(
                playbook_id=playbook_id,
                playbook_name=pb.get("name", ""),
                tenant_id=tenant_id,
                connection_id=pb.get("connection_id") or None,
                trigger=trigger,
                status=overall_status,
                severity=severity,
                steps_json=step_outcomes,
                step_count=len(step_outcomes),
                error=run_error,
                duration_ms=duration_ms,
                triggered_by=actor,
                ended_at=_now(),
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            run_id = row.id
    except Exception as exc:  # noqa: BLE001 - history is best-effort, never block a run
        logger.warning("Playbook %s run persist failed: %s", playbook_id, exc)

    # Emit an event if configured.
    alert = pb.get("alert", {}) or {}
    if alert.get("enabled"):
        floor = _SEV_RANK.get(alert.get("min_severity", "warning"), 1)
        if running_rank >= floor:
            try:
                from app.notifications.engine import publish

                await publish(
                    tenant_id=tenant_id,
                    type="playbook.completed",
                    source="playbook",
                    severity=severity,
                    title=f"Playbook: {pb.get('name', 'Playbook')} ({severity})",
                    body="; ".join(
                        f"{s.get('name')}: {s.get('severity', s.get('reason', 'n/a'))}"
                        for s in step_outcomes
                    )[:2000],
                    facts={"steps": len(step_outcomes)},
                    links={"playbook_id": playbook_id},
                    fingerprint=f"playbook:{playbook_id}:{severity}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Playbook %s event publish failed: %s", playbook_id, exc)

    return {
        "playbook_id": playbook_id,
        "name": pb.get("name", ""),
        "status": overall_status,
        "severity": severity,
        "steps": step_outcomes,
        "run_id": run_id,
        "duration_ms": duration_ms,
    }

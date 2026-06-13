"""Schedule target registry — what a ScheduledTask invokes when it fires.

A ScheduledTask has a ``target_type`` (agent | assessment | workbook | playbook) and a
``target_config`` payload. The scheduler is target-agnostic: it finds due tasks and asks
the matching target to execute. Each target returns an :class:`ExecResult` describing the
outcome (status, summary, and a deep-link ``result_ref`` to the produced artifact) which
the scheduler records on a TaskRun row. Adding a new schedulable kind is one class here.

Targets reuse the existing executors:
- agent      -> app.automations.runner.run_task (chat-thread agent turn)
- assessment -> app.assessments.runner.run_assessment (Well-Architected scoring)
- workbook   -> app.workbooks.executor.run_workbook (az/KQL/PowerShell + AI'fy)
- playbook   -> app.playbooks.runner.run_playbook (chained workbooks)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("app.automations.targets")

TARGET_TYPES = ("agent", "assessment", "workbook", "playbook", "radar")

TARGET_META: dict[str, dict[str, str]] = {
    "agent": {"label": "Sub Agent", "icon": "🤖"},
    "assessment": {"label": "Assessment", "icon": "🛡️"},
    "workbook": {"label": "Workbook", "icon": "📓"},
    "playbook": {"label": "Playbook", "icon": "📋"},
    "radar": {"label": "Retirement Radar", "icon": "📡"},
}


@dataclass
class ExecResult:
    status: str  # succeeded | failed | skipped
    summary: str = ""
    error: str | None = None
    result_ref: dict[str, Any] | None = None  # {kind, id} deep-link to the artifact
    thread_id: str | None = None  # agent runs produce a chat thread


class Target:
    """Base strategy for a schedulable target type."""

    type_name: str = ""

    def validate(self, cfg: dict[str, Any]) -> str | None:
        """Return an error string if the config is invalid, else None."""
        raise NotImplementedError

    def label(self, cfg: dict[str, Any]) -> str:
        """A human-readable description of what will be invoked."""
        raise NotImplementedError

    async def execute(self, task: Any) -> ExecResult:
        """Run the target for a ScheduledTask ORM row. Must not raise; capture errors."""
        raise NotImplementedError


# --------------------------------------------------------------------- agent
class AgentTarget(Target):
    type_name = "agent"

    def validate(self, cfg: dict[str, Any]) -> str | None:
        # Agent tasks may reference an agent_id on the task row itself (legacy) or in cfg.
        return None

    def label(self, cfg: dict[str, Any]) -> str:
        from app.automations import agents as agents_registry

        agent = agents_registry.get_agent(cfg.get("agent_id") or "") or {}
        name = agent.get("name") or "agent"
        return f"Run sub agent: {name}"

    async def execute(self, task: Any) -> ExecResult:
        # Delegates to the established agent runner, which already creates a chat thread,
        # records a TaskRun, advances the lifecycle, and notifies. To avoid double
        # bookkeeping we let run_task own the TaskRun; the scheduler treats agent as a
        # special case (see scheduler._dispatch). This method is therefore unused for the
        # agent path but defined for interface completeness.
        from app.automations.runner import run_task

        run_id = await run_task(task.id, trigger="schedule")
        return ExecResult(status="succeeded", result_ref={"kind": "task_run", "id": run_id})


# ----------------------------------------------------------------- assessment
class AssessmentTarget(Target):
    type_name = "assessment"

    def validate(self, cfg: dict[str, Any]) -> str | None:
        if not cfg.get("workload_ids") and not cfg.get("workload_id"):
            return "Select at least one workload for the assessment schedule."
        from app.assessments import catalog

        pillars = [p for p in (cfg.get("pillars") or []) if p in catalog.PILLARS]
        if not pillars:
            return "Select at least one assessment pillar."
        return None

    def label(self, cfg: dict[str, Any]) -> str:
        wids = cfg.get("workload_ids") or ([cfg["workload_id"]] if cfg.get("workload_id") else [])
        pillars = cfg.get("pillars") or []
        return f"Assess {len(wids)} workload(s) · {', '.join(pillars) or 'all pillars'}"

    async def execute(self, task: Any) -> ExecResult:
        from app.assessments import catalog
        from app.assessments.runner import run_assessment

        cfg = task.target_config or {}
        wids = cfg.get("workload_ids") or ([cfg["workload_id"]] if cfg.get("workload_id") else [])
        pillars = [p for p in (cfg.get("pillars") or []) if p in catalog.PILLARS] or list(catalog.PILLARS)
        use_ai = bool(cfg.get("use_ai", True))
        conn = cfg.get("connection_id") or task.connection_id or None

        run_ids: list[str] = []
        scores: list[int] = []
        errors: list[str] = []
        for wid in wids:
            done_payload: dict | None = None
            try:
                async for ev in run_assessment(
                    workload_id=wid,
                    pillars=pillars,
                    tenant_id=task.tenant_id,
                    connection_id=conn,
                    actor=f"schedule:{task.id}",
                    trigger="schedule",
                    use_ai=use_ai,
                ):
                    if ev.get("type") == "done":
                        done_payload = ev
                    elif ev.get("type") == "error":
                        errors.append(str(ev.get("message", "error")))
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
            if done_payload:
                if done_payload.get("run_id"):
                    run_ids.append(done_payload["run_id"])
                if done_payload.get("overall_score") is not None:
                    scores.append(int(done_payload["overall_score"]))
                await _maybe_alert_assessment(task, cfg, done_payload)

        if not run_ids and errors:
            return ExecResult(status="failed", error="; ".join(errors)[:1000])
        avg = round(sum(scores) / len(scores)) if scores else None
        summary = f"Assessed {len(run_ids)} workload(s)" + (f"; avg score {avg}/100" if avg is not None else "")
        # Deep-link to the single run, else the list.
        ref = {"kind": "assessment_run", "id": run_ids[0]} if len(run_ids) == 1 else {"kind": "assessment_runs", "ids": run_ids}
        return ExecResult(status="succeeded", summary=summary, result_ref=ref)


async def _maybe_alert_assessment(task: Any, cfg: dict[str, Any], done: dict) -> None:
    """Publish a notification when a scheduled assessment has new findings (per config)."""
    if not cfg.get("alert_on_new_findings", True):
        return
    diff = done.get("diff") or {}
    new_failures = diff.get("new_failures") or []
    if not new_failures:
        return
    sev_rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    min_sev = cfg.get("alert_min_severity", "warning")
    worst = "info"
    for nf in new_failures:
        sv = nf.get("severity", "warning") if isinstance(nf, dict) else "warning"
        if sev_rank.get(sv, 0) > sev_rank.get(worst, 0):
            worst = sv
    if sev_rank.get(worst, 0) < sev_rank.get(min_sev, 1):
        return
    names = [nf.get("title", "") if isinstance(nf, dict) else str(nf) for nf in new_failures][:5]
    try:
        from app.notifications.engine import publish

        await publish(
            tenant_id=task.tenant_id,
            type="assessment.new_findings",
            source="assessment",
            severity=worst,
            title=f"{len(new_failures)} new finding(s) from scheduled assessment '{task.name}'",
            body="New: " + ", ".join(names),
            facts={"new_count": len(new_failures)},
            links={"run_id": done.get("run_id")},
            fingerprint=f"assessment-sched:{task.id}",
        )
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------- workbook
class WorkbookTarget(Target):
    type_name = "workbook"

    def validate(self, cfg: dict[str, Any]) -> str | None:
        if not cfg.get("workbook_id"):
            return "Select a workbook to schedule."
        from app.workbooks import registry as wb_registry

        if wb_registry.get_workbook(cfg["workbook_id"]) is None:
            return "The selected workbook no longer exists."
        return None

    def label(self, cfg: dict[str, Any]) -> str:
        from app.workbooks import registry as wb_registry

        wb = wb_registry.get_workbook(cfg.get("workbook_id") or "") or {}
        return f"Run workbook: {wb.get('name', 'workbook')}"

    async def execute(self, task: Any) -> ExecResult:
        from app.workbooks.executor import run_workbook

        cfg = task.target_config or {}
        try:
            result = await run_workbook(
                cfg["workbook_id"],
                tenant_id=task.tenant_id,
                actor=f"schedule:{task.id}",
                params=cfg.get("params") or {},
                connection_id=cfg.get("connection_id") or task.connection_id or None,
                trigger="schedule",
                confirm=bool(cfg.get("confirm", False)),
            )
        except Exception as exc:  # noqa: BLE001
            return ExecResult(status="failed", error=str(exc)[:1000])
        status = "succeeded" if result.get("status") == "succeeded" else "failed"
        return ExecResult(
            status=status,
            summary=(result.get("narrative") or "")[:1000],
            error=result.get("error"),
            result_ref={"kind": "workbook_run", "id": result.get("id", "")},
        )


# ------------------------------------------------------------------- playbook
class PlaybookTarget(Target):
    type_name = "playbook"

    def validate(self, cfg: dict[str, Any]) -> str | None:
        if not cfg.get("playbook_id"):
            return "Select a playbook to schedule."
        from app.playbooks import registry as pb_registry

        if pb_registry.get_playbook(cfg["playbook_id"]) is None:
            return "The selected playbook no longer exists."
        return None

    def label(self, cfg: dict[str, Any]) -> str:
        from app.playbooks import registry as pb_registry

        pb = pb_registry.get_playbook(cfg.get("playbook_id") or "") or {}
        return f"Run playbook: {pb.get('name', 'playbook')}"

    async def execute(self, task: Any) -> ExecResult:
        from app.playbooks.runner import run_playbook

        cfg = task.target_config or {}
        try:
            result = await run_playbook(
                cfg["playbook_id"],
                tenant_id=task.tenant_id,
                actor=f"schedule:{task.id}",
                trigger="schedule",
            )
        except Exception as exc:  # noqa: BLE001
            return ExecResult(status="failed", error=str(exc)[:1000])
        status = "succeeded" if result.get("status") in ("succeeded", "ok", None) else "failed"
        steps = result.get("steps") or []
        summary = f"Ran {len(steps)} step(s); severity {result.get('severity', 'info')}"
        return ExecResult(
            status=status,
            summary=summary,
            result_ref={"kind": "playbook_run", "id": result.get("run_id") or cfg["playbook_id"], "playbook_id": cfg["playbook_id"]},
        )


# --------------------------------------------------------------------- radar
class RadarTarget(Target):
    """Runs the Retirement & Breaking-Change Radar on a cadence and pushes a digest of
    NEW + deadline-approaching items (only) via the notification engine (in-app + any
    matching connector channels). cfg: {workload_id|subscription_id, connection_id?}."""

    type_name = "radar"

    def validate(self, cfg: dict[str, Any]) -> str | None:
        if not (cfg.get("workload_id") or cfg.get("subscription_id")):
            return "Radar schedule needs a workload_id or subscription_id."
        return None

    def label(self, cfg: dict[str, Any]) -> str:
        scope = cfg.get("workload_id") or cfg.get("subscription_id") or "demo"
        return f"Retirement Radar digest ({scope})"

    async def execute(self, task: Any) -> ExecResult:
        from app.core.app_settings import load_settings
        from app.core.azure_connections import get_default_connection
        from app.radar import cache as radar_cache
        from app.radar import demo as radar_demo
        from app.radar import state as radar_state
        from app.radar.collector import collect_radar
        from app.radar.digest import current_tracking_ids, select_digest_items
        from app.workloads.registry import get_workload

        cfg = task.target_config or {}
        wid = cfg.get("workload_id")
        sid = cfg.get("subscription_id")
        scope_kind, scope_id = ("workload", wid) if wid else ("subscription", sid)
        tenant_id = task.tenant_id or "default"
        s = load_settings()
        lead = [int(x) for x in (s.get("radar_digest_lead_days") or [90, 60, 30]) if int(x) > 0] or [90, 60, 30]

        prev = radar_cache.read_snapshot(tenant_id, scope_kind, scope_id) or {}
        known_ids = set(current_tracking_ids(prev))

        try:
            if radar_demo.is_demo_scope(scope_kind, scope_id):
                snap = radar_demo.seed_demo(tenant_id=tenant_id)
            else:
                connection = get_default_connection()
                workload = get_workload(scope_id) if scope_kind == "workload" else None
                snap = await collect_radar(connection, scope_kind=scope_kind, scope_id=scope_id, workload=workload)
                radar_cache.write_snapshot(tenant_id, scope_kind, scope_id, snap)
        except Exception as exc:  # noqa: BLE001
            return ExecResult(status="failed", error=str(exc)[:1000])

        snap["events"] = radar_state.apply_states(tenant_id, list(snap.get("events", [])))
        sel = select_digest_items(snap, known_ids=known_ids, lead_days=lead)

        pushed = sel["new_count"] + sel["approaching_count"]
        if pushed or sel["models"]:
            await self._publish(task, scope_id, sel)
        return ExecResult(
            status="succeeded",
            summary=sel["summary"],
            result_ref={"kind": "radar", "scope_kind": scope_kind, "scope_id": scope_id},
        )

    async def _publish(self, task: Any, scope_id: str, sel: dict[str, Any]) -> None:
        from app.notifications.engine import publish

        lines = [
            f"- [{e['severity'].upper()}] {e['title']} — {e['retirement_date'] or 'TBD'}"
            + (f" ({e['days_until']}d)" if e.get("days_until") is not None else "")
            + f" · {e['impacted_count']} impacted · {e['reason']}"
            for e in sel["events"][:15]
        ]
        for m in sel["models"][:10]:
            lines.append(f"- [AI] {m['model']} {m.get('model_version', '')} retires {m.get('retirement_date', 'TBD')}")
        severity = "error" if any(e["severity"] == "red" for e in sel["events"]) else "warning"
        try:
            await publish(
                tenant_id=task.tenant_id or "default",
                type="radar.digest",
                source="radar",
                severity=severity,
                title=f"Retirement Radar: {sel['summary']}",
                body="\n".join(lines) or "No items.",
                facts={"scope_id": scope_id, "new": sel["new_count"], "approaching": sel["approaching_count"]},
                links={"radar": "/radar"},
            )
        except Exception:  # noqa: BLE001
            logger.warning("Radar digest publish failed", exc_info=True)


_REGISTRY: dict[str, Target] = {
    "agent": AgentTarget(),
    "assessment": AssessmentTarget(),
    "workbook": WorkbookTarget(),
    "playbook": PlaybookTarget(),
    "radar": RadarTarget(),
}


def get_target(target_type: str) -> Target:
    return _REGISTRY.get(target_type, _REGISTRY["agent"])


def validate_config(target_type: str, cfg: dict[str, Any]) -> str | None:
    if target_type not in _REGISTRY:
        return f"Unknown schedule type '{target_type}'."
    return _REGISTRY[target_type].validate(cfg or {})


def target_label(target_type: str, cfg: dict[str, Any]) -> str:
    try:
        return _REGISTRY.get(target_type, _REGISTRY["agent"]).label(cfg or {})
    except Exception:  # noqa: BLE001
        return TARGET_META.get(target_type, {}).get("label", target_type)

"""Execute a scheduled task as an agent turn.

A run resolves the task's custom agent (instructions, model, tenant, allowed tools,
run mode), creates or reuses a chat thread, runs the Orchestrator with the task prompt
plus connector + Azure MCP tools, and records a TaskRun row. The agent investigates and
then calls a connector tool (email/Teams/Jira/Grafana) to deliver the result — exactly
the SRE-Agent "trigger → investigate → act → notify" flow.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone
from typing import Any

from app.agent.orchestrator import Orchestrator
from app.automations import agents as agents_registry
from app.connectors.registry import build_toolset
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.utils import format_error
from app.models import AuditLog, Chat, Message, ScheduledTask, TaskRun, Usage

logger = logging.getLogger("app.automations.runner")
settings = get_settings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_task(task_id: str, trigger: str = "schedule") -> str:
    """Execute one run of a scheduled task. Returns the TaskRun id."""
    async with SessionLocal() as db:
        task = await db.get(ScheduledTask, task_id)
        if task is None:
            raise ValueError("Task not found")

        agent = agents_registry.get_agent(task.agent_id or "") or {}
        # Resolve provider/model + tenant: the agent's, falling back to globals.
        from app.core.llm_config import get_active

        provider = agent.get("provider") or ""
        model = agent.get("model") or ""
        _active = get_active(provider or None, model or None)
        turn_provider = _active.get("provider", "")
        turn_model = _active.get("model", "") or model

        # Tenant (Azure connection) for the run.
        from app.core.azure_connections import resolve_connection

        conn_id = task.connection_id or agent.get("connection_id") or ""
        azure_conn = resolve_connection(conn_id or None)

        # Connector toolset scoped to the agent's allowed connector tools.
        allowed = agent.get("connector_tools") if agent else None
        toolset = build_toolset(allowed)

        # Run mode → write policy override. Autonomous executes writes; review gates them.
        run_mode = task.run_mode or agent.get("run_mode") or "review"
        write_override = "off" if run_mode == "autonomous" else "gated"

        # Resolve/create the chat thread.
        if task.message_grouping == "same_thread" and task.thread_id:
            chat = await db.get(Chat, task.thread_id)
        else:
            chat = None
        if chat is None:
            chat = Chat(
                tenant_id=task.tenant_id,
                user_id=task.created_by or "scheduler",
                title=f"Scheduled: {task.name}",
                provider=turn_provider,
                model=turn_model,
                connection_id=azure_conn["id"] if azure_conn else None,
            )
            db.add(chat)
            await db.commit()
            await db.refresh(chat)
            if task.message_grouping == "same_thread":
                task.thread_id = chat.id
        chat_id = chat.id

        # Record the user (task prompt) + assistant messages.
        prompt = task.instructions or task.name
        db.add(Message(chat_id=chat_id, role="user", content=prompt))
        assistant = Message(
            chat_id=chat_id, role="assistant", content="", provider=turn_provider, model=turn_model
        )
        db.add(assistant)

        run = TaskRun(
            task_id=task.id,
            task_name=task.name,
            tenant_id=task.tenant_id,
            thread_id=chat_id,
            trigger=trigger,
            status="running",
        )
        db.add(run)
        task.last_run_at = _now()
        await db.commit()
        await db.refresh(assistant)
        await db.refresh(run)
        assistant_id = assistant.id
        run_id = run.id

    # Build the agent instructions (custom-agent persona).
    extra_instructions = None
    if agent.get("instructions"):
        extra_instructions = (
            "You are running as a scheduled automation (custom agent: "
            f"{agent.get('name', 'unnamed')}). Follow these instructions:\n"
            f"{agent['instructions']}\n\n"
            "After investigating, use the available connector tool(s) to deliver the "
            "result (e.g. send an email or post to Teams) as the instructions require."
        )

    started = _time.perf_counter()
    orchestrator = Orchestrator(
        settings,
        provider=turn_provider,
        model=turn_model,
        connection=azure_conn,
        connector_toolset=toolset,
        extra_instructions=extra_instructions,
        write_policy_override=write_override,
    )
    assistant_text = ""
    activity: list[dict[str, Any]] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    error: str | None = None
    try:
        history = [{"role": "user", "content": prompt}]
        async for ev in orchestrator.run(history):
            if ev.type == "token":
                assistant_text += ev.data["text"]
            elif ev.type == "done":
                usage["prompt_tokens"] = ev.data.get("prompt_tokens", 0)
                usage["completion_tokens"] = ev.data.get("completion_tokens", 0)
                if ev.data.get("content"):
                    assistant_text = ev.data["content"] or assistant_text
            elif ev.type in ("tool_start", "approval_required"):
                activity.append(
                    {
                        "kind": "tool",
                        "name": ev.data.get("tool_name"),
                        "args": ev.data.get("arguments", {}),
                        "status": "awaiting_approval"
                        if ev.type == "approval_required"
                        else "running",
                    }
                )
            elif ev.type == "tool_result":
                for step in reversed(activity):
                    if step.get("kind") == "tool" and step.get("status") == "running":
                        step["status"] = "done"
                        step["summary"] = ev.data.get("summary")
                        step["duration"] = ev.data.get("duration_ms")
                        break
            elif ev.type == "error":
                error = ev.data.get("message")
    except Exception as exc:  # noqa: BLE001
        error = format_error(exc)
        logger.warning("Scheduled task %s failed: %s", task_id, error)
    finally:
        orchestrator.close()

    duration_ms = int((_time.perf_counter() - started) * 1000)

    # Persist results + advance the task lifecycle.
    async with SessionLocal() as db:
        assistant = await db.get(Message, assistant_id)
        if assistant is not None:
            # Never leave a blank (or punctuation-only) assistant bubble — it looks like
            # an "empty thread". Require some real content; otherwise show a clear note.
            meaningful = sum(1 for ch in (assistant_text or "") if ch.isalnum()) >= 8
            fallback = (
                f"⚠️ {error}"
                if error
                else "⚠️ The agent finished without producing a result. It may have "
                "stopped on a tool error or a gated write. Check the activity log below "
                "or re-run the task."
            )
            assistant.content = assistant_text.strip() if meaningful else fallback
            assistant.activity_json = activity or None
            assistant.duration_ms = duration_ms
        notify_ids: list[str] = []
        task_name = ""
        task_tenant = ""
        run = await db.get(TaskRun, run_id)
        if run is not None:
            run.status = "failed" if error else "succeeded"
            run.summary = (assistant_text or "")[:1000]
            run.error = error
            run.ended_at = _now()
            run.duration_ms = duration_ms
        task = await db.get(ScheduledTask, task_id)
        if task is not None:
            task.completed_runs = (task.completed_runs or 0) + 1
            # Capture notify targets + name before the session closes (used below).
            notify_ids = list(task.notify_connector_ids or [])
            task_name = task.name
            task_tenant = task.tenant_id
            # Compute next run (or end the task).
            from app.automations.schedule import compute_next_run

            task_dict = _task_to_dict(task)
            reached_limit = task.max_runs is not None and task.completed_runs >= task.max_runs
            nxt = None if reached_limit else compute_next_run(task_dict)
            task.next_run_at = nxt
            if nxt is None and task.status == "on":
                task.status = "ended"
            if error:
                # Keep running on schedule but surface failure.
                logger.info("Task %s run failed; next run at %s", task_id, nxt)
        db.add(
            Usage(
                tenant_id=task.tenant_id if task else "",
                user_id="scheduler",
                chat_id=chat_id,
                provider=turn_provider or None,
                model=turn_model,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
            )
        )
        db.add(
            AuditLog(
                tenant_id=task.tenant_id if task else "",
                actor_id="scheduler",
                action="task.run",
                target=task_id,
                provider=turn_provider,
                model=turn_model,
                metadata_json={"trigger": trigger, "status": "failed" if error else "succeeded"},
            )
        )
        await db.commit()

    # Deliver the result to the task's selected notification connectors (if any). Runs
    # after the DB commit so a notify failure never rolls back the recorded run.
    if notify_ids:
        summary = (assistant_text or "").strip() or (error or "Task completed.")
        try:
            from app.connectors.notify import deliver_task_result

            results = await deliver_task_result(
                notify_ids, f"Scheduled task: {task_name}", summary[:3000], bool(error)
            )
            delivered = sum(1 for r in results if r.get("ok"))
            logger.info("Task %s notified %d/%d connectors", task_id, delivered, len(results))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Task %s notify dispatch failed: %s", task_id, exc)

    # Publish to the notification engine so rule-based routing + the in-app center see
    # every task outcome (independent of the task's own notify_connector_ids).
    try:
        from app.notifications.engine import publish

        await publish(
            tenant_id=task_tenant or "",
            type="task.failed" if error else "task.succeeded",
            source="task",
            severity="error" if error else "info",
            title=f"Scheduled task {'failed' if error else 'succeeded'}: {task_name}",
            body=((assistant_text or "").strip() or (error or "Task completed."))[:2000],
            facts={"trigger": trigger},
            links={"thread_id": chat_id, "task_id": task_id},
            fingerprint=f"task:{task_id}:{'failed' if error else 'ok'}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Task %s event publish failed: %s", task_id, exc)
    return run_id


def _task_to_dict(task: ScheduledTask) -> dict[str, Any]:
    return {
        "schedule_kind": task.schedule_kind,
        "cron_expr": task.cron_expr,
        "time_of_day": task.time_of_day,
        "weekday": task.weekday,
        "timezone": task.timezone,
        "start_date": task.start_date,
        "end_date": task.end_date,
    }


async def run_target_task(task_id: str, trigger: str = "schedule") -> str:
    """Execute one run of a non-agent scheduled task (assessment/workbook/playbook).

    Creates a TaskRun, dispatches to the matching target executor, advances the task
    lifecycle (next_run_at / completed_runs / end), records audit + a notification, and
    delivers to the task's notify connectors. Returns the TaskRun id."""
    from app.automations.schedule import compute_next_run
    from app.automations.targets import get_target

    async with SessionLocal() as db:
        task = await db.get(ScheduledTask, task_id)
        if task is None:
            raise ValueError("Task not found")
        run = TaskRun(
            task_id=task.id,
            task_name=task.name,
            tenant_id=task.tenant_id,
            target_type=task.target_type or "agent",
            trigger=trigger,
            status="running",
        )
        db.add(run)
        task.last_run_at = _now()
        await db.commit()
        await db.refresh(run)
        run_id = run.id
        target_type = task.target_type or "agent"
        task_name = task.name
        task_tenant = task.tenant_id
        notify_ids = list(task.notify_connector_ids or [])

    started = _time.perf_counter()
    target = get_target(target_type)
    # Re-fetch a live ORM row for the executor (targets read task.target_config etc.).
    async with SessionLocal() as db:
        task = await db.get(ScheduledTask, task_id)
        result = await target.execute(task)
    duration_ms = int((_time.perf_counter() - started) * 1000)
    error = result.error if result.status == "failed" else None

    async with SessionLocal() as db:
        run = await db.get(TaskRun, run_id)
        if run is not None:
            run.status = result.status
            run.summary = (result.summary or "")[:2000]
            run.error = error
            run.result_ref = result.result_ref
            run.thread_id = result.thread_id
            run.ended_at = _now()
            run.duration_ms = duration_ms
        task = await db.get(ScheduledTask, task_id)
        if task is not None:
            task.completed_runs = (task.completed_runs or 0) + 1
            reached_limit = task.max_runs is not None and task.completed_runs >= task.max_runs
            nxt = None if reached_limit else compute_next_run(_task_to_dict(task))
            task.next_run_at = nxt
            if nxt is None and task.status == "on":
                task.status = "ended"
        db.add(
            AuditLog(
                tenant_id=task_tenant or "",
                actor_id="scheduler",
                action="task.run",
                target=task_id,
                metadata_json={"trigger": trigger, "target_type": target_type, "status": result.status},
            )
        )
        await db.commit()

    # Deliver to notify connectors (best-effort, after commit).
    if notify_ids:
        summary = (result.summary or "").strip() or (error or "Task completed.")
        try:
            from app.connectors.notify import deliver_task_result

            await deliver_task_result(
                notify_ids, f"Scheduled {target_type}: {task_name}", summary[:3000], bool(error)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Task %s notify dispatch failed: %s", task_id, exc)

    try:
        from app.notifications.engine import publish

        await publish(
            tenant_id=task_tenant or "",
            type="task.failed" if error else "task.succeeded",
            source="task",
            severity="error" if error else "info",
            title=f"Scheduled {target_type} {'failed' if error else 'succeeded'}: {task_name}",
            body=((result.summary or "").strip() or (error or "Task completed."))[:2000],
            facts={"trigger": trigger, "target_type": target_type},
            links={"task_id": task_id, **(result.result_ref or {})},
            fingerprint=f"task:{task_id}:{'failed' if error else 'ok'}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Task %s event publish failed: %s", task_id, exc)
    return run_id


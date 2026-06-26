"""SQLAlchemy ORM models.

Mirrors the data model in docs/TECHNICAL_SPEC.md §5. Strict tenant scoping is
enforced at the query layer (every read filters by tenant_id).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(256), default="New Chat")
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AI provider + model this chat uses (persisted so each chat keeps its own model).
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Azure connection (tenant) this chat is bound to (persisted so it sticks).
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Reasoning effort for this chat: "normal" (standard turn) or "deep" (structured
    # multi-phase deep investigation). Persisted so it stays on for subsequent messages.
    thinking_level: Mapped[str] = mapped_column(String(16), default="normal")
    # Optional custom agent (persona + tools + model) applied to this chat's turns.
    # When set, the chat runs as that agent; when None it's the default assistant.
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Optional Azure Workload (a hand-picked scope of resources) this chat is scoped to.
    workload_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    archived: Mapped[bool] = mapped_column(default=False)
    pinned: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan", order_by="Message.created_at"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(32))  # user | assistant | system | tool
    content: Mapped[str] = mapped_column(Text, default="")
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    # Activity timeline (reasoning + tool calls) captured for assistant messages so
    # it survives navigation/reload. List of step dicts; null for user messages.
    activity_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Base64 data-URL images attached to a user message (vision input). Persisted so
    # they survive reload/navigation and are re-sent to the model on later turns.
    images_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Provider/model that produced an assistant message (for attribution in the UI).
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Wall-clock processing time for an assistant turn, in milliseconds.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Structured deep-investigation result (phases + hypothesis tree + conclusion),
    # persisted for assistant messages produced in "deep" mode so the investigation
    # panel survives reload. Null for normal turns.
    investigation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    chat: Mapped["Chat"] = relationship(back_populates="messages")


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    message_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    chat_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    tool_name: Mapped[str] = mapped_column(String(256))
    arguments_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="read")  # read | write
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # pending | running | succeeded | failed | awaiting_approval | rejected
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tool_call_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    approver_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    actor_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(128))
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # AI provider + model active when the action occurred (for accountability).
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Usage(Base):
    __tablename__ = "usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    chat_id: Mapped[str] = mapped_column(String(36), index=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ScheduledTask(Base):
    """A recurring automation: trigger → custom agent → tools → notify.

    The schedule is either a simple recurrence (daily/weekly + time) or a custom cron
    expression. Each run creates a chat thread and a TaskRun row (history preserved
    across edits)."""

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    instructions: Mapped[str] = mapped_column(Text, default="")
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # What this schedule invokes. Default 'agent' so existing rows keep their behavior.
    target_type: Mapped[str] = mapped_column(String(16), default="agent")  # agent|assessment|workbook|playbook
    target_config: Mapped[dict] = mapped_column(JSON, default=dict)  # type-specific payload
    # Scheduling.
    schedule_kind: Mapped[str] = mapped_column(String(16), default="daily")  # daily|weekly|cron
    cron_expr: Mapped[str | None] = mapped_column(String(128), nullable=True)
    time_of_day: Mapped[str | None] = mapped_column(String(8), nullable=True)  # "08:00"
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0=Mon..6=Sun
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Behavior.
    run_mode: Mapped[str] = mapped_column(String(16), default="review")  # review|autonomous
    message_grouping: Mapped[str] = mapped_column(String(16), default="new_thread")  # new_thread|same_thread
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Connector ids to deliver the run result to after each run (notification targets).
    notify_connector_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Lifecycle.
    status: Mapped[str] = mapped_column(String(16), default="on")  # on|off|ended|failed|deleted
    completed_runs: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TaskRun(Base):
    """One execution of a scheduled task (history; never overwritten on edit)."""

    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    task_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_type: Mapped[str] = mapped_column(String(16), default="agent")  # agent|assessment|workbook|playbook
    # Deep-link to the produced artifact: {kind, id} (assessment run, workbook run, etc.)
    result_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")  # schedule|manual
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # queued|running|succeeded|failed|skipped
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MissionRun(Base):
    """One Workload Mission Control run — a single sweep that runs many per-workload
    analyses (architecture, memory, assessment, monitoring/telemetry/backup coverage,
    performance, radar) and rolls their outcomes into a readiness verdict.

    History is preserved so missions can be diffed over time. ``systems_json`` holds the
    per-system outcomes (status, headline, deep-link ref); ``readiness`` is the go/warn/
    nogo rollup. The mission is driven by an in-process orchestrator that updates this row
    incrementally, so partial progress survives a crash."""

    __tablename__ = "mission_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    workload_id: Mapped[str] = mapped_column(String(36), index=True)
    workload_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|partial|failed|cancelled
    readiness: Mapped[str] = mapped_column(String(8), default="unknown")  # go|warn|nogo|unknown
    systems_total: Mapped[int] = mapped_column(Integer, default=0)
    systems_done: Mapped[int] = mapped_column(Integer, default=0)
    systems_attention: Mapped[int] = mapped_column(Integer, default=0)
    systems_json: Mapped[list] = mapped_column(JSON, default=list)  # [{key,label,status,headline,detail,score,link,result_ref,started_at,ended_at,error}]
    log_json: Mapped[list] = mapped_column(JSON, default=list)  # [{ts,key,message}] mission activity log (persisted so it reloads on reopen)
    force: Mapped[bool] = mapped_column(default=False)  # re-ran systems even if fresh
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(128), default="")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|schedule|fleet
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # soft-delete (trash)


class WorkbookRun(Base):
    """One execution of a workbook (az/KQL/PowerShell snippet) with AI'fied output.

    History is preserved (never overwritten) so runs can be charted and diffed over
    time. ``structured_json`` holds the AI-extracted schema; ``narrative`` the human
    summary; ``severity`` the AI classification used for dashboard tiles + events."""

    __tablename__ = "workbook_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workbook_id: Mapped[str] = mapped_column(String(36), index=True)
    workbook_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    runtime: Mapped[str] = mapped_column(String(16), default="az")  # az|kql|powershell
    command: Mapped[str | None] = mapped_column(Text, nullable=True)  # rendered command
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|schedule|playbook|tile
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)  # raw stdout (truncated)
    structured_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # AI extract
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)  # AI summary
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info|warning|error|critical
    diff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # change vs previous run
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(128), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlaybookRun(Base):
    """One execution of a playbook (a chained sequence of workbook steps).

    History is preserved so playbook runs can be reviewed over time. ``steps_json``
    holds the per-step outcomes (id, name, workbook run id, severity, status,
    narrative, or skip reason); ``severity`` is the worst step severity."""

    __tablename__ = "playbook_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    playbook_id: Mapped[str] = mapped_column(String(36), index=True)
    playbook_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|schedule
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info|warning|error|critical
    steps_json: Mapped[list] = mapped_column(JSON, default=list)  # per-step outcomes
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(128), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssessmentRun(Base):
    """One assessment run against a workload (Security/Reliability pillars).

    History is preserved so per-pillar scores can be charted and runs diffed (drift).
    ``findings_json`` holds the per-check results; ``scores_json`` the 0-100 pillar
    scores; ``summary`` the AI executive narrative."""

    __tablename__ = "assessment_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workload_id: Mapped[str] = mapped_column(String(36), index=True)
    workload_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    pillars: Mapped[list] = mapped_column(JSON, default=list)  # ["security","reliability"]
    status: Mapped[str] = mapped_column(String(16), default="running")  # queued|running|succeeded|failed|cancelled
    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-100
    scores_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {pillar: {score, passed, failed, na, total}}
    totals_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {passed, failed, na, by_severity}
    severity: Mapped[str] = mapped_column(String(16), default="info")  # worst failing severity
    findings_json: Mapped[list] = mapped_column(JSON, default=list)  # per-check results
    resource_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # total resources scanned in scope
    resources_json: Mapped[list] = mapped_column(JSON, default=list)  # capped sample of scanned resources
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # AI executive summary
    used_ai: Mapped[bool] = mapped_column(default=False)
    catalog_version: Mapped[str | None] = mapped_column(String(32), nullable=True)  # control-catalog version
    schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # finding-result schema version
    completeness_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)  # % of applicable controls evaluated
    confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)  # high|medium|low result confidence
    baseline_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_baseline: Mapped[bool] = mapped_column(default=False)  # admin-pinned reference run
    diff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # change vs previous run
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(128), default="")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # soft-delete (trash)


class AssessmentWaiver(Base):
    """A risk acceptance / waiver suppressing a control finding for a workload.

    A waiver removes a check (optionally a single flagged resource) from the FAILED set
    and from scoring while active, with an auditable justification, approver, and expiry.
    Expired waivers stop applying automatically."""

    __tablename__ = "assessment_waivers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    workload_id: Mapped[str] = mapped_column(String(36), index=True)
    check_id: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(512), nullable=True)  # null = whole check
    justification: Mapped[str] = mapped_column(Text, default="")
    approver: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|revoked
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AssessmentFindingState(Base):
    """Ownership + lifecycle for a (workload, check) finding across runs.

    Persists assignment, status (open → in_progress → resolved → waived), due date,
    notes, and any linked remediation ticket — independent of individual run rows."""

    __tablename__ = "assessment_finding_state"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    workload_id: Mapped[str] = mapped_column(String(36), index=True)
    check_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|in_progress|resolved|waived|risk_accepted
    assignee: Mapped[str | None] = mapped_column(String(256), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ticket_connector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(128), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RbacScanRun(Base):
    """One completed RBAC (access review) refresh — a compact history point for drift.

    The heavy per-scope rows live in the file cache; this table keeps only the summary needed
    to chart movement and diff "new privileged access since last scan". ``privileged_keys_json``
    is the set of ``effectivePrincipal|role|scope`` keys present at scan time so a later run can
    compute added/removed privileged grants."""

    __tablename__ = "rbac_scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    scope: Mapped[str] = mapped_column(String(512), default="__all__")  # refreshed scope or __all__
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|scheduled
    status: Mapped[str] = mapped_column(String(16), default="succeeded")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    privileged_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_principals: Mapped[int] = mapped_column(Integer, default=0)
    kpis_json: Mapped[dict] = mapped_column(JSON, default=dict)
    scopes_json: Mapped[list] = mapped_column(JSON, default=list)  # per-scope summary
    privileged_keys_json: Mapped[list] = mapped_column(JSON, default=list)  # for drift diff
    diff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # change vs previous run
    demo: Mapped[bool] = mapped_column(default=False)
    triggered_by: Mapped[str] = mapped_column(String(128), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class QuotaScanRun(Base):
    """One completed quota scan over a subscription (optionally a subset of regions) — a
    compact history point for trend charting.

    The heavy per-quota rows live in the file cache (``quota_cache.json``); this table keeps
    only the run metadata + risk roll-up so movement can be charted and a later run can diff
    "what newly crossed into Warning/Critical". ``regions_json`` records the regions actually
    scanned; ``counts_json`` holds the per-risk roll-up; ``provider_errors_json`` the
    per-provider failures so a run's partial coverage is auditable."""

    __tablename__ = "quota_scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    subscription_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    subscription_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scope: Mapped[str] = mapped_column(String(512), default="")  # cache scope_id
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual only (no schedules)
    status: Mapped[str] = mapped_column(String(16), default="succeeded")  # succeeded|partial|failed
    regions_json: Mapped[list] = mapped_column(JSON, default=list)  # regions actually scanned
    categories_json: Mapped[list] = mapped_column(JSON, default=list)  # categories scanned
    total_results: Mapped[int] = mapped_column(Integer, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    watch_count: Mapped[int] = mapped_column(Integer, default=0)
    counts_json: Mapped[dict] = mapped_column(JSON, default=dict)  # full per-risk roll-up
    provider_errors_json: Mapped[list] = mapped_column(JSON, default=list)  # per-provider failures
    # The set of "{region}|{provider}|{quota}" keys at Warning+ so a later run diffs movement.
    risk_keys_json: Mapped[list] = mapped_column(JSON, default=list)
    diff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # change vs previous run
    demo: Mapped[bool] = mapped_column(default=False)
    triggered_by: Mapped[str] = mapped_column(String(128), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Notification(Base):
    """A normalized event published to the notification engine (in-app + channels)."""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)  # e.g. task.failed, workbook.severity
    source: Mapped[str] = mapped_column(String(64), default="")  # workbook|task|investigation|...
    severity: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    facts_json: Mapped[dict] = mapped_column(JSON, default=dict)
    links_json: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class NotificationDelivery(Base):
    """One channel delivery attempt for a notification (in-app or a connector)."""

    __tablename__ = "notification_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    notification_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    channel: Mapped[str] = mapped_column(String(64), default="")  # "in_app" | connector id
    channel_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|sent|failed
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationRule(Base):
    """A global routing rule: match events by type/severity/source → deliver to channels."""

    __tablename__ = "notification_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    # Match criteria.
    event_types: Mapped[list] = mapped_column(JSON, default=list)  # [] = any
    sources: Mapped[list] = mapped_column(JSON, default=list)  # [] = any
    min_severity: Mapped[str] = mapped_column(String(16), default="warning")
    # Targets.
    in_app: Mapped[bool] = mapped_column(default=True)
    connector_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VmRun(Base):
    """One command executed on a sandbox troubleshooting VM over SSH.

    History is preserved (never overwritten) so an admin can audit what the agent ran on
    each box. ``output``/``stderr`` are truncated; ``trigger`` records what initiated it."""

    __tablename__ = "vm_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    vm_id: Mapped[str] = mapped_column(String(36), index=True)
    vm_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    destructive: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed|timeout|blocked
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)  # stdout (truncated)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info|warning|error|critical
    trigger: Mapped[str] = mapped_column(String(16), default="chat")  # manual|chat|investigation
    chat_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Auth / access-control models (users, roles, groups, sessions, identity providers).
# Imported here so they register on Base.metadata for create_all / schema sync.
from app.models.auth import (  # noqa: E402,F401
    Group,
    IdentityProvider,
    Role,
    Session,
    User,
    UserGroup,
    UserRole,
)

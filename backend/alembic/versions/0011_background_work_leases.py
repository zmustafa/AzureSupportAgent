"""multi-replica leases for durable background work

Revision ID: 0011_background_work_leases
Revises: 0010_access_control_tenants
Create Date: 2026-08-30
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_background_work_leases"
down_revision: str | None = "0010_access_control_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEASE_COLUMNS = (
    sa.Column("lease_owner", sa.String(128), nullable=True),
    sa.Column("lease_token", sa.String(36), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
)


def _add_missing_columns(table: str, columns: tuple[sa.Column, ...]) -> None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column.copy())


def _create_index(name: str, table: str, columns: list[str]) -> None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return
    existing_columns = {column["name"] for column in inspector.get_columns(table)}
    if not set(columns).issubset(existing_columns):
        return
    if name not in {index["name"] for index in inspector.get_indexes(table)}:
        op.create_index(name, table, columns)


def _drop_index_if_present(name: str, table: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if table in inspector.get_table_names() and name in {
        index["name"] for index in inspector.get_indexes(table)
    }:
        op.drop_index(name, table_name=table)


_PERFORMANCE_INDEXES = (
    ("ix_tool_calls_tenant_created", "tool_calls", ["tenant_id", "created_at"]),
    (
        "ix_tool_calls_tenant_status_created",
        "tool_calls",
        ["tenant_id", "status", "created_at"],
    ),
    (
        "ix_approvals_tenant_decision_created",
        "approvals",
        ["tenant_id", "decision", "created_at"],
    ),
    (
        "ix_usage_tenant_provider_model",
        "usage",
        ["tenant_id", "provider", "model"],
    ),
    (
        "ix_usage_tenant_model_created",
        "usage",
        ["tenant_id", "model", "created_at"],
    ),
    (
        "ix_assessment_runs_tenant_deleted_started",
        "assessment_runs",
        ["tenant_id", "deleted_at", "started_at"],
    ),
    (
        "ix_assessment_runs_tenant_workload_deleted_started",
        "assessment_runs",
        ["tenant_id", "workload_id", "deleted_at", "started_at"],
    ),
    (
        "ix_cases_tenant_deleted_status_updated",
        "cases",
        ["tenant_id", "deleted_at", "status", "updated_at"],
    ),
    (
        "ix_case_events_tenant_case_created",
        "case_events",
        ["tenant_id", "case_id", "created_at"],
    ),
    (
        "ix_notifications_tenant_created",
        "notifications",
        ["tenant_id", "created_at"],
    ),
    (
        "ix_notification_deliveries_tenant_channel_notification",
        "notification_deliveries",
        ["tenant_id", "channel", "notification_id"],
    ),
    ("ix_sessions_revoked", "sessions", ["revoked"]),
    ("ix_sessions_expires", "sessions", ["expires_at"]),
    ("ix_sessions_last_seen", "sessions", ["last_seen_at"]),
    (
        "ix_rbac_scan_runs_tenant_started",
        "rbac_scan_runs",
        ["tenant_id", "started_at"],
    ),
    (
        "ix_quota_scan_runs_tenant_started",
        "quota_scan_runs",
        ["tenant_id", "started_at"],
    ),
)


def upgrade() -> None:
    # ``provider`` predates Alembic and was historically added by runtime schema sync.
    # Add it here as well so a migration-only existing install can build the usage index.
    _add_missing_columns(
        "usage", (sa.Column("provider", sa.String(64), nullable=True),)
    )
    _add_missing_columns("work_batch_items", _LEASE_COLUMNS)
    _add_missing_columns("perf_profile_fleet_items", _LEASE_COLUMNS)
    _add_missing_columns(
        "scheduled_tasks",
        _LEASE_COLUMNS
        + (sa.Column("lease_occurrence_at", sa.DateTime(timezone=True), nullable=True),),
    )
    _create_index(
        "ix_work_batch_items_claim",
        "work_batch_items",
        ["status", "lease_expires_at", "available_at"],
    )
    _create_index(
        "ix_perf_fleet_items_claim",
        "perf_profile_fleet_items",
        ["status", "lease_expires_at", "batch_id"],
    )
    _create_index(
        "ix_scheduled_tasks_due_claim",
        "scheduled_tasks",
        ["status", "next_run_at", "lease_expires_at"],
    )
    _create_index(
        "ix_scheduled_tasks_occurrence_claim",
        "scheduled_tasks",
        ["status", "lease_occurrence_at", "lease_expires_at"],
    )
    for name, table, columns in _PERFORMANCE_INDEXES:
        _create_index(name, table, columns)

    # The three-column delivery index has the old runtime-created two-column index as a
    # prefix, so retaining both only adds write and storage cost.
    _drop_index_if_present(
        "ix_notifdeliv_tenant_channel", "notification_deliveries"
    )


def downgrade() -> None:
    indexes = tuple(
        (name, table) for name, table, _columns in reversed(_PERFORMANCE_INDEXES)
    ) + (
        ("ix_scheduled_tasks_occurrence_claim", "scheduled_tasks"),
        ("ix_scheduled_tasks_due_claim", "scheduled_tasks"),
        ("ix_perf_fleet_items_claim", "perf_profile_fleet_items"),
        ("ix_work_batch_items_claim", "work_batch_items"),
    )
    for name, table in indexes:
        _drop_index_if_present(name, table)
    for table, extra in (
        ("scheduled_tasks", ("lease_occurrence_at",)),
        ("perf_profile_fleet_items", ()),
        ("work_batch_items", ()),
    ):
        inspector = sa.inspect(op.get_bind())
        if table not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name in (*extra, "lease_heartbeat_at", "lease_expires_at", "lease_token", "lease_owner"):
            if name in existing:
                op.drop_column(table, name)
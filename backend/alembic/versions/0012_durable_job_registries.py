"""durable generic jobs and chat turns

Revision ID: 0012_durable_job_registries
Revises: 0011_background_work_leases
Create Date: 2026-08-30
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_durable_job_registries"
down_revision: str | None = "0011_background_work_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_index(name: str, table: str, columns: list[str]) -> None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return
    if name not in {index["name"] for index in inspector.get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    # Runtime ``Base.metadata.create_all`` may have created these tables before Alembic runs
    # (for example a diagnostic revision that booted against an older alembic_version). Adopt
    # complete existing tables instead of failing the deployment with DuplicateTable.
    if "durable_jobs" not in tables:
        op.create_table(
            "durable_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("feature", sa.String(64), nullable=False),
            sa.Column("job_key", sa.String(512), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("owner_id", sa.String(128), nullable=True),
            sa.Column("lease_token", sa.String(36), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("next_event_seq", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index(
        "ix_durable_jobs_scope_started",
        "durable_jobs",
        ["tenant_id", "feature", "job_key", "started_at"],
    )
    _create_index(
        "ix_durable_jobs_active", "durable_jobs", ["tenant_id", "feature", "status"]
    )
    _create_index(
        "ix_durable_jobs_cleanup", "durable_jobs", ["status", "expires_at"]
    )

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "durable_job_slots" not in tables:
        op.create_table(
            "durable_job_slots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("feature", sa.String(64), nullable=False),
            sa.Column("job_key", sa.String(512), nullable=False),
            sa.Column("current_job_id", sa.String(36), nullable=True),
            sa.Column("lease_owner", sa.String(128), nullable=True),
            sa.Column("lease_token", sa.String(36), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["current_job_id"], ["durable_jobs.id"], ondelete="SET NULL"
            ),
            sa.UniqueConstraint(
                "tenant_id", "feature", "job_key", name="uq_durable_job_slot_scope"
            ),
        )
    _create_index(
        "ix_durable_job_slots_lease", "durable_job_slots", ["lease_expires_at"]
    )

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "durable_job_events" not in tables:
        op.create_table(
            "durable_job_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("job_id", sa.String(36), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("data_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["durable_jobs.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("job_id", "seq", name="uq_durable_job_event_seq"),
        )
    _create_index(
        "ix_durable_job_events_job_id", "durable_job_events", ["job_id"]
    )
    _create_index(
        "ix_durable_job_events_job_seq", "durable_job_events", ["job_id", "seq"]
    )


def downgrade() -> None:
    op.drop_index("ix_durable_job_events_job_seq", table_name="durable_job_events")
    op.drop_index("ix_durable_job_events_job_id", table_name="durable_job_events")
    op.drop_table("durable_job_events")
    op.drop_index("ix_durable_job_slots_lease", table_name="durable_job_slots")
    op.drop_table("durable_job_slots")
    op.drop_index("ix_durable_jobs_cleanup", table_name="durable_jobs")
    op.drop_index("ix_durable_jobs_active", table_name="durable_jobs")
    op.drop_index("ix_durable_jobs_scope_started", table_name="durable_jobs")
    op.drop_table("durable_jobs")
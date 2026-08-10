"""durable Performance Profiler fleet batches

Revision ID: 0007_perf_profile_fleet
Revises: 0006_case_principal
Create Date: 2026-08-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_perf_profile_fleet"
down_revision: str | None = "0006_case_principal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "perf_profile_fleet_batches" not in tables:
        op.create_table(
            "perf_profile_fleet_batches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("window", sa.String(64), nullable=False, server_default="P1D"),
            sa.Column("start_time", sa.String(64), nullable=False, server_default=""),
            sa.Column("end_time", sa.String(64), nullable=False, server_default=""),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("succeeded", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("partial", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cancelled", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("triggered_by", sa.String(128), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_perf_fleet_tenant_idempotency"),
        )
        op.create_index("ix_perf_profile_fleet_batches_tenant_id", "perf_profile_fleet_batches", ["tenant_id"])
        op.create_index("ix_perf_profile_fleet_batches_status", "perf_profile_fleet_batches", ["status"])
        op.create_index("ix_perf_fleet_tenant_created", "perf_profile_fleet_batches", ["tenant_id", "created_at"])
        op.create_index("ix_perf_fleet_status_created", "perf_profile_fleet_batches", ["status", "created_at"])

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "perf_profile_fleet_items" not in tables:
        op.create_table(
            "perf_profile_fleet_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "batch_id",
                sa.String(36),
                sa.ForeignKey("perf_profile_fleet_batches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("workload_id", sa.String(36), nullable=False),
            sa.Column("workload_name", sa.String(256), nullable=False, server_default=""),
            sa.Column("connection_id", sa.String(36), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("run_id", sa.String(36), nullable=True),
            sa.Column("resources_completed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("resources_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("collection_json", sa.JSON(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.UniqueConstraint("batch_id", "workload_id", name="uq_perf_fleet_batch_workload"),
        )
        op.create_index("ix_perf_profile_fleet_items_batch_id", "perf_profile_fleet_items", ["batch_id"])
        op.create_index("ix_perf_profile_fleet_items_tenant_id", "perf_profile_fleet_items", ["tenant_id"])
        op.create_index("ix_perf_profile_fleet_items_workload_id", "perf_profile_fleet_items", ["workload_id"])
        op.create_index("ix_perf_profile_fleet_items_status", "perf_profile_fleet_items", ["status"])
        op.create_index("ix_perf_fleet_items_batch_status", "perf_profile_fleet_items", ["batch_id", "status"])
        op.create_index("ix_perf_fleet_items_status_started", "perf_profile_fleet_items", ["status", "started_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "perf_profile_fleet_items" in tables:
        op.drop_table("perf_profile_fleet_items")
    if "perf_profile_fleet_batches" in tables:
        op.drop_table("perf_profile_fleet_batches")

"""generic durable work batches

Revision ID: 0008_generic_work_batches
Revises: 0007_perf_profile_fleet
Create Date: 2026-08-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_generic_work_batches"
down_revision: str | None = "0007_perf_profile_fleet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "work_batches" not in tables:
        op.create_table(
            "work_batches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("feature", sa.String(48), nullable=False),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("config_json", sa.JSON(), nullable=False),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("succeeded", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("partial", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cancelled", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("triggered_by", sa.String(128), nullable=False, server_default=""),
            sa.Column("trigger", sa.String(24), nullable=False, server_default="manual"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("tenant_id", "feature", "idempotency_key", name="uq_work_batch_tenant_feature_idempotency"),
        )
        op.create_index("ix_work_batches_tenant_id", "work_batches", ["tenant_id"])
        op.create_index("ix_work_batches_feature", "work_batches", ["feature"])
        op.create_index("ix_work_batches_status", "work_batches", ["status"])
        op.create_index("ix_work_batch_tenant_feature_created", "work_batches", ["tenant_id", "feature", "created_at"])
        op.create_index("ix_work_batch_status_created", "work_batches", ["status", "created_at"])

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "work_batch_items" not in tables:
        op.create_table(
            "work_batch_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("batch_id", sa.String(36), sa.ForeignKey("work_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("item_key", sa.String(256), nullable=False),
            sa.Column("workload_id", sa.String(128), nullable=True),
            sa.Column("workload_name", sa.String(256), nullable=False, server_default=""),
            sa.Column("connection_id", sa.String(128), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("progress_current", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("progress_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("message", sa.Text(), nullable=False, server_default=""),
            sa.Column("result_ref", sa.JSON(), nullable=True),
            sa.Column("result_json", sa.JSON(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.UniqueConstraint("batch_id", "item_key", name="uq_work_batch_item_key"),
        )
        op.create_index("ix_work_batch_items_batch_id", "work_batch_items", ["batch_id"])
        op.create_index("ix_work_batch_items_tenant_id", "work_batch_items", ["tenant_id"])
        op.create_index("ix_work_batch_items_workload_id", "work_batch_items", ["workload_id"])
        op.create_index("ix_work_batch_items_status", "work_batch_items", ["status"])
        op.create_index("ix_work_batch_items_batch_status", "work_batch_items", ["batch_id", "status"])
        op.create_index("ix_work_batch_items_status_started", "work_batch_items", ["status", "started_at"])
        op.create_index("ix_work_batch_items_status_available", "work_batch_items", ["status", "available_at"])
        op.create_index("ix_work_batch_items_lane", "work_batch_items", ["tenant_id", "connection_id", "status"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "work_batch_items" in tables:
        op.drop_table("work_batch_items")
    if "work_batches" in tables:
        op.drop_table("work_batches")
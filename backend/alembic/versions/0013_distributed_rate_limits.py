"""distributed principal rate-limit state

Revision ID: 0013_distributed_rate_limits
Revises: 0012_durable_job_registries
Create Date: 2026-08-30
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_distributed_rate_limits"
down_revision: str | None = "0012_durable_job_registries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "distributed_rate_limits" not in tables:
        op.create_table(
            "distributed_rate_limits",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("starts_json", sa.JSON(), nullable=False),
            sa.Column("blocked_until_epoch", sa.Float(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "changeexplorer_analysis_leases" not in tables:
        op.create_table(
            "changeexplorer_analysis_leases",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("lane_hash", sa.String(64), nullable=False),
            sa.Column("owner", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("lane_hash", name="uq_changeexplorer_analysis_leases_lane_hash"),
        )
        op.create_index(
            "ix_changeexplorer_analysis_leases_lane_hash",
            "changeexplorer_analysis_leases",
            ["lane_hash"],
        )
        op.create_index(
            "ix_changeexplorer_analysis_leases_expires_at",
            "changeexplorer_analysis_leases",
            ["expires_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_changeexplorer_analysis_leases_expires_at", table_name="changeexplorer_analysis_leases")
    op.drop_index("ix_changeexplorer_analysis_leases_lane_hash", table_name="changeexplorer_analysis_leases")
    op.drop_table("changeexplorer_analysis_leases")
    op.drop_table("distributed_rate_limits")
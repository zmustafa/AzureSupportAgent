"""add Alerts Manager approval and rollback ledger

Revision ID: 0003_alerts_manager_changes
Revises: 0002_message_activity
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_alerts_manager_changes"
down_revision: str | None = "0002_message_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_manager_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(512), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("risk", sa.String(16), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("desired_encrypted", sa.Text(), nullable=False),
        sa.Column("before_encrypted", sa.Text(), nullable=False),
        sa.Column("after_encrypted", sa.Text(), nullable=False),
        sa.Column("expected_state_hash", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("applied_by", sa.String(128), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("rollback_of", sa.String(36), nullable=True),
        sa.Column("evidence_id", sa.String(36), nullable=True),
        sa.Column("auto_apply", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_alert_manager_changes_tenant_id", "alert_manager_changes", ["tenant_id"])
    op.create_index("ix_alert_manager_changes_connection_id", "alert_manager_changes", ["connection_id"])
    op.create_index("ix_alert_manager_changes_target_id", "alert_manager_changes", ["target_id"])
    op.create_index("ix_alert_manager_changes_status", "alert_manager_changes", ["status"])
    op.create_index("ix_alert_manager_changes_rollback_of", "alert_manager_changes", ["rollback_of"])
    op.create_index("ix_alert_changes_tenant_requested", "alert_manager_changes", ["tenant_id", "requested_at"])
    op.create_index("ix_alert_changes_tenant_status", "alert_manager_changes", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_table("alert_manager_changes")
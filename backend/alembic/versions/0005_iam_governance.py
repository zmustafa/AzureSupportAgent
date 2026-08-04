"""add IAM access review campaigns and bounded run-row retention

Revision ID: 0005_iam_governance
Revises: 0004_backup_manager
Create Date: 2026-08-01
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_iam_governance"
down_revision: str | None = "0004_backup_manager"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "iam_review_campaign",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("selector_json", sa.JSON(), nullable=False),
        sa.Column("baseline_run_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("reviewer_strategy", sa.String(24), nullable=False, server_default="owner"),
        sa.Column("reviewer_fallback_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_days", sa.JSON(), nullable=False),
        sa.Column("stats_json", sa.JSON(), nullable=False),
        sa.Column("evidence_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_iam_review_campaign_tenant_id", "iam_review_campaign", ["tenant_id"])
    op.create_index("ix_iam_review_campaign_status", "iam_review_campaign", ["status"])

    op.create_table(
        "iam_review_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("row_key", sa.String(512), nullable=False),
        sa.Column("row_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("reviewer_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("reviewer_source", sa.String(24), nullable=False, server_default=""),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("decided_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delegated_to", sa.String(128), nullable=False, server_default=""),
        sa.Column("changed_since_baseline", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("remediation_state", sa.String(24), nullable=False, server_default="none"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("campaign_id", "row_key", name="uq_iam_review_item"),
    )
    op.create_index("ix_iam_review_item_campaign_id", "iam_review_item", ["campaign_id"])
    op.create_index("ix_iam_review_item_tenant_id", "iam_review_item", ["tenant_id"])
    op.create_index("ix_iam_review_item_reviewer_id", "iam_review_item", ["reviewer_id"])

    # Bounded row retention on the run history: the newest run plus anything pinned.
    _extend_rbac_scan_runs()


# `rbac_scan_runs` is NOT created by any migration — it is owned by the app's `create_all()`
# at startup. On a FRESH database `alembic upgrade head` runs BEFORE the app boots, so the
# table does not exist yet and an unguarded ALTER kills the container with
# `UndefinedTableError: relation "rbac_scan_runs" does not exist`. When the table is absent,
# `create_all()` will later build it with these columns already present, so skipping is correct.
def _extend_rbac_scan_runs() -> None:
    inspector = sa.inspect(op.get_bind())
    if "rbac_scan_runs" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("rbac_scan_runs")}
    with op.batch_alter_table("rbac_scan_runs") as batch:
        if "rows_json" not in existing:
            batch.add_column(sa.Column("rows_json", sa.JSON(), nullable=True))
        if "pinned" not in existing:
            batch.add_column(sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "pin_reason" not in existing:
            batch.add_column(sa.Column("pin_reason", sa.String(256), nullable=False, server_default=""))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "rbac_scan_runs" in inspector.get_table_names():
        with op.batch_alter_table("rbac_scan_runs") as batch:
            batch.drop_column("pin_reason")
            batch.drop_column("pinned")
            batch.drop_column("rows_json")
    op.drop_table("iam_review_item")
    op.drop_table("iam_review_campaign")

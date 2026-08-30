"""per-user recently visited items

Revision ID: 0009_recent_items
Revises: 0008_generic_work_batches
Create Date: 2026-08-29
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_recent_items"
down_revision: str | None = "0008_generic_work_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "recent_items" in tables:
        return
    op.create_table(
        "recent_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("item_key", sa.String(256), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("subtitle", sa.String(256), nullable=False, server_default=""),
        sa.Column("route", sa.String(1024), nullable=False),
        sa.Column("connection_id", sa.String(128), nullable=True),
        sa.Column("workload_id", sa.String(128), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("visit_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_visited_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "kind", "item_key",
            name="uq_recent_item_user_kind_key",
        ),
    )
    op.create_index("ix_recent_items_tenant_id", "recent_items", ["tenant_id"])
    op.create_index("ix_recent_items_user_id", "recent_items", ["user_id"])
    op.create_index(
        "ix_recent_items_user_visited",
        "recent_items", ["tenant_id", "user_id", "last_visited_at"],
    )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "recent_items" in tables:
        op.drop_table("recent_items")
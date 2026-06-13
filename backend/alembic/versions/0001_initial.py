"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False, index=True),
        sa.Column("user_id", sa.String(128), nullable=False, index=True),
        sa.Column("title", sa.String(256), nullable=False, server_default="New Chat"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chat_id", sa.String(36), sa.ForeignKey("chats.id", ondelete="CASCADE"), index=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), nullable=True, index=True),
        sa.Column("chat_id", sa.String(36), nullable=False, index=True),
        sa.Column("tenant_id", sa.String(128), nullable=False, index=True),
        sa.Column("tool_name", sa.String(256), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("subscription_id", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="read"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tool_call_id", sa.String(36), nullable=False, index=True),
        sa.Column("tenant_id", sa.String(128), nullable=False, index=True),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("approver_id", sa.String(128), nullable=True),
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False, index=True),
        sa.Column("actor_id", sa.String(128), nullable=False, index=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target", sa.String(512), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False, index=True),
        sa.Column("user_id", sa.String(128), nullable=False, index=True),
        sa.Column("chat_id", sa.String(36), nullable=False, index=True),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("usage")
    op.drop_table("audit_log")
    op.drop_table("approvals")
    op.drop_table("tool_calls")
    op.drop_table("messages")
    op.drop_table("chats")

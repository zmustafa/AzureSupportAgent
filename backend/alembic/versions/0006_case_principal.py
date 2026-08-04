"""add the identity subject to a case (Identity Investigate)

A case opened from an identity investigation is ABOUT a principal. Carrying that only in
the free-text summary makes "every case concerning this identity" unanswerable, which is
the second question an investigator asks. Nullable and un-backfilled on purpose: existing
cases are about workloads, not identities, and inventing a subject for them would be a
claim we cannot support.

Revision ID: 0006_case_principal
Revises: 0005_iam_governance
Create Date: 2026-08-03
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_case_principal"
down_revision: str | None = "0005_iam_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `cases` is NOT created by any migration — it is owned by the app's `create_all()` at startup.
# On a FRESH database `alembic upgrade head` runs BEFORE the app boots, so an unguarded ALTER
# here fails with `UndefinedTableError: relation "cases" does not exist`. When the table is
# absent, `create_all()` will later build it with these columns already present.
def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "cases" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("cases")}
    if "principal_id" not in existing:
        op.add_column("cases", sa.Column("principal_id", sa.String(128), nullable=True))
    if "principal_name" not in existing:
        op.add_column("cases", sa.Column("principal_name", sa.String(256), nullable=True))
    if "principal_kind" not in existing:
        op.add_column("cases", sa.Column("principal_kind", sa.String(32), nullable=True))
    if not any(i["name"] == "ix_cases_principal_id" for i in inspector.get_indexes("cases")):
        op.create_index("ix_cases_principal_id", "cases", ["principal_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "cases" not in inspector.get_table_names():
        return
    op.drop_index("ix_cases_principal_id", table_name="cases")
    op.drop_column("cases", "principal_kind")
    op.drop_column("cases", "principal_name")
    op.drop_column("cases", "principal_id")

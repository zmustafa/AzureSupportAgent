"""tenant-scope mutable access-control records

Revision ID: 0010_access_control_tenants
Revises: 0009_recent_items
Create Date: 2026-08-29
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_access_control_tenants"
down_revision: str | None = "0009_recent_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("roles", "groups", "identity_providers"):
        inspector = sa.inspect(op.get_bind())
        # These auth tables historically came from Base.metadata.create_all after
        # Alembic completed. On a fresh install they do not exist yet; the current
        # models will create them directly with the tenant-aware schema at startup.
        if table not in inspector.get_table_names():
            continue
        if "tenant_id" not in {column["name"] for column in inspector.get_columns(table)}:
            op.add_column(
                table,
                sa.Column("tenant_id", sa.String(128), nullable=False, server_default="default"),
            )
        if f"ix_{table}_tenant_id" not in {
            index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)
        }:
            op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    # Roles and groups were globally named before workspaces became mutable auth
    # boundaries. All legacy rows belong to the legacy/default workspace; future
    # workspaces may reuse a familiar custom name without colliding globally.
    naming = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
    for table in ("roles", "groups"):
        inspector = sa.inspect(op.get_bind())
        if table not in inspector.get_table_names():
            continue
        constraints = inspector.get_unique_constraints(table)
        if any(
            constraint.get("column_names") == ["tenant_id", "name"]
            for constraint in constraints
        ):
            continue
        existing_name = next(
            (
                constraint.get("name")
                for constraint in constraints
                if constraint.get("column_names") == ["name"]
            ),
            None,
        )
        with op.batch_alter_table(table, naming_convention=naming) as batch:
            if existing_name is not None or any(
                constraint.get("column_names") == ["name"]
                for constraint in constraints
            ):
                batch.drop_constraint(existing_name or f"uq_{table}_name", type_="unique")
            batch.create_unique_constraint(
                f"uq_{table}_tenant_name", ["tenant_id", "name"]
            )


def downgrade() -> None:
    # The pre-0010 schema required role/group names to be globally unique. Once two
    # tenants reuse a name there is no lossless representation in that schema, so fail
    # before changing any constraint instead of leaving a half-downgraded database.
    connection = op.get_bind()
    duplicate_queries = {
        "roles": sa.text(
            'SELECT name FROM "roles" GROUP BY name HAVING COUNT(*) > 1 LIMIT 1'
        ),
        "groups": sa.text(
            'SELECT name FROM "groups" GROUP BY name HAVING COUNT(*) > 1 LIMIT 1'
        ),
    }
    for table in ("roles", "groups"):
        if table not in sa.inspect(connection).get_table_names():
            continue
        duplicate = connection.execute(duplicate_queries[table]).scalar_one_or_none()
        if duplicate is not None:
            raise RuntimeError(
                f"Cannot downgrade access-control tenancy: {table} contains the name "
                f"{duplicate!r} in more than one tenant."
            )
    naming = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
    for table in ("groups", "roles"):
        inspector = sa.inspect(op.get_bind())
        if table not in inspector.get_table_names():
            continue
        constraints = inspector.get_unique_constraints(table)
        with op.batch_alter_table(table, naming_convention=naming) as batch:
            if any(
                constraint.get("column_names") == ["tenant_id", "name"]
                for constraint in constraints
            ):
                batch.drop_constraint(f"uq_{table}_tenant_name", type_="unique")
            if not any(
                constraint.get("column_names") == ["name"]
                for constraint in constraints
            ):
                batch.create_unique_constraint(f"uq_{table}_name", ["name"])
    for table in ("identity_providers", "groups", "roles"):
        inspector = sa.inspect(op.get_bind())
        if table not in inspector.get_table_names():
            continue
        if f"ix_{table}_tenant_id" in {index["name"] for index in inspector.get_indexes(table)}:
            op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        if "tenant_id" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}:
            op.drop_column(table, "tenant_id")
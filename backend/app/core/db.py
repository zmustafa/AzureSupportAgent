"""Async SQLAlchemy engine and session management."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

_db_url = settings.resolved_database_url
_is_sqlite = _db_url.startswith("sqlite")
# For SQLite, give writers a generous busy timeout so concurrent background workers
# (e.g. batched assessment runs) wait for the single-writer lock instead of erroring
# with "database is locked".
_connect_args = {"timeout": 30} if _is_sqlite else {}

# `pool_pre_ping` issues a `SELECT 1` on every connection checkout to detect a connection the
# server has dropped underneath us — a real hazard for a NETWORK database behind a firewall or
# an idle timeout, and meaningless for a local SQLite file, which cannot go stale.
#
# It is not free, and its cost is worst exactly when it hurts most. Measured here: a database
# round trip costs ~0 ms idle and **210 ms while one CPU-bound worker thread holds the GIL**, so
# on SQLite the pre-ping doubles the round trips of every authenticated request precisely while
# a heavy IAM analysis is running. Kept for Postgres, where it earns its cost.
#
# The pool is sized EXPLICITLY. Left at SQLAlchemy's defaults it was 5 + 10 overflow with a 30
# second wait, which produced both failure modes at once on a small deployment: four background
# workers exhausted the fifteen slots, every request then queued for thirty seconds before
# failing, and four replicas together asked the server for sixty connections. SQLite ignores
# pool sizing (it uses a different pool class), so these only apply to a real server.
def pool_kwargs(is_sqlite: bool) -> dict[str, object]:
    """Engine pool arguments for this backend. A pure function so it is testable without
    building an engine or reloading this module."""
    if is_sqlite:
        return {}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle_s,
    }


engine = create_async_engine(
    _db_url, echo=False, pool_pre_ping=not _is_sqlite, connect_args=_connect_args,
    **pool_kwargs(_is_sqlite),
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

#: Admission control for background loops. Created lazily because a semaphore binds to the
#: running loop, and this module is imported long before one exists.
_background_gate: asyncio.Semaphore | None = None


def background_slots() -> asyncio.Semaphore:
    global _background_gate
    if _background_gate is None:
        _background_gate = asyncio.Semaphore(max(1, settings.db_background_slots))
    return _background_gate


@asynccontextmanager
async def background_session() -> AsyncGenerator[AsyncSession, None]:
    """A database session for a background loop, behind an admission gate.

    The gate is the point: background work and HTTP requests share one pool, and the workers
    are in a loop while a person is not. Without a cap the loops win every race and the login
    page is what fails."""
    async with background_slots():
        async with SessionLocal() as session:
            yield session


def reset_background_gate() -> None:
    """Drop the semaphore so the next caller rebinds it. For tests and for a fresh loop."""
    global _background_gate
    _background_gate = None

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        """Enable WAL + a busy timeout so concurrent readers/writers don't lock out."""
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()



class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


# Columns added after the initial schema. For local SQLite dev we apply these
# idempotently on startup so the DB stays in sync without a manual alembic run.
_RUNTIME_COLUMNS: dict[str, dict[str, str]] = {
    "messages": {
        "activity_json": "JSON",
        "images_json": "JSON",
        "provider": "VARCHAR(64)",
        "model": "VARCHAR(128)",
        "duration_ms": "INTEGER",
        "investigation_json": "JSON",
    },
    "chats": {
        "pinned": "BOOLEAN DEFAULT 0",
        "provider": "VARCHAR(64)",
        "connection_id": "VARCHAR(36)",
        "thinking_level": "VARCHAR(16) DEFAULT 'normal'",
        "agent_id": "VARCHAR(36)",
        "workload_id": "VARCHAR(36)",
    },
    "audit_log": {
        "provider": "VARCHAR(64)",
        "model": "VARCHAR(128)",
    },
    # Bounded row retention for IAM run diffing (P7). `create_all` makes missing TABLES but
    # never adds a column to a table that already exists, so a late-added column has to be
    # registered here or every existing install fails on the next query with "no such column".
    "rbac_scan_runs": {
        "rows_json": "JSON",
        "pinned": "BOOLEAN DEFAULT 0",
        "pin_reason": "VARCHAR(256) DEFAULT ''",
    },
    "scheduled_tasks": {
        "deleted_at": "DATETIME",
        "notify_connector_ids": "JSON",
        "target_type": "VARCHAR(16) DEFAULT 'agent'",
        "target_config": "JSON",
        "lease_owner": "VARCHAR(128)",
        "lease_token": "VARCHAR(36)",
        "lease_expires_at": "DATETIME",
        "lease_heartbeat_at": "DATETIME",
        "lease_occurrence_at": "DATETIME",
    },
    "task_runs": {
        "task_name": "VARCHAR(256)",
        "target_type": "VARCHAR(16) DEFAULT 'agent'",
        "result_ref": "JSON",
    },
    "assessment_runs": {
        "is_baseline": "BOOLEAN DEFAULT 0",
        "deleted_at": "DATETIME",
        "resource_count": "INTEGER",
        "resources_json": "JSON",
        "catalog_version": "VARCHAR(32)",
        "schema_version": "INTEGER",
        "completeness_pct": "INTEGER",
        "confidence": "VARCHAR(8)",
    },
    "usage": {
        "provider": "VARCHAR(64)",
    },
    "users": {
        "first_name": "VARCHAR(128)",
        "last_name": "VARCHAR(128)",
        "language": "VARCHAR(16)",
        "default_role": "VARCHAR(64)",
    },
    "roles": {
        "tenant_id": "VARCHAR(128) DEFAULT 'default' NOT NULL",
    },
    "groups": {
        "tenant_id": "VARCHAR(128) DEFAULT 'default' NOT NULL",
    },
    "identity_providers": {
        "tenant_id": "VARCHAR(128) DEFAULT 'default' NOT NULL",
    },
    "sessions": {
        "active_role": "VARCHAR(64)",
    },
    "mission_runs": {
        # The mission activity log, persisted so it reloads when the mission is reopened
        # (the live in-memory log is evicted after the run finishes / on a restart).
        "log_json": "JSON",
    },
    "work_batch_items": {
        "lease_owner": "VARCHAR(128)",
        "lease_token": "VARCHAR(36)",
        "lease_expires_at": "DATETIME",
        "lease_heartbeat_at": "DATETIME",
    },
    "perf_profile_fleet_items": {
        "lease_owner": "VARCHAR(128)",
        "lease_token": "VARCHAR(36)",
        "lease_expires_at": "DATETIME",
        "lease_heartbeat_at": "DATETIME",
    },
    # A case opened from an identity investigation is ABOUT a principal. Existing installs
    # already have a `cases` table, so these have to be patched on here as well as in the
    # migration — `create_all` never adds a column to a table that already exists.
    "cases": {
        "principal_id": "VARCHAR(128)",
        "principal_name": "VARCHAR(256)",
        "principal_kind": "VARCHAR(32)",
    },
}


# Composite indexes that match the actual `WHERE … ORDER BY … LIMIT` shapes of the hot list
# endpoints (history tables filtered by a parent id / tenant and ordered by time). They are
# created idempotently with `CREATE INDEX IF NOT EXISTS` on BOTH SQLite and Postgres so an
# existing deployed DB gets them without a bespoke Alembic migration. Single-column indexes
# already declared on the models (tenant_id, *_id, …) are not repeated here.
#   (index_name, table, "col_a, col_b")
_RUNTIME_INDEXES: list[tuple[str, str, str]] = [
    ("ix_messages_chat_created", "messages", "chat_id, created_at"),
    ("ix_audit_tenant_created", "audit_log", "tenant_id, created_at"),
    ("ix_usage_tenant_created", "usage", "tenant_id, created_at"),
    ("ix_tool_calls_tenant_created", "tool_calls", "tenant_id, created_at"),
    ("ix_tool_calls_tenant_status_created", "tool_calls", "tenant_id, status, created_at"),
    ("ix_approvals_tenant_decision_created", "approvals", "tenant_id, decision, created_at"),
    ("ix_usage_tenant_provider_model", "usage", "tenant_id, provider, model"),
    ("ix_usage_tenant_model_created", "usage", "tenant_id, model, created_at"),
    ("ix_taskruns_task_started", "task_runs", "task_id, started_at"),
    ("ix_taskruns_tenant_started", "task_runs", "tenant_id, started_at"),
    ("ix_workbookruns_wb_started", "workbook_runs", "workbook_id, started_at"),
    ("ix_playbookruns_pb_started", "playbook_runs", "playbook_id, started_at"),
    ("ix_missionruns_wl_started", "mission_runs", "workload_id, started_at"),
    ("ix_assessmentruns_wl", "assessment_runs", "workload_id, tenant_id"),
    ("ix_assessment_runs_tenant_deleted_started", "assessment_runs", "tenant_id, deleted_at, started_at"),
    ("ix_assessment_runs_tenant_workload_deleted_started", "assessment_runs", "tenant_id, workload_id, deleted_at, started_at"),
    # Notifications: the unread-count poll filters deliveries by (tenant, channel) and the
    # notification list joins+filters by (tenant, read). These back both hot paths.
    ("ix_notif_tenant_read", "notifications", "tenant_id, read"),
    ("ix_notifications_tenant_created", "notifications", "tenant_id, created_at"),
    ("ix_notification_deliveries_tenant_channel_notification", "notification_deliveries", "tenant_id, channel, notification_id"),
    # "Every case about this identity" is the second question an investigator asks.
    ("ix_cases_principal_id", "cases", "principal_id"),
    ("ix_cases_tenant_deleted_status_updated", "cases", "tenant_id, deleted_at, status, updated_at"),
    ("ix_case_events_tenant_case_created", "case_events", "tenant_id, case_id, created_at"),
    ("ix_sessions_revoked", "sessions", "revoked"),
    ("ix_sessions_expires", "sessions", "expires_at"),
    ("ix_sessions_last_seen", "sessions", "last_seen_at"),
    ("ix_rbac_scan_runs_tenant_started", "rbac_scan_runs", "tenant_id, started_at"),
    ("ix_quota_scan_runs_tenant_started", "quota_scan_runs", "tenant_id, started_at"),
    ("ix_roles_tenant_id", "roles", "tenant_id"),
    ("ix_groups_tenant_id", "groups", "tenant_id"),
    ("ix_identity_providers_tenant_id", "identity_providers", "tenant_id"),
    ("ix_work_batch_items_claim", "work_batch_items", "status, lease_expires_at, available_at"),
    ("ix_perf_fleet_items_claim", "perf_profile_fleet_items", "status, lease_expires_at, batch_id"),
    ("ix_scheduled_tasks_due_claim", "scheduled_tasks", "status, next_run_at, lease_expires_at"),
    ("ix_scheduled_tasks_occurrence_claim", "scheduled_tasks", "status, lease_occurrence_at, lease_expires_at"),
]


#: Arbitrary but STABLE key for the schema advisory lock. Every replica must pick the same
#: number or the lock serialises nothing.
_SCHEMA_LOCK_KEY = 8_274_119_004_512_337


async def ensure_schema() -> None:
    """Create any missing tables and add late-added columns (idempotent).

    Works on both SQLite (local dev) and PostgreSQL (deployed): ``create_all`` makes any
    fully-missing tables with the complete current schema, then the late-added columns are
    patched onto pre-existing tables (e.g. ones an older Alembic migration created).

    On PostgreSQL the whole thing runs under an advisory lock. Every replica executes this at
    boot, and the statements below take ``AccessExclusiveLock``; two replicas starting together
    deadlocked on it, the loser raised inside the lifespan, and the container exited and was
    restarted into the same race. The lock makes one replica migrate while the others wait."""
    # Import models so they register on Base.metadata before create_all.
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        if not _is_sqlite:
            # Session-scoped and re-entrant per connection; released when the block exits.
            await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SCHEMA_LOCK_KEY})
        await conn.run_sync(Base.metadata.create_all)
        if _is_sqlite:
            for table, columns in _RUNTIME_COLUMNS.items():
                existing = await conn.run_sync(
                    lambda sync_conn, t=table: {
                        row[1]
                        for row in sync_conn.exec_driver_sql(f"PRAGMA table_info({t})").fetchall()
                    }
                )
                for col, coltype in columns.items():
                    if col not in existing:
                        await conn.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"
                        )
        else:
            # PostgreSQL: translate the SQLite-flavored column types and use
            # ADD COLUMN IF NOT EXISTS (idempotent, no PRAGMA introspection needed).
            def _pg_type(t: str) -> str:
                return (
                    t.replace("DATETIME", "TIMESTAMP")
                    .replace("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false")
                )
            for table, columns in _RUNTIME_COLUMNS.items():
                for col, coltype in columns.items():
                    await conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {_pg_type(coltype)}"
                    )

        # Composite indexes for the hot list endpoints. `CREATE INDEX IF NOT EXISTS` is
        # supported by both SQLite and Postgres and is a no-op when the index already
        # exists, so this is safe to run on every boot for either backend.
        for ix_name, table, cols in _RUNTIME_INDEXES:
            try:
                await conn.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS {ix_name} ON {table} ({cols})"
                )
            except Exception:  # noqa: BLE001 - a missing optional table must not block boot
                pass

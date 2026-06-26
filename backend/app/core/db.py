"""Async SQLAlchemy engine and session management."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
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

engine = create_async_engine(
    _db_url, echo=False, pool_pre_ping=True, connect_args=_connect_args
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

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
    "scheduled_tasks": {
        "deleted_at": "DATETIME",
        "notify_connector_ids": "JSON",
        "target_type": "VARCHAR(16) DEFAULT 'agent'",
        "target_config": "JSON",
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
    "sessions": {
        "active_role": "VARCHAR(64)",
    },
    "mission_runs": {
        # The mission activity log, persisted so it reloads when the mission is reopened
        # (the live in-memory log is evicted after the run finishes / on a restart).
        "log_json": "JSON",
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
    ("ix_taskruns_task_started", "task_runs", "task_id, started_at"),
    ("ix_taskruns_tenant_started", "task_runs", "tenant_id, started_at"),
    ("ix_workbookruns_wb_started", "workbook_runs", "workbook_id, started_at"),
    ("ix_playbookruns_pb_started", "playbook_runs", "playbook_id, started_at"),
    ("ix_missionruns_wl_started", "mission_runs", "workload_id, started_at"),
    ("ix_assessmentruns_wl", "assessment_runs", "workload_id, tenant_id"),
    # Notifications: the unread-count poll filters deliveries by (tenant, channel) and the
    # notification list joins+filters by (tenant, read). These back both hot paths.
    ("ix_notif_tenant_read", "notifications", "tenant_id, read"),
    ("ix_notifdeliv_tenant_channel", "notification_deliveries", "tenant_id, channel"),
]


async def ensure_schema() -> None:
    """Create any missing tables and add late-added columns (idempotent).

    Works on both SQLite (local dev) and PostgreSQL (deployed): ``create_all`` makes any
    fully-missing tables with the complete current schema, then the late-added columns are
    patched onto pre-existing tables (e.g. ones an older Alembic migration created)."""
    # Import models so they register on Base.metadata before create_all.
    import app.models  # noqa: F401

    async with engine.begin() as conn:
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

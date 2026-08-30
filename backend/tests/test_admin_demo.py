from __future__ import annotations

import threading

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import admin_demo
from app.core.db import Base
from app.core.security import Principal
from app.models import Chat


class _Db:
    def __init__(self) -> None:
        self.added = []
        self.committed = False

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_demo_seed_offloads_synchronous_registry_work(monkeypatch: pytest.MonkeyPatch) -> None:
    event_loop_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []

    def record(name: str, result):
        def inner(_tenant_id: str):
            calls.append((name, threading.get_ident()))
            return result

        return inner

    monkeypatch.setattr(admin_demo, "_purge_features", record("purge", {}))
    monkeypatch.setattr(admin_demo, "_seed_all", record("seed", {"seeded": ["workload"], "errors": {}}))
    monkeypatch.setattr(admin_demo, "_status", record("status", {"loaded": True, "present": {}}))

    db = _Db()
    principal = Principal(
        subject="tester",
        email="tester@example.invalid",
        tenant_id="test-tenant",
        role="admin",
    )
    result = await admin_demo.demo_seed(principal=principal, db=db)  # type: ignore[arg-type]

    assert [name for name, _ in calls] == ["purge", "seed", "status"]
    assert all(thread_id != event_loop_thread for _, thread_id in calls)
    assert result["ok"] is True
    assert result["status"]["loaded"] is True
    assert db.committed is True
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_demo_chat_purge_is_tenant_scoped() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            own = Chat(
                tenant_id="tenant-a", user_id="user-a", workload_id=admin_demo.DEMO_WORKLOAD_ID,
            )
            foreign = Chat(
                tenant_id="tenant-b", user_id="user-b", workload_id=admin_demo.DEMO_WORKLOAD_ID,
            )
            db.add_all([own, foreign])
            await db.commit()

            removed = await admin_demo._purge_demo_chats("tenant-a", db)

            assert removed == 1
            assert await db.get(Chat, own.id) is None
            assert await db.get(Chat, foreign.id) is not None
    finally:
        await engine.dispose()


def test_demo_architecture_purge_requests_only_the_caller_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.architectures import registry

    seen: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        registry,
        "list_architectures",
        lambda tenant_id, include_deleted=False: seen.append((tenant_id, include_deleted)) or [],
    )

    assert admin_demo._purge_demo_architectures("tenant-a") == 0
    assert seen == [("tenant-a", True)]
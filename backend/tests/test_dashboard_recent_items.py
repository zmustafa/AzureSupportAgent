"""Per-user dashboard recent-item history."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import chats, dashboard
from app.core.db import Base
from app.core.security import Principal
from app.models import Chat, Message, RecentItem


def _principal(user: str = "u1", tenant: str = "t1", *permissions: str) -> Principal:
    return Principal(
        subject=user,
        email=f"{user}@example.test",
        tenant_id=tenant,
        role="user",
        permissions=frozenset(permissions or ("workloads.read", "chat.use")),
    )


@pytest.fixture
def session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recent.db'}")

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def _touch(kind="workload", key="w1", route="/workloads/w1", title="Payments"):
    return dashboard.RecentItemTouch(
        kind=kind,
        item_key=key,
        title=title,
        subtitle="Production · Tenant one",
        route=route,
        connection_id="c1",
        workload_id="w1",
    )


def test_touch_deduplicates_and_updates_recency(session_factory) -> None:
    Session = session_factory

    async def run():
        async with Session() as db:
            principal = _principal()
            first = await dashboard.touch_recent_item(_touch(), principal, db)
            second = await dashboard.touch_recent_item(
                _touch(title="Payments API"), principal, db,
            )
            listed = await dashboard.list_recent_items(8, principal, db)
            return first, second, listed

    first, second, listed = asyncio.run(run())
    assert first["item"]["id"] == second["item"]["id"]
    assert second["item"]["visit_count"] == 2
    assert listed["items"][0]["title"] == "Payments API"


def test_history_is_tenant_user_and_permission_scoped(session_factory) -> None:
    Session = session_factory

    async def run():
        async with Session() as db:
            await dashboard.touch_recent_item(_touch(), _principal("u1", "t1"), db)
            same = await dashboard.list_recent_items(8, _principal("u1", "t1"), db)
            other_user = await dashboard.list_recent_items(8, _principal("u2", "t1"), db)
            other_tenant = await dashboard.list_recent_items(8, _principal("u1", "t2"), db)
            no_permission = await dashboard.list_recent_items(
                8, _principal("u1", "t1", "chat.use"), db,
            )
            return same, other_user, other_tenant, no_permission

    same, other_user, other_tenant, no_permission = asyncio.run(run())
    assert len(same["items"]) == 1
    assert other_user["items"] == []
    assert other_tenant["items"] == []
    assert no_permission["items"] == []


def test_touch_rejects_external_mismatched_and_unauthorized_routes(session_factory) -> None:
    Session = session_factory

    async def run():
        async with Session() as db:
            with pytest.raises(HTTPException) as external:
                await dashboard.touch_recent_item(
                    _touch(route="https://example.test/steal"), _principal(), db,
                )
            with pytest.raises(HTTPException) as mismatch:
                await dashboard.touch_recent_item(
                    _touch(route="/admin/providers"), _principal(), db,
                )
            with pytest.raises(HTTPException) as denied:
                await dashboard.touch_recent_item(
                    _touch(), _principal("u1", "t1", "chat.use"), db,
                )
            return external.value, mismatch.value, denied.value

    external, mismatch, denied = asyncio.run(run())
    assert external.status_code == 422
    assert mismatch.status_code == 422
    assert denied.status_code == 403


def test_pin_remove_and_clear_preserves_pins_by_default(session_factory) -> None:
    Session = session_factory

    async def run():
        async with Session() as db:
            principal = _principal()
            one = (await dashboard.touch_recent_item(_touch(), principal, db))["item"]
            two = (await dashboard.touch_recent_item(
                _touch(key="w2", route="/workloads/w2", title="Orders"), principal, db,
            ))["item"]
            await dashboard.pin_recent_item(
                one["id"], dashboard.PinRequest(pinned=True), principal, db,
            )
            cleared = await dashboard.clear_recent_items(False, principal, db)
            remaining = await dashboard.list_recent_items(8, principal, db)
            removed = await dashboard.remove_recent_item(one["id"], principal, db)
            empty = await dashboard.list_recent_items(8, principal, db)
            return two, cleared, remaining, removed, empty

    _, cleared, remaining, removed, empty = asyncio.run(run())
    assert cleared["deleted"] == 1
    assert len(remaining["items"]) == 1 and remaining["items"][0]["pinned"] is True
    assert removed == {"ok": True}
    assert empty["items"] == []


def test_pin_rechecks_permission_after_role_change(session_factory) -> None:
    Session = session_factory

    async def run():
        async with Session() as db:
            item = (await dashboard.touch_recent_item(
                _touch(), _principal("u1", "t1", "workloads.read"), db,
            ))["item"]
            with pytest.raises(HTTPException) as denied:
                await dashboard.pin_recent_item(
                    item["id"],
                    dashboard.PinRequest(pinned=True),
                    _principal("u1", "t1", "chat.use"),
                    db,
                )
            return denied.value

    denied = asyncio.run(run())
    assert denied.status_code == 403


def test_storage_cap_prioritizes_pins_but_remains_bounded(session_factory, monkeypatch) -> None:
    Session = session_factory
    monkeypatch.setattr(dashboard, "_MAX_STORED", 3)

    async def run():
        async with Session() as db:
            principal = _principal()
            for number in range(5):
                item = (await dashboard.touch_recent_item(
                    _touch(
                        key=f"w{number}",
                        route=f"/workloads/w{number}",
                        title=f"Workload {number}",
                    ),
                    principal,
                    db,
                ))["item"]
                if number < 2:
                    await dashboard.pin_recent_item(
                        item["id"], dashboard.PinRequest(pinned=True), principal, db,
                    )
            count = (await db.execute(select(func.count()).select_from(RecentItem))).scalar_one()
            rows = list((await db.execute(
                select(RecentItem).order_by(
                    RecentItem.pinned.desc(), RecentItem.last_visited_at.desc(),
                )
            )).scalars().all())
            return count, rows

    count, rows = asyncio.run(run())
    assert count == 3
    assert [(row.item_key, row.pinned) for row in rows[:2]] == [("w1", True), ("w0", True)]
    assert rows[2].item_key == "w4"


def test_trashed_chat_is_not_returned(session_factory) -> None:
    Session = session_factory

    async def run():
        async with Session() as db:
            principal = _principal()
            chat = Chat(tenant_id="t1", user_id="u1", title="Incident")
            db.add(chat)
            await db.commit()
            await db.refresh(chat)
            await dashboard.touch_recent_item(
                _touch("chat", chat.id, f"/c/{chat.id}", "Incident"), principal, db,
            )
            before = await dashboard.list_recent_items(8, principal, db)
            chat.archived = True
            await db.commit()
            after = await dashboard.list_recent_items(8, principal, db)
            return before, after

    before, after = asyncio.run(run())
    assert len(before["items"]) == 1
    assert after["items"] == []


def test_investigation_history_and_stats_are_owner_scoped(session_factory) -> None:
    Session = session_factory

    async def run():
        async with Session() as db:
            own = Chat(tenant_id="t1", user_id="u1", title="Own investigation")
            other = Chat(tenant_id="t1", user_id="u2", title="Other investigation")
            db.add_all([own, other])
            await db.flush()
            db.add_all([
                Message(chat_id=own.id, role="assistant", content="done", investigation_json={"conclusion": {"summary": "own"}}),
                Message(chat_id=other.id, role="assistant", content="done", investigation_json={"conclusion": {"summary": "other"}}),
            ])
            await db.commit()
            principal = _principal()
            history = await chats.list_investigations(30, principal, db)
            stats = await chats.investigation_stats(7, principal, db)
            return history, stats

    history, stats = asyncio.run(run())
    assert [item["title"] for item in history["investigations"]] == ["Own investigation"]
    assert stats == {"count": 1, "since_days": 7}


def test_investigation_rca_save_cannot_target_another_users_message(session_factory) -> None:
    Session = session_factory

    async def run():
        async with Session() as db:
            other = Chat(tenant_id="t1", user_id="u2", title="Other investigation")
            db.add(other)
            await db.flush()
            message = Message(
                chat_id=other.id,
                role="assistant",
                content="done",
                investigation_json={"conclusion": {"root_cause": "Synthetic root cause"}},
            )
            db.add(message)
            await db.commit()
            await db.refresh(message)

            with pytest.raises(HTTPException) as denied:
                await chats.save_investigation_rca(
                    message.id,
                    chats.SaveRcaIn(),
                    _principal("u1", "t1", "chat.use", "architectures.write"),
                    db,
                )
            return denied.value

    denied = asyncio.run(run())
    assert denied.status_code == 404

"""Tests for the durable Case File store (CRUD + timeline + tenant scoping)."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cases import store
from app.core.db import Base


async def _session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cases.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


@pytest.mark.asyncio
async def test_create_case_records_opened_event(tmp_path):
    Session, engine = await _session_factory(tmp_path)
    try:
        async with Session() as db:
            case = await store.create_case(
                db, tenant_id="t1", actor="alice@x.com", title="Front Door 5xx spike",
                severity="error", workload_id="wl-1", workload_name="Web",
            )
            events = await store.list_events(db, "t1", case.id)
        assert case.status == "open"
        assert case.severity == "error"
        assert len(events) == 1 and events[0].kind == "opened"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_transition_sets_resolved_and_event(tmp_path):
    Session, engine = await _session_factory(tmp_path)
    try:
        async with Session() as db:
            case = await store.create_case(db, tenant_id="t1", title="DB timeouts")
            await store.update_case(db, "t1", case.id, fields={"status": "investigating"}, actor="bob")
            resolved = await store.update_case(db, "t1", case.id, fields={"status": "resolved"}, actor="bob")
            events = await store.list_events(db, "t1", case.id)
        assert resolved.status == "resolved"
        assert resolved.resolved_at is not None
        kinds = [e.kind for e in events]
        assert "status" in kinds  # open → investigating
        assert "resolved" in kinds  # investigating → resolved
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_attach_dedupes_and_records_event(tmp_path):
    Session, engine = await _session_factory(tmp_path)
    try:
        async with Session() as db:
            case = await store.create_case(db, tenant_id="t1", title="SNAT exhaustion")
            await store.attach(db, "t1", case.id, field="finding_uids", values=["f1", "f2"], actor="x")
            # Re-attaching f2 plus new f3: only f3 is added.
            again = await store.attach(db, "t1", case.id, field="finding_uids", values=["f2", "f3"], actor="x")
            events = await store.list_events(db, "t1", case.id)
        assert again.finding_uids == ["f1", "f2", "f3"]
        assert sum(1 for e in events if e.kind == "attach") == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tenant_isolation(tmp_path):
    Session, engine = await _session_factory(tmp_path)
    try:
        async with Session() as db:
            c1 = await store.create_case(db, tenant_id="t1", title="t1 case")
            await store.create_case(db, tenant_id="t2", title="t2 case")
            t1_list = await store.list_cases(db, "t1")
            cross = await store.get_case(db, "t2", c1.id)  # t2 must not see t1's case
        assert len(t1_list) == 1 and t1_list[0].title == "t1 case"
        assert cross is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_open_only_filter_excludes_resolved(tmp_path):
    Session, engine = await _session_factory(tmp_path)
    try:
        async with Session() as db:
            c = await store.create_case(db, tenant_id="t1", title="will resolve")
            await store.create_case(db, tenant_id="t1", title="stays open")
            await store.update_case(db, "t1", c.id, fields={"status": "resolved"}, actor="x")
            all_cases = await store.list_cases(db, "t1", include_resolved=True)
            open_cases = await store.list_cases(db, "t1", include_resolved=False)
        assert len(all_cases) == 2
        assert len(open_cases) == 1 and open_cases[0].title == "stays open"
    finally:
        await engine.dispose()

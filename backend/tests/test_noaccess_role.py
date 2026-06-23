"""The 'noaccess' system role + server-side lockout: an auto-provisioned SSO user defaults
to noaccess (zero permissions) and is blocked from every API path except the self/logout
allowlist, until an admin grants them a real role."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _auth_engine(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401
    import app.models.auth  # noqa: F401
    from app.core.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_noaccess_role_exists_with_no_permissions():
    from app.auth.permissions import NO_ACCESS_ROLE, SYSTEM_ROLES

    by_name = {n: perms for n, _d, perms in SYSTEM_ROLES}
    assert NO_ACCESS_ROLE == "noaccess"
    assert "noaccess" in by_name
    assert by_name["noaccess"] == []  # zero permissions


def test_sso_default_role_is_noaccess():
    from app.auth.settings import DEFAULTS

    assert DEFAULTS["sso_default_role"] == "noaccess"


def _req(path: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path,
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80),
    })


def test_noaccess_user_blocked_server_side(tmp_path):
    from fastapi import HTTPException

    from app.auth.service import seed_system_roles, set_user_roles
    from app.core.security import get_principal
    from app.models.auth import Role, Session as AuthSession, User

    sid = "sess-noaccess-1"

    async def run():
        engine, Session = await _auth_engine(tmp_path)
        async with Session() as db:
            await seed_system_roles(db)
            noaccess = (await db.execute(
                __import__("sqlalchemy").select(Role).where(Role.name == "noaccess")
            )).scalars().first()
            u = User(
                id="u1", email="sso@corp.com", username="sso", display_name="SSO",
                status="active", auth_source="oidc", tenant_id="default",
            )
            db.add(u)
            now = datetime.now(timezone.utc)
            db.add(AuthSession(id=sid, user_id="u1", created_at=now, last_seen_at=now,
                               expires_at=now + timedelta(days=1)))
            await db.commit()
            await set_user_roles(db, "u1", [noaccess.id])
            await db.commit()

        async with Session() as db:
            # /api/me resolves the principal (so the UI can show a "no access" screen).
            p = await get_principal(_req("/api/me"), azsupagent_session=sid, db=db)
            assert p.role == "noaccess"
            assert p.permissions == frozenset()
            assert p.is_admin is False
            # Every other endpoint is blocked 403.
            with pytest.raises(HTTPException) as ei:
                await get_principal(_req("/api/chats"), azsupagent_session=sid, db=db)
            assert ei.value.status_code == 403
            with pytest.raises(HTTPException):
                await get_principal(_req("/api/ownership/owners"), azsupagent_session=sid, db=db)
            # Logout stays reachable.
            await get_principal(_req("/api/auth/logout"), azsupagent_session=sid, db=db)
        await engine.dispose()

    _run(run())


def test_active_role_downscopes_permissions(tmp_path):
    """A user assigned both admin + auditor, acting as 'auditor', gets only auditor perms."""
    import sqlalchemy as sa

    from app.auth.service import seed_system_roles, set_user_roles
    from app.core.security import get_principal
    from app.models.auth import Role, Session as AuthSession, User

    sid = "sess-act-1"

    async def run():
        engine, Session = await _auth_engine(tmp_path)
        async with Session() as db:
            await seed_system_roles(db)
            admin = (await db.execute(sa.select(Role).where(Role.name == "admin"))).scalars().first()
            auditor = (await db.execute(sa.select(Role).where(Role.name == "auditor"))).scalars().first()
            db.add(User(id="u1", email="multi@corp.com", username="multi", display_name="Multi",
                        status="active", auth_source="local", tenant_id="default"))
            now = datetime.now(timezone.utc)
            db.add(AuthSession(id=sid, user_id="u1", created_at=now, last_seen_at=now,
                               expires_at=now + timedelta(days=1)))
            await db.commit()
            await set_user_roles(db, "u1", [admin.id, auditor.id])
            await db.commit()

        async with Session() as db:
            # No active role → union (admin wins, is_admin true).
            p = await get_principal(_req("/api/me"), azsupagent_session=sid, db=db)
            assert p.is_admin is True
            assert set(p.assigned_roles) == {"admin", "auditor"}

            # Switch the session to act as 'auditor'.
            s = await db.get(AuthSession, sid)
            s.active_role = "auditor"
            await db.commit()

            p2 = await get_principal(_req("/api/me"), azsupagent_session=sid, db=db)
            assert p2.role == "auditor"
            assert p2.is_admin is False                      # downscoped — no admin powers
            assert "users.manage" not in p2.permissions
            assert "audit.read" in p2.permissions            # has auditor's perms
        await engine.dispose()

    _run(run())

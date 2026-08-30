from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import users
from app.core.db import Base
from app.core.security import Principal
from app.models.auth import Group, IdentityProvider, Role, Session, User, UserRole


@pytest.mark.asyncio
async def test_user_list_is_tenant_scoped() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            db.add_all([
                User(username="alpha", email="alpha@example.invalid", tenant_id="tenant-a"),
                User(username="bravo", email="bravo@example.invalid", tenant_id="tenant-b"),
            ])
            await db.commit()
            principal = Principal(
                subject="admin-a",
                email="admin-a@example.invalid",
                tenant_id="tenant-a",
                role="admin",
            )

            result = await users.list_users(principal=principal, db=db)

        assert [user["username"] for user in result] == ["alpha"]
        assert all(user["tenant_id"] == "tenant-a" for user in result)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_list_batches_access_control_queries() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            db.add_all([
                User(username=f"user-{index}", email=f"user-{index}@example.invalid", tenant_id="tenant-a")
                for index in range(20)
            ])
            await db.commit()
            calls = 0
            original_execute = db.execute

            async def counted_execute(*args, **kwargs):
                nonlocal calls
                calls += 1
                return await original_execute(*args, **kwargs)

            db.execute = counted_execute  # type: ignore[method-assign]
            principal = Principal(
                subject="admin-a",
                email="admin-a@example.invalid",
                tenant_id="tenant-a",
                role="admin",
            )

            result = await users.list_users(principal=principal, db=db)

        assert len(result) == 20
        assert calls == 5  # users + direct roles + memberships + groups + roles
    finally:
        await engine.dispose()


def _principal(tenant: str = "tenant-a") -> Principal:
    return Principal(
        subject=f"admin-{tenant}",
        email=f"admin-{tenant}@example.invalid",
        tenant_id=tenant,
        role="admin",
    )


@pytest.mark.asyncio
async def test_user_mutations_cannot_target_another_tenant() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            own = User(username="own", email="own@example.invalid", tenant_id="tenant-a")
            foreign = User(username="foreign", email="foreign@example.invalid", tenant_id="tenant-b")
            db.add_all([own, foreign])
            await db.flush()
            db.add(Session(
                id="foreign-session",
                user_id=foreign.id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ))
            await db.commit()

            operations = [
                users.update_user(foreign.id, users.UserUpdate(display_name="changed"), _principal(), db),
                users.reset_password(foreign.id, users.PasswordReset(new_password="long-enough-password"), _principal(), db),
                users.revoke_sessions(foreign.id, _principal(), db),
                users.delete_user(foreign.id, _principal(), db),
            ]
            for operation in operations:
                with pytest.raises(HTTPException) as denied:
                    await operation
                assert denied.value.status_code == 404

            await db.refresh(foreign)
            session = await db.get(Session, "foreign-session")
            assert foreign.display_name == ""
            assert foreign.password_hash is None
            assert session is not None and session.revoked is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_admin_endpoints_are_tenant_scoped() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            own = User(username="own-sessions", email="own-sessions@example.invalid", tenant_id="tenant-a")
            foreign = User(username="foreign-sessions", email="foreign-sessions@example.invalid", tenant_id="tenant-b")
            db.add_all([own, foreign])
            await db.flush()
            expired = datetime.now(timezone.utc) - timedelta(hours=1)
            db.add_all([
                Session(id="own-session", user_id=own.id, expires_at=expired),
                Session(id="foreign-session", user_id=foreign.id, expires_at=expired),
            ])
            await db.commit()

            listed = await users.list_sessions(True, _principal(), db)
            assert [row["id"] for row in listed["sessions"]] == ["own-session"]
            with pytest.raises(HTTPException) as denied:
                await users.revoke_one_session("foreign-session", _principal(), db)
            assert denied.value.status_code == 404

            result = await users.revoke_expired_sessions(_principal(), db)
            assert result["revoked"] == 1
            own_session = await db.get(Session, "own-session")
            foreign_session = await db.get(Session, "foreign-session")
            assert own_session is not None and own_session.revoked is True
            assert foreign_session is not None and foreign_session.revoked is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_last_admin_is_enforced_per_tenant_before_other_edits() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            # A same-named tenant row can exist after the tenancy migration even though
            # the API reserves system names. Last-admin protection must use the system row.
            shadow_role = Role(name="admin", tenant_id="tenant-a", is_system=False)
            role = Role(
                name="admin", tenant_id="default", is_system=True,
                permissions_json=["users.manage"],
            )
            own = User(username="only-admin-a", email="only-admin-a@example.invalid", tenant_id="tenant-a")
            foreign = User(username="only-admin-b", email="only-admin-b@example.invalid", tenant_id="tenant-b")
            db.add_all([shadow_role, role, own, foreign])
            await db.flush()
            db.add_all([
                UserRole(user_id=own.id, role_id=role.id),
                UserRole(user_id=foreign.id, role_id=role.id),
            ])
            await db.commit()

            with pytest.raises(HTTPException) as denied:
                await users.update_user(
                    own.id,
                    users.UserUpdate(email="must-not-persist@example.invalid", role_ids=[]),
                    _principal(),
                    db,
                )
            assert denied.value.status_code == 400
            persisted = (await db.execute(select(User).where(User.id == own.id))).scalar_one()
            assert persisted.email == "only-admin-a@example.invalid"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_profile_and_access_assignments_update_atomically(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            old_role = Role(name="old-role", tenant_id="tenant-a")
            new_role = Role(name="new-role", tenant_id="tenant-a")
            target = User(
                username="atomic-target", email="before@example.invalid", tenant_id="tenant-a",
            )
            db.add_all([old_role, new_role, target])
            await db.flush()
            old_role_id = old_role.id
            new_role_id = new_role.id
            target_id = target.id
            db.add_all([
                UserRole(user_id=target.id, role_id=old_role.id),
                Session(
                    id="atomic-session", user_id=target.id,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                ),
            ])
            await db.commit()

            async def fail_group_update(*_args, **_kwargs):
                raise RuntimeError("simulated group write failure")

            monkeypatch.setattr(users, "set_user_groups", fail_group_update)
            with pytest.raises(RuntimeError, match="simulated group write failure"):
                await users.update_user(
                    target_id,
                    users.UserUpdate(
                        email="after@example.invalid",
                        status="disabled",
                        role_ids=[new_role_id],
                        group_ids=[],
                    ),
                    _principal(),
                    db,
                )
            await db.rollback()

            persisted = await db.get(User, target_id)
            assigned = list((await db.execute(
                select(UserRole.role_id).where(UserRole.user_id == target_id)
            )).scalars().all())
            session = await db.get(Session, "atomic-session")
            assert persisted is not None and persisted.email == "before@example.invalid"
            assert persisted.status == "active"
            assert assigned == [old_role_id]
            assert session is not None and session.revoked is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_system_role_seed_never_promotes_tenant_custom_role() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            shadow = Role(
                name="admin", tenant_id="tenant-a", is_system=False,
                permissions_json=["chat.use"],
            )
            db.add(shadow)
            await db.commit()

            seeded = await users.seed_system_roles(db)
            await db.commit()
            await db.refresh(shadow)

            assert seeded["admin"].id != shadow.id
            assert seeded["admin"].tenant_id == "default"
            assert seeded["admin"].is_system is True
            assert shadow.is_system is False
            assert shadow.permissions_json == ["chat.use"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_access_control_records_are_tenant_scoped() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            system_role = Role(
                name="auditor",
                tenant_id="default",
                is_system=True,
                permissions_json=["audit.read"],
            )
            own_role = Role(name="tenant-a-role", tenant_id="tenant-a")
            foreign_role = Role(name="tenant-b-role", tenant_id="tenant-b")
            foreign_system_role = Role(
                name="foreign-system-role", tenant_id="tenant-b", is_system=True,
            )
            own_group = Group(name="tenant-a-group", tenant_id="tenant-a")
            foreign_group = Group(name="tenant-b-group", tenant_id="tenant-b")
            own_idp = IdentityProvider(name="Tenant A", type="oidc", tenant_id="tenant-a")
            foreign_idp = IdentityProvider(name="Tenant B", type="oidc", tenant_id="tenant-b")
            db.add_all([
                system_role,
                own_role,
                foreign_role,
                foreign_system_role,
                own_group,
                foreign_group,
                own_idp,
                foreign_idp,
            ])
            await db.commit()

            principal = _principal()
            assert {r["name"] for r in await users.list_roles(principal, db)} == {
                "auditor",
                "tenant-a-role",
            }
            assert [g["name"] for g in await users.list_groups(principal, db)] == [
                "tenant-a-group"
            ]
            assert [p["name"] for p in await users.list_idps(principal, db)] == ["Tenant A"]

            denied_calls = [
                users.update_role(
                    foreign_role.id,
                    users.RoleBody(name="changed"),
                    principal,
                    db,
                ),
                users.update_group(
                    foreign_group.id,
                    users.GroupBody(name="changed"),
                    principal,
                    db,
                ),
                users.update_idp(
                    foreign_idp.id,
                    users.IdPBody(name="changed", type="oidc"),
                    principal,
                    db,
                ),
            ]
            for denied_call in denied_calls:
                with pytest.raises(HTTPException) as denied:
                    await denied_call
                assert denied.value.status_code == 404
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_assignments_reject_foreign_role_and_group_ids() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            foreign_role = Role(name="foreign-role", tenant_id="tenant-b")
            foreign_group = Group(name="foreign-group", tenant_id="tenant-b")
            db.add_all([foreign_role, foreign_group])
            await db.commit()

            with pytest.raises(HTTPException) as role_denied:
                await users.create_user(
                    users.UserCreate(
                        username="role-target",
                        email="role-target@example.invalid",
                        role_ids=[foreign_role.id],
                    ),
                    _principal(),
                    db,
                )
            assert role_denied.value.status_code == 400

            with pytest.raises(HTTPException) as group_denied:
                await users.create_user(
                    users.UserCreate(
                        username="group-target",
                        email="group-target@example.invalid",
                        group_ids=[foreign_group.id],
                    ),
                    _principal(),
                    db,
                )
            assert group_denied.value.status_code == 400
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_custom_role_and_group_names_are_unique_per_tenant() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as db:
            first_role = await users.create_role(
                users.RoleBody(name="Responder"), _principal("tenant-a"), db,
            )
            second_role = await users.create_role(
                users.RoleBody(name="Responder"), _principal("tenant-b"), db,
            )
            first_group = await users.create_group(
                users.GroupBody(name="On-call"), _principal("tenant-a"), db,
            )
            second_group = await users.create_group(
                users.GroupBody(name="On-call"), _principal("tenant-b"), db,
            )

            assert first_role["id"] != second_role["id"]
            assert first_group["id"] != second_group["id"]

            with pytest.raises(HTTPException) as reserved:
                await users.update_role(
                    first_role["id"], users.RoleBody(name="admin"),
                    _principal("tenant-a"), db,
                )
            assert reserved.value.status_code == 409
    finally:
        await engine.dispose()
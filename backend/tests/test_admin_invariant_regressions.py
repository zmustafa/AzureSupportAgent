"""Admin invariants: real transactions, synthetic identities, no application lifespan.

SQLite uses a temporary WAL file and two independent engine/session pools, not a
shared in-memory connection. PostgreSQL cases are opt-in via
ADMIN_INVARIANT_PG_URL: only a password-free dedicated aznetagent_admin_test
database and user on loopback are accepted. Each case creates
and drops its own random schema, with no public-schema search-path fallback.
There is deliberately no fallback to DATABASE_URL or configured credentials.

Run only in the caller's isolated test environment: the parent conftest has
collection-time configuration reads. This module overrides its schema bootstrap,
defers app imports until settings are injected, and never imports app.main.
Concurrency barriers are BEFORE requests, never inside the protected check; a
working database lock must not be required to admit two writers simultaneously.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import AsyncExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import combinations_with_replacement
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema


pytestmark = pytest.mark.asyncio
_TENANT = "tenant-a"
_ADMIN = "builtin-admin"
_PATH = "/api/admin/access/users"
_OPERATIONS = ("demote", "disable", "delete")


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """Replace the parent bootstrap: only this module's disposable DBs are used."""
    yield


def _forbidden_io(*_args, **_kwargs):
    pytest.fail("Admin invariant tests must not access external services or configured state")


async def _forbidden_async_io(*_args, **_kwargs):
    _forbidden_io()


@pytest.fixture
def modules(monkeypatch, tmp_path):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _forbidden_io)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _forbidden_async_io)
    monkeypatch.setattr(subprocess, "Popen", _forbidden_io)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbidden_async_io)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbidden_async_io)
    key = Fernet.generate_key()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key.decode("ascii"))

    from app.core import config, jsonstore

    # Construct from synthetic literals/defaults only; do not hydrate dotenv files
    # or ambient provider/database credentials, even during deferred app imports.
    settings = config.Settings.model_construct(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'unused.sqlite').as_posix()}",
        environment="test", dev_auth=False, llm_provider="",
        secrets_encryption_key=key.decode("ascii"),
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(jsonstore, "read_json", _forbidden_io)
    monkeypatch.setattr(jsonstore, "mutate_json", _forbidden_io)

    from app.api import users
    from app.auth import service
    from app.core import crypto, db, security
    from app.models import AuditLog
    from app.models import auth

    monkeypatch.setattr(crypto, "_fernet", Fernet(key))
    monkeypatch.setattr(users, "load_auth_settings", lambda: dict(users.AUTH_DEFAULTS))
    monkeypatch.setattr(service, "load_auth_settings", lambda: dict(users.AUTH_DEFAULTS))
    monkeypatch.setattr(service, "_schedule_slide", _forbidden_io)
    return SimpleNamespace(
        users=users, db=db, security=security, auth=auth, AuditLog=AuditLog,
    )


def _postgres_url():
    raw = os.environ.get("ADMIN_INVARIANT_PG_URL")
    if not raw:
        pytest.skip("PostgreSQL requires explicit ADMIN_INVARIANT_PG_URL for an isolated local test database")
    url = make_url(raw)
    if not (
        url.drivername == "postgresql+asyncpg"
        and url.host in {"127.0.0.1", "::1", "localhost"}
        and url.username == url.database == "aznetagent_admin_test"
        and not url.password
        and not url.query
    ):
        pytest.fail("Refusing a URL outside the password-free local aznetagent_admin_test database/user")
    return url


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def api(request, modules, tmp_path, monkeypatch):
    m = modules
    engines = []
    control = None
    schema = None
    schema_created = False
    try:
        if request.param == "sqlite":
            url = f"sqlite+aiosqlite:///{(tmp_path / 'access.sqlite').as_posix()}"
            for _ in range(2):
                engine = create_async_engine(url, connect_args={"timeout": 10})

                @event.listens_for(engine.sync_engine, "connect")
                def pragmas(connection, _record):
                    cursor = connection.cursor()
                    try:
                        cursor.execute("PRAGMA journal_mode=WAL")
                        cursor.execute("PRAGMA foreign_keys=ON")
                        cursor.execute("PRAGMA busy_timeout=10000")
                    finally:
                        cursor.close()

                engines.append(engine)
        else:
            url = _postgres_url()
            schema = "admin_invariant_" + uuid4().hex
            options = {
                "password": "", "ssl": False, "timeout": 10, "command_timeout": 20,
                "server_settings": {
                    "search_path": schema,
                    "statement_timeout": "15000", "lock_timeout": "10000",
                    "idle_in_transaction_session_timeout": "30000",
                },
            }
            control = create_async_engine(url, connect_args=options)
            async with control.begin() as connection:
                identity = (await connection.execute(text(
                    "SELECT current_database(), current_user"
                ))).one()
                assert tuple(identity) == ("aznetagent_admin_test", "aznetagent_admin_test")
                await connection.execute(CreateSchema(schema))
            schema_created = True
            for _ in range(2):
                engines.append(create_async_engine(
                    url, connect_args=options, isolation_level="READ COMMITTED",
                    pool_size=2, max_overflow=0,
                ))
            for engine in engines:
                async with engine.connect() as connection:
                    scope = (await connection.execute(text(
                        "SELECT current_schema(), current_setting('search_path'), "
                        "current_setting('transaction_isolation')"
                    ))).one()
                    assert tuple(scope) == (schema, schema, "read committed")

        tables = [
            m.auth.User.__table__, m.auth.Role.__table__, m.auth.Group.__table__,
            m.auth.UserRole.__table__, m.auth.UserGroup.__table__,
            m.auth.Session.__table__, m.AuditLog.__table__,
        ]
        async with engines[0].begin() as connection:
            await connection.run_sync(lambda sync: m.db.Base.metadata.create_all(sync, tables=tables))
        makers = [async_sessionmaker(engine, expire_on_commit=False) for engine in engines]
        monkeypatch.setattr(m.db, "engine", engines[0])
        monkeypatch.setattr(m.db, "SessionLocal", makers[0])
        env = SimpleNamespace(m=m, sessions=makers, tables=tables, clients=[])
        # Authorization does not require a direct built-in role; do not accidentally
        # add another recovery admin just to authorize these requests.
        env.principal = m.security.Principal(
            subject="access-manager", email="manager@example.invalid", tenant_id=_TENANT,
            role="operator", permissions=frozenset({"users.manage"}),
        )

        def application(maker):
            async def database():
                async with maker() as session:
                    yield session

            async def identity():
                return env.principal

            app = FastAPI()
            app.include_router(m.users.router, prefix="/api")
            app.dependency_overrides[m.db.get_db] = database
            # Leave the real users.manage guard active, overriding identity only.
            app.dependency_overrides[m.security.get_principal] = identity
            return app

        async with AsyncExitStack() as stack:
            for maker in makers:
                env.clients.append(await stack.enter_async_context(httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application(maker), raise_app_exceptions=False),
                    base_url="http://offline.invalid", trust_env=False,
                )))
            yield env
    finally:
        try:
            for engine in engines:
                await engine.dispose()
        finally:
            if control is not None:
                try:
                    if schema_created:
                        assert schema is not None
                        async with control.begin() as connection:
                            await connection.execute(DropSchema(schema, cascade=True))
                finally:
                    await control.dispose()


async def _seed(api, *, peer_status: str | None = "active", target_status: str = "active"):
    a = api.m.auth
    people = [
        ("admin-a", _TENANT, target_status),
        ("legacy-holder", _TENANT, "active"),
        ("group-holder", _TENANT, "active"),
        ("foreign-admin", "tenant-b", "active"),
    ]
    if peer_status is not None:
        people.append(("admin-b", _TENANT, peer_status))
    async with api.sessions[0]() as db:
        # Same-name legacy row deliberately precedes the real built-in role.
        db.add(a.Role(id="legacy-admin", name="admin", tenant_id=_TENANT, is_system=False))
        await db.flush()
        db.add_all([
            a.Role(id=_ADMIN, name="admin", tenant_id="default", is_system=True,
                   description="Immutable built-in role", permissions_json=["users.manage"]),
            a.Role(id="foreign-role", name="admin", tenant_id="tenant-b", is_system=True),
            a.Group(id="own-group", name="Own group", tenant_id=_TENANT),
            a.Group(id="admin-group", name="Group admins", tenant_id=_TENANT,
                    role_ids_json=[_ADMIN]),
            a.Group(id="foreign-group", name="Foreign group", tenant_id="tenant-b"),
        ])
        for user_id, tenant, status in people:
            db.add(a.User(
                id=user_id, username=user_id, email=f"{user_id}@example.invalid",
                display_name="Original name", tenant_id=tenant, status=status,
                locked_until=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ))
        await db.flush()
        for user_id, _, _ in people:
            if user_id != "group-holder":
                db.add(a.UserRole(
                    user_id=user_id,
                    role_id="legacy-admin" if user_id == "legacy-holder" else _ADMIN,
                ))
            db.add(a.Session(
                id=f"{user_id}-session", user_id=user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ))
        db.add_all([
            a.UserGroup(user_id="admin-a", group_id="own-group"),
            a.UserGroup(user_id="group-holder", group_id="admin-group"),
        ])
        await db.commit()


async def _snapshot(api):
    """Read all relevant columns with a fresh session, never a cached ORM object."""
    async with api.sessions[0]() as db:
        result = {}
        for table in api.tables:
            rows = await db.execute(select(table).order_by(*table.primary_key.columns))
            result[table.name] = [deepcopy(dict(row)) for row in rows.mappings()]
        return result


async def _active_admins(api):
    a = api.m.auth
    async with api.sessions[0]() as db:
        return list((await db.execute(
            select(a.User.id).join(a.UserRole, a.UserRole.user_id == a.User.id).where(
                a.User.tenant_id == _TENANT, a.User.status == "active",
                a.UserRole.role_id == _ADMIN,
            ).order_by(a.User.id)
        )).scalars())


def _subject_state(snapshot, user_id):
    return {
        table: [row for row in snapshot[table] if row[key] == user_id]
        for table, key in (
            ("users", "id"), ("user_roles", "user_id"),
            ("user_groups", "user_id"), ("sessions", "user_id"),
        )
    }


async def _request(api, operation, user_id="admin-a", client=0):
    if operation == "delete":
        return await api.clients[client].delete(f"{_PATH}/{user_id}")
    changes = {
        "demote": {"role_ids": []},
        "disable": {"status": "disabled"},
        "disable-keep-role": {"status": "disabled", "role_ids": [_ADMIN]},
        "replace-legacy": {"role_ids": ["legacy-admin"]},
    }[operation]
    return await api.clients[client].patch(f"{_PATH}/{user_id}", json={
        **changes, "email": f"changed-{user_id}@example.invalid",
        "display_name": "Changed name", "group_ids": [],
    })


@pytest.mark.parametrize("peer_status", [None, "disabled", "invited"])
@pytest.mark.parametrize("operation", [*_OPERATIONS, "disable-keep-role", "replace-legacy"])
async def test_last_active_direct_admin_rejection_preserves_all_state(api, peer_status, operation):
    await _seed(api, peer_status=peer_status)
    before = await _snapshot(api)
    response = await _request(api, operation)
    assert response.status_code == 400, response.text
    assert "administrator" in response.json()["detail"].lower()
    assert await _active_admins(api) == ["admin-a"]
    # Includes same-name roles, foreign admins, group-derived admins and sessions.
    assert await _snapshot(api) == before


async def test_disabled_admin_does_not_allow_last_active_admin_self_demotion(api):
    await _seed(api, peer_status="disabled")
    api.principal = api.m.security.Principal(
        subject="admin-a", email="admin-a@example.invalid", tenant_id=_TENANT,
        role="admin", permissions=frozenset({"users.manage"}),
    )
    before = await _snapshot(api)
    response = await _request(api, "demote")
    assert response.status_code == 400, response.text
    assert "administrator" in response.json()["detail"].lower()
    assert await _active_admins(api) == ["admin-a"]
    assert await _snapshot(api) == before


async def test_self_demotion_is_allowed_with_another_active_direct_admin(api):
    await _seed(api)
    api.principal.subject = "admin-a"
    response = await _request(api, "demote")
    assert response.status_code == 200, response.text
    assert await _active_admins(api) == ["admin-b"]


@pytest.mark.parametrize("operation", ["disable", "delete"])
async def test_self_disable_and_delete_remain_forbidden_with_another_active_admin(api, operation):
    await _seed(api)
    api.principal.subject = "admin-a"
    before = await _snapshot(api)
    response = await _request(api, operation)
    assert response.status_code == 400, response.text
    assert "your own account" in response.json()["detail"]
    assert await _snapshot(api) == before


@pytest.mark.parametrize("denial", ["last-admin", "self-disable"])
async def test_safety_rejection_cannot_stage_edits_for_a_later_commit(api, denial):
    await _seed(api, peer_status="disabled" if denial == "last-admin" else "active")
    api.principal.subject = "admin-a"
    before = await _snapshot(api)
    async with api.sessions[0]() as db:
        with pytest.raises(HTTPException) as rejected:
            await api.m.users.update_user(
                "admin-a", api.m.users.UserUpdate(
                    role_ids=[], status="disabled" if denial == "self-disable" else None,
                    email="must-not-persist@example.invalid", display_name="Rejected", group_ids=[],
                ), api.principal, db,
            )
        assert rejected.value.status_code == 400
        await db.commit()
    assert await _snapshot(api) == before


@pytest.mark.parametrize("changes", [
    {"status": "invited"}, {"role_ids": ["foreign-role"]},
    {"role_ids": ["missing-role"]}, {"group_ids": ["foreign-group"]},
    {"group_ids": ["missing-group"]},
])
async def test_invalid_update_cannot_stage_profile_changes_for_a_later_commit(api, changes):
    await _seed(api)
    before = await _snapshot(api)
    # Deliberately keep and commit the caller's session after validation fails.
    # Rollback on HTTP cleanup alone could otherwise hide prematurely staged edits.
    async with api.sessions[0]() as db:
        with pytest.raises(HTTPException) as denied:
            await api.m.users.update_user(
                "admin-a", api.m.users.UserUpdate(
                    **changes, email="must-not-persist@example.invalid", display_name="Rejected",
                ), api.principal, db,
            )
        assert denied.value.status_code == 400
        await db.commit()
    assert await _snapshot(api) == before


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("denial", ["permission", "foreign", "missing"])
async def test_unauthorized_or_missing_target_cannot_mutate_any_state(api, operation, denial):
    await _seed(api)
    target = {"permission": "admin-a", "foreign": "foreign-admin", "missing": "missing"}[denial]
    if denial == "permission":
        api.principal.role = "user"
        api.principal.permissions = frozenset({"chat.use"})
    before = await _snapshot(api)
    response = await _request(api, operation, user_id=target)
    assert response.status_code == (403 if denial == "permission" else 404), response.text
    assert await _snapshot(api) == before


@pytest.mark.parametrize("body", [
    {"display_name": "Allowed profile"},
    {"role_ids": [_ADMIN], "status": "active", "display_name": "Allowed profile"},
])
async def test_last_admin_can_keep_access_and_edit_profile(api, body):
    await _seed(api, peer_status="disabled")
    before = await _snapshot(api)
    response = await api.clients[0].patch(f"{_PATH}/admin-a", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Allowed profile"
    assert response.json()["role_ids"] == [_ADMIN]
    assert await _active_admins(api) == ["admin-a"]
    after = await _snapshot(api)
    assert after["sessions"] == before["sessions"]
    assert after["roles"] == before["roles"]
    assert [(row["action"], row["target"]) for row in after["audit_log"]] == [
        ("access.user_updated", "admin-a"),
    ]


@pytest.mark.parametrize("operation", _OPERATIONS)
async def test_removal_is_allowed_when_another_active_direct_admin_remains(api, operation):
    await _seed(api)
    before = await _snapshot(api)
    response = await _request(api, operation)
    assert response.status_code == 200, response.text
    assert await _active_admins(api) == ["admin-b"]
    after = await _snapshot(api)
    assert _subject_state(after, "admin-b") == _subject_state(before, "admin-b")
    if operation == "delete":
        assert all(not rows for rows in _subject_state(after, "admin-a").values())
    else:
        target = _subject_state(after, "admin-a")
        assert target["users"][0]["email"] == "changed-admin-a@example.invalid"
        assert target["users"][0]["status"] == ("disabled" if operation == "disable" else "active")
        assert target["sessions"][0]["revoked"] is (operation == "disable")
        assert [row["role_id"] for row in target["user_roles"]] == (
            [_ADMIN] if operation == "disable" else []
        )


@pytest.mark.parametrize("target_status", ["disabled", "invited"])
@pytest.mark.parametrize("operation", _OPERATIONS)
async def test_inactive_direct_admin_is_not_itself_the_protected_active_admin(
    api, target_status, operation,
):
    await _seed(api, peer_status=None, target_status=target_status)
    response = await _request(api, operation)
    assert response.status_code == 200, response.text
    assert await _active_admins(api) == []


async def test_activating_replacement_allows_subsequent_demotion(api):
    await _seed(api, peer_status="disabled")
    enabled = await api.clients[0].patch(f"{_PATH}/admin-b", json={"status": "active"})
    assert enabled.status_code == 200, enabled.text
    demoted = await _request(api, "demote")
    assert demoted.status_code == 200, demoted.text
    assert await _active_admins(api) == ["admin-b"]


async def test_cached_inactive_target_is_reloaded_under_lock_before_demotion(api):
    await _seed(api, peer_status=None, target_status="disabled")
    async with api.sessions[0]() as stale:
        cached = await stale.get(api.m.auth.User, "admin-a")
        assert cached is not None and cached.status == "disabled"
        await stale.commit()  # Keep identity-map state, not an old database snapshot.
        enabled = await api.clients[1].patch(f"{_PATH}/admin-a", json={"status": "active"})
        assert enabled.status_code == 200, enabled.text
        assert cached.status == "disabled"
        before = await _snapshot(api)
        with pytest.raises(HTTPException) as denied:
            await api.m.users.update_user(
                "admin-a", api.m.users.UserUpdate(role_ids=[]), api.principal, stale,
            )
        assert denied.value.status_code == 400
        await stale.commit()
    assert await _active_admins(api) == ["admin-a"]
    assert await _snapshot(api) == before


@pytest.mark.parametrize("operations", list(combinations_with_replacement(_OPERATIONS, 2)))
async def test_concurrent_removals_commit_exactly_one_and_preserve_one_active_admin(api, operations):
    await _seed(api)
    before = await _snapshot(api)
    ready = asyncio.Barrier(2)

    async def contender(index):
        await ready.wait()  # No lock or transaction is held at this barrier.
        return await _request(api, operations[index], f"admin-{'ab'[index]}", client=index)

    tasks = [asyncio.create_task(contender(index)) for index in range(2)]
    try:
        async with asyncio.timeout(30):
            responses = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert sorted(response.status_code for response in responses) == [200, 400], [
        (response.status_code, response.text) for response in responses
    ]
    rejected = next(index for index, response in enumerate(responses) if response.status_code == 400)
    survivor = f"admin-{'ab'[rejected]}"
    assert "administrator" in responses[rejected].json()["detail"].lower()
    assert await _active_admins(api) == [survivor]
    after = await _snapshot(api)
    assert _subject_state(after, survivor) == _subject_state(before, survivor)
    assert after["roles"] == before["roles"]
    assert len(after["audit_log"]) == 1
    assert after["audit_log"][0]["target"] == f"admin-{'ab'[1 - rejected]}"
    for decoy in ("legacy-holder", "group-holder", "foreign-admin"):
        assert _subject_state(after, decoy) == _subject_state(before, decoy)


async def test_failure_after_flushed_mutations_rolls_back_and_releases_lock(api, monkeypatch):
    await _seed(api)
    before = await _snapshot(api)
    original = api.m.users.set_user_groups

    async def fail_after_real_group_write(db, user_id, group_ids, *, commit=True):
        assert commit is False
        await original(db, user_id, group_ids, commit=False)
        await db.flush()  # Profile, status, revocation and role removal really reached SQL.
        raise RuntimeError("Synthetic failure after access-control writes")

    with monkeypatch.context() as patch:
        patch.setattr(api.m.users, "set_user_groups", fail_after_real_group_write)
        response = await api.clients[0].patch(f"{_PATH}/admin-a", json={
            "email": "rollback@example.invalid", "display_name": "Rollback",
            "status": "disabled", "role_ids": [], "group_ids": [],
        })
    assert response.status_code == 500
    assert await _snapshot(api) == before
    async with asyncio.timeout(20):
        retried = await _request(api, "demote", client=1)
    assert retried.status_code == 200, retried.text
    assert await _active_admins(api) == ["admin-b"]
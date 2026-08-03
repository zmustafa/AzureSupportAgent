"""The recency strip must never show one user another user's investigation history.

`GET /entra/investigate/recent` reads the audit log, which holds EVERY user's views. The
filter that keeps it honest is one WHERE clause; this is the test that notices if it goes.

Reading the audit log wholesale needs `audit.read` (admin only) precisely because it names
who looked at whom. This endpoint deliberately does not require that — it is gated on
`investigate.read` and returns only the caller's own trail. Without the actor filter it
would quietly become "show me who the compliance team has been investigating".

The handler is called DIRECTLY rather than over `TestClient`. A second TestClient in the
process gets its own event loop, and the app's in-process `asyncio.Event` binds to the
first — the other module's teardown then dies with "bound to a different event loop".
Route-level auth is already swept by `test_route_authz_matrix`; what needs proving here is
the WHERE clause, and that is reachable without HTTP.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

CONN = "c-recent-test"
TENANT = "t-recent"


def _principal(subject: str, tenant: str = TENANT):
    from app.core.security import Principal

    return Principal(
        subject=subject,
        email=f"{subject}@example.test",
        tenant_id=tenant,
        role="admin",
        permissions=frozenset(["investigate.read", "entra.read"]),
        display_name=subject,
        auth_source="test",
    )


async def _seed(db, rows: list[tuple[str, str, str]], tenant: str = TENANT) -> None:
    """(actor_id, target, name) -> one ``investigate.view`` audit row each."""
    from app.models import AuditLog

    for actor, target, name in rows:
        db.add(AuditLog(
            id=str(uuid.uuid4()), tenant_id=tenant, actor_id=actor,
            action="investigate.view", target=target,
            metadata_json={"kind": "user", "resolution": "resolved", "name": name,
                           "connection_id": CONN},
            created_at=datetime.now(timezone.utc),
        ))
    await db.commit()


async def _recent_ids(db, subject: str, *, tenant: str = TENANT,
                      since: str | None = None) -> set[str]:
    from app.api.entra import investigate_recent

    body = await investigate_recent(
        since=since, limit=25, connection_id=CONN,
        principal=_principal(subject, tenant), db=db,
    )
    return {e["id"] for e in body["recent"]}


async def _view_count(db) -> int:
    from sqlalchemy import func, select

    from app.models import AuditLog

    return int((await db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.action == "investigate.view")
    )).scalar() or 0)


@pytest.mark.asyncio
async def test_recent_returns_only_the_callers_own_investigations():
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        await _seed(db, [
            ("alice", "p-alice-1", "Alice Subject"),
            ("bob", "p-bob-SECRET", "Bob's Subject"),
            ("alice", "p-alice-2", "Second Subject"),
        ])

        alice = await _recent_ids(db, "alice")
        assert {"p-alice-1", "p-alice-2"} <= alice
        # The whole point: Bob's subject must not be visible to Alice.
        assert "p-bob-SECRET" not in alice

        bob = await _recent_ids(db, "bob")
        assert "p-bob-SECRET" in bob
        assert "p-alice-1" not in bob


@pytest.mark.asyncio
async def test_recent_does_not_leak_across_tenants():
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        await _seed(db, [("carol", "p-tenant1", "Tenant One Subject")])
        # Same actor id, different tenant — the row belongs to TENANT, and must not follow them.
        assert "p-tenant1" not in await _recent_ids(db, "carol", tenant="t-other")


@pytest.mark.asyncio
async def test_clearing_hides_entries_without_destroying_the_audit_trail():
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        await _seed(db, [("dave", "p-dave-1", "Dave Subject")])
        before = await _view_count(db)

        assert await _recent_ids(db, "dave", since="2999-01-01T00:00:00Z") == set()

        # A history the investigator can erase is not an audit trail.
        assert await _view_count(db) == before


@pytest.mark.asyncio
async def test_an_unparsable_watermark_does_not_hide_everything():
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        await _seed(db, [("erin", "p-erin-1", "Erin Subject")])
        assert "p-erin-1" in await _recent_ids(db, "erin", since="not-a-date")


@pytest.mark.asyncio
async def test_the_strip_is_capped_and_deduplicated():
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        # The same principal opened repeatedly is one chip; the cap keeps the strip readable.
        await _seed(db, [("frank", "p-frank-1", "Repeat Subject")] * 5
                    + [("frank", f"p-frank-{i}", f"Subject {i}") for i in range(2, 40)])
        assert len(await _recent_ids(db, "frank")) == 25

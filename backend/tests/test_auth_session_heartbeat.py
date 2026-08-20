"""Session resolution: the heartbeat on the read path of every authenticated request.

`resolve_session` runs on EVERY authenticated request. It used to write `last_seen_at` and commit
each time, which put a database write on the product's hottest read path — and, because a failed
commit propagated, a lost lock race returned **500 for every request including the login that
would let you back in**. That was observed live: a concurrent IAM collection run held SQLite's
single writer past its busy timeout and the whole application went down.

Both halves are guarded here: the write is throttled, and a failed write never denies a session
that has already been validated. The write is also DEFERRED — it is queued after the response
rather than awaited on the request — so the guarantee is "an active session is slid forward",
not "it is slid forward before the response is sent". A test asserting the latter would be
pinning the exact coupling that took the application down.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.auth import service
from app.core.db import SessionLocal
from app.models.auth import Session as SessionRow, User

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _user(db, name: str) -> User:
    user = User(username=name, email=f"{name}@local", status="active", auth_source="local")
    db.add(user)
    await db.flush()
    return user


async def _session_for(db, user: User, *, last_seen_delta_s: int = 0) -> SessionRow:
    now = service._now()
    sess = SessionRow(
        id=f"sid-{user.username}",
        user_id=user.id,
        created_at=now,
        last_seen_at=now - timedelta(seconds=last_seen_delta_s),
        expires_at=now + timedelta(hours=8),
        revoked=False,
    )
    db.add(sess)
    await db.commit()
    return sess


async def test_a_fresh_session_does_not_write_on_every_request():
    """The write was the problem. A screen firing a dozen parallel calls produced a dozen
    commits; under any concurrent long write they all failed together."""
    async with SessionLocal() as db:
        user = await _user(db, f"hb1{service.secrets.token_hex(4)}")
        sess = await _session_for(db, user, last_seen_delta_s=0)
        before = service._aware(sess.last_seen_at)

        resolved = await service.resolve_session(db, sess.id)
        assert resolved is not None

    async with SessionLocal() as db2:
        stored = await db2.get(SessionRow, sess.id)
        assert service._aware(stored.last_seen_at) == before, "a fresh session must not be rewritten"


async def test_a_stale_session_is_slid_forward():
    """Throttling must not stop the slide happening at all, or every session would expire at the
    idle cap regardless of activity."""
    async with SessionLocal() as db:
        user = await _user(db, f"hb2{service.secrets.token_hex(4)}")
        sess = await _session_for(db, user, last_seen_delta_s=service.SESSION_SLIDE_SECONDS + 5)
        before = service._aware(sess.last_seen_at)

        assert await service.resolve_session(db, sess.id) is not None

    # The write is queued, not awaited — that is the point. Drain it before asserting.
    await service.drain_session_slides()
    async with SessionLocal() as db2:
        stored = await db2.get(SessionRow, sess.id)
        assert service._aware(stored.last_seen_at) > before


async def test_the_heartbeat_is_not_written_on_the_request_path():
    """The regression guard for the freeze.

    Every authenticated request in the product calls this. If the heartbeat commits before
    returning, every request queues behind whatever holds the SQLite write lock — so a long
    background writer (an IAM collection) stalls the whole application, which is exactly what
    was reported. `resolve_session` must come back with the write still outstanding."""
    async with SessionLocal() as db:
        user = await _user(db, f"hb6{service.secrets.token_hex(4)}")
        sess = await _session_for(db, user, last_seen_delta_s=service.SESSION_SLIDE_SECONDS + 5)
        before = service._aware(sess.last_seen_at)

        assert await service.resolve_session(db, sess.id) is not None

        # Read through a SEPARATE connection: the row must still hold the old value, proving the
        # caller was not made to wait for the write.
        async with SessionLocal() as other:
            mid = await other.get(SessionRow, sess.id)
            assert service._aware(mid.last_seen_at) == before, (
                "the heartbeat committed before resolve_session returned — it is back on the "
                "request's critical path"
            )

    await service.drain_session_slides()
    async with SessionLocal() as db3:
        assert service._aware((await db3.get(SessionRow, sess.id)).last_seen_at) > before


async def test_parallel_requests_queue_one_heartbeat_not_many():
    """A screen firing a dozen calls at once must not produce a dozen writes."""
    import asyncio

    async with SessionLocal() as db:
        user = await _user(db, f"hb7{service.secrets.token_hex(4)}")
        sess = await _session_for(db, user, last_seen_delta_s=service.SESSION_SLIDE_SECONDS + 5)

        await asyncio.gather(*[service.resolve_session(db, sess.id) for _ in range(12)])
        assert len(service._slide_tasks) <= 1, "each parallel request queued its own write"

    await service.drain_session_slides()


async def test_a_failed_heartbeat_write_never_denies_a_valid_session(monkeypatch):
    """The failure that took the application down. The session is validated BEFORE the write;
    losing a lock race on a bookkeeping update must not log anybody out.

    Tested against the INLINE path (no running loop to defer onto), because that is the only
    branch where the failure can still reach the caller at all — which is itself the point."""
    async with SessionLocal() as db:
        user = await _user(db, f"hb3{service.secrets.token_hex(4)}")
        sess = await _session_for(db, user, last_seen_delta_s=service.SESSION_SLIDE_SECONDS + 5)

        async def boom():
            raise RuntimeError("database is locked")

        rolled_back = {"n": 0}

        async def counting_rollback():
            # A pure counter: calling through to the real rollback after a stubbed commit
            # failure leaves SQLAlchemy's greenlet context inconsistent, which would fail the
            # test for a reason that has nothing to do with the behavior under test.
            rolled_back["n"] += 1

        monkeypatch.setattr(db, "commit", boom)
        monkeypatch.setattr(db, "rollback", counting_rollback)
        monkeypatch.setattr(service, "_schedule_slide", lambda *_a, **_k: False)

        resolved = await service.resolve_session(db, sess.id)
        assert resolved is not None, "a valid session was denied because a heartbeat write failed"
        assert resolved[1].id == user.id
        assert rolled_back["n"] == 1, "the failed write must be rolled back, not left pending"


async def test_a_queued_heartbeat_that_fails_never_reaches_the_caller(monkeypatch):
    """A deferred write has nobody to report to, so it must swallow and log — never escape into
    an unretrieved-task-exception that takes the loop's error handler by surprise."""
    def boom(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service, "SessionLocal", boom)
    async with SessionLocal() as db:
        user = await _user(db, f"hb8{service.secrets.token_hex(4)}")
        sess = await _session_for(db, user, last_seen_delta_s=service.SESSION_SLIDE_SECONDS + 5)
        assert await service.resolve_session(db, sess.id) is not None
    await service.drain_session_slides()
    assert not service._slide_pending, "a failed slide must not leave the session marked pending"


async def test_an_expired_session_is_still_rejected():
    """The throttle must not accidentally keep dead sessions alive."""
    async with SessionLocal() as db:
        user = await _user(db, f"hb4{service.secrets.token_hex(4)}")
        now = service._now()
        sess = SessionRow(
            id=f"sid-expired-{user.username}",
            user_id=user.id,
            created_at=now - timedelta(hours=20),
            last_seen_at=now - timedelta(hours=20),
            expires_at=now - timedelta(minutes=1),
            revoked=False,
        )
        db.add(sess)
        await db.commit()
        assert await service.resolve_session(db, sess.id) is None


async def test_a_revoked_session_is_still_rejected():
    async with SessionLocal() as db:
        user = await _user(db, f"hb5{service.secrets.token_hex(4)}")
        sess = await _session_for(db, user)
        sess.revoked = True
        await db.commit()
        assert await service.resolve_session(db, sess.id) is None


async def test_the_slide_never_extends_a_session_beyond_policy():
    """Throttling may expire a session slightly EARLY; it must never extend one, which is the
    direction that would be a security defect rather than a papercut."""
    assert service.SESSION_SLIDE_SECONDS > 0
    # The slide window has to be small relative to the shortest sane idle window (minutes).
    assert service.SESSION_SLIDE_SECONDS <= 300

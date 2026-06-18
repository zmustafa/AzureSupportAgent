"""Tests for the security fixes shipped together with v39:

- IP-based brute-force lockout with auto-unlock (C5).
- Tenant guard helpers in the architectures / playbooks API modules (C1-C3).
- Security-headers middleware (C4) — exercised live via TestClient.

These tests cover the precise behavior that an external pen tester would re-verify:
they don't make assumptions about the implementation, only the observable contract.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import architectures as arch_api
from app.api import playbooks as pb_api
from app.architectures import registry as arch_registry
from app.auth.ip_lockout import IpLockoutStore
from app.playbooks import registry as pb_registry


# ----------------------------------------------------------------- C5: IP lockout


def _run(coro):
    """Run a coroutine to completion in a fresh, dedicated event loop.

    Avoids the ``RuntimeError: asyncio.Lock is bound to a different event loop``
    that ``asyncio.get_event_loop()`` produces when sibling tests (e.g. ``TestClient``)
    instantiate their own loops. Each call gets its own loop and closes it on exit.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _throttle_session(tmp_path):
    """Fresh SQLite engine + sessionmaker for the DB-backed IP lockout store."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401 - register ORM models on Base.metadata
    import app.models.auth  # noqa: F401 - registers LoginThrottle
    from app.core.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'throttle.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_ip_lockout_blocks_after_threshold_and_auto_unlocks(tmp_path):
    """Threshold reached -> locked. Cooldown elapses -> auto-unlocked."""
    store = IpLockoutStore()
    ip = "203.0.113.5"

    async def run():
        engine, Session = await _throttle_session(tmp_path)
        async with Session() as db:
            for _ in range(2):
                locked, _r = await store.record_failure(
                    db, ip, max_attempts=3, window_seconds=60, lockout_seconds=0.2
                )
                assert locked is False
            locked, remaining = await store.record_failure(
                db, ip, max_attempts=3, window_seconds=60, lockout_seconds=0.2
            )
            assert locked is True
            assert 0 < remaining <= 0.2 + 0.01
            # While inside the cooldown window the IP stays locked.
            is_locked, _r = await store.check_locked(db, ip)
            assert is_locked is True
            # After the cooldown, check_locked must auto-unlock.
            await asyncio.sleep(0.25)
            is_locked, _r = await store.check_locked(db, ip)
            assert is_locked is False
        await engine.dispose()

    _run(run())


def test_ip_lockout_window_only_counts_recent_failures(tmp_path):
    """Failures older than the sliding window must not contribute to the count."""
    store = IpLockoutStore()
    ip = "198.51.100.7"

    async def run():
        engine, Session = await _throttle_session(tmp_path)
        async with Session() as db:
            await store.record_failure(db, ip, max_attempts=2, window_seconds=0.2, lockout_seconds=60)
            await asyncio.sleep(0.25)
            # The next failure starts a fresh window — should NOT trip the threshold.
            locked, _r = await store.record_failure(
                db, ip, max_attempts=2, window_seconds=0.2, lockout_seconds=60
            )
            assert locked is False
        await engine.dispose()

    _run(run())


def test_ip_lockout_clear_resets_state(tmp_path):
    store = IpLockoutStore()
    ip = "192.0.2.42"

    async def run():
        engine, Session = await _throttle_session(tmp_path)
        async with Session() as db:
            await store.record_failure(db, ip, max_attempts=3, window_seconds=60, lockout_seconds=60)
            await store.record_failure(db, ip, max_attempts=3, window_seconds=60, lockout_seconds=60)
            await store.clear(db, ip)
            locked, _r = await store.record_failure(
                db, ip, max_attempts=3, window_seconds=60, lockout_seconds=60
            )
            assert locked is False  # counter was wiped, single failure can't trip threshold
        await engine.dispose()

    _run(run())


def test_ip_lockout_none_ip_is_noop(tmp_path):
    """Missing client IP must never block legitimate requests."""
    store = IpLockoutStore()

    async def run():
        engine, Session = await _throttle_session(tmp_path)
        async with Session() as db:
            assert await store.check_locked(db, None) == (False, 0.0)
            locked, _r = await store.record_failure(
                db, None, max_attempts=1, window_seconds=60, lockout_seconds=60
            )
            assert locked is False
        await engine.dispose()

    _run(run())


def test_ip_lockout_persists_across_sessions(tmp_path):
    """H4: the counter is stored in the DB, so a NEW session (≈ another replica or a
    process restart) still sees the lockout — the property the in-process store lacked."""
    store = IpLockoutStore()
    ip = "203.0.113.99"

    async def run():
        engine, Session = await _throttle_session(tmp_path)
        async with Session() as db:
            for _ in range(3):
                await store.record_failure(db, ip, max_attempts=3, window_seconds=60, lockout_seconds=60)
        # Fresh session = a different worker/replica: the lock must still be enforced.
        async with Session() as db2:
            is_locked, remaining = await store.check_locked(db2, ip)
            assert is_locked is True
            assert remaining > 0
        await engine.dispose()

    _run(run())


# --------------------------------------------------- C1+C2: architecture tenant guard


def _principal(tenant: str = "t1", role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=tenant,
        subject=f"user@{tenant}",
        email=f"user@{tenant}.example",
        role=role,
        permissions=set(),
        display_name="Test",
        auth_source="local",
        must_change_password=False,
    )


def _make_arch(tenant: str, name: str = "Test arch") -> dict:
    return arch_registry.upsert_architecture(
        {"name": name, "tenant_id": tenant}, actor=f"user@{tenant}"
    )


def test_tenant_arch_or_404_returns_arch_for_owner():
    arch = _make_arch("alpha", "Owner reads")
    out = arch_api._tenant_arch_or_404(arch["id"], _principal("alpha"))
    assert out["id"] == arch["id"]


def test_tenant_arch_or_404_blocks_cross_tenant_with_404():
    """A foreign tenant must get 404 (NOT 403) so existence isn't confirmed."""
    arch = _make_arch("alpha", "Foreign read")
    with pytest.raises(HTTPException) as excinfo:
        arch_api._tenant_arch_or_404(arch["id"], _principal("beta"))
    assert excinfo.value.status_code == 404
    assert "not found" in str(excinfo.value.detail).lower()


def test_tenant_arch_or_404_legacy_empty_tenant_is_visible():
    """Pre-multitenant rows (tenant_id == '') stay visible to any tenant.

    This matches the list endpoint's behavior and avoids breaking older deployments.
    Newly-created architectures always carry a tenant_id, so this only affects legacy data.
    """
    legacy = arch_registry.upsert_architecture(
        {"name": "Legacy global"}, actor="seed"
    )
    out = arch_api._tenant_arch_or_404(legacy["id"], _principal("anyone"))
    assert out["id"] == legacy["id"]


def test_tenant_arch_or_404_missing_id_returns_404():
    with pytest.raises(HTTPException) as excinfo:
        arch_api._tenant_arch_or_404("does-not-exist", _principal("alpha"))
    assert excinfo.value.status_code == 404


# ----------------------------------------------------------- C3: playbook tenant guard


def _make_playbook(tenant: str, name: str = "Test pb") -> dict:
    return pb_registry.upsert_playbook(
        {"name": name, "tenant_id": tenant, "steps": []}
    )


def test_tenant_playbook_or_404_returns_playbook_for_owner():
    pb = _make_playbook("alpha", "Owner reads pb")
    out = pb_api._tenant_playbook_or_404(pb["id"], _principal("alpha"))
    assert out["id"] == pb["id"]


def test_tenant_playbook_or_404_blocks_cross_tenant_with_404():
    pb = _make_playbook("alpha", "Foreign read pb")
    with pytest.raises(HTTPException) as excinfo:
        pb_api._tenant_playbook_or_404(pb["id"], _principal("beta"))
    assert excinfo.value.status_code == 404


def test_tenant_playbook_or_404_legacy_global_is_visible():
    legacy = pb_registry.upsert_playbook({"name": "Legacy global pb", "steps": []})
    out = pb_api._tenant_playbook_or_404(legacy["id"], _principal("anyone"))
    assert out["id"] == legacy["id"]


def test_tenant_playbook_or_404_missing_returns_404():
    with pytest.raises(HTTPException) as excinfo:
        pb_api._tenant_playbook_or_404("no-such-pb", _principal("alpha"))
    assert excinfo.value.status_code == 404


def test_import_playbook_pins_tenant_id():
    """A bundle without a tenant_id must inherit the caller's tenant, not land as global."""
    from app.automations.portability import (  # imported lazily to avoid heavy startup
        export_playbook,
        import_playbook,
    )

    src = pb_registry.upsert_playbook({"name": "Bundle src", "tenant_id": "alpha", "steps": []})
    bundle = export_playbook(src["id"])
    assert bundle is not None
    result = import_playbook(bundle, actor="user@beta", tenant_id="beta")
    saved = result["playbook"]
    assert saved["tenant_id"] == "beta"


# ----------------------------------------------------- C4: security-headers middleware


@contextmanager
def _test_client():
    """Build a fresh TestClient. Imports inside the function so the heavy app
    only loads when this test actually runs.

    NOTE: this helper is kept for potential future use, but the security-header
    coverage moved to ``tests/test_security_e2e.py`` where a module-scoped
    client is shared across all live HTTP tests (avoids the cross-test
    asyncio.Event loop-binding RuntimeError).
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


# (test_security_headers_present_on_api_and_health was moved to test_security_e2e.py
#  as ``test_security_headers_on_every_response`` — a module-scoped TestClient there
#  shares one event loop across all live-HTTP tests in this codebase.)

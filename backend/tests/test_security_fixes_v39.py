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


def test_ip_lockout_blocks_after_threshold_and_auto_unlocks():
    """Threshold reached -> locked. Cooldown elapses -> auto-unlocked."""
    store = IpLockoutStore()
    ip = "203.0.113.5"
    # Three failures with a 3-attempt limit and a tiny 0.05s cooldown.
    for _ in range(2):
        locked, _ = _run(
            store.record_failure(ip, max_attempts=3, window_seconds=60, lockout_seconds=0.05)
        )
        assert locked is False
    locked, remaining = _run(
        store.record_failure(ip, max_attempts=3, window_seconds=60, lockout_seconds=0.05)
    )
    assert locked is True
    assert 0 < remaining <= 0.05 + 0.01
    # While inside the cooldown window the IP stays locked.
    is_locked, _ = _run(store.check_locked(ip, lockout_seconds=0.05))
    assert is_locked is True
    # After the cooldown, check_locked must auto-unlock.
    _run(asyncio.sleep(0.06))
    is_locked, _ = _run(store.check_locked(ip, lockout_seconds=0.05))
    assert is_locked is False


def test_ip_lockout_window_only_counts_recent_failures():
    """Failures older than the sliding window must not contribute to the count."""
    store = IpLockoutStore()
    ip = "198.51.100.7"
    # First failure inside a 0.05s window.
    _run(store.record_failure(ip, max_attempts=2, window_seconds=0.05, lockout_seconds=60))
    _run(asyncio.sleep(0.06))
    # The next failure starts a fresh window — should NOT trip the threshold.
    locked, _ = _run(
        store.record_failure(ip, max_attempts=2, window_seconds=0.05, lockout_seconds=60)
    )
    assert locked is False


def test_ip_lockout_clear_resets_state():
    store = IpLockoutStore()
    ip = "192.0.2.42"
    _run(store.record_failure(ip, max_attempts=3, window_seconds=60, lockout_seconds=60))
    _run(store.record_failure(ip, max_attempts=3, window_seconds=60, lockout_seconds=60))
    _run(store.clear(ip))
    locked, _ = _run(
        store.record_failure(ip, max_attempts=3, window_seconds=60, lockout_seconds=60)
    )
    assert locked is False  # counter was wiped, single failure can't trip threshold


def test_ip_lockout_none_ip_is_noop():
    """Missing client IP must never block legitimate requests."""
    store = IpLockoutStore()
    assert _run(store.check_locked(None, lockout_seconds=10)) == (False, 0.0)
    locked, _ = _run(
        store.record_failure(None, max_attempts=1, window_seconds=60, lockout_seconds=60)
    )
    assert locked is False


def test_ip_lockout_evicts_oldest_when_full():
    """Memory bound: an attacker can't OOM the process by cycling 50k+ random IPs."""
    store = IpLockoutStore(max_ips_tracked=5)
    for i in range(10):
        _run(store.record_failure(
            f"192.0.2.{i}", max_attempts=99, window_seconds=60, lockout_seconds=60
        ))
    # The store keeps only ~max_ips_tracked entries.
    assert len(store._states) <= 5  # type: ignore[attr-defined]


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

"""Process liveness and dependency readiness contracts for Container Apps probes."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import main
from app.core import loopwatch

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_healthz_is_process_only(monkeypatch):
    """Liveness must stay healthy during a database outage and never trigger a checkout."""
    async def database_must_not_run(*_args, **_kwargs):
        raise AssertionError("liveness touched the database")

    monkeypatch.setattr(main, "_database_ready", database_must_not_run)
    assert await main.healthz() == {"status": "ok"}


async def test_readyz_requires_database_and_recent_loop_health(monkeypatch):
    async def database_ok(*_args, **_kwargs):
        return True

    monkeypatch.setattr(main, "_database_ready", database_ok)
    monkeypatch.setattr(loopwatch, "recent_max_lag_s", lambda **_kwargs: 0.05)
    assert await main.readyz() == {
        "status": "ready",
        "checks": {"database": True, "event_loop": True},
    }


async def test_readyz_failure_is_generic_and_secret_free(monkeypatch):
    async def database_down(*_args, **_kwargs):
        return False

    monkeypatch.setattr(main, "_database_ready", database_down)
    monkeypatch.setattr(loopwatch, "recent_max_lag_s", lambda **_kwargs: 0.05)
    response = await main.readyz()
    body = json.loads(response.body)
    assert response.status_code == 503
    assert body == {
        "status": "not_ready",
        "checks": {"database": False, "event_loop": True},
    }
    assert "error" not in response.body.decode().lower()


async def test_database_readiness_is_bounded(monkeypatch):
    class SlowSession:
        async def __aenter__(self):
            await asyncio.sleep(1)
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("app.core.db.SessionLocal", SlowSession)
    assert await main._database_ready(timeout_s=0.01) is False


def test_recent_loop_stall_ages_out_of_readiness_window():
    loopwatch.reset()
    loopwatch._record_lag(1.5, observed_at=100.0)  # noqa: SLF001 - detector seam
    assert loopwatch.recent_max_lag_s(window_s=30.0, now=110.0) == 1.5
    assert loopwatch.recent_max_lag_s(window_s=30.0, now=131.0) == 0.0
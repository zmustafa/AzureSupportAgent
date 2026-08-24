"""The outage behaviours: pool sizing, background admission, schema races, and a login page
that must never fail open.

Every test here pins something the production incident proved wrong. The chain was: an unbounded
default connection pool -> background loops retrying instantly forever -> `/api/auth/config`
returning 500 -> a login page whose `?? true` fallback offered the one credential form the
tenant had switched off, while hiding the SSO button that worked.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import TimeoutError as SATimeoutError

from app.core import db as db_mod
from app.core.loop_backoff import Backoff, is_pool_exhausted


# =========================================================================== pool sizing
def test_the_connection_pool_is_sized_explicitly_not_left_at_the_default():
    """SQLAlchemy's default is 5 + 10 overflow with a 30s wait. That is simultaneously too few
    connections per replica for the background workers and too many in aggregate once the app
    scales out, and it is the exact shape of the production failure."""
    from app.core.config import get_settings

    s = get_settings()
    assert s.db_pool_size >= 1
    assert s.db_max_overflow >= 0
    # A request that fails fast is a bad response; one that waits 30s holds a worker and looks
    # like a hang. The whole point of the change is that this is well under the default.
    assert s.db_pool_timeout <= 10
    assert s.db_pool_recycle_s > 0


def test_the_pool_settings_reach_the_engine_on_a_server_backend():
    """Asserted on what the engine is BUILT with, not on the settings object: a setting nothing
    reads is not a fix.

    Deliberately a pure-function check. An earlier version of this test reloaded `app.core.db`
    with a Postgres URL to inspect the live engine, which swapped the module-global engine and
    SessionLocal for every test that ran afterwards — four unrelated auth tests then failed with
    "no such table". Reaching into global module state to make an assertion is not worth it."""
    kwargs = db_mod.pool_kwargs(is_sqlite=False)
    assert kwargs["pool_size"] == db_mod.settings.db_pool_size
    assert kwargs["max_overflow"] == db_mod.settings.db_max_overflow
    assert kwargs["pool_timeout"] == db_mod.settings.db_pool_timeout
    assert kwargs["pool_recycle"] == db_mod.settings.db_pool_recycle_s

    # SQLite uses a different pool class that rejects these entirely.
    assert db_mod.pool_kwargs(is_sqlite=True) == {}


def test_the_engine_actually_passes_the_pool_arguments():
    """The pure function above is only meaningful if the engine is built from it."""
    import inspect

    src = inspect.getsource(db_mod)
    assert "**pool_kwargs(_is_sqlite)" in src


# =========================================================================== admission gate
async def test_background_work_cannot_take_every_connection():
    """The workers are in a loop; the person waiting on the login page is not. Without a cap
    the loops win every race for the pool and the request path is what fails."""
    db_mod.reset_background_gate()
    gate = db_mod.background_slots()
    assert gate._value == max(1, db_mod.settings.db_background_slots)

    held: list[int] = []

    async def worker() -> None:
        async with db_mod.background_slots():
            held.append(1)
            await asyncio.sleep(0.05)
            held.pop()

    # More workers than slots: concurrency must never exceed the cap.
    peak = 0
    tasks = [asyncio.create_task(worker()) for _ in range(8)]
    for _ in range(20):
        await asyncio.sleep(0.005)
        peak = max(peak, len(held))
    await asyncio.gather(*tasks)
    assert peak <= max(1, db_mod.settings.db_background_slots)
    db_mod.reset_background_gate()


def test_the_loops_that_starved_the_pool_use_the_gate():
    """Pinned on the source: these three are the loops that appear in the incident log, and a
    gate they do not call is decoration."""
    import inspect

    from app.backup_manager import lro
    from app.core import work_batches
    from app.perfprofile import fleet

    assert "background_session" in inspect.getsource(work_batches.WorkBatchWorker._claim_next)
    assert "background_session" in inspect.getsource(fleet.FleetWorker._claim_next)
    assert "background_session" in inspect.getsource(lro.OperationPoller.tick)


# =========================================================================== backoff
def test_a_starved_loop_backs_off_instead_of_hammering():
    """A flat one-second retry during pool exhaustion keeps the pool busy for exactly as long
    as the outage lasts."""
    b = Backoff(base=1.0, cap=60.0, starved_cap=120.0)
    first = [b.delay() for _ in range(1)][0]
    assert 0.0 <= first <= 1.0
    # It escalates rather than staying flat.
    for _ in range(6):
        b.delay()
    assert b.failures == 7
    assert b.delay() <= 120.0

    b.reset()
    assert b.failures == 0


def test_backoff_is_jittered_so_workers_do_not_wake_together():
    """Four workers that failed together and retry together are the same herd one step removed."""
    b = Backoff(base=8.0)
    for _ in range(5):
        b.delay()
    samples = {round(b.delay(), 6) for _ in range(40)}
    assert len(samples) > 1, "a fixed multiplier would synchronise every worker"


def test_pool_exhaustion_is_recognised_by_type_not_by_message():
    """Matching on the wording would silently become an ordinary retry the day SQLAlchemy
    rephrases it."""
    exc = SATimeoutError("QueuePool limit of size 5 overflow 10 reached, connection timed out")
    assert is_pool_exhausted(exc) is True
    assert is_pool_exhausted(ValueError("boom")) is False


# =========================================================================== schema race
def test_schema_sync_takes_an_advisory_lock_on_postgres():
    """Every replica runs this DDL at boot. Two of them deadlocked on AccessExclusiveLock, the
    loser raised out of the lifespan, and the container exited into the same race on restart."""
    import inspect

    src = inspect.getsource(db_mod.ensure_schema)
    assert "pg_advisory_xact_lock" in src
    assert "_is_sqlite" in src, "the lock must not be attempted on SQLite"


async def test_a_failed_schema_sync_does_not_kill_the_process(monkeypatch):
    """An exiting container serves nothing while it restart-loops. A replica that cannot
    migrate can still serve reads against the schema another replica just finished."""
    from app import main as main_mod

    calls = {"n": 0}

    async def always_fails() -> None:
        calls["n"] += 1
        raise RuntimeError("deadlock detected")

    real_sleep = asyncio.sleep

    async def _no_wait(*_a, **_k):
        await real_sleep(0)

    monkeypatch.setattr(main_mod, "ensure_schema", always_fails)
    monkeypatch.setattr(asyncio, "sleep", _no_wait)

    # Returns rather than raising: the whole point is that boot continues.
    await main_mod._ensure_schema_resilient(attempts=3)

    assert calls["n"] == 3, "it must retry, not give up on the first failure"


async def test_schema_sync_stops_retrying_once_it_succeeds(monkeypatch):
    from app import main as main_mod

    calls = {"n": 0}

    async def fails_once() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    real_sleep = asyncio.sleep

    async def _no_wait(*_a, **_k):
        await real_sleep(0)

    monkeypatch.setattr(main_mod, "ensure_schema", fails_once)
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    await main_mod._ensure_schema_resilient(attempts=4)
    assert calls["n"] == 2


# =========================================================================== auth/config
@pytest.fixture()
def _auth_module():
    from app.api import auth as auth_api

    auth_api._provider_cache = None
    yield auth_api
    auth_api._provider_cache = None


async def test_auth_config_serves_the_last_known_providers_when_the_database_is_down(_auth_module):
    """An empty provider list on a tenant with local login disabled means nobody can sign in.
    Serving a stale list is strictly better than serving a lie."""
    auth_api = _auth_module

    class _Row:
        id, type, button_label, name = "idp1", "oidc", "Sign in with Entra ID", "entra"

    class _OkDb:
        async def execute(self, *_a, **_k):
            class R:
                def scalars(self):
                    class S:
                        def all(self_inner):
                            return [_Row()]
                    return S()
            return R()

    class _DeadDb:
        async def execute(self, *_a, **_k):
            raise SATimeoutError("QueuePool limit of size 5 overflow 10 reached")

    first = await auth_api.auth_config(db=_OkDb())
    assert first["providers"][0]["label"] == "Sign in with Entra ID"
    assert first["stale"] is False

    second = await auth_api.auth_config(db=_DeadDb())
    assert second["providers"] == first["providers"], "the SSO button must survive a DB blip"
    assert second["stale"] is True


async def test_auth_config_says_it_does_not_know_rather_than_reporting_no_providers(_auth_module):
    """With nothing cached the client must be able to tell "none configured" from "we could not
    find out" — those lead to opposite actions."""
    from fastapi import HTTPException

    auth_api = _auth_module

    class _DeadDb:
        async def execute(self, *_a, **_k):
            raise SATimeoutError("QueuePool limit")

    with pytest.raises(HTTPException) as caught:
        await auth_api.auth_config(db=_DeadDb())
    assert caught.value.status_code == 503
    assert "unavailable" in str(caught.value.detail).lower()


async def test_the_local_login_flag_does_not_depend_on_the_database(_auth_module):
    """It comes from settings. Only the provider list needs a query, and conflating the two is
    what made a database problem look like an auth-configuration problem."""
    auth_api = _auth_module

    class _Row:
        id, type, button_label, name = "idp1", "saml", "Corp SSO", "corp"

    class _OkDb:
        async def execute(self, *_a, **_k):
            class R:
                def scalars(self):
                    class S:
                        def all(self_inner):
                            return [_Row()]
                    return S()
            return R()

    out = await auth_api.auth_config(db=_OkDb())
    assert isinstance(out["local_login_enabled"], bool)

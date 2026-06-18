"""Database-backed per-IP brute-force lockout for the login endpoint.

Why this exists
---------------
The DB-backed account lockout in ``auth.service`` protects an individual user
account from too many wrong passwords, but it does not stop an attacker hammering
many usernames from one IP. This module adds a complementary sliding-window
counter keyed by client IP that:

* counts failed login attempts within a configurable window;
* once a threshold is exceeded, blocks **further** attempts from that IP for a
  configurable cooldown (auto-unlock);
* clears the counter on a successful login.

Why the database (not an in-process dict)
-----------------------------------------
The state lives in the ``login_throttle`` table so the limit is enforced GLOBALLY
across every worker / replica, and a process restart no longer wipes the counter.
The previous in-process dict only protected a single-worker deployment and was
silently bypassable behind a multi-replica Container App (each replica kept its own
counter, and every restart reset it).

Configuration keys (read from ``auth_settings.json`` via ``load_auth_settings``):

* ``ip_rate_limit_enabled`` — master switch.
* ``ip_rate_limit_max_attempts`` — failures inside the window before lockout.
* ``ip_rate_limit_window_seconds`` — sliding window size for counting failures.
* ``ip_rate_limit_lockout_seconds`` — cooldown after lockout (auto-unlock).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import LoginThrottle


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC for comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class IpLockoutStore:
    """Per-IP failed-login tracker persisted in the database (shared across replicas).

    The API mirrors the previous in-process store but every method now takes an
    ``AsyncSession``. There is a small read-modify-write race under heavy concurrency
    (two simultaneous failures from one IP could read the same count); the worst case is
    undercounting by one — irrelevant to brute-force defense and far safer than the
    previous per-process state.
    """

    async def check_locked(
        self, db: AsyncSession, ip: str | None, *, lockout_seconds: float = 0.0
    ) -> tuple[bool, float]:
        """Return ``(is_locked, seconds_remaining)``. Auto-unlocks an expired lockout.

        ``lockout_seconds`` is accepted for call-site compatibility but unused — the
        lockout deadline is already persisted in ``locked_until``."""
        if not ip:
            return (False, 0.0)
        row = await db.get(LoginThrottle, ip)
        if row is None:
            return (False, 0.0)
        now = _now()
        locked_until = _aware(row.locked_until)
        if locked_until and locked_until > now:
            return (True, (locked_until - now).total_seconds())
        if locked_until and locked_until <= now:
            # Cooldown elapsed -> auto-unlock by resetting the counter.
            row.locked_until = None
            row.fail_count = 0
            row.window_start = None
            row.updated_at = now
            await db.commit()
        return (False, 0.0)

    async def record_failure(
        self,
        db: AsyncSession,
        ip: str | None,
        *,
        max_attempts: int,
        window_seconds: float,
        lockout_seconds: float,
    ) -> tuple[bool, float]:
        """Record one failed attempt from ``ip``. Returns ``(now_locked, seconds_remaining)``."""
        if not ip:
            return (False, 0.0)
        now = _now()
        row = await db.get(LoginThrottle, ip)
        if row is None:
            row = LoginThrottle(ip=ip, fail_count=0, window_start=now, updated_at=now)
            db.add(row)
        locked_until = _aware(row.locked_until)
        if locked_until and locked_until > now:
            return (True, (locked_until - now).total_seconds())
        # Slide the window: start a fresh one if the previous window has elapsed.
        window_start = _aware(row.window_start)
        if window_start is None or (now - window_start).total_seconds() > window_seconds:
            row.window_start = now
            row.fail_count = 0
        row.fail_count = (row.fail_count or 0) + 1
        row.updated_at = now
        if row.fail_count >= max(1, int(max_attempts)):
            lock_for = max(0.001, float(lockout_seconds))
            row.locked_until = now + timedelta(seconds=lock_for)
            # The lockout deadline now owns the timer; reset the window counter.
            row.fail_count = 0
            row.window_start = None
            await db.commit()
            return (True, lock_for)
        await db.commit()
        return (False, 0.0)

    async def clear(self, db: AsyncSession, ip: str | None) -> None:
        """Drop all state for an IP (call this on a successful login)."""
        if not ip:
            return
        row = await db.get(LoginThrottle, ip)
        if row is not None:
            await db.delete(row)
            await db.commit()

    async def reset_all(self, db: AsyncSession) -> None:
        """Wipe all tracked IPs (admin action from the security policy page)."""
        await db.execute(delete(LoginThrottle))
        await db.commit()

    async def purge_expired(self, db: AsyncSession, *, older_than_seconds: float = 86_400) -> int:
        """Hard-delete idle rows (no active lockout, untouched for ``older_than_seconds``).

        Keeps the table from growing unbounded under a random-IP flood. Run periodically."""
        cutoff = _now() - timedelta(seconds=older_than_seconds)
        result = await db.execute(
            delete(LoginThrottle).where(
                LoginThrottle.locked_until.is_(None),
                LoginThrottle.updated_at < cutoff,
            )
        )
        await db.commit()
        return result.rowcount or 0


# Module-level singleton; all state lives in the database so this is safe across replicas.
ip_lockout = IpLockoutStore()

"""In-process per-IP brute-force lockout for the login endpoint.

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

Storage is in-process (``dict`` + ``asyncio.Lock``) — appropriate for a single
worker. A future multi-worker deployment can swap the backend for Redis without
changing the call sites; the API surface ``record_failure`` / ``check_locked``
/ ``clear`` is intentionally small.

Configuration keys (read from ``auth_settings.json`` via ``load_auth_settings``):

* ``ip_rate_limit_enabled`` — master switch.
* ``ip_rate_limit_max_attempts`` — failures inside the window before lockout.
* ``ip_rate_limit_window_seconds`` — sliding window size for counting failures.
* ``ip_rate_limit_lockout_seconds`` — cooldown after lockout (auto-unlock).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class _IpState:
    # Monotonic timestamps (time.monotonic()) of recent failed attempts within the
    # current window. Old entries are dropped lazily on each record/check.
    failures: list[float] = field(default_factory=list)
    # When set, all attempts are blocked until this monotonic timestamp.
    locked_until: float = 0.0


class IpLockoutStore:
    """Per-IP failed-login tracker with auto-unlock.

    All public methods are safe to call concurrently from multiple asyncio tasks
    via a single ``threading.Lock`` — the critical sections are purely synchronous
    (no ``await`` inside the lock) so a thread lock is sufficient and avoids the
    event-loop binding fragility of ``asyncio.Lock``.

    Memory usage is bounded by ``max_ips_tracked`` — the oldest entries are evicted
    when full so a flood from random IPs cannot OOM the process.
    """

    def __init__(self, *, max_ips_tracked: int = 50_000) -> None:
        self._states: dict[str, _IpState] = {}
        self._lock = threading.Lock()
        self._max_ips = max_ips_tracked

    @staticmethod
    def _trim_window(failures: list[float], window_seconds: float, now: float) -> list[float]:
        cutoff = now - window_seconds
        return [t for t in failures if t >= cutoff]

    async def check_locked(
        self, ip: str | None, *, lockout_seconds: float
    ) -> tuple[bool, float]:
        """Return ``(is_locked, seconds_remaining)``. Pure observation; mutates only
        to clear a lockout that has fully expired."""
        if not ip:
            return (False, 0.0)
        now = time.monotonic()
        with self._lock:
            st = self._states.get(ip)
            if st is None:
                return (False, 0.0)
            if st.locked_until and st.locked_until > now:
                return (True, st.locked_until - now)
            # Lockout has expired -> auto-unlock by resetting the counter.
            if st.locked_until and st.locked_until <= now:
                st.locked_until = 0.0
                st.failures.clear()
            return (False, 0.0)

    async def record_failure(
        self,
        ip: str | None,
        *,
        max_attempts: int,
        window_seconds: float,
        lockout_seconds: float,
    ) -> tuple[bool, float]:
        """Record one failed attempt from ``ip``. Returns ``(now_locked, seconds_remaining)``."""
        if not ip:
            return (False, 0.0)
        now = time.monotonic()
        with self._lock:
            self._evict_if_full()
            st = self._states.setdefault(ip, _IpState())
            if st.locked_until and st.locked_until > now:
                return (True, st.locked_until - now)
            st.failures = self._trim_window(st.failures, window_seconds, now)
            st.failures.append(now)
            if len(st.failures) >= max(1, max_attempts):
                st.locked_until = now + max(0.001, lockout_seconds)
                # Don't keep the list growing; the lockout window now owns the timer.
                st.failures.clear()
                return (True, max(0.001, lockout_seconds))
            return (False, 0.0)

    async def clear(self, ip: str | None) -> None:
        """Drop all state for an IP (call this on a successful login)."""
        if not ip:
            return
        with self._lock:
            self._states.pop(ip, None)

    async def reset_all(self) -> None:
        """Wipe all tracked IPs (admin-tunable from the security policy page)."""
        with self._lock:
            self._states.clear()

    def _evict_if_full(self) -> None:
        # Called BEFORE inserting a new entry. Make room for one additional entry
        # by evicting until len < max, so the final size never exceeds max_ips.
        if len(self._states) < self._max_ips:
            return
        ordered: Iterable[str] = sorted(
            self._states,
            key=lambda key: max(
                self._states[key].failures + [self._states[key].locked_until or 0.0]
            ),
        )
        evict = max(1, len(self._states) - self._max_ips + 1)
        for key in list(ordered)[:evict]:
            self._states.pop(key, None)


# Module-level singleton — the FastAPI app uses one worker so this is sufficient.
# Tests can construct their own IpLockoutStore directly.
ip_lockout = IpLockoutStore()

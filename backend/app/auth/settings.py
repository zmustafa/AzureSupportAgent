"""Security policy settings (admin-configurable, persisted to JSON under .data).

Mirrors app_settings/llm_config: a small JSON file so admins can tune auth behavior
from the dashboard without a restart. Read on each request where relevant.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "auth_settings.json"

DEFAULTS: dict[str, Any] = {
    # Local password auth on/off (you can run SSO-only by disabling this).
    "local_login_enabled": True,
    # Self-service is disabled by default; admins create users.
    "allow_self_registration": False,
    # Password policy.
    "password_min_length": 8,
    "password_require_complexity": False,
    # Brute-force protection (per-account).
    "max_failed_attempts": 5,
    "lockout_minutes": 15,
    # Brute-force protection (per-IP) — complements the per-account lockout above.
    # The per-IP limiter trips before the per-account one when an attacker hammers
    # MANY usernames from the same IP, and auto-unlocks after the configured cooldown.
    "ip_rate_limit_enabled": True,
    "ip_rate_limit_max_attempts": 15,
    "ip_rate_limit_window_seconds": 300,   # count failures over a 5-minute sliding window
    "ip_rate_limit_lockout_seconds": 900,  # 15-minute auto-unlock
    # Session lifetimes (minutes). idle=sliding, absolute=hard cap.
    "session_idle_minutes": 480,      # 8h
    "session_absolute_minutes": 10080,  # 7d
    # SSO: auto-provision users on first successful login.
    "sso_auto_provision": True,
    # Default role granted to JIT-provisioned SSO users with no group mapping. Defaults to
    # "noaccess" (zero permissions) so auto-provisioning a user NEVER implicitly grants access
    # — an admin must deliberately assign a real role. Set to "user" for the old behavior.
    "sso_default_role": "noaccess",
}


#: Memo: (file stamp, settings). `load_auth_settings` is called by `resolve_session`, which runs
#: on EVERY authenticated request in the product — so this used to `exists()`, `read_text()` and
#: `json.loads()` a file per request, synchronously, on the event loop. Cheap while the machine
#: is idle and distinctly not cheap while a worker thread is holding the GIL through a long IAM
#: analysis, which is exactly when the application is already under strain.
#:
#: Keyed on the file's mtime + size rather than a TTL, so a saved change is picked up on the
#: very next request and the steady state costs one `stat()`.
_cache: tuple[tuple[int, int], dict[str, Any]] | None = None


def _stamp() -> tuple[int, int]:
    try:
        st = _PATH.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def load_auth_settings() -> dict[str, Any]:
    global _cache
    stamp = _stamp()
    if _cache is not None and _cache[0] == stamp:
        # A copy: callers treat this as their own dict, and handing out the memo would let one
        # of them silently rewrite the auth policy for every subsequent request.
        return dict(_cache[1])
    data = dict(DEFAULTS)
    saved = jsonstore.read_json(_PATH, {})
    if isinstance(saved, dict):
        data.update({k: saved[k] for k in DEFAULTS if k in saved})
    _cache = (stamp, dict(data))
    return data


def save_auth_settings(patch: dict[str, Any]) -> dict[str, Any]:
    global _cache
    result: dict[str, Any] = {}

    def _mutate(saved: Any) -> dict[str, Any]:
        data = dict(DEFAULTS)
        if isinstance(saved, dict):
            data.update({k: saved[k] for k in DEFAULTS if k in saved})
        for k, v in patch.items():
            if k in DEFAULTS:
                data[k] = v
        result.update(data)
        return data

    jsonstore.mutate_json(_PATH, {}, _mutate)
    # Explicit, not left to the stamp: two saves inside one filesystem mtime tick that happen to
    # produce the same file size would otherwise serve the first one's settings forever.
    _cache = None
    return result

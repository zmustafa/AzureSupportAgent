"""Security policy settings (admin-configurable, persisted to JSON under .data).

Mirrors app_settings/llm_config: a small JSON file so admins can tune auth behavior
from the dashboard without a restart. Read on each request where relevant.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    # Default role granted to JIT-provisioned SSO users with no group mapping.
    "sso_default_role": "user",
}


def load_auth_settings() -> dict[str, Any]:
    data = dict(DEFAULTS)
    if _PATH.exists():
        try:
            saved = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update({k: saved[k] for k in DEFAULTS if k in saved})
        except (json.JSONDecodeError, OSError):
            pass
    return data


def save_auth_settings(patch: dict[str, Any]) -> dict[str, Any]:
    data = load_auth_settings()
    for k, v in patch.items():
        if k in DEFAULTS:
            data[k] = v
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data

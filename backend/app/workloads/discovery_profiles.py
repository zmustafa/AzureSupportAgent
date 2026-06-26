"""Saved Autopilot discovery profiles (Tier 4).

A *profile* captures the sculpt configuration a user dialed in for a scope — filters,
tag-seed keys, granularity, confidence floor, budget — so re-running discovery on the same
subscription / management group is one click instead of re-configuring every control.

Stored at backend/.data/autopilot_profiles.json (Azure Files volume), keyed by
``<tenant>::<connection>``. No secrets → no encryption. A small, capped list per bucket.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "autopilot_profiles.json"
_MAX_PROFILES = 50  # per (tenant, connection) bucket


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _key(tenant_id: str, connection_id: str) -> str:
    return f"{tenant_id or 'default'}::{connection_id or ''}"


# Fields of the sculpt config a profile is allowed to persist (everything else is ignored so a
# profile can never smuggle in unexpected keys).
_CONFIG_FIELDS = (
    "strategy", "mode", "granularity", "preset", "tag_key",
    "exclude_noise", "exclude_system_rgs", "rg_globs", "tag_seed_keys",
    "include_types", "exclude_types", "environments", "regions", "subscriptions",
    "name_contains", "confidence_floor", "max_ai_calls", "naming_hint",
)


def _sanitize_config(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: raw[k] for k in _CONFIG_FIELDS if k in raw}


def list_profiles(tenant_id: str, connection_id: str) -> list[dict[str, Any]]:
    """All saved profiles for a (tenant, connection), newest first."""
    data = _read()
    bucket = data.get(_key(tenant_id, connection_id), [])
    return list(reversed(bucket))


def save_profile(
    tenant_id: str,
    connection_id: str,
    *,
    name: str,
    config: dict[str, Any],
    scope_kind: str = "",
    scope_id: str = "",
    scope_name: str = "",
    profile_id: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Create or update a profile. When ``profile_id`` matches an existing one it's updated
    in place (preserving created_at); otherwise a new profile is appended. Returns it."""
    data = _read()
    key = _key(tenant_id, connection_id)
    bucket = data.setdefault(key, [])
    clean = _sanitize_config(config or {})
    name = (name or "Untitled profile").strip()[:80]

    existing = next((p for p in bucket if p.get("id") == profile_id), None) if profile_id else None
    if existing is not None:
        existing.update({
            "name": name,
            "config": clean,
            "scope_kind": scope_kind or existing.get("scope_kind", ""),
            "scope_id": scope_id or existing.get("scope_id", ""),
            "scope_name": scope_name or existing.get("scope_name", ""),
            "updated_at": _now(),
            "updated_by": actor,
        })
        _write(data)
        return existing

    profile = {
        "id": uuid.uuid4().hex,
        "name": name,
        "config": clean,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "created_at": _now(),
        "updated_at": _now(),
        "created_by": actor,
        "updated_by": actor,
    }
    bucket.append(profile)
    if len(bucket) > _MAX_PROFILES:
        data[key] = bucket[-_MAX_PROFILES:]
    _write(data)
    return profile


def delete_profile(tenant_id: str, connection_id: str, profile_id: str) -> bool:
    """Remove a profile by id. Returns True when one was deleted."""
    data = _read()
    key = _key(tenant_id, connection_id)
    bucket = data.get(key, [])
    new = [p for p in bucket if p.get("id") != profile_id]
    if len(new) == len(bucket):
        return False
    data[key] = new
    _write(data)
    return True

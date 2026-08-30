"""Saved Autopilot discovery profiles (Tier 4).

A *profile* captures the sculpt configuration a user dialed in for a scope — filters,
tag-seed keys, granularity, confidence floor, budget — so re-running discovery on the same
subscription / management group is one click instead of re-configuring every control.

Stored at backend/.data/autopilot_profiles.json (Azure Files volume), keyed by
``<tenant>::<connection>``. No secrets → no encryption. A small, capped list per bucket.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "autopilot_profiles.json"
_MAX_PROFILES = 50  # per (tenant, connection) bucket


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


def _key(tenant_id: str, connection_id: str) -> str:
    return f"{tenant_id or 'default'}::{connection_id or ''}"


# Fields of the sculpt config a profile is allowed to persist (everything else is ignored so a
# profile can never smuggle in unexpected keys).
_CONFIG_FIELDS = (
    "strategy", "mode", "granularity", "preset", "tag_key",
    "exclude_noise", "exclude_system_rgs", "rg_globs", "tag_seed_keys",
    "include_types", "exclude_types", "environments", "regions", "subscriptions",
    "name_contains", "confidence_floor", "max_ai_calls", "naming_hint",
    "min_candidate_resources",
)


def _sanitize_config(raw: dict[str, Any]) -> dict[str, Any]:
    clean = {k: raw[k] for k in _CONFIG_FIELDS if k in raw}
    try:
        clean["min_candidate_resources"] = max(
            1, min(5_000, int(clean.get("min_candidate_resources", 1) or 1))
        )
    except (TypeError, ValueError):
        clean["min_candidate_resources"] = 1
    return clean


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
    key = _key(tenant_id, connection_id)
    clean = _sanitize_config(config or {})
    name = (name or "Untitled profile").strip()[:80]
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        bucket = data.setdefault(key, [])
        existing = (
            next((profile for profile in bucket if profile.get("id") == profile_id), None)
            if profile_id
            else None
        )
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
            result.update(existing)
            return
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
        result.update(profile)

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return result


def delete_profile(tenant_id: str, connection_id: str, profile_id: str) -> bool:
    """Remove a profile by id. Returns True when one was deleted."""
    key = _key(tenant_id, connection_id)
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        bucket = data.get(key, [])
        new = [profile for profile in bucket if profile.get("id") != profile_id]
        if len(new) != len(bucket):
            data[key] = new
            deleted = True

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return deleted

"""RBAC-gated read-only share tokens for snapshots.

A share is a random token + optional expiry recorded on the snapshot metadata. Resolving a
token still re-checks the snapshot exists and logs the view; the token is unguessable but the
endpoint remains tenant-scoped via the snapshot's own tenant. (Application-level sharing — not
a public anonymous CDN link.)"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from app.evidence import registry


def create_share(tenant_id: str, snapshot_id: str, *, actor: str, ttl_days: int = 30) -> dict[str, Any] | None:
    token = secrets.token_urlsafe(24)
    expires = (datetime.now(timezone.utc) + timedelta(days=max(1, ttl_days))).isoformat()
    share = {"token": token, "created_by": actor, "created_at": datetime.now(timezone.utc).isoformat(), "expires_at": expires}
    m = registry.add_share(tenant_id, snapshot_id, share)
    if m is None:
        return None
    return share


def resolve_share(token: str) -> dict[str, Any] | None:
    """Return the snapshot meta for a valid, non-expired token, else None."""
    m = registry.find_by_share_token(token)
    if not m:
        return None
    for s in m.get("shares", []) or []:
        if s.get("token") != token:
            continue
        exp = s.get("expires_at")
        if exp:
            try:
                if datetime.fromisoformat(exp) < datetime.now(timezone.utc):
                    return None
            except (ValueError, TypeError):
                pass
        return m
    return None

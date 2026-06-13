"""Authentication / identity resolution.

Resolves the current ``Principal`` from a server-side session cookie. All auth methods
(local password, OIDC, SAML) converge here. A ``dev_auth`` fast-path remains for local
development (off by default once real auth is enabled).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import ALL_PERMISSIONS
from app.auth.service import effective, primary_role, resolve_session
from app.core.config import get_settings
from app.core.db import get_db

settings = get_settings()

SESSION_COOKIE = "azsupagent_session"


@dataclass
class Principal:
    subject: str
    email: str
    tenant_id: str
    role: str  # user | auditor | operator | admin (highest assigned)
    permissions: frozenset[str] = field(default_factory=frozenset)
    display_name: str = ""
    auth_source: str = "local"
    must_change_password: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" or "users.manage" in self.permissions

    def has(self, perm: str) -> bool:
        return perm in self.permissions


def _dev_principal() -> Principal:
    return Principal(
        subject="dev-user",
        email=settings.dev_auth_email,
        tenant_id=settings.dev_auth_tenant,
        role=settings.dev_auth_role,
        permissions=frozenset(ALL_PERMISSIONS),
        display_name="Dev User",
    )


async def get_principal(
    azsupagent_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """Resolve the current principal from the session cookie (or dev fast-path)."""
    if settings.dev_auth:
        return _dev_principal()

    if not azsupagent_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    resolved = await resolve_session(db, azsupagent_session)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    _sess, user = resolved
    perms, role_names = await effective(db, user)
    return Principal(
        subject=user.id,
        email=user.email,
        tenant_id=user.tenant_id,
        role=primary_role(role_names),
        permissions=frozenset(perms),
        display_name=user.display_name or user.username,
        auth_source=user.auth_source,
        must_change_password=user.must_change_password,
    )


async def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return principal


def require_permission(permission: str):
    """Dependency factory enforcing a specific capability (fine-grained RBAC)."""

    async def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.is_admin or principal.has(permission):
            return principal
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission}",
        )

    return _dep


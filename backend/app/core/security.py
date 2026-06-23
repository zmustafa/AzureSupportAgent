"""Authentication / identity resolution.

Resolves the current ``Principal`` from a server-side session cookie. All auth methods
(local password, OIDC, SAML) converge here. A ``dev_auth`` fast-path remains for local
development (off by default once real auth is enabled).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import ALL_PERMISSIONS
from app.auth.service import effective_scoped, resolve_session
from app.core.config import get_settings
from app.core.db import get_db

settings = get_settings()

SESSION_COOKIE = "azsupagent_session"

# When a principal must change their password, only these endpoints are reachable until
# they do. Everything else returns 403 server-side — the forced reset is no longer just a
# client-side nudge that an attacker could skip by calling the API directly (M1).
_PASSWORD_CHANGE_ALLOWLIST = frozenset(
    {
        "/api/auth/change-password",
        "/api/auth/me",
        "/api/auth/logout",
        "/api/me",
    }
)

# A "no access" principal (the noaccess role, or a user with no roles at all → zero
# permissions) is blocked from EVERY API path except this minimal allowlist, so the frontend
# can still resolve who they are (to show a "no access" screen) and let them sign out. This
# enforces the lockout server-side — not just by hiding UI sections — so a noaccess user can't
# reach any data by calling the API directly.
_NO_ACCESS_ALLOWLIST = frozenset(
    {
        "/api/me",
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/config",
        # Let a user who downscoped to a low/no-permission role switch back, and edit their
        # own profile, without being trapped by the no-access lockout.
        "/api/auth/active-role",
        "/api/auth/profile",
    }
)


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
    # Every role the user holds (direct + via groups) — the "Active Role" picker options.
    assigned_roles: tuple[str, ...] = ()
    # The role the session is currently acting as (== role); "" when no explicit pick.
    active_role: str = ""

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
    request: Request,
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
    perms, role_names, eff_role = await effective_scoped(db, user, _sess.active_role)
    principal = Principal(
        subject=user.id,
        email=user.email,
        tenant_id=user.tenant_id,
        role=eff_role,
        permissions=frozenset(perms),
        display_name=user.display_name or user.username,
        auth_source=user.auth_source,
        must_change_password=user.must_change_password,
        assigned_roles=tuple(sorted(role_names)),
        active_role=(_sess.active_role or "").strip(),
    )
    # Enforce a forced password change server-side: until the user sets a new password,
    # block every endpoint except the change-password / identity / logout allowlist. This
    # closes the gap where a seeded/forced-reset credential could call the API directly.
    if principal.must_change_password and request.url.path not in _PASSWORD_CHANGE_ALLOWLIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required before continuing.",
        )
    # Hard "no access" lockout: a principal with zero effective permissions (the noaccess
    # role, or a user with no roles) is denied every endpoint except the self/logout
    # allowlist. This makes the noaccess role a real server-side wall, so auto-provisioned
    # SSO users can authenticate but reach nothing until an admin grants them a role.
    if not principal.permissions and not principal.is_admin and request.url.path not in _NO_ACCESS_ALLOWLIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has no access. Contact an administrator to be granted a role.",
        )
    return principal


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


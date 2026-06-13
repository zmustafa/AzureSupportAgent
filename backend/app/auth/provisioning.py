"""JIT provisioning + group→role mapping shared by OIDC and SAML logins."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import set_user_roles
from app.auth.settings import load_auth_settings
from app.models.auth import IdentityProvider, Role, User, UserRole


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _role_id_by_name(db: AsyncSession, name: str) -> str | None:
    return (
        await db.execute(select(Role.id).where(Role.name == name))
    ).scalars().first()


async def provision_sso_user(
    db: AsyncSession,
    idp: IdentityProvider,
    *,
    external_id: str,
    email: str,
    display_name: str,
    groups: list[str],
) -> User | None:
    """Find-or-create a user from an SSO assertion and apply group→role mapping.

    Returns the active User, or None if provisioning is not allowed / user disabled.
    """
    cfg = load_auth_settings()
    email = (email or "").strip().lower()

    # Match by (idp, external_id) first, then by verified email (account linking).
    user = (
        await db.execute(
            select(User).where(
                User.external_idp == idp.id, User.external_id == external_id
            )
        )
    ).scalars().first()
    if user is None and email:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalars().first()

    auto = bool(idp.config_json.get("auto_provision", cfg.get("sso_auto_provision", True)))
    if user is None:
        if not auto:
            return None
        # Derive a unique username from email local-part.
        base = (email.split("@")[0] if email else external_id)[:120] or "ssouser"
        username = base
        i = 1
        while (await db.execute(select(User).where(User.username == username))).scalars().first():
            i += 1
            username = f"{base}{i}"
        user = User(
            email=email or f"{external_id}@{idp.type}",
            username=username,
            display_name=display_name or email or username,
            password_hash=None,
            status="active",
            auth_source=idp.type,
            external_idp=idp.id,
            external_id=external_id,
            tenant_id="default",
        )
        db.add(user)
        await db.flush()
    else:
        # Keep linkage + profile fresh.
        user.external_idp = idp.id
        user.external_id = external_id
        if display_name:
            user.display_name = display_name
        if user.auth_source == "local" and user.password_hash is None:
            user.auth_source = idp.type

    if user.status != "active":
        return None

    # Map IdP groups -> role ids. Fall back to the configured default role.
    mapping: dict[str, str] = idp.config_json.get("group_role_map", {}) or {}
    matched_role_names: list[str] = []
    gset = {g.strip() for g in groups if g and g.strip()}
    for grp, role_name in mapping.items():
        if grp in gset and role_name:
            matched_role_names.append(role_name)
    if not matched_role_names:
        default_role = idp.config_json.get("default_role") or cfg.get("sso_default_role", "user")
        matched_role_names = [default_role]

    role_ids: list[str] = []
    for rn in dict.fromkeys(matched_role_names):
        rid = await _role_id_by_name(db, rn)
        if rid:
            role_ids.append(rid)

    # Only (re)assign roles for SSO-managed mapping when we have a result; never strip
    # an admin's manually-granted roles to nothing.
    if role_ids:
        # Preserve existing direct roles AND apply mapped ones (union) so SSO never
        # demotes a user an admin elevated. Mapped roles are additive.
        existing = list(
            (await db.execute(select(UserRole.role_id).where(UserRole.user_id == user.id)))
            .scalars()
            .all()
        )
        await set_user_roles(db, user.id, list(dict.fromkeys(existing + role_ids)))

    user.last_login_at = _now()
    await db.commit()
    return user

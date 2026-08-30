"""Admin access-control API: users, roles, groups, identity providers, sessions, and
security policies. All endpoints require the ``users.manage`` permission.

These power the Settings → Security pages. Local auth (login/logout/me/change-password)
and the SSO callback flows live in ``app.api.auth``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password
from app.auth.permissions import PERMISSION_GROUPS, PERMISSIONS, SYSTEM_ROLE_NAMES
from app.auth.service import (
    effective,
    revoke_all_for_user,
    seed_system_roles,
    set_user_groups,
    set_user_roles,
    user_group_ids,
    user_role_ids,
)
from app.auth.settings import DEFAULTS as AUTH_DEFAULTS
from app.auth.settings import load_auth_settings, save_auth_settings
from app.core.crypto import decrypt, encrypt
from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog
from app.models.auth import (
    Group,
    IdentityProvider,
    Role,
    Session,
    User,
    UserGroup,
    UserRole,
)

router = APIRouter(prefix="/admin/access", tags=["access-control"])

# Every endpoint requires the users.manage capability.
_guard = require_permission("users.manage")

# Secret fields that are Fernet-encrypted at rest, per IdP type.
_SECRET_FIELDS = ("client_secret",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime to UTC-aware for safe comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _session_expired(sess: Session, cfg: dict[str, Any], now: datetime) -> bool:
    """A session is dead when it's past its absolute cap OR its idle window.

    Mirrors the validity checks in app.auth.service.resolve_session so the admin list
    and the auth path agree on what "active" means.
    """
    exp = _aware(sess.expires_at)
    if exp is not None and now > exp:
        return True
    last_seen = _aware(sess.last_seen_at)
    if last_seen is not None:
        from datetime import timedelta

        idle_cap = last_seen + timedelta(minutes=int(cfg["session_idle_minutes"]))
        if now > idle_cap:
            return True
    return False


async def _audit(
    db: AsyncSession, actor: Principal, action: str, target: str, meta: dict[str, Any]
) -> None:
    db.add(
        AuditLog(
            tenant_id=actor.tenant_id,
            actor_id=actor.subject,
            action=action,
            target=target,
            metadata_json=meta,
        )
    )
    await db.commit()


# =============================================================================== users
class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=320)
    display_name: str = ""
    password: str | None = None
    role_ids: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)
    must_change_password: bool = True


class UserUpdate(BaseModel):
    email: str | None = None
    display_name: str | None = None
    status: str | None = None  # active | disabled
    role_ids: list[str] | None = None
    group_ids: list[str] | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=1)
    must_change_password: bool = True


async def _tenant_user_or_404(
    db: AsyncSession, principal: Principal, user_id: str,
) -> User:
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


async def _user_out(db: AsyncSession, u: User) -> dict[str, Any]:
    perms, role_names = await effective(db, u)
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "display_name": u.display_name,
        "status": u.status,
        "auth_source": u.auth_source,
        "must_change_password": u.must_change_password,
        "tenant_id": u.tenant_id,
        "role_ids": await user_role_ids(db, u.id),
        "group_ids": await user_group_ids(db, u.id),
        "role_names": sorted(role_names),
        "permissions": sorted(perms),
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "locked": bool(u.locked_until and (u.locked_until.replace(tzinfo=u.locked_until.tzinfo or timezone.utc) > _now())),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


async def _users_out(db: AsyncSession, users: list[User]) -> list[dict[str, Any]]:
    """Serialize a user page with a fixed number of access-control queries.

    The former loop called `_user_out` for every row, which performed direct-role, group,
    group-role, role, role-id and group-id reads per user. A test-populated local workspace took
    3.8 seconds to list users and the cost grew linearly with directory size.
    """
    if not users:
        return []
    user_ids = [user.id for user in users]
    tenant_id = users[0].tenant_id
    direct_rows = (
        await db.execute(select(UserRole).where(UserRole.user_id.in_(user_ids)))
    ).scalars().all()
    membership_rows = (
        await db.execute(select(UserGroup).where(UserGroup.user_id.in_(user_ids)))
    ).scalars().all()
    groups = (
        await db.execute(select(Group).where(Group.tenant_id == tenant_id))
    ).scalars().all()
    roles = (
        await db.execute(
            select(Role).where(
                (Role.is_system.is_(True) & (Role.tenant_id == "default"))
                | (Role.tenant_id == tenant_id)
            )
        )
    ).scalars().all()

    direct_by_user: dict[str, list[str]] = {user_id: [] for user_id in user_ids}
    groups_by_user: dict[str, list[str]] = {user_id: [] for user_id in user_ids}
    for row in direct_rows:
        direct_by_user[row.user_id].append(row.role_id)
    for row in membership_rows:
        groups_by_user[row.user_id].append(row.group_id)
    group_roles = {group.id: group.role_ids_json or [] for group in groups}
    role_by_id = {role.id: role for role in roles}

    result: list[dict[str, Any]] = []
    for user in users:
        effective_role_ids = set(direct_by_user[user.id])
        for group_id in groups_by_user[user.id]:
            effective_role_ids.update(group_roles.get(group_id, []))
        effective_roles = [role_by_id[role_id] for role_id in effective_role_ids if role_id in role_by_id]
        permissions = sorted({permission for role in effective_roles for permission in (role.permissions_json or [])})
        result.append({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status,
            "auth_source": user.auth_source,
            "must_change_password": user.must_change_password,
            "tenant_id": user.tenant_id,
            "role_ids": direct_by_user[user.id],
            "group_ids": groups_by_user[user.id],
            "role_names": sorted(role.name for role in effective_roles),
            "permissions": permissions,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "locked": bool(user.locked_until and (user.locked_until.replace(tzinfo=user.locked_until.tzinfo or timezone.utc) > _now())),
            "created_at": user.created_at.isoformat() if user.created_at else None,
        })
    return result


@router.get("/users")
async def list_users(
    principal: Principal = Depends(_guard), db: AsyncSession = Depends(get_db)
):
    users = (
        await db.execute(
            select(User)
            .where(User.tenant_id == principal.tenant_id)
            .order_by(User.created_at)
        )
    ).scalars().all()
    return await _users_out(db, list(users))


@router.post("/users")
async def create_user(
    body: UserCreate,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    uname = body.username.strip().lower()
    email = body.email.strip().lower()
    exists = (
        await db.execute(
            select(User).where((User.username == uname) | (User.email == email))
        )
    ).scalars().first()
    if exists:
        raise HTTPException(status_code=409, detail="A user with that username or email already exists.")
    await _validate_role_ids(db, principal, body.role_ids)
    await _validate_group_ids(db, principal, body.group_ids)
    cfg = load_auth_settings()
    if body.password and len(body.password) < int(cfg["password_min_length"]):
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {cfg['password_min_length']} characters.",
        )
    user = User(
        username=uname,
        email=email,
        display_name=body.display_name or uname,
        password_hash=hash_password(body.password) if body.password else None,
        status="active",
        auth_source="local",
        must_change_password=bool(body.password) and body.must_change_password,
        tenant_id=principal.tenant_id,
    )
    db.add(user)
    await db.flush()
    if body.role_ids:
        await set_user_roles(db, user.id, body.role_ids, commit=False)
    if body.group_ids:
        await set_user_groups(db, user.id, body.group_ids, commit=False)
    await db.commit()
    await _audit(db, principal, "access.user_created", user.id, {"username": uname})
    return await _user_out(db, user)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdate,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    user = await _tenant_user_or_404(db, principal, user_id)
    # Validate the lockout invariant before changing or committing any other field. A rejected
    # role change must not still persist an email/status edit from the same request.
    if body.role_ids is not None:
        await _validate_role_ids(db, principal, body.role_ids)
        await _ensure_not_last_admin(db, principal, user, body.role_ids)
    if body.group_ids is not None:
        await _validate_group_ids(db, principal, body.group_ids)
    if body.email is not None:
        user.email = body.email.strip().lower()
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="status must be 'active' or 'disabled'.")
        # Don't let an admin disable their own account (lockout safety).
        if body.status == "disabled" and user.id == principal.subject:
            raise HTTPException(status_code=400, detail="You cannot disable your own account.")
        user.status = body.status
        if body.status == "disabled":
            await revoke_all_for_user(db, user.id, commit=False)
    user.locked_until = None
    user.updated_at = _now()
    if body.role_ids is not None:
        await set_user_roles(db, user.id, body.role_ids, commit=False)
    if body.group_ids is not None:
        await set_user_groups(db, user.id, body.group_ids, commit=False)
    await db.commit()
    await _audit(db, principal, "access.user_updated", user.id, {})
    return await _user_out(db, user)


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    body: PasswordReset,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    user = await _tenant_user_or_404(db, principal, user_id)
    cfg = load_auth_settings()
    if len(body.new_password) < int(cfg["password_min_length"]):
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {cfg['password_min_length']} characters.",
        )
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = body.must_change_password
    user.auth_source = "local"
    user.locked_until = None
    user.failed_attempts = 0
    await revoke_all_for_user(db, user.id, commit=False)
    await db.commit()
    await _audit(db, principal, "access.password_reset", user.id, {})
    return {"ok": True}


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_sessions(
    user_id: str,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    await _tenant_user_or_404(db, principal, user_id)
    n = await revoke_all_for_user(db, user_id)
    await _audit(db, principal, "access.sessions_revoked", user_id, {"count": n})
    return {"ok": True, "revoked": n}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    user = await _tenant_user_or_404(db, principal, user_id)
    if user.id == principal.subject:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    await _ensure_not_last_admin(db, principal, user, [])
    await db.execute(delete(UserRole).where(UserRole.user_id == user_id))
    await db.execute(delete(UserGroup).where(UserGroup.user_id == user_id))
    await revoke_all_for_user(db, user_id, commit=False)
    await db.delete(user)
    await db.commit()
    await _audit(db, principal, "access.user_deleted", user_id, {"username": user.username})
    return {"ok": True}


async def _ensure_not_last_admin(
    db: AsyncSession, principal: Principal, user: User, new_role_ids: list[str],
) -> None:
    """Block changes that would remove the final direct admin from this tenant."""
    admin_role = (
        await db.execute(
            select(Role).where(
                Role.name == "admin",
                Role.is_system.is_(True),
                Role.tenant_id == "default",
            )
        )
    ).scalars().first()
    if admin_role is None:
        return
    # Is this user currently an admin (directly)?
    current = await user_role_ids(db, user.id)
    user_is_admin = admin_role.id in current
    will_be_admin = admin_role.id in new_role_ids
    if user_is_admin and not will_be_admin:
        others = (
            await db.execute(
                select(UserRole.user_id)
                .join(User, User.id == UserRole.user_id)
                .where(
                    UserRole.role_id == admin_role.id,
                    UserRole.user_id != user.id,
                    User.tenant_id == principal.tenant_id,
                )
            )
        ).scalars().all()
        if not others:
            raise HTTPException(
                status_code=400,
                detail="At least one administrator must remain. Assign admin to another user first.",
            )


# =============================================================================== roles
@router.get("/permissions")
async def list_permissions(_: Principal = Depends(_guard)):
    return [
        {"key": key, "label": label, "group": group}
        for group, items in PERMISSION_GROUPS
        for key, label in items
    ]


class RoleBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    permissions: list[str] = Field(default_factory=list)


def _role_out(r: Role) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "is_system": r.is_system,
        "permissions": list(r.permissions_json or []),
    }


def _visible_roles(tenant_id: str):
    return (
        (Role.is_system.is_(True) & (Role.tenant_id == "default"))
        | (Role.tenant_id == tenant_id)
    )


async def _validate_role_ids(
    db: AsyncSession, principal: Principal, role_ids: list[str],
) -> None:
    requested = set(role_ids)
    if not requested:
        return
    visible = set(
        (
            await db.execute(
                select(Role.id).where(
                    Role.id.in_(requested),
                    _visible_roles(principal.tenant_id),
                )
            )
        ).scalars().all()
    )
    if visible != requested:
        raise HTTPException(
            status_code=400,
            detail="One or more roles are not available in this workspace.",
        )


async def _validate_group_ids(
    db: AsyncSession, principal: Principal, group_ids: list[str],
) -> None:
    requested = set(group_ids)
    if not requested:
        return
    visible = set(
        (
            await db.execute(
                select(Group.id).where(
                    Group.id.in_(requested),
                    Group.tenant_id == principal.tenant_id,
                )
            )
        ).scalars().all()
    )
    if visible != requested:
        raise HTTPException(
            status_code=400,
            detail="One or more groups are not available in this workspace.",
        )


@router.get("/roles")
async def list_roles(principal: Principal = Depends(_guard), db: AsyncSession = Depends(get_db)):
    roles = (
        await db.execute(
            select(Role).where(_visible_roles(principal.tenant_id)).order_by(Role.name)
        )
    ).scalars().all()
    return [_role_out(r) for r in roles]


@router.post("/roles")
async def create_role(
    body: RoleBody,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    name = body.name.strip()
    if name in SYSTEM_ROLE_NAMES:
        raise HTTPException(status_code=409, detail="That name is reserved for a system role.")
    exists = (
        await db.execute(
            select(Role).where(
                Role.name == name,
                Role.tenant_id == principal.tenant_id,
            )
        )
    ).scalars().first()
    if exists:
        raise HTTPException(status_code=409, detail="A role with that name already exists.")
    perms = [p for p in body.permissions if p in PERMISSIONS]
    role = Role(
        tenant_id=principal.tenant_id,
        name=name,
        description=body.description,
        is_system=False,
        permissions_json=perms,
    )
    db.add(role)
    await db.commit()
    await _audit(db, principal, "access.role_created", role.id, {"name": name})
    return _role_out(role)


@router.patch("/roles/{role_id}")
async def update_role(
    role_id: str,
    body: RoleBody,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    role = (
        await db.execute(
            select(Role).where(Role.id == role_id, _visible_roles(principal.tenant_id))
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found.")
    if role.is_system:
        raise HTTPException(status_code=400, detail="System roles cannot be modified.")
    name = body.name.strip()
    if name in SYSTEM_ROLE_NAMES:
        raise HTTPException(status_code=409, detail="That name is reserved for a system role.")
    duplicate = (
        await db.execute(
            select(Role.id).where(
                Role.tenant_id == principal.tenant_id,
                Role.name == name,
                Role.id != role.id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=409, detail="A role with that name already exists.")
    role.name = name
    role.description = body.description
    role.permissions_json = [p for p in body.permissions if p in PERMISSIONS]
    await db.commit()
    await _audit(db, principal, "access.role_updated", role.id, {})
    return _role_out(role)


@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: str,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    role = (
        await db.execute(
            select(Role).where(Role.id == role_id, _visible_roles(principal.tenant_id))
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found.")
    if role.is_system:
        raise HTTPException(status_code=400, detail="System roles cannot be deleted.")
    await db.execute(delete(UserRole).where(UserRole.role_id == role_id))
    # Remove from any group role assignments.
    groups = (
        await db.execute(select(Group).where(Group.tenant_id == principal.tenant_id))
    ).scalars().all()
    for g in groups:
        if role_id in (g.role_ids_json or []):
            g.role_ids_json = [x for x in g.role_ids_json if x != role_id]
    await db.delete(role)
    await db.commit()
    await _audit(db, principal, "access.role_deleted", role_id, {"name": role.name})
    return {"ok": True}


# ============================================================================== groups
class GroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    role_ids: list[str] = Field(default_factory=list)


def _group_out(g: Group) -> dict[str, Any]:
    return {
        "id": g.id,
        "name": g.name,
        "description": g.description,
        "role_ids": list(g.role_ids_json or []),
    }


@router.get("/groups")
async def list_groups(principal: Principal = Depends(_guard), db: AsyncSession = Depends(get_db)):
    groups = (
        await db.execute(
            select(Group)
            .where(Group.tenant_id == principal.tenant_id)
            .order_by(Group.name)
        )
    ).scalars().all()
    out = []
    for g in groups:
        member_count = len(
            (
                await db.execute(
                    select(UserGroup.user_id)
                    .join(User, User.id == UserGroup.user_id)
                    .where(
                        UserGroup.group_id == g.id,
                        User.tenant_id == principal.tenant_id,
                    )
                )
            ).scalars().all()
        )
        out.append({**_group_out(g), "member_count": member_count})
    return out


@router.post("/groups")
async def create_group(
    body: GroupBody,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    name = body.name.strip()
    await _validate_role_ids(db, principal, body.role_ids)
    exists = (
        await db.execute(
            select(Group).where(
                Group.name == name,
                Group.tenant_id == principal.tenant_id,
            )
        )
    ).scalars().first()
    if exists:
        raise HTTPException(status_code=409, detail="A group with that name already exists.")
    group = Group(
        tenant_id=principal.tenant_id,
        name=name,
        description=body.description,
        role_ids_json=list(body.role_ids),
    )
    db.add(group)
    await db.commit()
    await _audit(db, principal, "access.group_created", group.id, {"name": name})
    return _group_out(group)


@router.patch("/groups/{group_id}")
async def update_group(
    group_id: str,
    body: GroupBody,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    group = (
        await db.execute(
            select(Group).where(
                Group.id == group_id,
                Group.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found.")
    await _validate_role_ids(db, principal, body.role_ids)
    name = body.name.strip()
    duplicate = (
        await db.execute(
            select(Group.id).where(
                Group.tenant_id == principal.tenant_id,
                Group.name == name,
                Group.id != group.id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=409, detail="A group with that name already exists.")
    group.name = name
    group.description = body.description
    group.role_ids_json = list(body.role_ids)
    await db.commit()
    await _audit(db, principal, "access.group_updated", group.id, {})
    return _group_out(group)


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: str,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    group = (
        await db.execute(
            select(Group).where(
                Group.id == group_id,
                Group.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found.")
    await db.execute(delete(UserGroup).where(UserGroup.group_id == group_id))
    await db.delete(group)
    await db.commit()
    await _audit(db, principal, "access.group_deleted", group_id, {"name": group.name})
    return {"ok": True}


# ================================================================== identity providers
class IdPBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: str  # oidc | saml
    enabled: bool = False
    button_label: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


def _idp_out(p: IdentityProvider) -> dict[str, Any]:
    cfg = dict(p.config_json or {})
    # Never return secret values; expose a boolean so the UI can show "configured".
    for f in _SECRET_FIELDS:
        if cfg.get(f):
            cfg[f] = ""
            cfg[f"{f}_set"] = True
    return {
        "id": p.id,
        "name": p.name,
        "type": p.type,
        "enabled": p.enabled,
        "button_label": p.button_label,
        "config": cfg,
    }


def _merge_idp_config(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Encrypt secrets; preserve an existing secret when the incoming value is blank."""
    merged = dict(existing or {})
    for k, v in incoming.items():
        if k.endswith("_set"):
            continue
        if k in _SECRET_FIELDS:
            if v:  # only replace when a new secret is provided
                merged[k] = encrypt(v)
            # blank -> keep existing encrypted secret
        else:
            merged[k] = v
    return merged


@router.get("/identity-providers")
async def list_idps(principal: Principal = Depends(_guard), db: AsyncSession = Depends(get_db)):
    providers = (
        await db.execute(
            select(IdentityProvider)
            .where(IdentityProvider.tenant_id == principal.tenant_id)
            .order_by(IdentityProvider.created_at)
        )
    ).scalars().all()
    return [_idp_out(p) for p in providers]


@router.post("/identity-providers")
async def create_idp(
    body: IdPBody,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    if body.type not in ("oidc", "saml"):
        raise HTTPException(status_code=400, detail="type must be 'oidc' or 'saml'.")
    p = IdentityProvider(
        tenant_id=principal.tenant_id,
        name=body.name.strip(),
        type=body.type,
        enabled=body.enabled,
        button_label=body.button_label,
        config_json=_merge_idp_config({}, body.config),
    )
    db.add(p)
    await db.commit()
    await _audit(db, principal, "access.idp_created", p.id, {"name": p.name, "type": p.type})
    return _idp_out(p)


@router.patch("/identity-providers/{idp_id}")
async def update_idp(
    idp_id: str,
    body: IdPBody,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    p = (
        await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.id == idp_id,
                IdentityProvider.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Identity provider not found.")
    p.name = body.name.strip()
    p.enabled = body.enabled
    p.button_label = body.button_label
    p.config_json = _merge_idp_config(p.config_json or {}, body.config)
    p.updated_at = _now()
    await db.commit()
    await _audit(db, principal, "access.idp_updated", p.id, {})
    return _idp_out(p)


@router.post("/identity-providers/test")
async def test_idp(
    body: IdPBody,
    idp_id: str | None = None,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    """Best-effort validation of an OIDC/SAML provider config WITHOUT saving. For OIDC it
    fetches the discovery document + JWKS; for SAML it parses the signing certificate and
    checks required fields. When ``idp_id`` is given, blank secret/certificate fields fall
    back to the stored config so an Edit can be tested without re-entering them."""
    from app.auth import oidc as oidc_mod, saml as saml_mod

    cfg = {k: v for k, v in (body.config or {}).items() if not str(k).endswith("_set")}
    # For an existing provider, fill blank secrets/cert from the stored (decrypted) config.
    if idp_id:
        p = (
            await db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.id == idp_id,
                    IdentityProvider.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if p is not None:
            stored = dict(p.config_json or {})
            if not cfg.get("client_secret") and stored.get("client_secret"):
                try:
                    cfg["client_secret"] = decrypt(stored["client_secret"])
                except Exception:  # noqa: BLE001
                    pass
            if not cfg.get("certificate") and stored.get("certificate"):
                cfg["certificate"] = stored["certificate"]
    try:
        if body.type == "saml":
            checks = saml_mod.test_config(cfg)
        else:
            checks = await oidc_mod.test_config(cfg)
    except Exception as e:  # noqa: BLE001 - surface any unexpected error as a failed check
        checks = [{"name": "Test run", "ok": False, "detail": str(e)[:200], "critical": True}]
    critical_ok = all(c["ok"] for c in checks if c.get("critical", True))
    passed = sum(1 for c in checks if c["ok"])
    return {
        "ok": critical_ok,
        "checks": checks,
        "summary": f"{passed}/{len(checks)} checks passed."
        + ("" if critical_ok else " Some required checks failed."),
    }


@router.delete("/identity-providers/{idp_id}")
async def delete_idp(
    idp_id: str,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    p = (
        await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.id == idp_id,
                IdentityProvider.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Identity provider not found.")
    await db.delete(p)
    await db.commit()
    await _audit(db, principal, "access.idp_deleted", idp_id, {"name": p.name})
    return {"ok": True}


# ============================================================================ sessions
@router.get("/sessions")
async def list_sessions(
    include_expired: bool = False,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Session)
            .join(User, User.id == Session.user_id)
            .where(
                Session.revoked.is_(False),
                User.tenant_id == principal.tenant_id,
            )
            .order_by(Session.last_seen_at.desc())
        )
    ).scalars().all()
    user_ids = {s.user_id for s in rows}
    users = {}
    if user_ids:
        for u in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all():
            users[u.id] = u
    cfg = load_auth_settings()
    now = _now()
    out = []
    expired_count = 0
    for s in rows:
        expired = _session_expired(s, cfg, now)
        if expired:
            expired_count += 1
            if not include_expired:
                continue
        u = users.get(s.user_id)
        out.append(
            {
                "id": s.id,
                "user_id": s.user_id,
                "username": u.username if u else "(deleted)",
                "display_name": u.display_name if u else "",
                "ip": s.ip,
                "user_agent": s.user_agent,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "expired": expired,
                "status": "expired" if expired else "active",
            }
        )
    return {"sessions": out, "expired_count": expired_count}


@router.delete("/sessions/{session_id}")
async def revoke_one_session(
    session_id: str,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    s = (
        await db.execute(
            select(Session)
            .join(User, User.id == Session.user_id)
            .where(
                Session.id == session_id,
                User.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    s.revoked = True
    await db.commit()
    await _audit(db, principal, "access.session_revoked", session_id, {})
    return {"ok": True}


@router.post("/sessions/revoke-expired")
async def revoke_expired_sessions(
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    """Revoke every session that is already past its idle window or absolute lifetime.

    These are 'zombie' rows: invalid on the auth path but still listed until the
    periodic purge removes them. This gives admins one-click cleanup of the backlog.
    """
    rows = (
        await db.execute(
            select(Session)
            .join(User, User.id == Session.user_id)
            .where(
                Session.revoked.is_(False),
                User.tenant_id == principal.tenant_id,
            )
        )
    ).scalars().all()
    cfg = load_auth_settings()
    now = _now()
    revoked = 0
    for s in rows:
        if _session_expired(s, cfg, now):
            s.revoked = True
            revoked += 1
    if revoked:
        await db.commit()
    await _audit(db, principal, "access.expired_sessions_revoked", "sessions", {"count": revoked})
    return {"ok": True, "revoked": revoked}


# =================================================================== security policies
class PolicyPatch(BaseModel):
    local_login_enabled: bool | None = None
    allow_self_registration: bool | None = None
    password_min_length: int | None = Field(default=None, ge=1, le=128)
    password_require_complexity: bool | None = None
    max_failed_attempts: int | None = Field(default=None, ge=1, le=100)
    lockout_minutes: int | None = Field(default=None, ge=1, le=1440)
    session_idle_minutes: int | None = Field(default=None, ge=5, le=43200)
    session_absolute_minutes: int | None = Field(default=None, ge=5, le=131400)
    sso_auto_provision: bool | None = None
    sso_default_role: str | None = None


@router.get("/policies")
async def get_policies(_: Principal = Depends(_guard)):
    return {"values": load_auth_settings(), "defaults": AUTH_DEFAULTS}


@router.put("/policies")
async def update_policies(
    body: PolicyPatch,
    principal: Principal = Depends(_guard),
    db: AsyncSession = Depends(get_db),
):
    patch = body.model_dump(exclude_none=True)
    saved = save_auth_settings(patch)
    await _audit(db, principal, "access.policies_updated", "auth_settings", {"keys": list(patch.keys())})
    return saved


# Re-seed system roles on demand (e.g. after a permission catalog change).
@router.post("/roles/reseed")
async def reseed_system_roles(
    principal: Principal = Depends(_guard), db: AsyncSession = Depends(get_db)
):
    await seed_system_roles(db)
    await db.commit()
    await _audit(db, principal, "access.roles_reseeded", "system", {})
    return {"ok": True}

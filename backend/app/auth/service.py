"""Auth service: user/role/group queries, effective permissions, sessions, seeding."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password
from app.auth.permissions import SYSTEM_ROLES, role_rank
from app.auth.settings import load_auth_settings
from app.models.auth import Group, Role, Session, User, UserGroup, UserRole


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC for comparisons."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- roles
async def seed_system_roles(db: AsyncSession) -> dict[str, Role]:
    """Ensure the built-in roles exist (and keep their permission sets in sync)."""
    existing = {r.name: r for r in (await db.execute(select(Role))).scalars().all()}
    for name, desc, perms in SYSTEM_ROLES:
        r = existing.get(name)
        if r is None:
            r = Role(name=name, description=desc, is_system=True, permissions_json=list(perms))
            db.add(r)
            existing[name] = r
        else:
            # Keep system-role permissions authoritative from code.
            r.is_system = True
            r.permissions_json = list(perms)
            r.description = desc
    await db.flush()
    return existing


async def seed_admin(db: AsyncSession) -> None:
    """Bootstrap: create the initial admin user on first run.

    Username/password come from SEED_ADMIN_USERNAME / SEED_ADMIN_PASSWORD (defaults
    admin/admin). The bootstrap admin is forced to set a new password on first login
    (``must_change_password``) so a deployment never keeps the seeded/default credential.
    """
    from app.core.config import get_settings

    settings = get_settings()
    roles = await seed_system_roles(db)
    count = (await db.execute(select(User))).scalars().first()
    if count is not None:
        await db.commit()
        return
    admin = User(
        email="admin@local",
        username=settings.seed_admin_username,
        display_name="Administrator",
        password_hash=hash_password(settings.seed_admin_password),
        status="active",
        auth_source="local",
        must_change_password=True,
        tenant_id="default",
    )
    db.add(admin)
    await db.flush()
    db.add(UserRole(user_id=admin.id, role_id=roles["admin"].id))
    await db.commit()


# ----------------------------------------------------------------- effective perms
async def effective(db: AsyncSession, user: User) -> tuple[set[str], list[str]]:
    """Return (permissions, role_names) for a user across direct roles + group roles."""
    direct = (
        await db.execute(select(UserRole.role_id).where(UserRole.user_id == user.id))
    ).scalars().all()
    group_ids = (
        await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    ).scalars().all()
    role_ids: set[str] = set(direct)
    if group_ids:
        groups = (
            await db.execute(select(Group).where(Group.id.in_(group_ids)))
        ).scalars().all()
        for g in groups:
            role_ids.update(g.role_ids_json or [])
    if not role_ids:
        return set(), []
    roles = (await db.execute(select(Role).where(Role.id.in_(role_ids)))).scalars().all()
    perms: set[str] = set()
    names: list[str] = []
    for r in roles:
        perms.update(r.permissions_json or [])
        names.append(r.name)
    return perms, names


def primary_role(role_names: list[str]) -> str:
    if not role_names:
        return "user"
    return max(role_names, key=role_rank)


# --------------------------------------------------------------------- sessions
async def create_session(
    db: AsyncSession, user: User, ip: str | None, user_agent: str | None
) -> Session:
    cfg = load_auth_settings()
    sid = secrets.token_urlsafe(32)
    now = _now()
    sess = Session(
        id=sid,
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=int(cfg["session_absolute_minutes"])),
        ip=ip,
        user_agent=(user_agent or "")[:512],
    )
    db.add(sess)
    user.last_login_at = now
    user.failed_attempts = 0
    await db.commit()
    return sess


async def resolve_session(db: AsyncSession, sid: str) -> tuple[Session, User] | None:
    """Validate a session id against absolute + idle expiry; slide last_seen."""
    sess = await db.get(Session, sid)
    if sess is None or sess.revoked:
        return None
    now = _now()
    cfg = load_auth_settings()
    if _aware(sess.expires_at) and now > _aware(sess.expires_at):
        return None
    idle_cap = _aware(sess.last_seen_at) + timedelta(minutes=int(cfg["session_idle_minutes"]))
    if now > idle_cap:
        return None
    user = await db.get(User, sess.user_id)
    if user is None or user.status != "active":
        return None
    # Slide the idle window (cheap update).
    sess.last_seen_at = now
    await db.commit()
    return sess, user


async def revoke_session(db: AsyncSession, sid: str) -> None:
    sess = await db.get(Session, sid)
    if sess is not None:
        sess.revoked = True
        await db.commit()


async def revoke_all_for_user(db: AsyncSession, user_id: str) -> int:
    rows = (
        await db.execute(select(Session).where(Session.user_id == user_id, Session.revoked.is_(False)))
    ).scalars().all()
    for s in rows:
        s.revoked = True
    await db.commit()
    return len(rows)


async def purge_stale_sessions(db: AsyncSession, retain_days: int = 7) -> int:
    """Hard-delete expired or revoked sessions older than ``retain_days``.

    Sessions are validated on each request, but expired/revoked rows are never removed
    on their own — without this the ``sessions`` table grows unbounded and slows every
    login. Run periodically (see the scheduler)."""
    cutoff = _now() - timedelta(days=retain_days)
    result = await db.execute(
        delete(Session).where(
            (Session.revoked.is_(True)) | (Session.expires_at < cutoff)
        )
    )
    await db.commit()
    return result.rowcount or 0


# ------------------------------------------------------------------ user helpers
async def find_user_by_login(db: AsyncSession, login: str) -> User | None:
    login = (login or "").strip().lower()
    return (
        await db.execute(
            select(User).where((User.username == login) | (User.email == login))
        )
    ).scalars().first()


async def set_user_roles(db: AsyncSession, user_id: str, role_ids: list[str]) -> None:
    await db.execute(delete(UserRole).where(UserRole.user_id == user_id))
    for rid in dict.fromkeys(role_ids):
        db.add(UserRole(user_id=user_id, role_id=rid))
    await db.commit()


async def set_user_groups(db: AsyncSession, user_id: str, group_ids: list[str]) -> None:
    await db.execute(delete(UserGroup).where(UserGroup.user_id == user_id))
    for gid in dict.fromkeys(group_ids):
        db.add(UserGroup(user_id=user_id, group_id=gid))
    await db.commit()


async def user_role_ids(db: AsyncSession, user_id: str) -> list[str]:
    return list(
        (await db.execute(select(UserRole.role_id).where(UserRole.user_id == user_id)))
        .scalars()
        .all()
    )


async def user_group_ids(db: AsyncSession, user_id: str) -> list[str]:
    return list(
        (await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user_id)))
        .scalars()
        .all()
    )

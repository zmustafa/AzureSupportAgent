"""Public authentication endpoints: login/logout/me/change-password + SSO flows.

These power the login page. RBAC management lives in app.api.users (admin-only).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import oidc as oidc_mod
from app.auth import saml as saml_mod
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.provisioning import provision_sso_user
from app.auth.service import (
    create_session,
    effective,
    find_user_by_login,
    primary_role,
    revoke_session,
)
from app.auth.settings import load_auth_settings
from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import SESSION_COOKIE, Principal, get_principal
from app.models import AuditLog
from app.models.auth import IdentityProvider, User

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

_OIDC_STATE_COOKIE = "azsupagent_oidc_state"


def _aware_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _set_session_cookie(response: Response, sid: str) -> None:
    cfg = load_auth_settings()
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        max_age=int(cfg["session_absolute_minutes"]) * 60,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


async def _audit(db: AsyncSession, actor: str, action: str, meta: dict[str, Any]) -> None:
    db.add(
        AuditLog(tenant_id="default", actor_id=actor, action=action, metadata_json=meta)
    )
    await db.commit()


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


# --------------------------------------------------------------------------- config
@router.get("/config")
async def auth_config(db: AsyncSession = Depends(get_db)):
    """Non-sensitive info for the login page: local-login on/off + enabled SSO buttons."""
    cfg = load_auth_settings()
    providers = (
        await db.execute(select(IdentityProvider).where(IdentityProvider.enabled.is_(True)))
    ).scalars().all()
    return {
        "local_login_enabled": bool(cfg["local_login_enabled"]),
        "providers": [
            {
                "id": p.id,
                "type": p.type,
                "label": p.button_label or p.name,
            }
            for p in providers
        ],
    }


# ---------------------------------------------------------------------------- me
@router.get("/me")
async def me(principal: Principal = Depends(get_principal)):
    return {
        "subject": principal.subject,
        "email": principal.email,
        "tenant_id": principal.tenant_id,
        "role": principal.role,
        "permissions": sorted(principal.permissions),
        "display_name": principal.display_name,
        "auth_source": principal.auth_source,
        "must_change_password": principal.must_change_password,
    }


# ------------------------------------------------------------------------- login
class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    cfg = load_auth_settings()
    if not cfg["local_login_enabled"]:
        raise HTTPException(status_code=403, detail="Local login is disabled.")

    user = await find_user_by_login(db, body.username)
    # Generic failure to avoid user enumeration.
    fail = HTTPException(status_code=401, detail="Invalid username or password.")

    if user is None or user.status != "active":
        await _audit(db, body.username, "auth.login_failed", {"reason": "unknown_or_inactive"})
        raise fail

    # Lockout check. SQLite returns naive datetimes; treat them as UTC for comparison.
    if user.locked_until:
        from datetime import timezone

        lu = user.locked_until if user.locked_until.tzinfo else user.locked_until.replace(tzinfo=timezone.utc)
        if _aware_now() < lu:
            raise HTTPException(status_code=423, detail="Account temporarily locked. Try again later.")

    if not verify_password(user.password_hash, body.password):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= int(cfg["max_failed_attempts"]):
            from datetime import timedelta

            user.locked_until = _aware_now() + timedelta(minutes=int(cfg["lockout_minutes"]))
            user.failed_attempts = 0
        await db.commit()
        await _audit(db, user.username, "auth.login_failed", {"reason": "bad_password"})
        raise fail

    # Success: rehash if params changed, reset counters, create session.
    if needs_rehash(user.password_hash or ""):
        user.password_hash = hash_password(body.password)
    user.locked_until = None
    sess = await create_session(db, user, _client_ip(request), request.headers.get("user-agent"))
    _set_session_cookie(response, sess.id)
    await _audit(db, user.username, "auth.login", {"source": "local"})
    perms, role_names = await effective(db, user)
    return {
        "ok": True,
        "user": {
            "subject": user.id,
            "email": user.email,
            "role": primary_role(role_names),
            "permissions": sorted(perms),
            "display_name": user.display_name or user.username,
            "must_change_password": user.must_change_password,
        },
    }


@router.post("/logout")
async def logout(
    response: Response,
    azsupagent_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    if azsupagent_session:
        await revoke_session(db, azsupagent_session)
    _clear_session_cookie(response)
    return {"ok": True}


class ChangePwBody(BaseModel):
    current_password: str | None = None
    new_password: str = Field(min_length=1)


@router.post("/change-password")
async def change_password(
    body: ChangePwBody,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    cfg = load_auth_settings()
    if len(body.new_password) < int(cfg["password_min_length"]):
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {cfg['password_min_length']} characters.",
        )
    if cfg["password_require_complexity"]:
        pw = body.new_password
        if not (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)):
            raise HTTPException(status_code=400, detail="Password must include upper, lower, and a digit.")
    user = await db.get(User, principal.subject)
    if user is None or user.auth_source != "local":
        raise HTTPException(status_code=400, detail="Password change is only for local accounts.")
    # If the user already has a password, require the current one (unless forced reset).
    if user.password_hash and not user.must_change_password:
        if not body.current_password or not verify_password(user.password_hash, body.current_password):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    await db.commit()
    await _audit(db, user.username, "auth.password_changed", {})
    return {"ok": True}


# ----------------------------------------------------------------------- OIDC SSO
def _redirect_uri_oidc(idp_id: str) -> str:
    return settings.public_base_url.rstrip("/") + f"/auth/oidc/{idp_id}/callback"


async def _get_idp(db: AsyncSession, idp_id: str, kind: str) -> IdentityProvider:
    idp = await db.get(IdentityProvider, idp_id)
    if idp is None or not idp.enabled or idp.type != kind:
        raise HTTPException(status_code=404, detail="Identity provider not found or disabled.")
    return idp


@router.get("/oidc/{idp_id}/login")
async def oidc_login(idp_id: str, db: AsyncSession = Depends(get_db)):
    idp = await _get_idp(db, idp_id, "oidc")
    authorize_url, state_cookie = await oidc_mod.build_authorize_url(
        idp.config_json, _redirect_uri_oidc(idp_id)
    )
    resp = RedirectResponse(authorize_url, status_code=302)
    resp.set_cookie(
        _OIDC_STATE_COOKIE, state_cookie, httponly=True, secure=settings.cookie_secure,
        samesite="lax", max_age=oidc_mod.STATE_TTL_SECONDS, path="/",
    )
    return resp


@router.get("/oidc/{idp_id}/callback")
async def oidc_callback(
    idp_id: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    azsupagent_oidc_state: str | None = Cookie(default=None, alias=_OIDC_STATE_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    front = settings.frontend_origin
    if error or not code:
        return RedirectResponse(f"{front}/login?error=sso_failed", status_code=302)
    saved = oidc_mod.decode_state(azsupagent_oidc_state or "")
    if not saved or saved.get("state") != state:
        return RedirectResponse(f"{front}/login?error=sso_state", status_code=302)
    idp = await _get_idp(db, idp_id, "oidc")
    try:
        claims = await oidc_mod.exchange_and_validate(
            idp.config_json,
            code=code,
            redirect_uri=_redirect_uri_oidc(idp_id),
            verifier=saved["verifier"],
            nonce=saved.get("nonce", ""),
        )
        identity = oidc_mod.extract_identity(claims, idp.config_json)
        user = await provision_sso_user(db, idp, **identity)
    except Exception:  # noqa: BLE001
        return RedirectResponse(f"{front}/login?error=sso_failed", status_code=302)
    if user is None:
        return RedirectResponse(f"{front}/login?error=sso_denied", status_code=302)
    sess = await create_session(db, user, _client_ip(request), request.headers.get("user-agent"))
    await _audit(db, user.username, "auth.login", {"source": "oidc", "idp": idp.name})
    resp = RedirectResponse(f"{front}/", status_code=302)
    _set_session_cookie(resp, sess.id)
    resp.delete_cookie(_OIDC_STATE_COOKIE, path="/")
    return resp


# ----------------------------------------------------------------------- SAML SSO
@router.get("/saml/{idp_id}/login")
async def saml_login(idp_id: str, db: AsyncSession = Depends(get_db)):
    idp = await _get_idp(db, idp_id, "saml")
    url = saml_mod.build_authn_request(idp.config_json, settings.public_base_url, idp_id)
    return RedirectResponse(url, status_code=302)


@router.get("/saml/{idp_id}/metadata")
async def saml_metadata(idp_id: str):
    xml = saml_mod.sp_metadata(settings.public_base_url, idp_id)
    return Response(content=xml, media_type="application/xml")


@router.post("/saml/{idp_id}/acs")
async def saml_acs(idp_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    front = settings.frontend_origin
    form = await request.form()
    saml_response = form.get("SAMLResponse")
    if not saml_response:
        return RedirectResponse(f"{front}/login?error=saml_missing", status_code=302)
    idp = await _get_idp(db, idp_id, "saml")
    try:
        identity = saml_mod.validate_response(str(saml_response), idp.config_json)
        user = await provision_sso_user(db, idp, **identity)
    except Exception:  # noqa: BLE001
        return RedirectResponse(f"{front}/login?error=saml_failed", status_code=302)
    if user is None:
        return RedirectResponse(f"{front}/login?error=sso_denied", status_code=302)
    sess = await create_session(db, user, _client_ip(request), request.headers.get("user-agent"))
    await _audit(db, user.username, "auth.login", {"source": "saml", "idp": idp.name})
    resp = RedirectResponse(f"{front}/", status_code=302)
    _set_session_cookie(resp, sess.id)
    return resp

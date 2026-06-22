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
from app.auth.ip_lockout import ip_lockout
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
_SAML_REQ_COOKIE = "azsupagent_saml_req"


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
    """Resolve the originating client IP.

    Only honors ``X-Forwarded-For`` when the request's *direct* peer is a
    pre-configured trusted reverse proxy (``settings.trusted_proxies``). Otherwise
    the header is ignored so an attacker can't spoof their IP for audit logs or the
    per-IP brute-force counter. With no proxy configured (default), we always fall
    back to ``request.client.host``.
    """
    direct = request.client.host if request.client else None
    trusted = {ip.strip() for ip in (settings.trusted_proxies or "").split(",") if ip.strip()}
    if direct and direct in trusted:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Standard format is `client, proxy1, proxy2, ...` — the first entry is
            # the original client (the proxy chain prepends as it forwards).
            first = xff.split(",")[0].strip()
            if first:
                return first
    return direct


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

    client_ip = _client_ip(request)

    # Per-IP brute-force gate (auto-unlocks). Runs BEFORE the username lookup so a
    # locked-out attacker can't use the endpoint as a username oracle either.
    ip_enabled = bool(cfg.get("ip_rate_limit_enabled", True))
    ip_max = int(cfg.get("ip_rate_limit_max_attempts", 15))
    ip_window = float(cfg.get("ip_rate_limit_window_seconds", 300))
    ip_lock_secs = float(cfg.get("ip_rate_limit_lockout_seconds", 900))
    if ip_enabled and client_ip:
        is_locked, remaining = await ip_lockout.check_locked(
            db, client_ip, lockout_seconds=ip_lock_secs
        )
        if is_locked:
            retry_after = max(1, int(remaining))
            await _audit(db, body.username, "auth.login_blocked",
                         {"reason": "ip_rate_limit", "ip": client_ip, "retry_after_s": retry_after})
            raise HTTPException(
                status_code=429,
                detail="Too many failed sign-in attempts from this address. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )

    user = await find_user_by_login(db, body.username)
    # Generic failure to avoid user enumeration.
    fail = HTTPException(status_code=401, detail="Invalid username or password.")

    async def _ip_failure() -> None:
        if not (ip_enabled and client_ip):
            return
        await ip_lockout.record_failure(
            db,
            client_ip,
            max_attempts=ip_max,
            window_seconds=ip_window,
            lockout_seconds=ip_lock_secs,
        )

    if user is None or user.status != "active":
        await _audit(db, body.username, "auth.login_failed", {"reason": "unknown_or_inactive", "ip": client_ip})
        await _ip_failure()
        raise fail

    # Lockout check. SQLite returns naive datetimes; treat them as UTC for comparison.
    if user.locked_until:
        from datetime import timezone

        lu = user.locked_until if user.locked_until.tzinfo else user.locked_until.replace(tzinfo=timezone.utc)
        if _aware_now() < lu:
            await _ip_failure()
            raise HTTPException(status_code=423, detail="Account temporarily locked. Try again later.")

    if not verify_password(user.password_hash, body.password):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= int(cfg["max_failed_attempts"]):
            from datetime import timedelta

            user.locked_until = _aware_now() + timedelta(minutes=int(cfg["lockout_minutes"]))
            user.failed_attempts = 0
        await db.commit()
        await _audit(db, user.username, "auth.login_failed", {"reason": "bad_password", "ip": client_ip})
        await _ip_failure()
        raise fail

    # Success: rehash if params changed, reset counters, create session.
    if needs_rehash(user.password_hash or ""):
        user.password_hash = hash_password(body.password)
    user.locked_until = None
    # Clear the per-IP counter on a successful login so the user isn't punished
    # for past mistakes from the same network.
    await ip_lockout.clear(db, client_ip)
    sess = await create_session(db, user, client_ip, request.headers.get("user-agent"))
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
    # The auth router is mounted under the global ``/api`` prefix (see app.main:
    # ``api = APIRouter(prefix="/api")``), so the callback the IdP redirects back to is
    # ``/api/auth/oidc/{id}/callback``. The redirect_uri sent in the authorize request
    # MUST include ``/api`` or Entra/OIDC returns the user to a path that doesn't match
    # any backend route (it falls through to the SPA / 404s, and the auth code is never
    # exchanged). This value must also match the Redirect URI registered on the app.
    return settings.public_base_url.rstrip("/") + f"/api/auth/oidc/{idp_id}/callback"


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
    url, req_id = saml_mod.build_authn_request(idp.config_json, settings.public_base_url, idp_id)
    resp = RedirectResponse(url, status_code=302)
    # Single-use encrypted cookie carrying the AuthnRequest ID so the ACS can bind the
    # response (InResponseTo) and reject replays / unsolicited responses. The ACS is a
    # cross-site POST from the IdP, so the cookie needs SameSite=None — which browsers
    # only honour with Secure (HTTPS). Fall back to Lax for local HTTP dev.
    _samesite = "none" if settings.cookie_secure else "lax"
    resp.set_cookie(
        _SAML_REQ_COOKIE,
        saml_mod.encode_relay({"id": req_id, "idp": idp_id}),
        httponly=True,
        secure=settings.cookie_secure,
        samesite=_samesite,  # type: ignore[arg-type]
        max_age=saml_mod.RELAY_TTL_SECONDS,
        path="/",
    )
    return resp


@router.get("/saml/{idp_id}/metadata")
async def saml_metadata(idp_id: str):
    xml = saml_mod.sp_metadata(settings.public_base_url, idp_id)
    return Response(content=xml, media_type="application/xml")


@router.post("/saml/{idp_id}/acs")
async def saml_acs(
    idp_id: str,
    request: Request,
    azsupagent_saml_req: str | None = Cookie(default=None, alias=_SAML_REQ_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    front = settings.frontend_origin
    form = await request.form()
    saml_response = form.get("SAMLResponse")
    if not saml_response:
        return RedirectResponse(f"{front}/login?error=saml_missing", status_code=302)
    idp = await _get_idp(db, idp_id, "saml")
    # Bind the response to the AuthnRequest we issued (single-use cookie). Missing or
    # foreign request state ⇒ unsolicited / replayed response ⇒ reject before parsing.
    relay = saml_mod.decode_relay(azsupagent_saml_req or "")
    if not relay or relay.get("idp") != idp_id:
        return RedirectResponse(f"{front}/login?error=saml_state", status_code=302)
    try:
        identity = saml_mod.validate_response(
            str(saml_response),
            idp.config_json,
            sp_entity_id=saml_mod.sp_entity_id(settings.public_base_url),
            acs_url=saml_mod.acs_url(settings.public_base_url, idp_id),
            expected_in_response_to=relay.get("id"),
        )
        user = await provision_sso_user(db, idp, **identity)
    except Exception:  # noqa: BLE001
        resp = RedirectResponse(f"{front}/login?error=saml_failed", status_code=302)
        resp.delete_cookie(_SAML_REQ_COOKIE, path="/")
        return resp
    if user is None:
        resp = RedirectResponse(f"{front}/login?error=sso_denied", status_code=302)
        resp.delete_cookie(_SAML_REQ_COOKIE, path="/")
        return resp
    sess = await create_session(db, user, _client_ip(request), request.headers.get("user-agent"))
    await _audit(db, user.username, "auth.login", {"source": "saml", "idp": idp.name})
    resp = RedirectResponse(f"{front}/", status_code=302)
    _set_session_cookie(resp, sess.id)
    # Consume the single-use request cookie so the same response can't be replayed.
    resp.delete_cookie(_SAML_REQ_COOKIE, path="/")
    return resp

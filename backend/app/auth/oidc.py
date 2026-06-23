"""OIDC (OpenID Connect) login — covers Microsoft Entra ID, Okta, Auth0, Google, and
any compliant provider. Authorization Code flow with PKCE; id_token validated against
the provider's JWKS. The short-lived flow state is carried in a Fernet-encrypted cookie
(no server state needed), and users are JIT-provisioned via app.auth.provisioning.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from app.core.crypto import decrypt, encrypt

_DISCOVERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWK_CLIENTS: dict[str, PyJWKClient] = {}
# Bound the module-level caches (one entry per unique issuer / JWKS endpoint). A handful
# of IdPs is normal; the cap stops pathological growth if many distinct URLs appear.
_CACHE_MAX = 32
STATE_TTL_SECONDS = 600


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = _b64url(secrets.token_bytes(40))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


async def discover(issuer: str, discovery_url: str | None) -> dict[str, Any]:
    url = discovery_url or issuer.rstrip("/") + "/.well-known/openid-configuration"
    cached = _DISCOVERY_CACHE.get(url)
    now = time.time()
    if cached and now - cached[0] < 3600:
        return cached[1]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        doc = resp.json()
    _DISCOVERY_CACHE[url] = (now, doc)
    if len(_DISCOVERY_CACHE) > _CACHE_MAX:
        # Evict the oldest entry by cached-at timestamp.
        oldest = min(_DISCOVERY_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _DISCOVERY_CACHE.pop(oldest, None)
    return doc


def encode_state(payload: dict[str, Any]) -> str:
    payload = {**payload, "ts": int(time.time())}
    return encrypt(json.dumps(payload))


def decode_state(token: str) -> dict[str, Any] | None:
    try:
        raw = decrypt(token)
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if int(time.time()) - int(data.get("ts", 0)) > STATE_TTL_SECONDS:
        return None
    return data


async def test_config(idp_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort validation of an OIDC provider config. Returns a list of checks
    ``{name, ok, detail, critical}`` — no login is performed (that needs a real user), so
    this validates the *discovery* surface: required fields, the well-known document, the
    advertised endpoints, and that the JWKS exposes signing keys."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "critical": critical})

    issuer = (idp_cfg.get("issuer") or "").strip()
    discovery_url = (idp_cfg.get("discovery_url") or "").strip()
    client_id = (idp_cfg.get("client_id") or "").strip()

    add("Issuer / Discovery URL set", bool(issuer or discovery_url),
        "Provide an Issuer URL (or an explicit Discovery URL)." if not (issuer or discovery_url)
        else (discovery_url or (issuer.rstrip("/") + "/.well-known/openid-configuration")))
    add("Client ID set", bool(client_id),
        "Client ID is required." if not client_id else client_id)
    if not (issuer or discovery_url):
        return checks

    doc: dict[str, Any] | None = None
    try:
        doc = await discover(issuer, discovery_url or None)
        add("Discovery document reachable", True, "Fetched the OpenID configuration.")
    except httpx.HTTPStatusError as e:
        add("Discovery document reachable", False, f"HTTP {e.response.status_code} fetching the discovery URL.")
    except Exception as e:  # noqa: BLE001
        add("Discovery document reachable", False, f"Could not fetch discovery document: {str(e)[:160]}")

    if doc:
        for ep in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            add(f"{ep} present", bool(doc.get(ep)), doc.get(ep) or f"Missing {ep} in the discovery document.")
        doc_issuer = (doc.get("issuer") or "").strip()
        if issuer and doc_issuer:
            add("Issuer matches discovery", doc_issuer.rstrip("/") == issuer.rstrip("/"),
                f"Configured '{issuer}' vs discovery '{doc_issuer}'.", critical=False)
        jwks_uri = doc.get("jwks_uri")
        if jwks_uri:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(jwks_uri)
                    r.raise_for_status()
                    keys = (r.json() or {}).get("keys") or []
                add("JWKS exposes signing keys", len(keys) > 0,
                    f"{len(keys)} signing key(s) published." if keys else "No keys at jwks_uri.")
            except Exception as e:  # noqa: BLE001
                add("JWKS exposes signing keys", False, f"Could not fetch JWKS: {str(e)[:160]}")
    return checks


async def build_authorize_url(idp_cfg: dict[str, Any], redirect_uri: str) -> tuple[str, str]:
    """Return (authorize_url, encrypted_state_cookie)."""
    doc = await discover(idp_cfg.get("issuer", ""), idp_cfg.get("discovery_url"))
    verifier, challenge = new_pkce()
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    scopes = idp_cfg.get("scopes") or "openid email profile"
    params = {
        "client_id": idp_cfg["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    # Optional OIDC `prompt`. Without it, the IdP silently reuses an existing browser session
    # (Entra SSO) and never shows an account picker — so a shared machine always signs in the
    # already-authenticated user. Set "select_account" (the form's "Select account upon sign in"
    # toggle) to always show the chooser; "login" forces re-auth. Empty = default silent SSO.
    prompt = (idp_cfg.get("login_prompt") or "").strip()
    if prompt:
        params["prompt"] = prompt
    authorize_url = doc["authorization_endpoint"] + "?" + urlencode(params)
    cookie = encode_state({"state": state, "verifier": verifier, "nonce": nonce})
    return authorize_url, cookie


async def exchange_and_validate(
    idp_cfg: dict[str, Any],
    *,
    code: str,
    redirect_uri: str,
    verifier: str,
    nonce: str,
) -> dict[str, Any]:
    """Exchange the auth code, validate the id_token, and return its claims."""
    doc = await discover(idp_cfg.get("issuer", ""), idp_cfg.get("discovery_url"))
    client_secret = idp_cfg.get("client_secret", "")
    if client_secret.startswith("enc:"):
        client_secret = decrypt(client_secret)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": idp_cfg["client_id"],
        "code_verifier": verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(doc["token_endpoint"], data=data)
        if resp.status_code >= 400:
            raise RuntimeError(f"Token exchange failed: {resp.text[:300]}")
        tok = resp.json()
    id_token = tok.get("id_token")
    if not id_token:
        raise RuntimeError("No id_token returned by the identity provider.")
    jwks_uri = doc["jwks_uri"]
    jwk_client = _JWK_CLIENTS.get(jwks_uri) or PyJWKClient(jwks_uri)
    _JWK_CLIENTS[jwks_uri] = jwk_client
    if len(_JWK_CLIENTS) > _CACHE_MAX:
        _JWK_CLIENTS.pop(next(iter(_JWK_CLIENTS)), None)
    signing_key = jwk_client.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256", "RS384", "RS512", "ES256"],
        audience=idp_cfg["client_id"],
        issuer=doc.get("issuer") or idp_cfg.get("issuer"),
        options={"verify_aud": True},
    )
    if nonce and claims.get("nonce") and claims["nonce"] != nonce:
        raise RuntimeError("OIDC nonce mismatch.")
    return claims


def extract_identity(claims: dict[str, Any], idp_cfg: dict[str, Any]) -> dict[str, Any]:
    group_claim = idp_cfg.get("group_claim") or "groups"
    groups = claims.get(group_claim) or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    # email_verified: honour the provider's claim when present; default True when absent
    # (most enterprise IdPs only emit verified addresses and omit the claim). An explicit
    # false blocks email-based account linking in provisioning (account-takeover defense).
    ev = claims.get("email_verified")
    email_verified = True if ev is None else bool(ev)
    return {
        "external_id": str(claims.get("sub", "")),
        "email": claims.get("email") or claims.get("preferred_username") or "",
        "display_name": claims.get("name") or claims.get("preferred_username") or "",
        "groups": [str(g) for g in groups],
        "email_verified": email_verified,
    }

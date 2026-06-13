"""ChatGPT (Codex) OAuth token manager — self-contained.

Tokens are obtained via an interactive PKCE OAuth sign-in (browser or paste-URL) and
stored ONLY in this app's own data dir. This module never reads or writes the Codex
CLI's ~/.codex/auth.json — sign-in is entirely owned by the app.

Stored token file (backend/.data/chatgpt_oauth_tokens.json):
    { "access_token": "...", "refresh_token": "...", "account_id": "...",
      "id_token": "...", "last_refresh": "2026-06-06T..." }
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

_DATA_DIR = Path(__file__).resolve().parents[2] / ".data"
# Chromium's SingletonLock can't be created on a network share (Azure Files / SMB), so
# the browser profile lives on local-writable storage (BROWSER_PROFILE_DIR, e.g. /tmp in
# a container) — NOT on the data volume. Token/PKCE files below stay on the volume.
_PROFILE_BASE = Path(os.environ.get("BROWSER_PROFILE_DIR") or _DATA_DIR)
# This app's own token store — NOT ~/.codex/auth.json.
_TOKENS_PATH = _DATA_DIR / "chatgpt_oauth_tokens.json"
# Public OAuth client id used by the Codex CLI (same as the C# app).
_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
# Redirect URI the Codex OAuth client is registered with (the browser is redirected
# here after sign-in; we intercept it — nothing actually needs to listen on 1455).
_REDIRECT_URI = "http://localhost:1455/auth/callback"
_SCOPE = "openid profile email offline_access"

# Pending PKCE state for an in-progress interactive sign-in (persisted so the paste-URL
# completion can run as a separate request, even across a backend restart).
_PKCE_PATH = _DATA_DIR / "chatgpt_oauth_pkce.json"

# Access tokens are JWTs (~ a few minutes to an hour). Refresh a bit early.
_REFRESH_SKEW_SECONDS = 120

# In-memory cache of the most recently refreshed token so we don't refresh per request.
_cache: dict[str, Any] = {"access_token": "", "expires_at": 0.0, "account_id": ""}


def _read_tokens() -> dict[str, Any]:
    """Read this app's stored ChatGPT tokens (never ~/.codex/auth.json)."""
    if not _TOKENS_PATH.exists():
        return {}
    try:
        data = json.loads(_TOKENS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _jwt_exp(token: str) -> float:
    """Best-effort: read the `exp` claim from a JWT access token (epoch seconds)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload.get("exp", 0))
    except Exception:  # noqa: BLE001
        return 0.0


def _jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT's payload claims (e.g. to read the account id)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        return {}


def _write_tokens(access_token: str, refresh_token: str, account_id: str, id_token: str = "") -> None:
    """Persist tokens to this app's own store (preserving prior fields when blank)."""
    data = _read_tokens()
    data["access_token"] = access_token
    if refresh_token:
        data["refresh_token"] = refresh_token
    if account_id:
        data["account_id"] = account_id
    if id_token:
        data["id_token"] = id_token
    data["last_refresh"] = datetime.now(timezone.utc).isoformat()
    try:
        _TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKENS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def status() -> dict[str, Any]:
    """Non-sensitive status for the admin UI."""
    data = _read_tokens()
    access = data.get("access_token", "")
    if not access:
        return {"signed_in": False, "has_token": False, "expired": True, "account_id": ""}
    exp = _jwt_exp(access)
    return {
        "signed_in": True,
        "has_token": True,
        "expired": bool(exp and exp <= time.time()),
        "account_id": data.get("account_id", ""),
    }


async def _refresh(refresh_token: str) -> dict[str, Any] | None:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _OAUTH_CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(_TOKEN_URL, data=payload)
        except httpx.HTTPError:
            return None
    if resp.status_code != 200:
        return None
    return resp.json()


async def get_token() -> tuple[str, str]:
    """Return (access_token, account_id), refreshing via OAuth when expired.

    Raises RuntimeError if no app sign-in exists.
    """
    data = _read_tokens()
    access_token = data.get("access_token", "")
    if not access_token:
        raise RuntimeError(
            "Not signed in to ChatGPT. Open the admin AI Provider settings and click "
            "'Sign in with ChatGPT'."
        )
    refresh_token = data.get("refresh_token", "")
    account_id = data.get("account_id", "")

    exp = _jwt_exp(access_token)
    if exp and exp - _REFRESH_SKEW_SECONDS > time.time():
        return access_token, account_id

    # Expired (or unknown expiry): try to refresh.
    if refresh_token:
        refreshed = await _refresh(refresh_token)
        if refreshed and refreshed.get("access_token"):
            new_access = refreshed["access_token"]
            new_refresh = refreshed.get("refresh_token", refresh_token)
            _write_tokens(new_access, new_refresh, account_id)
            return new_access, account_id

    # Couldn't refresh — return the (possibly stale) token; the caller surfaces a 401.
    return access_token, account_id


async def force_refresh() -> dict[str, Any]:
    """Force a token refresh and return the new status (for the admin Refresh button)."""
    data = _read_tokens()
    refresh_token = data.get("refresh_token", "")
    if not refresh_token:
        raise RuntimeError("No refresh token. Sign in with ChatGPT first.")
    refreshed = await _refresh(refresh_token)
    if not refreshed or not refreshed.get("access_token"):
        raise RuntimeError("ChatGPT token refresh failed. Sign in with ChatGPT again.")
    _write_tokens(
        refreshed["access_token"],
        refreshed.get("refresh_token", refresh_token),
        data.get("account_id", ""),
    )
    return status()


# ---------------------------------------------------------------------------
# Interactive sign-in (PKCE authorization-code flow) — ported from BuddyAI's
# ChatGPTOAuthImportForm. Lets the user sign in with a browser instead of
# depending on `codex login` having written ~/.codex/auth.json.
# ---------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _new_pkce() -> dict[str, str]:
    """Generate a fresh PKCE verifier/challenge + state, persist, and return them."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_hex(16)
    pending = {"verifier": verifier, "state": state, "created_at": time.time()}
    try:
        _PKCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PKCE_PATH.write_text(json.dumps(pending), encoding="utf-8")
    except OSError:
        pass
    return {"verifier": verifier, "challenge": challenge, "state": state}


def _load_pending_pkce() -> dict[str, Any] | None:
    try:
        return json.loads(_PKCE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _clear_pending_pkce() -> None:
    try:
        _PKCE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def build_authorize_url() -> dict[str, str]:
    """Create a new PKCE challenge and return the OpenAI authorize URL to open.

    The caller (admin UI) navigates a browser here; after sign-in the browser is
    redirected to the (non-listening) redirect URI with `?code=...&state=...`.
    """
    pkce = _new_pkce()
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": _OAUTH_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "scope": _SCOPE,
        "code_challenge": pkce["challenge"],
        "code_challenge_method": "S256",
        "state": pkce["state"],
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return {"authorize_url": f"{_AUTHORIZE_URL}?{urlencode(params)}", "state": pkce["state"]}


async def _exchange_code(code: str, verifier: str) -> dict[str, Any]:
    """Exchange an authorization code for tokens (PKCE)."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "client_id": _OAUTH_CLIENT_ID,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_TOKEN_URL, data=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _persist_new_login(token_resp: dict[str, Any]) -> None:
    """Persist tokens from an interactive login into this app's own token store."""
    access = token_resp.get("access_token", "")
    refresh = token_resp.get("refresh_token", "")
    id_token = token_resp.get("id_token", "")
    # The account id lives in the access/id token claims (chatgpt_account_id / org).
    claims = _jwt_claims(access) or _jwt_claims(id_token)
    auth = (claims.get("https://api.openai.com/auth") or {}) if isinstance(claims, dict) else {}
    account_id = (
        auth.get("chatgpt_account_id")
        or auth.get("organization_id")
        or claims.get("account_id", "")
        or ""
    )
    _write_tokens(access, refresh, account_id, id_token=id_token)


def _extract_code_and_state(callback_url: str) -> tuple[str, str]:
    """Pull `code` and `state` from a pasted redirect URL (or a raw `code=...` query)."""
    text = callback_url.strip()
    query = urlparse(text).query or text.lstrip("?")
    params = parse_qs(query)
    if params.get("error"):
        raise RuntimeError(f"Authorization failed: {params['error'][0]}")
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    if not code:
        raise RuntimeError(
            "No authorization code found in the pasted URL. Paste the full URL you were "
            "redirected to (it should contain '?code=...')."
        )
    return code, state


async def complete_with_callback_url(callback_url: str) -> dict[str, Any]:
    """Finish an interactive sign-in from a pasted redirect URL (the prod-friendly path).

    Works even when the app is not hosted on localhost: the user signs in in their own
    browser, gets redirected to the (non-loading) callback URL, and pastes it here.
    """
    pending = _load_pending_pkce()
    if not pending or not pending.get("verifier"):
        raise RuntimeError(
            "No sign-in is in progress. Click 'Sign in with ChatGPT' first to start the "
            "flow, then paste the redirected URL."
        )
    code, state = _extract_code_and_state(callback_url)
    if state and pending.get("state") and state != pending["state"]:
        raise RuntimeError("State mismatch — start the sign-in again for security.")
    token_resp = await _exchange_code(code, pending["verifier"])
    _persist_new_login(token_resp)
    _clear_pending_pkce()
    return status()


def _capture_code_sync(authorize_url: str, timeout_ms: int) -> str:
    """Open a browser to the authorize URL and capture the `code` from the redirect to
    the callback URI. Blocking; run in a worker thread. Returns the code (or "")."""
    from playwright.sync_api import sync_playwright

    captured: dict[str, str] = {"code": ""}
    _PROFILE_BASE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(_PROFILE_BASE / "chatgpt_oauth_profile"),
            headless=False,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            def on_nav(req: Any) -> None:
                try:
                    url = req.url
                    if url.startswith(_REDIRECT_URI):
                        qs = parse_qs(urlparse(url).query)
                        code = (qs.get("code") or [""])[0]
                        if code:
                            captured["code"] = code
                except Exception:  # noqa: BLE001
                    pass

            # Watch both requests and frame navigations for the redirect.
            context.on("request", on_nav)
            page.on("framenavigated", lambda frame: on_nav(frame))  # type: ignore[arg-type]

            try:
                page.goto(authorize_url, wait_until="domcontentloaded", timeout=min(timeout_ms, 60_000))
            except Exception:  # noqa: BLE001 - navigation to localhost callback will fail; that's fine
                pass

            deadline = time.time() + timeout_ms / 1000
            while time.time() < deadline and not captured["code"]:
                try:
                    # Also check the current URL in case the listener missed it.
                    cur = page.url
                    if cur.startswith(_REDIRECT_URI):
                        qs = parse_qs(urlparse(cur).query)
                        code = (qs.get("code") or [""])[0]
                        if code:
                            captured["code"] = code
                            break
                    page.wait_for_timeout(400)
                except Exception:  # noqa: BLE001
                    break
            return captured["code"]
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


async def interactive_login(timeout_seconds: int = 240) -> dict[str, Any]:
    """Launch a browser for ChatGPT OAuth sign-in, capture the code, and store tokens.

    Mirrors BuddyAI's ChatGPTOAuthImportForm but uses Playwright. Falls back gracefully:
    if the browser can't capture the code (e.g. headless/remote host), the admin can use
    the paste-URL path (build_authorize_url + complete_with_callback_url) instead.
    """
    info = build_authorize_url()
    pending = _load_pending_pkce() or {}
    verifier = pending.get("verifier", "")
    code = await asyncio.to_thread(_capture_code_sync, info["authorize_url"], timeout_seconds * 1000)
    if not code:
        raise RuntimeError(
            "Sign-in window closed or timed out before the ChatGPT authorization code "
            "could be captured. You can instead use 'Get sign-in link' and paste the "
            "redirected URL."
        )
    token_resp = await _exchange_code(code, verifier)
    _persist_new_login(token_resp)
    _clear_pending_pkce()
    return status()


def sign_out() -> dict[str, Any]:
    """Forget the stored ChatGPT tokens and any in-progress sign-in / browser profile."""
    try:
        _TOKENS_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    _clear_pending_pkce()
    profile = _PROFILE_BASE / "chatgpt_oauth_profile"
    if profile.exists():
        import shutil

        shutil.rmtree(profile, ignore_errors=True)
    _cache.update({"access_token": "", "expires_at": 0.0, "account_id": ""})
    return status()

"""Claude (Pro/Max) OAuth token manager — self-contained.

Tokens are obtained via an interactive PKCE OAuth sign-in (browser or paste-code) against
the Claude.ai subscription login (the same public client the Claude Code CLI uses) and
stored ONLY in this app's own data dir. This module never reads or writes any external
CLI's credential file — sign-in is entirely owned by the app.

Stored token file (backend/.data/claude_oauth_tokens.json):
    { "access_token": "sk-ant-oat01-…", "refresh_token": "sk-ant-ort01-…",
      "organization_id": "…", "expires_at": 1750000000.0,
      "last_refresh": "2026-06-19T…" }

Differences from the ChatGPT (Codex) OAuth flow:
  * Claude access tokens are OPAQUE (not JWTs), so expiry is tracked from the token
    response's ``expires_in`` (stored as an absolute ``expires_at``), not decoded.
  * The console callback presents the result as ``<code>#<state>`` (a fragment), so the
    paste path accepts that form as well as a normal ``?code=…&state=…`` URL.
  * Inference with an OAuth token requires the Claude Code identity — see
    app.agent.claude_provider (the oauth header + system preamble live there).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

_DATA_DIR = Path(__file__).resolve().parents[2] / ".data"
# Chromium's SingletonLock can't be created on a network share (Azure Files / SMB), so
# the browser profile lives on local-writable storage (BROWSER_PROFILE_DIR, e.g. /tmp in
# a container) — NOT on the data volume. Token/PKCE files below stay on the volume.
_PROFILE_BASE = Path(os.environ.get("BROWSER_PROFILE_DIR") or _DATA_DIR)
# This app's own token store.
_TOKENS_PATH = _DATA_DIR / "claude_oauth_tokens.json"
# Pending PKCE state for an in-progress interactive sign-in (persisted so the paste-code
# completion can run as a separate request, even across a backend restart).
_PKCE_PATH = _DATA_DIR / "claude_oauth_pkce.json"

# Public OAuth client id used by the Claude Code CLI (env-overridable).
_OAUTH_CLIENT_ID = os.environ.get(
    "CLAUDE_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
)
# Pro/Max subscription sign-in (claude.ai); the token endpoint lives on console.
_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
_SCOPE = "org:create_api_key user:profile user:inference"

# Refresh a little before the real expiry to avoid races.
_REFRESH_SKEW_SECONDS = 120


def _read_tokens() -> dict[str, Any]:
    """Read this app's stored Claude OAuth tokens."""
    if not _TOKENS_PATH.exists():
        return {}
    try:
        data = json.loads(_TOKENS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_tokens(
    access_token: str,
    refresh_token: str,
    expires_in: float | int | None,
    organization_id: str = "",
) -> None:
    """Persist tokens to this app's own store (preserving prior fields when blank)."""
    data = _read_tokens()
    data["access_token"] = access_token
    if refresh_token:
        data["refresh_token"] = refresh_token
    if expires_in:
        # Store the absolute expiry; the refresh skew is applied at read time.
        data["expires_at"] = time.time() + float(expires_in)
    if organization_id:
        data["organization_id"] = organization_id
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
    expires_at = float(data.get("expires_at", 0) or 0)
    return {
        "signed_in": True,
        "has_token": True,
        "expired": bool(expires_at and expires_at <= time.time()),
        # The UI reuses the ChatgptStatus shape; surface the org id as account_id.
        "account_id": data.get("organization_id", ""),
    }


async def _refresh(refresh_token: str) -> dict[str, Any] | None:
    """Exchange a refresh token for a fresh access token (JSON body, per Anthropic)."""
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _OAUTH_CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(_TOKEN_URL, json=payload)
        except httpx.HTTPError:
            return None
    if resp.status_code != 200:
        return None
    return resp.json()


async def get_token() -> str:
    """Return a valid access token, refreshing via OAuth when expired.

    Raises RuntimeError if no app sign-in exists.
    """
    data = _read_tokens()
    access_token = data.get("access_token", "")
    if not access_token:
        raise RuntimeError(
            "Not signed in to Claude. Open the admin AI Provider settings and click "
            "'Sign in with Claude'."
        )
    refresh_token = data.get("refresh_token", "")
    organization_id = data.get("organization_id", "")
    expires_at = float(data.get("expires_at", 0) or 0)

    if expires_at and expires_at - _REFRESH_SKEW_SECONDS > time.time():
        return access_token

    # Expired (or unknown expiry): try to refresh.
    if refresh_token:
        refreshed = await _refresh(refresh_token)
        if refreshed and refreshed.get("access_token"):
            new_access = refreshed["access_token"]
            new_refresh = refreshed.get("refresh_token", refresh_token)
            _write_tokens(
                new_access, new_refresh, refreshed.get("expires_in"), organization_id
            )
            return new_access

    # Couldn't refresh — return the (possibly stale) token; the caller surfaces a 401.
    return access_token


async def force_refresh() -> dict[str, Any]:
    """Force a token refresh and return the new status (for the admin Refresh button)."""
    data = _read_tokens()
    refresh_token = data.get("refresh_token", "")
    if not refresh_token:
        raise RuntimeError("No refresh token. Sign in with Claude first.")
    refreshed = await _refresh(refresh_token)
    if not refreshed or not refreshed.get("access_token"):
        raise RuntimeError("Claude token refresh failed. Sign in with Claude again.")
    _write_tokens(
        refreshed["access_token"],
        refreshed.get("refresh_token", refresh_token),
        refreshed.get("expires_in"),
        data.get("organization_id", ""),
    )
    return status()


# ---------------------------------------------------------------------------
# Interactive sign-in (PKCE authorization-code flow).
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
    """Create a new PKCE challenge and return the Claude authorize URL to open.

    The caller (admin UI) navigates a browser here; after sign-in the browser is
    redirected to the console callback, which renders the result as ``<code>#<state>``.
    """
    pkce = _new_pkce()
    params = {
        "code": "true",
        "client_id": _OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _REDIRECT_URI,
        "scope": _SCOPE,
        "code_challenge": pkce["challenge"],
        "code_challenge_method": "S256",
        "state": pkce["state"],
    }
    return {"authorize_url": f"{_AUTHORIZE_URL}?{urlencode(params)}", "state": pkce["state"]}


async def _exchange_code(code: str, state: str, verifier: str) -> dict[str, Any]:
    """Exchange an authorization code for tokens (PKCE, JSON body)."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "state": state,
        "redirect_uri": _REDIRECT_URI,
        "client_id": _OAUTH_CLIENT_ID,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_TOKEN_URL, json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _persist_new_login(token_resp: dict[str, Any]) -> None:
    """Persist tokens from an interactive login into this app's own token store."""
    access = token_resp.get("access_token", "")
    refresh = token_resp.get("refresh_token", "")
    expires_in = token_resp.get("expires_in")
    # The organization id may be returned in a few shapes; capture it best-effort.
    org = token_resp.get("organization") if isinstance(token_resp.get("organization"), dict) else {}
    account = token_resp.get("account") if isinstance(token_resp.get("account"), dict) else {}
    organization_id = (
        (org or {}).get("uuid")
        or (org or {}).get("id")
        or (account or {}).get("uuid")
        or token_resp.get("organization_id", "")
        or ""
    )
    _write_tokens(access, refresh, expires_in, organization_id)


def _extract_code_and_state(callback: str) -> tuple[str, str]:
    """Pull `code` and `state` from a pasted value.

    Accepts any of:
      * a full redirect URL ``https://…/oauth/code/callback?code=X&state=Y``
      * the console's displayed ``<code>#<state>`` fragment string
      * a bare ``code`` (state then comes from the stored PKCE)
    """
    text = callback.strip()
    if not text:
        raise RuntimeError("No authorization code was provided.")

    # Full URL form: parse the query string.
    parsed = urlparse(text)
    if parsed.query:
        params = parse_qs(parsed.query)
        if params.get("error"):
            raise RuntimeError(f"Authorization failed: {params['error'][0]}")
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        # The state can also ride in the fragment of a copied URL.
        if not state and parsed.fragment:
            state = parsed.fragment
        if code:
            return code, state

    # Fragment form pasted on its own: "<code>#<state>".
    if "#" in text and not parsed.scheme:
        code, _, state = text.partition("#")
        return code.strip(), state.strip()

    # Bare code with no state.
    if parsed.scheme:
        raise RuntimeError(
            "No authorization code found in the pasted URL. Paste the value shown on the "
            "Claude callback page (it looks like 'code#state')."
        )
    return text, ""


async def complete_with_callback_url(callback_url: str) -> dict[str, Any]:
    """Finish an interactive sign-in from a pasted code/redirect (the prod-friendly path).

    Works even when the app is not hosted on localhost: the user signs in in their own
    browser, copies the displayed ``code#state`` value, and pastes it here.
    """
    pending = _load_pending_pkce()
    if not pending or not pending.get("verifier"):
        raise RuntimeError(
            "No sign-in is in progress. Click 'Sign in with Claude' first to start the "
            "flow, then paste the code you were shown."
        )
    code, state = _extract_code_and_state(callback_url)
    state = state or pending.get("state", "")
    if state and pending.get("state") and state != pending["state"]:
        raise RuntimeError("State mismatch — start the sign-in again for security.")
    token_resp = await _exchange_code(code, state, pending["verifier"])
    _persist_new_login(token_resp)
    _clear_pending_pkce()
    return status()


def _capture_code_sync(authorize_url: str, timeout_ms: int) -> str:
    """Removed: server-side browser capture is no longer supported.

    The interactive (Chromium) sign-in path was removed to drop the headful-browser
    stack from the container. Sign-in now uses the link/paste flow exclusively
    (``build_authorize_url`` + ``complete_with_callback_url``).
    """
    raise RuntimeError(
        "Server-side browser sign-in has been removed. Use 'Get sign-in link', open it "
        "in any browser, sign in, then paste the code shown (code#state)."
    )


async def interactive_login(timeout_seconds: int = 240) -> dict[str, Any]:
    """Removed: there is no server-side browser. Use the link/paste sign-in flow.

    Kept as a raising stub so any lingering caller fails loudly with guidance rather
    than importing a missing symbol. The supported flow is ``build_authorize_url`` then
    ``complete_with_callback_url`` (the admin UI's 'Get sign-in link' + paste).
    """
    raise RuntimeError(
        "Server-side browser sign-in has been removed. Use 'Get sign-in link', open it "
        "in any browser, sign in, then paste the code shown (code#state)."
    )


def sign_out() -> dict[str, Any]:
    """Forget the stored Claude tokens and any in-progress sign-in / browser profile."""
    try:
        _TOKENS_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    _clear_pending_pkce()
    profile = _PROFILE_BASE / "claude_oauth_profile"
    if profile.exists():
        import shutil

        shutil.rmtree(profile, ignore_errors=True)
    return status()

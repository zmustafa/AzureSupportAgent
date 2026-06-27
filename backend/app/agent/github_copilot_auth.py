"""Self-managed GitHub Copilot token capture via the GitHub OAuth device flow.

This replaces any dependency on the BuddyAI desktop app, and uses NO server-side
browser:

- The user runs the OAuth device flow (open a short URL on any device, type a code).
  GitHub returns a long-lived OAuth token (gho_…), which this backend exchanges for a
  short-lived Copilot bearer via the copilot_internal token endpoint. The same endpoint
  gives the correct per-account API base URL (api.individual / api.business /
  api.enterprise .githubcopilot.com).
- The OAuth token + bearer + base URL + expiry are cached to
  backend/.data/gh_copilot_token.json. "refresh" = re-mint the bearer from the stored
  OAuth token (no browser, no interaction).

The long-lived OAuth token is the credential; the minted bearer is the short-lived
access token. This is the same mechanism the VS Code Copilot extension and the GitHub
CLI use, so it works in a fully headless container (Azure Container Apps).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

_DATA_DIR = Path(__file__).resolve().parents[2] / ".data"
_TOKEN_FILE = _DATA_DIR / "gh_copilot_token.json"
# Pending OAuth device-flow state (device_code + interval) while the user authorizes on
# their own device. Persisted so polling survives a backend restart.
_DEVICE_FILE = _DATA_DIR / "gh_copilot_device.json"

DEFAULT_API_BASE_URL = "https://api.githubcopilot.com"

# --- GitHub OAuth Device Flow (headless / remote sign-in, no server browser) ----------
# This is the same mechanism the VS Code Copilot extension and the GitHub CLI use: the
# user opens a short URL on ANY device, types a code, and this backend polls for the
# resulting OAuth token — then exchanges it for a short-lived Copilot bearer. It needs no
# server-side browser, so it works in a headless container (Azure Container Apps).
_DEVICE_CLIENT_ID = os.environ.get("GITHUB_COPILOT_CLIENT_ID") or "Iv1.b507a08c87ecfe98"
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
_DEVICE_SCOPE = "read:user"
# Editor identity headers GitHub expects when minting a Copilot token.
_EDITOR_HEADERS = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "User-Agent": "GitHubCopilotChat/0.22.0",
    "Accept": "application/json",
}

# Minted bearer tokens are short-lived; assume ~20 min and refresh as needed.
_TOKEN_TTL_SECONDS = 20 * 60


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------
def _read_cache() -> dict[str, Any] | None:
    if not _TOKEN_FILE.exists():
        return None
    try:
        return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(
    token: str,
    api_base_url: str,
    *,
    expires_at: float | None = None,
    oauth_token: str | None = None,
    auth_scheme: str | None = None,
) -> dict[str, Any]:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    if expires_at:
        exp_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    else:
        exp_iso = (now + timedelta(seconds=_TOKEN_TTL_SECONDS)).isoformat()
    existing = _read_cache() or {}
    data: dict[str, Any] = {
        "access_token": token,
        "api_base_url": api_base_url or DEFAULT_API_BASE_URL,
        "captured_at": now.isoformat(),
        "expires_at": exp_iso,
        # Header scheme the Copilot API expects: device-flow editor tokens use "Bearer";
        # legacy browser-sniffed web tokens use "GitHub-Bearer".
        "auth_scheme": auth_scheme or existing.get("auth_scheme") or "GitHub-Bearer",
    }
    # Preserve the long-lived OAuth token (gho_…) so refresh can re-mint the short-lived
    # Copilot bearer with NO browser. Keep any previously stored one if not overriding.
    tok = oauth_token if oauth_token is not None else existing.get("oauth_token")
    if tok:
        data["oauth_token"] = tok
    _TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def auth_scheme() -> str:
    """The Authorization scheme the cached Copilot token must be sent with."""
    cache = _read_cache() or {}
    return cache.get("auth_scheme") or "GitHub-Bearer"


def _is_expired(cache: dict[str, Any]) -> bool:
    raw = cache.get("expires_at", "")
    if not raw:
        return True
    try:
        exp = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    # A naive cached timestamp is UTC; normalize so the comparison below never raises.
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


def _has_oauth_token() -> bool:
    """True if we hold a long-lived OAuth token (device-flow sign-in)."""
    cache = _read_cache()
    return bool(cache and cache.get("oauth_token"))


def status() -> dict[str, Any]:
    """Non-sensitive status for the admin UI."""
    cache = _read_cache()
    # Signed in if a device-flow OAuth token exists (the only supported sign-in).
    signed_in = bool(cache and cache.get("oauth_token"))
    if not cache:
        return {
            "signed_in": signed_in,
            "has_token": False,
            "expired": True,
            "api_base_url": "",
            "expires_at": "",
        }
    return {
        "signed_in": signed_in,
        "has_token": bool(cache.get("access_token")),
        "expired": _is_expired(cache),
        "api_base_url": cache.get("api_base_url", ""),
        "expires_at": cache.get("expires_at", ""),
    }


def sign_out() -> None:
    """Forget the token and any pending device flow (next use needs a fresh sign-in)."""
    for f in (_TOKEN_FILE, _DEVICE_FILE):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GitHub OAuth Device Flow (headless / remote — no server browser)
# ---------------------------------------------------------------------------
async def start_device_flow() -> dict[str, Any]:
    """Begin the GitHub OAuth device flow.

    Returns the user-facing code + verification URL. The device code (and poll interval)
    are persisted so poll_device_flow() can complete the sign-in, even across a backend
    restart. No browser is launched anywhere.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _DEVICE_CODE_URL,
            data={"client_id": _DEVICE_CLIENT_ID, "scope": _DEVICE_SCOPE},
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub device-code request failed ({resp.status_code}): {resp.text[:200]}"
        )
    body = resp.json()
    device_code = body.get("device_code", "")
    if not device_code:
        raise RuntimeError(f"GitHub did not return a device code: {body}")
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _DEVICE_FILE.write_text(
        json.dumps(
            {
                "device_code": device_code,
                "interval": int(body.get("interval", 5)),
                "started_at": time.time(),
                "expires_in": int(body.get("expires_in", 900)),
            }
        ),
        encoding="utf-8",
    )
    return {
        "user_code": body.get("user_code", ""),
        "verification_uri": body.get("verification_uri", "https://github.com/login/device"),
        "expires_in": int(body.get("expires_in", 900)),
        "interval": int(body.get("interval", 5)),
    }


async def _mint_copilot_token(oauth_token: str) -> tuple[str, str, float]:
    """Exchange a GitHub OAuth token (gho_…) for a short-lived Copilot bearer.

    Returns (bearer_token, api_base_url, expires_at_epoch). Raises on failure (e.g. the
    account has no Copilot entitlement)."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            _COPILOT_TOKEN_URL,
            headers={"Authorization": f"token {oauth_token}", **_EDITOR_HEADERS},
        )
    if resp.status_code != 200:
        raise RuntimeError(
            "Could not get a Copilot token — does this GitHub account have an active "
            f"Copilot subscription? ({resp.status_code}: {resp.text[:200]})"
        )
    body = resp.json()
    bearer = body.get("token", "")
    if not bearer:
        raise RuntimeError(f"Copilot token endpoint returned no token: {body}")
    endpoints = body.get("endpoints") or {}
    api_base = endpoints.get("api") or DEFAULT_API_BASE_URL
    expires_at = float(body.get("expires_at") or (time.time() + _TOKEN_TTL_SECONDS))
    return bearer, api_base, expires_at


async def poll_device_flow() -> dict[str, Any]:
    """Poll GitHub once for the device-flow result.

    Returns {"status": "pending"} while the user hasn't authorized yet, {"status":
    "authorized", **status()} once a Copilot token is minted, or {"status": "error",
    "detail": …} on a terminal failure (expired/denied/no pending flow).
    """
    if not _DEVICE_FILE.exists():
        return {"status": "error", "detail": "No sign-in in progress. Start again."}
    try:
        pending = json.loads(_DEVICE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "error", "detail": "Sign-in state was lost. Start again."}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _OAUTH_TOKEN_URL,
            data={
                "client_id": _DEVICE_CLIENT_ID,
                "device_code": pending.get("device_code", ""),
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    err = body.get("error")
    if err in ("authorization_pending", "slow_down"):
        return {"status": "pending"}
    if err in ("expired_token", "access_denied") or (err and not body.get("access_token")):
        _DEVICE_FILE.unlink(missing_ok=True)
        detail = {
            "expired_token": "The code expired before you authorized. Start again.",
            "access_denied": "Sign-in was cancelled.",
        }.get(err, f"Sign-in failed: {err}")
        return {"status": "error", "detail": detail}

    oauth_token = body.get("access_token", "")
    if not oauth_token:
        return {"status": "pending"}

    bearer, api_base, expires_at = await _mint_copilot_token(oauth_token)
    _write_cache(bearer, api_base, expires_at=expires_at, oauth_token=oauth_token, auth_scheme="Bearer")
    _DEVICE_FILE.unlink(missing_ok=True)
    return {"status": "authorized", **status()}


async def refresh_token() -> str | None:
    """Mint a fresh Copilot bearer without any browser.

    Re-exchanges the stored long-lived OAuth token (device-flow sign-in) for a fresh
    short-lived bearer. Returns the new bearer, or None if there is no stored OAuth
    token or it has been revoked (the user must sign in again).
    """
    cache = _read_cache() or {}
    oauth_token = cache.get("oauth_token")
    if not oauth_token:
        return None
    try:
        bearer, base, expires_at = await _mint_copilot_token(oauth_token)
    except Exception:  # noqa: BLE001 - token may have been revoked; signal re-login
        return None
    _write_cache(bearer, base, expires_at=expires_at, oauth_token=oauth_token, auth_scheme="Bearer")
    return bearer


async def get_valid_token() -> tuple[str, str]:
    """Return (access_token, api_base_url), refreshing headlessly when expired.

    Raises RuntimeError if there is no usable session (the user must sign in).
    """
    cache = _read_cache()
    if cache and cache.get("access_token") and not _is_expired(cache):
        return cache["access_token"], cache.get("api_base_url", DEFAULT_API_BASE_URL)

    token = await refresh_token()
    if token:
        fresh = _read_cache() or {}
        return token, fresh.get("api_base_url", DEFAULT_API_BASE_URL)

    # No valid session — fall back to a stale token if we have one, else fail.
    if cache and cache.get("access_token"):
        return cache["access_token"], cache.get("api_base_url", DEFAULT_API_BASE_URL)
    raise RuntimeError(
        "Not signed in to GitHub Copilot. Open the admin AI Provider settings and "
        "click 'Sign in with GitHub Copilot'."
    )

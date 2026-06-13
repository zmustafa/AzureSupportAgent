"""Self-managed GitHub Copilot token capture via a real browser session.

This replaces any dependency on the BuddyAI desktop app. It mirrors BuddyAI's
WebView2 approach, but the browser is driven by this backend with Playwright:

- A persistent browser profile (cookies) is kept in backend/.data/gh_copilot_profile,
  so the user signs in to GitHub *once*; afterwards tokens refresh non-interactively.
- We navigate to https://github.com/copilot and sniff the short-lived
  "GitHub-Bearer" token from the Authorization header of the requests the page makes
  to api.*.githubcopilot.com. The request host also gives us the correct per-account
  API base URL (api.individual / api.business / api.enterprise .githubcopilot.com).
- The token + base URL + expiry are cached to backend/.data/gh_copilot_token.json.
  Because the token is short-lived, "refresh" = re-sniff headlessly using the
  persisted cookies (exactly what BuddyAI's RefreshTokenAsync does).

The persistent cookies act as the long-lived credential; the sniffed bearer is the
short-lived access token. No GitHub client secret or device-flow polling is needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

_DATA_DIR = Path(__file__).resolve().parents[2] / ".data"
# Chromium needs a profile dir on a filesystem that supports its SingletonLock (a
# symlink/lock file). Network shares (Azure Files / SMB) reject that with EACCES, so the
# browser profile lives on local-writable storage (BROWSER_PROFILE_DIR, e.g. /tmp in a
# container) — NOT on the data volume. Only the token cache below stays on the volume.
_PROFILE_BASE = Path(os.environ.get("BROWSER_PROFILE_DIR") or _DATA_DIR)
_PROFILE_DIR = _PROFILE_BASE / "gh_copilot_profile"
_TOKEN_FILE = _DATA_DIR / "gh_copilot_token.json"
# Pending OAuth device-flow state (device_code + interval) while the user authorizes on
# their own device. Persisted so polling survives a backend restart.
_DEVICE_FILE = _DATA_DIR / "gh_copilot_device.json"

DEFAULT_API_BASE_URL = "https://api.githubcopilot.com"
_COPILOT_URL = "https://github.com/copilot"

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

# Sniffed bearer tokens are short-lived; assume ~20 min and refresh as needed.
_TOKEN_TTL_SECONDS = 20 * 60

# Serialize browser launches so we never open two Chromium profiles at once.
_capture_lock = asyncio.Lock()


def _normalize_bearer(raw: str | None) -> str:
    """Port of C# NormalizeBearer: strip 'GitHub-Bearer '/'Bearer ' prefixes."""
    if not raw:
        return ""
    value = raw.strip()
    for prefix in ("GitHub-Bearer ", "Bearer "):
        if value.lower().startswith(prefix.lower()):
            return value[len(prefix):].strip()
    return ""


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


def has_browser_profile() -> bool:
    """True if the user has signed in at least once (persistent cookies exist)."""
    return _PROFILE_DIR.exists() and any(_PROFILE_DIR.iterdir())


def _has_oauth_token() -> bool:
    """True if we hold a long-lived OAuth token (device-flow sign-in)."""
    cache = _read_cache()
    return bool(cache and cache.get("oauth_token"))


def status() -> dict[str, Any]:
    """Non-sensitive status for the admin UI."""
    cache = _read_cache()
    # Signed in if EITHER a device-flow OAuth token OR a browser session exists.
    signed_in = bool(cache and cache.get("oauth_token")) or has_browser_profile()
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
    """Forget the token and the browser session (next use needs a fresh sign-in)."""
    for f in (_TOKEN_FILE, _DEVICE_FILE):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    if _PROFILE_DIR.exists():
        import shutil

        shutil.rmtree(_PROFILE_DIR, ignore_errors=True)


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

    # Authorized — mint the Copilot bearer and cache both tokens.
    bearer, api_base, expires_at = await _mint_copilot_token(oauth_token)
    _write_cache(bearer, api_base, expires_at=expires_at, oauth_token=oauth_token, auth_scheme="Bearer")
    _DEVICE_FILE.unlink(missing_ok=True)
    return {"status": "authorized", **status()}


# ---------------------------------------------------------------------------
# Browser capture (runs the SYNC Playwright API in a worker thread)
# ---------------------------------------------------------------------------
def _capture_sync(headless: bool, timeout_ms: int) -> tuple[str, str]:
    """Open github.com/copilot and sniff a GitHub-Bearer token. Blocking; run in a thread."""
    from playwright.sync_api import sync_playwright

    captured: dict[str, str] = {"token": "", "base": ""}
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=headless,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            def on_request(request: Any) -> None:
                try:
                    if "githubcopilot.com" not in request.url:
                        return
                    token = _normalize_bearer(request.headers.get("authorization"))
                    if token:
                        captured["token"] = token
                        parsed = urlparse(request.url)
                        captured["base"] = f"{parsed.scheme}://{parsed.netloc}"
                except Exception:  # noqa: BLE001 - never let a sniff error abort capture
                    pass

            context.on("request", on_request)

            try:
                page.goto(_COPILOT_URL, wait_until="domcontentloaded", timeout=min(timeout_ms, 60_000))
            except Exception:  # noqa: BLE001 - navigation may stall; keep polling for the token
                pass

            deadline = time.time() + timeout_ms / 1000
            while time.time() < deadline and not captured["token"]:
                # Nudge the page so it issues authenticated Copilot API calls.
                try:
                    page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    break

            return captured["token"], captured["base"]
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


async def interactive_login(timeout_seconds: int = 180) -> dict[str, Any]:
    """Open a visible browser so the user can sign in to GitHub Copilot, then capture
    a token. Returns the new status. Raises RuntimeError if no token was captured."""
    async with _capture_lock:
        token, base = await asyncio.to_thread(_capture_sync, False, timeout_seconds * 1000)
    if not token:
        raise RuntimeError(
            "Sign-in window closed or timed out before a GitHub Copilot token could be "
            "captured. Make sure you signed in and that your account has Copilot access."
        )
    _write_cache(token, base)
    return status()


async def refresh_token() -> str | None:
    """Mint a fresh Copilot bearer without any browser.

    Preferred path: re-exchange the stored long-lived OAuth token (device-flow sign-in).
    Falls back to re-sniffing via the persisted browser session if that's all we have
    (legacy local sign-ins). Returns the new bearer, or None if no session is usable.
    """
    cache = _read_cache() or {}
    oauth_token = cache.get("oauth_token")
    if oauth_token:
        try:
            bearer, base, expires_at = await _mint_copilot_token(oauth_token)
        except Exception:  # noqa: BLE001 - token may have been revoked; signal re-login
            return None
        _write_cache(bearer, base, expires_at=expires_at, oauth_token=oauth_token, auth_scheme="Bearer")
        return bearer

    # Legacy fallback: re-sniff using a persisted browser profile (local dev only).
    if not has_browser_profile():
        return None
    async with _capture_lock:
        token, base = await asyncio.to_thread(_capture_sync, True, 45 * 1000)
    if not token:
        return None
    _write_cache(token, base)
    return token


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


# ---------------------------------------------------------------------------
# Image upload (ported from BuddyAI's C# GitHubCopilotWebViewBridge.UploadImageAsync)
# ---------------------------------------------------------------------------
_UPLOAD_JS = r"""
async (args) => {
    const { threadId, base64Data, fileName, mimeType, size } = args;
    try {
        let nonce = '', clientVersion = '';
        for (const m of document.querySelectorAll('meta[name]')) {
            const name = (m.getAttribute('name') || '').toLowerCase();
            const content = m.getAttribute('content') || '';
            if (!nonce && name.indexOf('nonce') !== -1 && content) nonce = content;
            if (!clientVersion && name.indexOf('client-version') !== -1 && content) clientVersion = content;
        }
        function ghHeaders(extra) {
            const h = Object.assign({
                'Accept': 'application/json',
                'GitHub-Verified-Fetch': 'true',
                'X-Requested-With': 'XMLHttpRequest'
            }, extra || {});
            if (nonce) h['X-Fetch-Nonce'] = nonce;
            if (clientVersion) h['X-GitHub-Client-Version'] = clientVersion;
            return h;
        }
        const policyForm = new FormData();
        policyForm.append('name', fileName);
        policyForm.append('size', String(size));
        policyForm.append('content_type', mimeType);
        policyForm.append('thread_id', threadId);
        const policyResp = await fetch('https://github.com/upload/policies/copilot-chat-attachments', {
            method: 'POST', body: policyForm, credentials: 'same-origin', headers: ghHeaders()
        });
        if (!policyResp.ok) {
            return { stage: 'policy', status: policyResp.status, body: await policyResp.text() };
        }
        const policy = await policyResp.json();
        const binaryStr = atob(base64Data);
        const bytes = new Uint8Array(binaryStr.length);
        for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);
        const blob = new Blob([bytes], { type: mimeType });
        const uploadForm = new FormData();
        const form = policy.form || {};
        for (const k in form) uploadForm.append(k, form[k]);
        uploadForm.append('file', blob, fileName);
        const uploadUrl = policy.upload_url || policy.upload_uri;
        const uploadResp = await fetch(uploadUrl, { method: 'POST', body: uploadForm, mode: 'cors' });
        if (!uploadResp.ok && uploadResp.status !== 204 && uploadResp.status !== 201) {
            return { stage: 'upload', status: uploadResp.status, body: await uploadResp.text() };
        }
        let finUrl = policy.asset_upload_url;
        if (finUrl && finUrl.startsWith('/')) finUrl = 'https://github.com' + finUrl;
        const finForm = new FormData();
        finForm.append('authenticity_token', policy.asset_upload_authenticity_token);
        const finResp = await fetch(finUrl, {
            method: 'PUT', body: finForm, credentials: 'same-origin', headers: ghHeaders()
        });
        const finText = await finResp.text();
        return { stage: 'done', status: finResp.status, body: finText, asset: policy.asset };
    } catch (e) {
        return { stage: 'exception', status: 0, body: String(e && e.message ? e.message : e) };
    }
}
"""


def _extract_asset_url(result: dict[str, Any]) -> str:
    if result.get("stage") != "done":
        raise RuntimeError(
            f"Copilot image upload failed at stage '{result.get('stage')}' "
            f"(status {result.get('status')}): {str(result.get('body'))[:300]}"
        )
    body = result.get("body") or ""
    try:
        fin = json.loads(body)
        for prop in ("href", "url", "asset_url", "download_url"):
            if isinstance(fin.get(prop), str) and fin[prop]:
                return fin[prop]
    except (json.JSONDecodeError, TypeError):
        pass
    asset = result.get("asset") or {}
    for prop in ("href", "url"):
        if isinstance(asset.get(prop), str) and asset[prop]:
            return asset[prop]
    raise RuntimeError("Copilot upload completed but no asset URL was returned.")


def _upload_image_sync(thread_id: str, data_url: str, timeout_ms: int) -> str:
    """Upload one image via the persistent browser session; returns the asset URL."""
    import base64 as _b64

    from playwright.sync_api import sync_playwright

    header, _, b64 = data_url.partition(";base64,")
    mime = header.split(":", 1)[1] if ":" in header else "image/png"
    ext = {"image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".png")
    size = len(_b64.b64decode(b64))

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=True,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(_COPILOT_URL, wait_until="domcontentloaded", timeout=min(timeout_ms, 60_000))
            result = page.evaluate(
                _UPLOAD_JS,
                {
                    "threadId": thread_id,
                    "base64Data": b64,
                    "fileName": f"image{ext}",
                    "mimeType": mime,
                    "size": size,
                },
            )
            return _extract_asset_url(result)
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


async def upload_image(thread_id: str, data_url: str, timeout_seconds: int = 60) -> str:
    """Upload an image attachment to a Copilot chat thread and return its asset URL."""
    async with _capture_lock:
        return await asyncio.to_thread(
            _upload_image_sync, thread_id, data_url, timeout_seconds * 1000
        )

"""Tests for the GitHub Copilot auth manager after the headless (device-flow) migration.

These verify the browser-free behaviour:
  * ``refresh_token`` mints a fresh Copilot bearer from a stored OAuth token (no browser);
  * ``refresh_token`` returns ``None`` when there is no stored OAuth token;
  * ``status``/``sign_out`` work off the token cache only (no browser profile);
  * the removed Chromium symbols are gone (so nothing can reintroduce a browser path).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent import github_copilot_auth as gh


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point the token + device-flow stores at a temp dir so tests don't touch real .data."""
    monkeypatch.setattr(gh, "_TOKEN_FILE", tmp_path / "gh_copilot_token.json")
    monkeypatch.setattr(gh, "_DEVICE_FILE", tmp_path / "gh_copilot_device.json")
    monkeypatch.setattr(gh, "_DATA_DIR", tmp_path)
    yield


def test_refresh_token_mints_from_oauth_token(monkeypatch):
    # Seed a cache that holds only the long-lived OAuth token (device-flow sign-in).
    gh._write_cache("stale-bearer", gh.DEFAULT_API_BASE_URL, oauth_token="gho_abc", auth_scheme="Bearer")

    async def _fake_mint(oauth_token):
        assert oauth_token == "gho_abc"
        return "fresh-bearer", "https://api.individual.githubcopilot.com", 9999999999.0

    monkeypatch.setattr(gh, "_mint_copilot_token", _fake_mint)

    token = _run(gh.refresh_token())
    assert token == "fresh-bearer"
    cache = gh._read_cache()
    assert cache["access_token"] == "fresh-bearer"
    assert cache["api_base_url"] == "https://api.individual.githubcopilot.com"
    # The long-lived OAuth token is preserved for the next refresh.
    assert cache["oauth_token"] == "gho_abc"


def test_refresh_token_none_without_oauth_token():
    # No cache at all → nothing to refresh, and definitely no browser attempt.
    assert _run(gh.refresh_token()) is None
    # A cache with a bearer but NO oauth_token also cannot refresh headlessly.
    gh._write_cache("only-bearer", gh.DEFAULT_API_BASE_URL)
    assert _run(gh.refresh_token()) is None


def test_status_signed_in_requires_oauth_token():
    assert gh.status()["signed_in"] is False
    gh._write_cache("bearer", gh.DEFAULT_API_BASE_URL, oauth_token="gho_x", auth_scheme="Bearer")
    st = gh.status()
    assert st["signed_in"] is True
    assert st["has_token"] is True


def test_sign_out_clears_cache():
    gh._write_cache("bearer", gh.DEFAULT_API_BASE_URL, oauth_token="gho_x", auth_scheme="Bearer")
    assert gh._read_cache() is not None
    gh.sign_out()
    assert gh._read_cache() is None
    assert gh.status()["signed_in"] is False


def test_poll_device_flow_authorizes(monkeypatch):
    # A pending device flow exists; GitHub returns an access token; we mint a bearer.
    gh._DEVICE_FILE.write_text(
        json.dumps({"device_code": "dc", "interval": 5, "started_at": 0, "expires_in": 900}),
        encoding="utf-8",
    )

    class _Resp:
        headers = {"content-type": "application/json"}

        def json(self):
            return {"access_token": "gho_minted"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(gh.httpx, "AsyncClient", _Client)

    async def _fake_mint(oauth_token):
        assert oauth_token == "gho_minted"
        return "bearer", gh.DEFAULT_API_BASE_URL, 9999999999.0

    monkeypatch.setattr(gh, "_mint_copilot_token", _fake_mint)

    result = _run(gh.poll_device_flow())
    assert result["status"] == "authorized"
    assert result["signed_in"] is True
    # The pending device-flow file is consumed on success.
    assert not gh._DEVICE_FILE.exists()


def test_chromium_symbols_removed():
    """The headful-browser path must not exist anymore (guards against regressions)."""
    for name in (
        "interactive_login",
        "upload_image",
        "_upload_image_sync",
        "_capture_sync",
        "has_browser_profile",
        "_normalize_bearer",
        "_PROFILE_DIR",
        "_capture_lock",
    ):
        assert not hasattr(gh, name), f"{name} should have been removed"


def test_oauth_interactive_login_stubs_raise():
    """Claude/ChatGPT server-side browser sign-in is removed; the stubs must raise."""
    from app.agent import chatgpt_oauth, claude_oauth

    with pytest.raises(RuntimeError):
        _run(claude_oauth.interactive_login())
    with pytest.raises(RuntimeError):
        _run(chatgpt_oauth.interactive_login())

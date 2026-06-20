"""Tests for the Claude (Pro/Max) OAuth token manager.

These cover the pure/local logic — PKCE build, opaque-token expiry tracking, the
code#state callback parsing, state-mismatch protection, token persistence, and
sign-out — without making any network calls (the token exchange is monkeypatched).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.agent import claude_oauth


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point the token + PKCE stores at a temp dir so tests don't touch real .data."""
    monkeypatch.setattr(claude_oauth, "_TOKENS_PATH", tmp_path / "claude_oauth_tokens.json")
    monkeypatch.setattr(claude_oauth, "_PKCE_PATH", tmp_path / "claude_oauth_pkce.json")
    monkeypatch.setattr(claude_oauth, "_PROFILE_BASE", tmp_path)
    yield


def test_build_authorize_url_has_pkce_and_persists_state():
    info = claude_oauth.build_authorize_url()
    assert info["authorize_url"].startswith("https://claude.ai/oauth/authorize?")
    assert "code_challenge=" in info["authorize_url"]
    assert "code_challenge_method=S256" in info["authorize_url"]
    assert "response_type=code" in info["authorize_url"]
    # The pending PKCE is persisted with a matching state + a verifier.
    pending = claude_oauth._load_pending_pkce()
    assert pending and pending["state"] == info["state"]
    assert pending.get("verifier")


def test_status_reflects_token_and_expiry():
    # No token yet.
    st = claude_oauth.status()
    assert st == {"signed_in": False, "has_token": False, "expired": True, "account_id": ""}

    # Fresh token → signed in, not expired, org surfaced as account_id.
    claude_oauth._write_tokens("sk-ant-oat01-fresh", "sk-ant-ort01-r", 3600, "org-123")
    st = claude_oauth.status()
    assert st["signed_in"] and st["has_token"] and not st["expired"]
    assert st["account_id"] == "org-123"

    # Past-expiry token → expired True.
    claude_oauth._write_tokens("sk-ant-oat01-old", "sk-ant-ort01-r", -10, "org-123")
    assert claude_oauth.status()["expired"] is True


def test_extract_code_and_state_forms():
    # Bare "code#state" fragment string.
    assert claude_oauth._extract_code_and_state("abc#xyz") == ("abc", "xyz")
    # Full redirect URL with query params.
    assert claude_oauth._extract_code_and_state(
        "https://console.anthropic.com/oauth/code/callback?code=AAA&state=BBB"
    ) == ("AAA", "BBB")
    # Bare code with no state.
    assert claude_oauth._extract_code_and_state("justcode") == ("justcode", "")
    # An explicit error in the URL is surfaced.
    with pytest.raises(RuntimeError):
        claude_oauth._extract_code_and_state(
            "https://console.anthropic.com/oauth/code/callback?error=access_denied"
        )


def test_complete_rejects_state_mismatch():
    info = claude_oauth.build_authorize_url()
    # A different state than the pending one must be rejected before any exchange.
    with pytest.raises(RuntimeError, match="State mismatch"):
        _run(claude_oauth.complete_with_callback_url(f"thecode#not-{info['state']}"))


def test_complete_persists_tokens(monkeypatch):
    info = claude_oauth.build_authorize_url()

    async def _fake_exchange(code, state, verifier):
        assert code == "thecode"
        assert state == info["state"]
        assert verifier
        return {
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "sk-ant-ort01-new",
            "expires_in": 3600,
            "organization": {"uuid": "org-xyz"},
        }

    monkeypatch.setattr(claude_oauth, "_exchange_code", _fake_exchange)
    st = _run(claude_oauth.complete_with_callback_url(f"thecode#{info['state']}"))
    assert st["signed_in"] and not st["expired"]
    assert st["account_id"] == "org-xyz"
    # Tokens persisted; pending PKCE cleared.
    data = claude_oauth._read_tokens()
    assert data["access_token"] == "sk-ant-oat01-new"
    assert data["refresh_token"] == "sk-ant-ort01-new"
    assert claude_oauth._load_pending_pkce() is None


def test_get_token_requires_sign_in():
    with pytest.raises(RuntimeError, match="Not signed in"):
        _run(claude_oauth.get_token())


def test_get_token_refreshes_when_expired(monkeypatch):
    claude_oauth._write_tokens("sk-ant-oat01-stale", "sk-ant-ort01-r", -10, "org-1")

    async def _fake_refresh(refresh_token):
        assert refresh_token == "sk-ant-ort01-r"
        return {
            "access_token": "sk-ant-oat01-refreshed",
            "refresh_token": "sk-ant-ort01-r2",
            "expires_in": 3600,
        }

    monkeypatch.setattr(claude_oauth, "_refresh", _fake_refresh)
    token = _run(claude_oauth.get_token())
    assert token == "sk-ant-oat01-refreshed"
    # The rotated refresh token is persisted.
    assert claude_oauth._read_tokens()["refresh_token"] == "sk-ant-ort01-r2"


def test_sign_out_clears_everything():
    claude_oauth._write_tokens("sk-ant-oat01-x", "sk-ant-ort01-r", 3600, "org-1")
    claude_oauth.build_authorize_url()  # creates pending PKCE
    st = claude_oauth.sign_out()
    assert st["signed_in"] is False
    assert claude_oauth._read_tokens() == {}
    assert claude_oauth._load_pending_pkce() is None

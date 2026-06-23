"""Regression: the staged "Test connection" diagnostics must treat ``claude_oauth`` as a
keyless (OAuth) provider. Previously the config step demanded an ``api_key`` that the
Claude Pro/Max OAuth flow never supplies, so Test connection failed at phase 1 with
"Missing: api_key" even though the OAuth token (and model-catalogue refresh) worked.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _first_event(gen):
    async def _take():
        async for ev in gen:
            return ev
        return None

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_take())
    finally:
        loop.close()


def _patch_config(monkeypatch, provider: str, *, model: str, base_url: str, api_key: str):
    from app.api import admin
    from app.core import llm_config

    monkeypatch.setattr(admin, "load_config", lambda: {"providers": {provider: {}}})
    monkeypatch.setattr(
        llm_config,
        "get_active",
        lambda *a, **k: {"model": model, "base_url": base_url, "api_key": api_key},
    )


def test_claude_oauth_config_step_does_not_require_api_key(monkeypatch):
    from app.api.admin import _diagnose_provider

    _patch_config(
        monkeypatch, "claude_oauth",
        model="claude-sonnet-4-6", base_url="https://api.anthropic.com", api_key="",
    )

    ev = _first_event(_diagnose_provider("claude_oauth"))
    assert ev is not None
    data = json.loads(ev["data"])
    assert data["step"] == "config"
    # The config phase must PASS for an OAuth provider with no api_key.
    assert data["status"] == "ok", data
    assert "auth=oauth/local" in data["detail"]


def test_keyed_provider_still_requires_api_key(monkeypatch):
    """Guard: the relaxation is scoped to OAuth/local providers only — a normal keyed
    provider with an empty key must still fail the config step."""
    from app.api.admin import _diagnose_provider

    _patch_config(
        monkeypatch, "claude",
        model="claude-sonnet-4-6", base_url="https://api.anthropic.com", api_key="",
    )

    ev = _first_event(_diagnose_provider("claude"))
    assert ev is not None
    data = json.loads(ev["data"])
    assert data["step"] == "config"
    assert data["status"] == "error"
    assert "api_key" in data["detail"]

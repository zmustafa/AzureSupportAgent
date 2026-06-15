"""Tests for the architecture designer's structured-JSON completion resilience.

gpt-5.x reasoning models (via the OpenAI Responses API on Copilot/Codex) can return an
empty completion when reasoning consumes the output budget. ``_complete_json`` requests a
generous ``max_output_tokens`` and retries once on an empty result; these cover that retry
and the markdown-fence / object-extraction parsing.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.architectures import designer


@dataclass
class _Ev:
    type: str
    text: str = ""


class _FakeProvider:
    """Yields a scripted sequence of completions across successive stream() calls."""

    def __init__(self, completions: list[str]):
        self._completions = completions
        self.calls = 0
        self.max_tokens_seen: list[int | None] = []

    async def stream(self, messages, tools, max_tokens=None):
        self.max_tokens_seen.append(max_tokens)
        out = self._completions[min(self.calls, len(self._completions) - 1)]
        self.calls += 1
        for ch in out:
            yield _Ev("token", ch)


@pytest.fixture()
def _patch_provider(monkeypatch):
    def _install(completions: list[str]) -> _FakeProvider:
        fp = _FakeProvider(completions)
        monkeypatch.setattr(designer, "build_provider", lambda: fp)
        return fp

    return _install


async def test_complete_json_retries_once_on_empty(_patch_provider):
    # First completion empty (reasoning ate the budget), second returns valid JSON.
    fp = _patch_provider(["", '{"nodes": [], "edges": []}'])
    parsed = await designer._complete_json("sys", "user")
    assert parsed == {"nodes": [], "edges": []}
    assert fp.calls == 2  # retried exactly once
    # The generous structured-JSON budget is always requested.
    assert fp.max_tokens_seen == [16000, 16000]


async def test_complete_json_no_retry_when_first_succeeds(_patch_provider):
    fp = _patch_provider(['{"ok": true}'])
    parsed = await designer._complete_json("sys", "user")
    assert parsed == {"ok": True}
    assert fp.calls == 1  # no wasted retry on success


async def test_complete_json_strips_markdown_fence(_patch_provider):
    _patch_provider(['```json\n{"a": 1}\n```'])
    parsed = await designer._complete_json("sys", "user")
    assert parsed == {"a": 1}


async def test_complete_json_extracts_object_from_prose(_patch_provider):
    _patch_provider(['Here is the architecture:\n{"a": 2}\nDone.'])
    parsed = await designer._complete_json("sys", "user")
    assert parsed == {"a": 2}


async def test_complete_json_returns_none_when_persistently_empty(_patch_provider):
    fp = _patch_provider([""])  # always empty
    parsed = await designer._complete_json("sys", "user")
    assert parsed is None
    assert fp.calls == 2  # tried twice, then gave up

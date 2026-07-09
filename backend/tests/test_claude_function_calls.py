"""Tests for Claude-Code `<function_calls>` XML tool-call handling.

Claude models under the Claude Code OAuth identity (required for `claude_oauth`) often
emit tool calls as `<function_calls>`/`<invoke>` XML *text* instead of native tool_use
blocks. These pin that:
  1. the parser turns that XML (incl. the malformed variant seen in the wild) into calls;
  2. the ClaudeProvider holds the XML back from the visible answer and surfaces it as a
     structured tool_calls event so the tools actually run.
"""
import pytest

from app.agent.provider import StreamEvent
from app.agent.tool_protocol import (
    FN_MARKER_HOLDBACK,
    find_function_call_marker,
    parse_anthropic_function_calls,
)

# The exact shape captured from chat ba36c86b (claude-haiku-4-5, deep investigation).
LEAKED = (
    "Let me begin investigating:\n\n"
    "**Step 1: Query Container App events.**\n"
    '<function_calls>\n'
    '<invoke name="containerapp">\n'
    '<parameter name="command">learn</parameter>\n'
    '<parameter name="intent">How do I query Container App diagnostic data?</parameter>\n'
    "</invoke>\n"
    "</function_calls>\n"
    '<invoke name="containerapp">\n'
    '<parameter name="command">containerapp_list</parameter>\n'
    '<parameter name="subscription">c3f6ae08-38a1-466d-abc2-972ad76b8661</parameter>\n'
    '<parameter name="resource_group">rg-azsupagent</parameter>\n'
    "</invoke>\n"
    "</function_calls>\n"
)


def test_parses_invoke_blocks_including_malformed_wrapper():
    calls = parse_anthropic_function_calls(LEAKED)
    assert [c.name for c in calls] == ["containerapp", "containerapp"]
    assert calls[0].arguments == {
        "command": "learn",
        "intent": "How do I query Container App diagnostic data?",
    }
    assert calls[1].arguments == {
        "command": "containerapp_list",
        "subscription": "c3f6ae08-38a1-466d-abc2-972ad76b8661",
        "resource_group": "rg-azsupagent",
    }


def test_coerces_numeric_and_keeps_guid_string():
    xml = (
        '<invoke name="monitor"><parameter name="hours">24</parameter>'
        '<parameter name="enabled">true</parameter>'
        '<parameter name="sub">c3f6ae08-38a1-466d-abc2-972ad76b8661</parameter></invoke>'
    )
    (call,) = parse_anthropic_function_calls(xml)
    assert call.arguments["hours"] == 24
    assert call.arguments["enabled"] is True
    assert call.arguments["sub"] == "c3f6ae08-38a1-466d-abc2-972ad76b8661"


def test_unescapes_html_entities_in_values():
    xml = '<invoke name="arm"><parameter name="query">a &gt; 5 &amp;&amp; b &lt; 3</parameter></invoke>'
    (call,) = parse_anthropic_function_calls(xml)
    assert call.arguments["query"] == "a > 5 && b < 3"


def test_no_invoke_returns_empty():
    assert parse_anthropic_function_calls("just a normal answer, no tools") == []
    assert parse_anthropic_function_calls("") == []


def test_marker_finder():
    assert find_function_call_marker("hello <function_calls> x") == 6
    assert find_function_call_marker('a <invoke name="x">') == 2
    assert find_function_call_marker("no marker here") == -1
    # earliest wins
    assert find_function_call_marker('<invoke name="a"> then <function_calls>') == 0


# ------------------------------------------------------------------ provider holdback
def _sse(events: list[dict]) -> list[str]:
    import json as _json

    return [f"data: {_json.dumps(e)}" for e in events]


class _FakeResp:
    def __init__(self, lines: list[str]) -> None:
        self.status_code = 200
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeClient:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, *a, **k):
        return _FakeResp(self._lines)


def _text_stream_events(text: str) -> list[dict]:
    """A minimal Anthropic SSE: one text block streamed in small chunks."""
    evts = [{"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}]
    # chunk it to exercise the split-marker holdback
    for i in range(0, len(text), 7):
        evts.append({"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": text[i:i + 7]}})
    evts.append({"type": "message_delta", "usage": {"output_tokens": 5}})
    return evts


async def _collect(provider) -> tuple[str, list]:
    tokens, calls = "", []
    async for ev in provider.stream([{"role": "user", "content": "go"}], tools=None):
        if ev.type == "token":
            tokens += ev.text
        elif ev.type == "tool_calls":
            calls = ev.tool_calls
    return tokens, calls


@pytest.mark.asyncio
async def test_provider_holds_back_xml_and_emits_tool_calls(monkeypatch):
    import httpx

    from app.agent import claude_provider as cp

    lines = _sse(_text_stream_events(LEAKED))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))

    prov = cp.ClaudeProvider(model="claude-haiku-4-5", api_key="x")
    monkeypatch.setattr(prov, "_auth_headers", lambda: _async_return({}))

    tokens, calls = await _collect(prov)
    # The raw XML must NOT appear in the visible answer…
    assert "<function_calls>" not in tokens
    assert "<invoke" not in tokens
    # …but the prose before it is preserved…
    assert "Step 1" in tokens
    # …and the tool calls were recovered.
    assert [c.name for c in calls] == ["containerapp", "containerapp"]


@pytest.mark.asyncio
async def test_provider_streams_normal_text_unchanged(monkeypatch):
    import httpx

    from app.agent import claude_provider as cp

    answer = "Here is a normal answer with no tool calls at all, just prose. " * 3
    lines = _sse(_text_stream_events(answer))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))

    prov = cp.ClaudeProvider(model="claude-haiku-4-5", api_key="x")
    monkeypatch.setattr(prov, "_auth_headers", lambda: _async_return({}))

    tokens, calls = await _collect(prov)
    assert tokens == answer
    assert calls == []


def _async_return(value):
    async def _coro():
        return value

    return _coro()

"""Tests for ReAct/plan artifact stripping in the final answer."""
from app.agent.tool_protocol import (
    extract_thought,
    flatten_messages,
    parse_tool_calls,
    strip_plan_preamble,
    strip_react_artifacts,
)


def test_strip_react_artifacts_empty():
    assert strip_react_artifacts("") == ""


def test_plain_prose_is_unchanged():
    text = "The VM is healthy and running in West Europe."
    assert strip_react_artifacts(text) == text


def test_strips_tool_result_line():
    text = 'Here is the status.\nTool result: {"count": 3}\nAll good.'
    out = strip_react_artifacts(text)
    assert "Tool result" not in out
    assert "Here is the status." in out
    assert "All good." in out


def test_leaked_directive_only_returns_fallback():
    # An answer that is nothing but a leaked tool-call directive must not be shown raw.
    text = '{"tool_calls": [{"name": "az_graph", "arguments": {}}]}'
    out = strip_react_artifacts(text)
    assert "tool_calls" not in out
    assert out  # a non-empty safe fallback, never a blank bubble


def test_strip_plan_preamble_removes_leading_plan():
    text = (
        "I understand you want a reliability review. Here's my plan:\n\n"
        "# Findings\n\n"
        "The disk is unattached and can be safely removed to cut cost."
    )
    out = strip_plan_preamble(text)
    assert out.startswith("# Findings")
    assert "my plan" not in out.lower()


def test_strip_plan_preamble_keeps_normal_answer():
    text = "The disk is unattached and can be safely removed."
    assert strip_plan_preamble(text) == text


def test_strip_plan_preamble_keeps_when_no_heading_follows():
    # Opens like a plan but has no real heading section — must not strip content.
    text = "I understand you want a plan but here are the results inline only."
    assert strip_plan_preamble(text) == text


# --- parse_tool_calls: the ReAct directive parser (providers w/o native tool calls) ---
def test_parse_tool_calls_valid_single():
    calls = parse_tool_calls('{"tool_calls": [{"name": "get_vm", "arguments": {"id": "1"}}]}')
    assert len(calls) == 1
    assert calls[0].name == "get_vm"
    assert calls[0].arguments == {"id": "1"}


def test_parse_tool_calls_empty_and_whitespace():
    assert parse_tool_calls("") == []
    assert parse_tool_calls("   \n\t  ") == []


def test_parse_tool_calls_plain_prose_no_directive():
    assert parse_tool_calls("The VM is healthy and running.") == []


def test_parse_tool_calls_truncated_json_is_safe():
    # A directive cut off mid-stream must never raise — just yield nothing.
    assert parse_tool_calls('{"tool_calls": [{"name": "foo"') == []


def test_parse_tool_calls_missing_name_skipped():
    assert parse_tool_calls('{"tool_calls": [{"arguments": {}}]}') == []


def test_parse_tool_calls_arguments_malformed_string_defaults_empty():
    # `arguments` given as an unparseable JSON string falls back to {} (not a crash).
    calls = parse_tool_calls('{"tool_calls": [{"name": "x", "arguments": "{bad json"}]}')
    assert len(calls) == 1
    assert calls[0].name == "x"
    assert calls[0].arguments == {}


def test_parse_tool_calls_inside_markdown_fence():
    text = '```json\n{"tool_calls": [{"name": "a", "arguments": {}}]}\n```'
    calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "a"


def test_parse_tool_calls_literal_newline_in_argument():
    # Raw newlines inside a string value (common with KQL/SQL) must be tolerated.
    text = '{"tool_calls": [{"name": "q", "arguments": {"kql": "T\n| take 1"}}]}'
    calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].arguments["kql"] == "T\n| take 1"


# --- extract_thought: the model's brief plan line shown before tool runs ---
def test_extract_thought_present():
    assert extract_thought('{"thought": "look up the VM", "tool_calls": []}') == "look up the VM"


def test_extract_thought_absent_returns_empty():
    assert extract_thought('{"tool_calls": []}') == ""


def test_extract_thought_whitespace_only_ignored():
    assert extract_thought('{"thought": "   \\n\\t  ", "tool_calls": []}') == ""


def test_extract_thought_non_json_returns_empty():
    assert extract_thought("just some prose, no envelope") == ""


# --- flatten_messages: OpenAI-style messages -> single ReAct transcript ---
def test_flatten_messages_empty():
    assert flatten_messages([]) == ""


def test_flatten_messages_roles_and_order():
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
        {"role": "tool", "content": "result-text"},
    ]
    out = flatten_messages(msgs)
    assert "You are helpful." in out
    assert "User: Hi" in out
    assert "Assistant: Hello!" in out
    assert "Tool result: result-text" in out


def test_flatten_messages_renders_valid_tool_call():
    msgs = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "get_vm", "arguments": "{}"}}],
        }
    ]
    out = flatten_messages(msgs)
    assert "Assistant (tool call):" in out
    assert "get_vm" in out


def test_flatten_messages_skips_blank_tool_call_keeps_content():
    # A malformed tool_call (function=None → no name) must NOT emit a blank {"name":""}
    # directive; any plain content on the same message is still rendered.
    msgs = [
        {
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [{"id": "1", "type": "function", "function": None}],
        }
    ]
    out = flatten_messages(msgs)
    assert '"name": ""' not in out
    assert "tool_calls" not in out
    assert "Let me check." in out


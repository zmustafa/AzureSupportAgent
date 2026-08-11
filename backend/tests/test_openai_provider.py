"""Tests for the OpenAI-compatible provider's request payload construction."""
from typing import Any

import pytest

from app.agent.openai_provider import OpenAIProvider
from app.agent.provider import ToolSpec


class _FakeCompletions:
    """Captures the kwargs the provider sends and returns an empty stream."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.captured = kwargs

        async def _empty():
            return
            yield  # pragma: no cover - makes this an async generator

        return _empty()


class _FakeClient:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = self


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


class _AdaptiveClient:
    def __init__(self, completions: Any | None = None) -> None:
        self.completions = completions or _FakeCompletions()
        self.chat = self
        self.responses = _FakeResponses()


class _ToolRoundResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        call_number = len(self.calls)

        async def _events():
            response = type(
                "Response",
                (),
                {
                    "id": f"resp-{call_number}",
                    "usage": type("Usage", (), {"input_tokens": 3, "output_tokens": 2})(),
                },
            )()
            yield type("Event", (), {"type": "response.created", "response": response})()
            if call_number == 1:
                item = type(
                    "Item",
                    (),
                    {
                        "type": "function_call",
                        "id": "item-1",
                        "call_id": "call-1",
                        "name": "check_health",
                        "arguments": '{"resource":"vm-1"}',
                    },
                )()
                yield type("Event", (), {"type": "response.output_item.done", "item": item})()
            else:
                yield type(
                    "Event",
                    (),
                    {"type": "response.output_text.delta", "delta": "Healthy"},
                )()
            yield type("Event", (), {"type": "response.completed", "response": response})()

        return _events()


class _ErrorThenEmptyCompletions:
    def __init__(self, message: str, *, accept_reasoning_none: bool = False) -> None:
        self.message = message
        self.accept_reasoning_none = accept_reasoning_none
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not (
            self.accept_reasoning_none
            and kwargs.get("reasoning_effort") == "none"
        ):
            raise RuntimeError(self.message)

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


def _provider() -> tuple[OpenAIProvider, _FakeCompletions]:
    prov = OpenAIProvider(provider="openai", api_key="test-key", model="gpt-4o")
    fake = _FakeClient()
    prov._client = fake  # type: ignore[assignment]
    return prov, fake.completions


async def _drain(prov: OpenAIProvider, tools: list[ToolSpec] | None) -> None:
    async for _ in prov.stream([{"role": "user", "content": "hi"}], tools):
        pass


@pytest.mark.asyncio
async def test_tools_key_omitted_when_none():
    # An explicit `tools: null` is rejected with a 400 by stricter OpenAI-compatible
    # endpoints, so the key must be absent rather than present-and-null.
    prov, completions = _provider()
    await _drain(prov, None)
    assert "tools" not in completions.captured


@pytest.mark.asyncio
async def test_tools_key_omitted_when_empty_list():
    prov, completions = _provider()
    await _drain(prov, [])
    assert "tools" not in completions.captured


@pytest.mark.asyncio
async def test_tools_key_present_and_well_formed_when_supplied():
    prov, completions = _provider()
    spec = ToolSpec(
        name="az_graph",
        description="Run a Resource Graph query",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    await _drain(prov, [spec])
    tools = completions.captured.get("tools")
    assert tools is not None
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "az_graph",
                "description": "Run a Resource Graph query",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_core_payload_fields_are_unchanged_for_toolless_calls():
    # Guard against the tools change accidentally dropping anything else.
    prov, completions = _provider()
    await _drain(prov, None)
    sent = completions.captured
    assert sent["model"] == "gpt-4o"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    assert "max_tokens" in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [None, [ToolSpec("search_tools", "Search", {"type": "object"})]])
async def test_gpt_5_6_sol_uses_responses_automatically(tools):
    provider = OpenAIProvider(provider="openai", api_key="test", model="gpt-5.6-sol")
    client = _AdaptiveClient()
    provider._client = client  # type: ignore[assignment]
    await _drain(provider, tools)
    assert client.responses.calls
    assert client.completions.captured == {}
    sent = client.responses.calls[0]
    assert sent["model"] == "gpt-5.6-sol"
    assert not any(tool.get("type") == "tool_search" for tool in sent.get("tools", []))


@pytest.mark.asyncio
async def test_gpt_5_6_sol_function_result_round_trip_uses_native_items():
    provider = OpenAIProvider(provider="openai", api_key="test", model="gpt-5.6-sol")
    client = _AdaptiveClient()
    client.responses = _ToolRoundResponses()
    provider._client = client  # type: ignore[assignment]
    tool = ToolSpec(
        "check_health",
        "Check health",
        {"type": "object", "properties": {"resource": {"type": "string"}}},
    )
    first = [
        event
        async for event in provider.stream(
            [{"role": "user", "content": "Check vm-1"}], [tool]
        )
    ]
    calls = [event for event in first if event.type == "tool_calls"]
    assert calls and calls[0].tool_calls[0].id == "call-1"

    history = [
        {"role": "user", "content": "Check vm-1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "check_health",
                        "arguments": '{"resource":"vm-1"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"healthy":true}'},
    ]
    second = [event async for event in provider.stream(history, [tool])]
    assert "".join(event.text for event in second if event.type == "token") == "Healthy"
    sent_items = client.responses.calls[1]["input"]
    assert any(item.get("type") == "function_call" for item in sent_items)
    assert any(item.get("type") == "function_call_output" for item in sent_items)


def test_responses_history_preserves_function_call_identity():
    instructions, items = OpenAIProvider._to_responses_input(
        [
            {"role": "system", "content": "System rule"},
            {"role": "user", "content": "Check it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-7",
                        "type": "function",
                        "function": {"name": "check_health", "arguments": '{"id":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-7", "content": '{"ok":true}'},
        ]
    )
    assert instructions == "System rule"
    assert {"type": "function_call", "call_id": "call-7", "name": "check_health", "arguments": '{"id":"x"}'} in items
    assert {"type": "function_call_output", "call_id": "call-7", "output": '{"ok":true}'} in items


@pytest.mark.asyncio
async def test_unknown_direct_openai_model_learns_responses_from_exact_400():
    error = (
        "Function tools with reasoning_effort are not supported for future-model in "
        "/v1/chat/completions. To use function tools, use /v1/responses or set "
        "reasoning_effort to 'none'."
    )
    completions = _ErrorThenEmptyCompletions(error)
    client = _AdaptiveClient(completions)
    provider = OpenAIProvider(provider="openai", api_key="test", model="future-model")
    provider._client = client  # type: ignore[assignment]
    tool = ToolSpec("check", "Check", {"type": "object"})
    await _drain(provider, [tool])
    assert len(completions.calls) == 1
    assert len(client.responses.calls) == 1
    # Runtime learning avoids paying the same rejected Chat call again.
    await _drain(provider, [tool])
    assert len(completions.calls) == 1
    assert len(client.responses.calls) == 2


@pytest.mark.asyncio
async def test_gateway_without_responses_retries_reasoning_none_once():
    error = (
        "Function tools with reasoning_effort are not supported for routed-model in "
        "/v1/chat/completions. To use function tools, use /v1/responses or set "
        "reasoning_effort to 'none'."
    )
    completions = _ErrorThenEmptyCompletions(error, accept_reasoning_none=True)
    provider = OpenAIProvider(
        provider="openrouter",
        api_key="test",
        model="routed-model",
        base_url="https://example.invalid/v1",
    )
    provider._client = _AdaptiveClient(completions)  # type: ignore[assignment]
    await _drain(provider, [ToolSpec("check", "Check", {"type": "object"})])
    assert len(completions.calls) == 2
    assert completions.calls[1]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_unrelated_provider_error_is_not_blindly_retried():
    completions = _ErrorThenEmptyCompletions("invalid request: unrelated parameter")
    provider = OpenAIProvider(provider="openai", api_key="test", model="gpt-4o")
    provider._client = _AdaptiveClient(completions)  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="unrelated parameter"):
        await _drain(provider, [ToolSpec("check", "Check", {"type": "object"})])
    assert len(completions.calls) == 1

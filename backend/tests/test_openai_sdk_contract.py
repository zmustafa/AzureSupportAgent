"""Pins the OpenAI SDK response contract the provider parses.

`test_openai_provider.py` covers request SHAPING with duck-typed stand-ins
(`type("Event", (), {...})()`). That is deliberate and fast, but it cannot notice when the
SDK changes the objects it hands BACK -- a stand-in always has whatever attribute the test
gave it. The openai 2.x -> 3.x major moved the transport to httpx2 and reshaped usage
details, so the streaming attribute paths are pinned here against REAL SDK models instead.

If a future SDK release renames or restructures any field read by `_stream_chat` or the
responses parser, these tests fail at the exact attribute rather than silently degrading a
live chat into empty tokens or lost tool calls.
"""
from typing import Any

import pytest
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import (
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import CompletionUsage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputItemDoneEvent,
    ResponseTextDeltaEvent,
)

from app.agent.openai_provider import OpenAIProvider
from app.agent.provider import ToolSpec


def _chat_chunks() -> list[ChatCompletionChunk]:
    """One text chunk, one tool-call chunk, one usage-only chunk, as the API streams them."""
    common = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o"}
    return [
        ChatCompletionChunk(
            **common, choices=[Choice(index=0, delta=ChoiceDelta(content="Hel"))]
        ),
        ChatCompletionChunk(
            **common, choices=[Choice(index=0, delta=ChoiceDelta(content="lo"))]
        ),
        ChatCompletionChunk(
            **common,
            choices=[
                Choice(
                    index=0,
                    delta=ChoiceDelta(
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_1",
                                function=ChoiceDeltaToolCallFunction(
                                    name="check_health", arguments='{"resource":'
                                ),
                            )
                        ]
                    ),
                )
            ],
        ),
        ChatCompletionChunk(
            **common,
            choices=[
                Choice(
                    index=0,
                    delta=ChoiceDelta(
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                function=ChoiceDeltaToolCallFunction(arguments='"vm-1"}'),
                            )
                        ]
                    ),
                )
            ],
        ),
        ChatCompletionChunk(
            **common,
            choices=[],
            usage=CompletionUsage(prompt_tokens=11, completion_tokens=22, total_tokens=33),
        ),
    ]


class _SdkCompletions:
    async def create(self, **_kwargs: Any) -> Any:
        async def _events():
            for chunk in _chat_chunks():
                yield chunk

        return _events()


class _SdkChatClient:
    def __init__(self) -> None:
        self.completions = _SdkCompletions()
        self.chat = self


class _SdkResponses:
    """Replays real Responses-API stream events, including a native function call."""

    async def create(self, **_kwargs: Any) -> Any:
        async def _events():
            yield ResponseTextDeltaEvent(
                type="response.output_text.delta",
                delta="Healthy",
                content_index=0,
                item_id="i1",
                output_index=0,
                sequence_number=1,
                logprobs=[],
            )
            yield ResponseOutputItemDoneEvent(
                type="response.output_item.done",
                item=ResponseFunctionToolCall(
                    type="function_call",
                    id="fc1",
                    call_id="call_9",
                    name="check_health",
                    arguments='{"resource":"vm-1"}',
                ),
                output_index=0,
                sequence_number=2,
            )

        return _events()


class _SdkResponsesClient:
    def __init__(self) -> None:
        self.completions = _SdkCompletions()
        self.chat = self
        self.responses = _SdkResponses()


async def _collect(provider: OpenAIProvider, tools: list[ToolSpec] | None = None) -> list[Any]:
    return [event async for event in provider.stream([{"role": "user", "content": "hi"}], tools)]


def test_client_constructors_accept_the_arguments_the_provider_passes():
    # Constructed for real: a renamed or dropped kwarg fails here instead of at first use.
    assert AsyncOpenAI(api_key="k", base_url="https://example.invalid/v1", default_headers={"a": "b"})
    assert AsyncOpenAI(api_key="k", default_query={"api-version": "2024-05-01-preview"})
    assert AsyncAzureOpenAI(
        api_key="k",
        azure_endpoint="https://example.invalid",
        api_version="2024-10-21",
        default_headers={"a": "b"},
    )


@pytest.mark.asyncio
async def test_chat_stream_parses_real_sdk_chunks():
    provider = OpenAIProvider(provider="openai", api_key="test", model="gpt-4o")
    provider._client = _SdkChatClient()  # type: ignore[assignment]

    events = await _collect(provider)

    assert "".join(e.text for e in events if e.type == "token") == "Hello"

    tool_events = [e for e in events if e.type == "tool_calls"]
    assert len(tool_events) == 1
    call = tool_events[0].tool_calls[0]
    # Proves index-keyed fragment reassembly across chunks, not just a single-chunk read.
    assert (call.id, call.name, call.arguments) == ("call_1", "check_health", {"resource": "vm-1"})

    done = [e for e in events if e.type == "done"]
    assert (done[-1].prompt_tokens, done[-1].completion_tokens) == (11, 22)


@pytest.mark.asyncio
async def test_responses_stream_parses_real_sdk_events():
    provider = OpenAIProvider(provider="openai", api_key="test", model="gpt-5.6-sol")
    provider._client = _SdkResponsesClient()  # type: ignore[assignment]

    events = await _collect(provider, [ToolSpec("check_health", "Check", {"type": "object"})])

    assert "".join(e.text for e in events if e.type == "token") == "Healthy"

    tool_events = [e for e in events if e.type == "tool_calls"]
    assert len(tool_events) == 1
    call = tool_events[0].tool_calls[0]
    # call_id is the identity the follow-up turn must echo back; id would not round-trip.
    assert (call.id, call.name, call.arguments) == ("call_9", "check_health", {"resource": "vm-1"})

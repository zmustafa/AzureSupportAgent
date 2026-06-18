"""Pluggable LLM provider abstraction.

A single interface so the orchestrator is provider-agnostic. Switching providers
(OpenAI / Anthropic / Azure OpenAI) is a config change, not a code change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class StreamEvent:
    """One event emitted while the model generates a response."""

    type: str  # "token" | "tool_calls" | "done" | "status"
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # For type=="status": a short machine phase ("connecting" | "request_sent" |
    # "response") so the UI can pick an icon; `text` carries the human message.
    phase: str = ""


class LLMProvider(ABC):
    """Provider interface: streaming chat completion with tool calling."""

    @abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a model response. Yields token events, then optionally a
        tool_calls event, then a final done event with usage.

        ``max_tokens`` optionally overrides the configured response cap for this one
        call (used by large structured-JSON completions like the architecture designer);
        providers that don't support it may ignore it."""
        raise NotImplementedError
        yield  # pragma: no cover

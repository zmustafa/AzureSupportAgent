"""OpenAI-compatible provider adapter (OpenAI, GitHub Models, Azure OpenAI)."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.agent.provider import LLMProvider, StreamEvent, ToolCallRequest, ToolSpec

# Models (e.g. Azure gpt-5 / o-series) that reject `max_tokens` and require
# `max_completion_tokens` instead. Learned at runtime so the failed first call is paid
# only once per process, then the correct param is sent up front.
_NEEDS_MAX_COMPLETION_TOKENS: set[str] = set()


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str = "",
        api_version: str = "2024-10-21",
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._model = model
        self._provider = provider
        if provider == "azure_openai":
            self._client: AsyncOpenAI | AsyncAzureOpenAI = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=base_url,
                api_version=api_version,
                default_headers=default_headers,
            )
        elif base_url:
            # GitHub Models, Azure AI Foundry, and any OpenAI-compatible gateway.
            default_query = None
            if provider == "azure_foundry":
                # Azure AI Foundry's model-inference endpoint
                # (…services.ai.azure.com/models) requires an api-version query param on
                # every call; the SDK posts to {base_url}/chat/completions with Bearer auth.
                default_query = {"api-version": api_version or "2024-05-01-preview"}
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers=default_headers,
                default_query=default_query,
            )
        else:
            self._client = AsyncOpenAI(api_key=api_key, default_headers=default_headers)

    # Friendly provider label for the connection status line (e.g. "OpenAI · gpt-4.1").
    _PROVIDER_NAMES = {
        "openai": "OpenAI",
        "azure_openai": "Azure OpenAI",
        "azure_foundry": "Azure Foundry",
        "github": "GitHub Models",
        "github_copilot": "GitHub Copilot",
        "gemini": "Google Gemini",
        "grok": "Grok (xAI)",
        "mistral": "Mistral",
        "openrouter": "OpenRouter",
        "ollama": "Ollama",
        "lmstudio": "LM Studio",
    }

    def _label(self) -> str:
        name = self._PROVIDER_NAMES.get(self._provider, self._provider.replace("_", " ").title())
        return f"{name} · {self._model}" if self._model else name

    @staticmethod
    def _to_openai_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Accumulate streamed tool-call fragments by index.
        tool_fragments: dict[int, dict[str, Any]] = {}

        from app.core.app_settings import generation_params

        params = generation_params()
        cap = int(max_tokens) if max_tokens else params["max_tokens"]
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": self._to_openai_tools(tools),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # gpt-5 / o-series Azure models use `max_completion_tokens`; everything else uses
        # `max_tokens`. Pick up front for known models so we don't fail-then-retry.
        cap_param = (
            "max_completion_tokens"
            if self._model in _NEEDS_MAX_COMPLETION_TOKENS
            else "max_tokens"
        )
        kwargs[cap_param] = cap

        # Surface connection milestones so the chat's "Working on your request…" feed shows
        # measured progress (instead of a static line) while the model is contacted.
        yield StreamEvent(type="status", phase="connecting", text=f"Connecting to {self._label()}…")
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - retry once on token-param rejection
            # Move the cap to `max_completion_tokens` when the model demands it (Azure
            # gpt-5 / o-series); otherwise drop the cap entirely and retry.
            msg = str(exc).lower()
            cap_val = kwargs.pop("max_tokens", None)
            cap_val = kwargs.pop("max_completion_tokens", cap_val)
            if cap_val and "max_completion_tokens" in msg:
                _NEEDS_MAX_COMPLETION_TOKENS.add(self._model)
                kwargs["max_completion_tokens"] = cap_val
            stream = await self._client.chat.completions.create(**kwargs)
        yield StreamEvent(type="status", phase="request_sent", text="Request sent · awaiting response…")

        prompt_tokens = 0
        completion_tokens = 0
        first_chunk = True

        async for chunk in stream:
            if first_chunk:
                first_chunk = False
                yield StreamEvent(type="status", phase="response", text="Response received · generating…")
            if chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens
                completion_tokens = chunk.usage.completion_tokens

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta and delta.content:
                yield StreamEvent(type="token", text=delta.content)

            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    frag = tool_fragments.setdefault(
                        tc.index, {"id": "", "name": "", "args": ""}
                    )
                    if tc.id:
                        frag["id"] = tc.id
                    if tc.function and tc.function.name:
                        frag["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        frag["args"] += tc.function.arguments

        if tool_fragments:
            calls: list[ToolCallRequest] = []
            for frag in tool_fragments.values():
                try:
                    args = json.loads(frag["args"]) if frag["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append(
                    ToolCallRequest(id=frag["id"], name=frag["name"], arguments=args)
                )
            yield StreamEvent(type="tool_calls", tool_calls=calls)

        yield StreamEvent(
            type="done",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

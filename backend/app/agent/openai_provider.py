"""OpenAI-compatible provider adapter (OpenAI, GitHub Models, Azure OpenAI)."""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.agent.provider import LLMProvider, StreamEvent, ToolCallRequest, ToolSpec
from app.agent.provider_capabilities import (
    ToolDefinitionLimitError,
    capabilities_for,
    validate_tool_count,
)

# Models (e.g. Azure gpt-5 / o-series) that reject `max_tokens` and require
# `max_completion_tokens` instead. Learned at runtime so the failed first call is paid
# only once per process, then the correct param is sent up front.
_NEEDS_MAX_COMPLETION_TOKENS: set[str] = set()
# Runtime-learned compatibility is keyed by provider+model so one definitive 400 is
# paid at most once per process. Static profiles remain the first source of truth.
_RESPONSES_REQUIRED_FOR_TOOLS: set[tuple[str, str]] = set()
_CHAT_TOOLS_REQUIRE_REASONING_NONE: set[tuple[str, str]] = set()


def _tool_reasoning_requires_responses(message: str) -> bool:
    value = (message or "").lower()
    return (
        "function tools with reasoning_effort are not supported" in value
        and "/v1/chat/completions" in value
        and "/v1/responses" in value
    )


def _token_cap_retry(message: str) -> str:
    """Return `max_completion_tokens`, `drop`, or empty for a non-cap error."""
    value = (message or "").lower()
    unsupported = "unsupported" in value or "not supported" in value
    if not unsupported:
        return ""
    if "max_tokens" in value and "max_completion_tokens" in value:
        return "max_completion_tokens"
    if "max_tokens" in value or "max_completion_tokens" in value:
        return "drop"
    return ""


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

    @staticmethod
    def _to_responses_tool(tool: ToolSpec, *, loaded: bool | None = None) -> dict[str, Any]:
        deferred = tool.defer_loading if loaded is None else not loaded
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": False,
            **({"defer_loading": True} if deferred else {}),
        }

    @staticmethod
    def _to_responses_input(
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Translate Chat-style history to native Responses input items.

        The orchestrator stores assistant calls and tool results in the shared Chat
        shape. Responses requires matching `function_call` / `function_call_output`
        items; converting them to prose loses call identity and breaks multi-round tools.
        """
        instructions: list[str] = []
        inputs: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = message.get("content")
            if role in {"system", "developer"}:
                if isinstance(content, str) and content.strip():
                    instructions.append(content.strip())
                continue
            if role == "tool":
                output = content if isinstance(content, str) else json.dumps(content)
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": output,
                    }
                )
                continue

            if isinstance(content, list):
                parts: list[dict[str, Any]] = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text" and item.get("text"):
                        parts.append({"type": "input_text", "text": item["text"]})
                    elif item.get("type") == "image_url":
                        url = (item.get("image_url") or {}).get("url", "")
                        if url:
                            parts.append({"type": "input_image", "image_url": url})
                if parts:
                    inputs.append({"role": role, "content": parts})
            elif isinstance(content, str) and content.strip():
                inputs.append({"role": role, "content": content})

            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    inputs.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "arguments": str(function.get("arguments") or "{}"),
                        }
                    )
        return "\n\n".join(instructions), inputs

    @staticmethod
    def _search_deferred_tools(
        tools: list[ToolSpec], arguments: Any, *, limit: int = 8
    ) -> list[ToolSpec]:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"query": arguments}
        args = arguments if isinstance(arguments, dict) else {}
        query = str(args.get("query") or args.get("search") or args)
        from app.agent.tool_router import allows_writes

        write_intent = allows_writes(query)
        tokens = set(re.findall(r"[a-z0-9]+", query.lower()))

        def score(tool: ToolSpec) -> tuple[int, str]:
            name_tokens = set(re.findall(r"[a-z0-9]+", tool.name.lower()))
            desc_tokens = set(re.findall(r"[a-z0-9]+", tool.description.lower()))
            value = 12 * len(tokens & name_tokens) + 2 * len(tokens & desc_tokens)
            return value, tool.name

        deferred = [
            tool
            for tool in tools
            if tool.defer_loading
            and (
                tool.kind != "write"
                or tool.source == "azure_mcp"
                or write_intent
            )
        ]
        ranked = sorted(deferred, key=lambda t: (-score(t)[0], score(t)[1]))
        positive = [tool for tool in ranked if score(tool)[0] > 0]
        return (positive or ranked)[: max(1, min(12, limit))]

    async def _stream_responses(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        *,
        max_tokens: int | None,
        enable_tool_search: bool = False,
        emit_connecting: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        """Stream through OpenAI Responses with ordinary and optionally deferred tools.

        The bounded local router is the baseline. Native search is an independent
        optimization: when enabled, OpenAI requests a search and this client returns
        only matching definitions from the already-authorized local catalog. Execution
        always remains in the orchestrator over local stdio/connector handlers.
        """
        from app.core.app_settings import generation_params

        instructions, inputs = self._to_responses_input(messages)
        cap = int(max_tokens) if max_tokens else generation_params()["max_tokens"]
        available_tools = list(tools or [])
        has_deferred = enable_tool_search and any(tool.defer_loading for tool in available_tools)
        response_tools = [self._to_responses_tool(tool) for tool in available_tools]
        if has_deferred:
            response_tools.append(
                {
                    "type": "tool_search",
                    "execution": "client",
                    "description": "Search the application's authorized deferred tool catalog.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                        },
                        "required": ["query"],
                    },
                }
            )

        next_input: Any = inputs or [{"role": "user", "content": "Continue."}]
        previous_response_id: str | None = None
        total_prompt = 0
        total_completion = 0
        yielded_request_status = False

        if emit_connecting:
            yield StreamEvent(type="status", phase="connecting", text=f"Connecting to {self._label()}…")
        for _ in range(4 if has_deferred else 1):
            kwargs: dict[str, Any] = {
                "model": self._model,
                "input": next_input,
                "stream": True,
                "store": has_deferred,
                "max_output_tokens": cap,
            }
            if response_tools:
                kwargs["tools"] = response_tools
            if instructions and previous_response_id is None:
                kwargs["instructions"] = instructions
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            stream = await self._client.responses.create(**kwargs)  # type: ignore[union-attr]
            if not yielded_request_status:
                yielded_request_status = True
                yield StreamEvent(
                    type="status", phase="request_sent", text="Request sent · awaiting response…"
                )

            search_items: list[Any] = []
            function_calls: list[ToolCallRequest] = []
            first_chunk = True
            response_id = previous_response_id or ""
            async for event in stream:
                event_type = str(getattr(event, "type", ""))
                response = getattr(event, "response", None)
                if response is not None and getattr(response, "id", None):
                    response_id = str(response.id)
                if first_chunk:
                    first_chunk = False
                    yield StreamEvent(
                        type="status", phase="response", text="Response received · generating…"
                    )
                if event_type == "response.output_text.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        yield StreamEvent(type="token", text=delta)
                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    item_type = str(getattr(item, "type", ""))
                    if item_type == "tool_search_call":
                        search_items.append(item)
                    elif item_type == "function_call":
                        raw_args = str(getattr(item, "arguments", "") or "")
                        try:
                            args = json.loads(raw_args) if raw_args else {}
                        except json.JSONDecodeError:
                            args = {}
                        function_calls.append(
                            ToolCallRequest(
                                id=str(
                                    getattr(item, "call_id", None)
                                    or getattr(item, "id", "")
                                ),
                                name=str(getattr(item, "name", "")),
                                arguments=args,
                            )
                        )
                elif event_type == "response.completed" and response is not None:
                    usage = getattr(response, "usage", None)
                    total_prompt += int(getattr(usage, "input_tokens", 0) or 0)
                    total_completion += int(getattr(usage, "output_tokens", 0) or 0)
                elif event_type in {"response.failed", "response.incomplete"}:
                    error = getattr(response, "error", None) if response is not None else None
                    raise RuntimeError(f"OpenAI Responses API failed: {error or event_type}")

            if function_calls:
                yield StreamEvent(type="tool_calls", tool_calls=function_calls)
                yield StreamEvent(
                    type="done",
                    prompt_tokens=total_prompt,
                    completion_tokens=total_completion,
                )
                return
            if not search_items:
                yield StreamEvent(
                    type="done",
                    prompt_tokens=total_prompt,
                    completion_tokens=total_completion,
                )
                return
            if not response_id:
                raise RuntimeError("OpenAI tool search did not return a response id.")

            outputs: list[dict[str, Any]] = []
            for item in search_items:
                arguments = getattr(item, "arguments", {})
                args = arguments if isinstance(arguments, dict) else {}
                requested_limit = int(args.get("limit") or 8) if args else 8
                selected = self._search_deferred_tools(
                    available_tools, arguments, limit=requested_limit
                )
                outputs.append(
                    {
                        "type": "tool_search_output",
                        "call_id": str(
                            getattr(item, "call_id", None) or getattr(item, "id", "")
                        ),
                        "execution": "client",
                        "status": "completed",
                        "tools": [self._to_responses_tool(tool, loaded=True) for tool in selected],
                    }
                )
            previous_response_id = response_id
            next_input = outputs

        raise RuntimeError("OpenAI native tool search exceeded its expansion-round limit.")

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        caps = capabilities_for(self._provider, self._model)
        key = (self._provider, self._model)
        deferred_search = bool(tools and any(tool.defer_loading for tool in tools))
        use_responses = (
            caps.api == "responses"
            or (bool(tools) and key in _RESPONSES_REQUIRED_FOR_TOOLS)
            or (deferred_search and caps.supports_responses and caps.native_tool_search)
        )
        if use_responses:
            async for event in self._stream_responses(
                messages,
                tools,
                max_tokens=max_tokens,
                enable_tool_search=deferred_search and caps.native_tool_search,
            ):
                yield event
            return
        validate_tool_count(self._provider, self._model, tools)
        # Accumulate streamed tool-call fragments by index.
        tool_fragments: dict[int, dict[str, Any]] = {}

        from app.core.app_settings import generation_params

        params = generation_params()
        cap = int(max_tokens) if max_tokens else params["max_tokens"]
        openai_tools = self._to_openai_tools(tools)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # Omit `tools` entirely when there are none. Sending an explicit null is
        # rejected with a 400 by stricter OpenAI-compatible endpoints (e.g. newer
        # Azure AI Foundry models), and toolless calls are common here — chat title
        # generation and the provider connection probe both pass tools=None.
        if openai_tools:
            kwargs["tools"] = openai_tools
            if key in _CHAT_TOOLS_REQUIRE_REASONING_NONE:
                kwargs["reasoning_effort"] = "none"
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
        except Exception as exc:  # noqa: BLE001 - classified compatibility retries only
            msg = str(exc).lower()
            if (
                "array_above_max_length" in msg
                or ("tools" in msg and "maximum length" in msg)
            ):
                raise ToolDefinitionLimitError(
                    self._provider,
                    len(openai_tools or []),
                    128,
                ) from exc

            if openai_tools and _tool_reasoning_requires_responses(msg):
                if caps.supports_responses:
                    _RESPONSES_REQUIRED_FOR_TOOLS.add(key)
                    async for event in self._stream_responses(
                        messages,
                        tools,
                        max_tokens=max_tokens,
                        enable_tool_search=False,
                        emit_connecting=False,
                    ):
                        yield event
                    return
                # The endpoint itself explicitly offered `reasoning_effort=none` as
                # the alternative. Cache and retry once for gateways with no Responses.
                _CHAT_TOOLS_REQUIRE_REASONING_NONE.add(key)
                kwargs["reasoning_effort"] = "none"
                stream = await self._client.chat.completions.create(**kwargs)
            else:
                cap_retry = _token_cap_retry(msg)
                if not cap_retry:
                    raise
                cap_val = kwargs.pop("max_tokens", None)
                cap_val = kwargs.pop("max_completion_tokens", cap_val)
                if cap_retry == "max_completion_tokens" and cap_val:
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

"""ChatGPT (Codex) provider — ported from BuddyAI's C# SendViaCodexResponsesAsync.

Talks to the ChatGPT backend Codex endpoint (https://chatgpt.com/backend-api/codex)
using the OpenAI *Responses* API streaming format (NOT chat/completions). Auth is the
ChatGPT OAuth access token from the Codex CLI (~/.codex/auth.json), auto-refreshed.

Tool use: the Codex backend doesn't expose arbitrary function tools, so we reuse the
shared ReAct protocol — tool instructions go in `instructions`, and a JSON tool-call
directive is detected in the streamed output text.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agent import chatgpt_oauth as oauth
from app.agent.provider import LLMProvider, StreamEvent, ToolSpec
from app.agent.tool_protocol import (
    ToolCallDetector,
    build_tool_instructions,
    flatten_messages,
)

DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"

CODEX_MODELS = [
    # Only these are accepted by Codex with a ChatGPT account (verified live).
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
]


async def list_models(_t: str | None = None) -> list[str]:
    return list(CODEX_MODELS)


def _split_system_and_convo(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Separate system text (Responses API `instructions`) from the input turns."""
    system_parts: list[str] = []
    inputs: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            continue
        if role == "assistant" and m.get("tool_calls"):
            # Render the prior tool call as text so the model has the loop context.
            calls = [
                {
                    "name": (c.get("function") or {}).get("name", ""),
                    "arguments": (c.get("function") or {}).get("arguments", ""),
                }
                for c in m["tool_calls"]
            ]
            inputs.append(
                {"role": "assistant", "content": json.dumps({"tool_calls": calls})}
            )
            continue
        if role == "tool":
            inputs.append({"role": "user", "content": f"Tool result: {content}"})
            continue
        if isinstance(content, list):
            # Multimodal: convert to Responses API input_text / input_image parts.
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
            continue
        if isinstance(content, str) and content.strip():
            inputs.append({"role": role, "content": content})
    return "\n\n".join(system_parts), inputs


class CodexProvider(LLMProvider):
    """Streams chat via the ChatGPT Codex Responses API, with ReAct-based MCP tools."""

    def __init__(self, *, model: str, api_key: str = "", base_url: str = "") -> None:
        self._model = model or "gpt-5.5"
        self._override_token = (api_key or "").strip()
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    async def _auth(self) -> tuple[str, dict[str, str]]:
        if self._override_token:
            token, account_id = self._override_token, ""
        else:
            token, account_id = await oauth.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "session_id": str(uuid.uuid4()),
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id
        return token, headers

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        _token, headers = await self._auth()
        instructions, inputs = _split_system_and_convo(messages)
        if tools:
            instructions = build_tool_instructions(tools) + "\n\n" + instructions
            # Nudge the model to emit the directive as its next message and not stall.
            if inputs:
                from app.agent.tool_protocol import NEXT_STEP_CUE

                inputs[-1]["content"] += NEXT_STEP_CUE

        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "input": inputs or [{"role": "user", "content": flatten_messages(messages)}],
            "stream": True,
            "store": False,
        }
        # Reasoning models spend output budget on reasoning; without an explicit cap the
        # default can be exhausted by reasoning, leaving no output text. Honor a requested
        # max_tokens (e.g. architecture/memory JSON) as max_output_tokens so the result returns.
        if max_tokens:
            payload["max_output_tokens"] = int(max_tokens)

        url = f"{self._base_url}/responses"
        detector = ToolCallDetector(tools_enabled=bool(tools))
        completion_chars = 0
        current_event: str | None = None

        from app.core.app_settings import request_timeout_seconds

        _timeout = httpx.Timeout(float(request_timeout_seconds()), connect=15.0)
        async with httpx.AsyncClient(timeout=_timeout) as client:
            # ChatGPT can revoke an access token BEFORE its JWT expiry (e.g. a newer
            # `codex login` rotates the session), so a token that still looks valid is
            # rejected with 401. On the first 401, force a refresh and retry once so the
            # turn self-heals instead of failing — only surface the error if it persists.
            for attempt in range(2):
                retry = False
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code == 401:
                        if attempt == 0 and not self._override_token:
                            retry = True
                        else:
                            raise RuntimeError(
                                "ChatGPT rejected the token (401). Run `codex login` again, "
                                "then click Refresh in the admin AI Provider settings."
                            )
                    elif resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise RuntimeError(f"ChatGPT Codex API error {resp.status_code}: {body[:500]}")
                    else:
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("event:"):
                                current_event = line[len("event:"):].strip()
                                continue
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                evt = json.loads(data)
                            except json.JSONDecodeError:
                                continue

                            delta = ""
                            if current_event == "response.output_text.delta":
                                delta = evt.get("delta", "") if isinstance(evt, dict) else ""
                            elif current_event == "response.error" or (
                                isinstance(evt, dict) and evt.get("type") == "error"
                            ):
                                msg = (evt.get("error") or {}).get("message") if isinstance(evt, dict) else None
                                raise RuntimeError(f"ChatGPT Codex error: {msg or data[:200]}")

                            if not delta:
                                continue
                            completion_chars += len(delta)
                            for tok in detector.feed(delta):
                                yield StreamEvent(type="token", text=tok)
                if retry:
                    # Refresh the token, rebuild auth headers, and retry the request once.
                    try:
                        await oauth.force_refresh()
                    except Exception:  # noqa: BLE001 - fall through to retry with current token
                        pass
                    _token, headers = await self._auth()
                    continue
                break

        calls, leftover = detector.finish()
        if calls:
            # Surface the model's restated understanding + plan before the tool call.
            from app.agent.tool_protocol import thought_for_calls

            thought = thought_for_calls(detector.buffer, calls)
            if thought:
                yield StreamEvent(type="token", text=thought)
            yield StreamEvent(type="tool_calls", tool_calls=calls)
        elif leftover:
            yield StreamEvent(type="token", text=leftover)

        yield StreamEvent(type="done", completion_tokens=max(1, completion_chars // 4))

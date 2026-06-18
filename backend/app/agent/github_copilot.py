"""GitHub Copilot (OAuth) chat provider — ported from BuddyAI's C# AiProviderClient,
but self-contained: the token is captured and refreshed by this app's own browser
session (see github_copilot_auth.py), with no dependency on the BuddyAI desktop app.

Chat uses GitHub Copilot's web chat *thread* API (NOT OpenAI /chat/completions):
    1. POST {base}/github/chat/threads                -> create a thread
    2. POST {base}/github/chat/threads/{id}/messages  -> post message, stream SSE
Auth header is `Authorization: GitHub-Bearer <token>` plus Copilot browser headers.

Tool use: the thread API has no native function/tool calling, so this provider teaches
the model a JSON tool-call protocol via the prompt (ReAct), intercepts the emitted
JSON, and surfaces it as a `tool_calls` StreamEvent. The orchestrator's normal loop
then runs the Azure MCP tools and feeds results back — so MCP tools work here too.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agent import github_copilot_auth as auth
from app.agent.provider import LLMProvider, StreamEvent, ToolCallRequest, ToolSpec

DEFAULT_API_BASE_URL = auth.DEFAULT_API_BASE_URL

# The Copilot thread API rejects very large message content with a 'contentTooLarge'
# error. The combined Azure + EntraID tool catalog (~100 tools) plus accumulated tool
# results (a single list call can return tens of KB) easily exceeds the limit. We budget
# the conversation portion of the prompt and trim the OLDEST tool output first, always
# preserving the tool catalog, the system framing, and the most recent turns.
_MAX_CONVERSATION_CHARS = 32000


def _trim_conversation(convo: str, budget: int = _MAX_CONVERSATION_CHARS) -> str:
    """Keep a conversation transcript within ``budget`` chars by eliding the middle
    (oldest detail), which is where bulky early tool results live. The tail — the latest
    user question and the freshest tool results needed to synthesize an answer — is kept
    intact, and a head slice preserves the original ask."""
    if len(convo) <= budget:
        return convo
    head = budget // 4
    tail = budget - head
    elided = len(convo) - head - tail
    return (
        convo[:head]
        + f"\n\n[… {elided} characters of earlier tool output trimmed to fit …]\n\n"
        + convo[-tail:]
    )


# Browser/editor headers the Copilot thread API expects (ported from C#).
COPILOT_HEADERS: dict[str, str] = {
    "copilot-integration-id": "copilot-chat",
    "X-GitHub-Api-Version": "2025-05-01",
    "Origin": "https://github.com",
    "Referer": "https://github.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) "
        "Gecko/20100101 Firefox/151.0"
    ),
}

# Headers the OpenAI-compatible Copilot /chat/completions endpoint expects when called
# with a device-flow editor token (mirrors what the VS Code Copilot extension sends).
_EDITOR_COMPLETION_HEADERS: dict[str, str] = {
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "User-Agent": "GitHubCopilotChat/0.22.0",
}

# Fallback model catalog used only when the live model list can't be fetched
# (e.g. not signed in). The live list is preferred — see list_models().
COPILOT_MODELS: list[str] = [
    "claude-opus-4.8",
    "claude-opus-4.7",
    "claude-opus-4.6",
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.2",
    "gpt-4.1",
]

# Short-lived cache of the live model list so the picker doesn't hit the API on
# every render. (ids, expires_at_monotonic)
_MODELS_CACHE: tuple[list[str], float] | None = None
_MODELS_TTL_SECONDS = 300.0


async def _fetch_live_models() -> list[str]:
    """Fetch the account's selectable Copilot models from the Copilot models endpoint.

    Returns the model ids that are enabled in the model picker (the same set the
    GitHub Copilot editor model picker shows — including Gemini, GPT-5 mini and any
    internal/preview variants the account is entitled to), preserving the API's order.
    Raises on any failure so the caller can fall back to the curated list.
    """
    token, base = await auth.get_valid_token()
    base = base.rstrip("/")
    # Device-flow editor tokens query the editor models endpoint with Bearer + editor
    # headers; legacy web tokens use GitHub-Bearer + the web headers.
    if auth.auth_scheme() == "Bearer":
        headers = {"Authorization": f"Bearer {token}", **_EDITOR_COMPLETION_HEADERS}
    else:
        headers = {"Authorization": f"{auth.auth_scheme()} {token}", **COPILOT_HEADERS}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{base}/models", headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data") or []

    ids: list[str] = []
    seen: set[str] = set()
    for entry in data:
        mid = entry.get("id")
        if not mid or mid in seen:
            continue
        # Only models the account is allowed to pick in chat (excludes embeddings and
        # legacy non-picker models). This mirrors the editor's model picker.
        if entry.get("model_picker_enabled") is not True:
            continue
        seen.add(mid)
        ids.append(mid)
    return ids


async def list_models(_token: str | None = None) -> list[str]:
    """Return the account's live Copilot model list, falling back to the curated set.

    The live list is fetched from the Copilot chat models endpoint and cached briefly
    so it matches exactly what the GitHub Copilot model picker shows for this account.
    """
    global _MODELS_CACHE
    import time

    now = time.monotonic()
    if _MODELS_CACHE and _MODELS_CACHE[1] > now:
        return list(_MODELS_CACHE[0])

    try:
        ids = await _fetch_live_models()
        if ids:
            _MODELS_CACHE = (ids, now + _MODELS_TTL_SECONDS)
            return list(ids)
    except Exception:
        # Not signed in or transient error — fall back to the curated catalog.
        pass
    return list(COPILOT_MODELS)


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Collapse the OpenAI-style message list into a single prompt string.

    Delegates to the shared helper (which also handles multimodal/list content).
    """
    from app.agent.tool_protocol import flatten_messages

    return flatten_messages(messages)


def _build_tool_instructions(tools: list[ToolSpec]) -> str:
    """Describe the available MCP tools and the ReAct call protocol for the model.

    The Copilot thread API has no native function-calling, so we ask the model — as a
    cooperative structured-output request from the user — to pick which tool to run.
    Framed as the *user's* tooling (not an identity override) to avoid Copilot's
    prompt-injection guardrails. The provider runs the chosen tool and feeds results back.

    The catalog is kept COMPACT (name + short description + REQUIRED params only)
    because the Copilot thread API rejects very large prompts with a 'contentTooLarge'
    error — and the combined Azure + EntraID catalog is ~100 tools. We therefore trim
    each description hard and omit the noisy optional args (tenant/auth-method/retry-*),
    showing only required parameters.
    """
    def _params_hint(schema: dict[str, Any]) -> str:
        props = (schema or {}).get("properties") or {}
        if not props:
            return ""
        required = list((schema or {}).get("required") or [])
        if not required:
            return ""
        return "args: " + ", ".join(required)

    def _line(t: ToolSpec) -> str:
        desc = (t.description or "").strip().splitlines()[0][:72] if t.description else ""
        hint = _params_hint(t.parameters)
        suffix = f" ({hint})" if hint else ""
        return f"  - {t.name}: {desc}{suffix}"

    lines = "\n".join(_line(t) for t in tools)
    first = tools[0].name if tools else "a_tool"
    return (
        "I'm troubleshooting my Azure environment and I have a helper system that can "
        "run read-only Azure queries for me. I can't run them by hand here — you decide "
        "which query to run and I'll paste back the results.\n\n"
        "Queries my helper can run (name: description (args; * = required)):\n"
        f"{lines}\n\n"
        "How to answer me:\n"
        "- If you'd like me to run one or more queries, reply with ONLY this JSON (no "
        "other words, no markdown fences):\n"
        '  {"tool_calls": [{"name": "<query name>", "arguments": { ...args... }}]}\n'
        f'  e.g. {{"tool_calls": [{{"name": "{first}", "arguments": {{}}}}]}}\n'
        "- I'll reply with lines starting 'Tool result:' containing the data.\n"
        "- Once you have what you need, give me your normal written analysis and next "
        "steps (plain text, no JSON).\n\n"
        "Please start by telling me which query to run (as the JSON above) if you need "
        "Azure data to answer."
    )


_TOOLCALL_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_tool_calls(text: str) -> list[ToolCallRequest]:
    """Detect a tool-call JSON directive in the model's output. Returns [] if none."""
    stripped = text.strip()
    if not stripped:
        return []
    # Strip an optional ```json ... ``` fence the model may add despite instructions.
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    if "{" not in stripped:
        return []
    match = _TOOLCALL_RE.search(stripped)
    if not match:
        return []
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []

    raw_calls: list[Any] = []
    if isinstance(obj.get("tool_calls"), list):
        raw_calls = obj["tool_calls"]
    elif isinstance(obj.get("tool_call"), dict):
        raw_calls = [obj["tool_call"]]
    elif obj.get("name"):  # bare {"name": ..., "arguments": ...}
        raw_calls = [obj]
    else:
        return []

    calls: list[ToolCallRequest] = []
    for rc in raw_calls:
        if not isinstance(rc, dict):
            continue
        name = rc.get("name") or (rc.get("function") or {}).get("name")
        if not name:
            continue
        args = rc.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append(ToolCallRequest(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args))
    return calls


def _extract_thread_id(body: str) -> str:
    """Port of ExtractGitHubCopilotThreadId."""
    try:
        root = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(root, dict):
        for key in ("id", "threadID", "threadId", "thread_id"):
            val = root.get(key)
            if isinstance(val, str) and val.strip():
                return val
        thread = root.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            return thread["id"]
    return ""


def _append_delta(element: Any, out: list[str]) -> None:
    """Port of AppendGitHubCopilotDelta: pull text out of a streamed event."""
    if isinstance(element, dict):
        if element.get("type") == "content" and isinstance(element.get("body"), str):
            out.append(element["body"])
            return
        delta = element.get("delta")
        if delta is not None:
            if isinstance(delta, str):
                out.append(delta)
            else:
                _append_delta(delta, out)
            return
        content = element.get("content")
        if content is not None:
            if isinstance(content, str):
                out.append(content)
            else:
                _append_delta(content, out)
            return
    elif isinstance(element, list):
        for item in element:
            _append_delta(item, out)


def _latest_user_images(messages: list[dict[str, Any]]) -> list[str]:
    """Return the data-URL images attached to the most recent user turn, if any."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            urls = [
                (item.get("image_url") or {}).get("url", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "image_url"
            ]
            return [u for u in urls if isinstance(u, str) and u.startswith("data:")]
        return []
    return []


class GitHubCopilotChatProvider(LLMProvider):
    """Streams chat via the GitHub Copilot web thread API, with ReAct-based MCP tools."""

    def __init__(self, *, model: str, api_key: str = "", base_url: str = "") -> None:
        self._model = model or "gpt-4.1"
        # Optional manual override; normally the token comes from the browser session.
        self._override_token = (api_key or "").strip()
        self._override_base = (base_url or "").strip().rstrip("/")

    async def _resolve_token(self) -> tuple[str, str]:
        if self._override_token:
            base = (self._override_base or DEFAULT_API_BASE_URL).rstrip("/")
            return self._override_token, base
        token, base = await auth.get_valid_token()
        return token, (self._override_base or base).rstrip("/")

    def _auth_headers(self, token: str) -> dict[str, str]:
        # Device-flow (editor) tokens use "Bearer"; legacy browser-sniffed web tokens use
        # "GitHub-Bearer". auth.auth_scheme() reports which the cached token needs.
        scheme = "GitHub-Bearer" if self._override_token else auth.auth_scheme()
        return {"Authorization": f"{scheme} {token}", **COPILOT_HEADERS}

    def _uses_editor_api(self) -> bool:
        """True when the cached credential is a device-flow editor token, which speaks the
        OpenAI-compatible /chat/completions API (with native tools) — NOT the web thread
        API. Legacy browser-sniffed web tokens (auth_scheme 'GitHub-Bearer') use threads."""
        return not self._override_token and auth.auth_scheme() == "Bearer"

    async def _stream_editor_api(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream via the OpenAI-compatible Copilot completions endpoint using the
        device-flow editor token. Supports native function/tool calling, so no ReAct
        prompt protocol is needed here (the orchestrator gets real tool_calls events).

        GPT-5 / o-series Copilot models are NOT served by /chat/completions (Copilot
        returns 'unsupported_api_for_model'); those fall back to the /responses API."""
        from app.agent.openai_provider import OpenAIProvider

        token, base = await auth.get_valid_token()
        op = OpenAIProvider(
            provider="github_copilot",
            api_key=token,
            model=self._model,
            base_url=(self._override_base or base).rstrip("/"),
            default_headers=_EDITOR_COMPLETION_HEADERS,
        )
        try:
            async for ev in op.stream(messages, tools, max_tokens=max_tokens):
                yield ev
            return
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            # GPT-5 family must use the Responses API on Copilot.
            if "unsupported_api_for_model" in msg or "/chat/completions endpoint" in msg:
                async for ev in self._stream_editor_responses(messages, tools, max_tokens=max_tokens):
                    yield ev
                return
            # Token rotated mid-session — refresh once and retry chat/completions.
            if "401" not in msg:
                raise
        refreshed = await auth.refresh_token()
        if not refreshed:
            raise RuntimeError(
                "GitHub Copilot token is no longer valid — sign in again on the AI "
                "Provider settings page."
            )
        op2 = OpenAIProvider(
            provider="github_copilot",
            api_key=refreshed,
            model=self._model,
            base_url=(self._override_base or (await auth.get_valid_token())[1]).rstrip("/"),
            default_headers=_EDITOR_COMPLETION_HEADERS,
        )
        async for ev in op2.stream(messages, tools, max_tokens=max_tokens):
            yield ev

    async def _stream_editor_responses(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a GPT-5 / o-series Copilot model via the OpenAI **Responses** API
        (/responses), which is the only endpoint those models accept on Copilot. Tools
        use the shared ReAct protocol (the directive is detected in the output text), so
        the orchestrator still gets tool_calls events."""
        from app.agent.codex_provider import _split_system_and_convo
        from app.agent.tool_protocol import (
            NEXT_STEP_CUE,
            ToolCallDetector,
            build_tool_instructions,
            flatten_messages,
            thought_for_calls,
        )
        from app.core.app_settings import request_timeout_seconds

        instructions, inputs = _split_system_and_convo(messages)
        if tools:
            instructions = build_tool_instructions(tools) + "\n\n" + instructions
            if inputs:
                inputs[-1]["content"] += NEXT_STEP_CUE

        token, base = await auth.get_valid_token()
        base = (self._override_base or base).rstrip("/")
        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "input": inputs or [{"role": "user", "content": flatten_messages(messages)}],
            "stream": True,
            "store": False,
        }
        # GPT-5 / o-series are reasoning models: reasoning tokens count against the output
        # budget, and Copilot applies a low default cap when max_output_tokens is unset — so
        # reasoning can exhaust it and leave ZERO output text (empty completion). A caller that
        # needs a large structured result (e.g. architecture JSON, max_tokens=16000) must have
        # that budget honored, or the JSON never comes back.
        if max_tokens:
            payload["max_output_tokens"] = int(max_tokens)
        detector = ToolCallDetector(tools_enabled=bool(tools))
        completion_chars = 0
        current_event: str | None = None
        _timeout = httpx.Timeout(float(request_timeout_seconds()), connect=15.0)
        _first_tok = True

        yield StreamEvent(type="status", phase="connecting", text=f"Connecting to GitHub Copilot · {self._model}…")
        async with httpx.AsyncClient(timeout=_timeout) as client:
            for attempt in range(2):
                retry = False
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    **_EDITOR_COMPLETION_HEADERS,
                }
                async with client.stream("POST", f"{base}/responses", json=payload, headers=headers) as resp:
                    if resp.status_code == 401 and attempt == 0:
                        retry = True
                    elif resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise RuntimeError(
                            f"GitHub Copilot Responses API error {resp.status_code}: {body[:500]}"
                        )
                    else:
                        yield StreamEvent(type="status", phase="request_sent", text="Request sent · awaiting response…")
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
                                m = (evt.get("error") or {}).get("message") if isinstance(evt, dict) else None
                                raise RuntimeError(f"GitHub Copilot Responses error: {m or data[:200]}")
                            if not delta:
                                continue
                            if _first_tok:
                                _first_tok = False
                                yield StreamEvent(type="status", phase="response", text="Response received · generating…")
                            completion_chars += len(delta)
                            for tok in detector.feed(delta):
                                yield StreamEvent(type="token", text=tok)
                if retry:
                    refreshed = await auth.refresh_token()
                    if refreshed:
                        token = refreshed
                    continue
                break

        calls, leftover = detector.finish()
        if calls:
            thought = thought_for_calls(detector.buffer, calls)
            if thought:
                yield StreamEvent(type="token", text=thought)
            yield StreamEvent(type="tool_calls", tool_calls=calls)
        elif leftover:
            yield StreamEvent(type="token", text=leftover)
        yield StreamEvent(type="done", completion_tokens=max(1, completion_chars // 4))

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Device-flow (editor) tokens use the OpenAI-compatible completions API with
        # native tool calling; only the legacy browser-sniffed web token uses the thread
        # API + ReAct protocol below.
        if self._uses_editor_api():
            async for ev in self._stream_editor_api(messages, tools, max_tokens=max_tokens):
                yield ev
            return

        token, base_url = await self._resolve_token()

        # Teach the model the ReAct tool-calling protocol (Copilot has no native tools).
        prompt = _flatten_messages(messages)
        if tools:
            from app.agent.tool_protocol import NEXT_STEP_CUE

            # Budget the conversation so the catalog + big tool results don't trip the
            # Copilot 'contentTooLarge' limit; the catalog itself is always preserved.
            prompt = (
                _build_tool_instructions(tools)
                + "\n\n--- our conversation so far ---\n"
                + _trim_conversation(prompt)
                + NEXT_STEP_CUE
            )
        else:
            # No tools (e.g. a final synthesis call): still guard against an oversized
            # transcript of accumulated tool output.
            prompt = _trim_conversation(prompt, _MAX_CONVERSATION_CHARS + 12000)

        from app.core.app_settings import request_timeout_seconds

        _timeout = httpx.Timeout(float(request_timeout_seconds()), connect=15.0)
        async with httpx.AsyncClient(timeout=_timeout) as client:
            # Step 1: create a thread (force-refresh the token once on a 401).
            create_url = f"{base_url}/github/chat/threads"
            create_resp = await client.post(
                create_url, content="{}",
                headers={**self._auth_headers(token), "Content-Type": "application/json"},
            )
            if create_resp.status_code == 401 and not self._override_token:
                refreshed = await auth.refresh_token()
                if refreshed:
                    token = refreshed
                    create_resp = await client.post(
                        create_url, content="{}",
                        headers={**self._auth_headers(token), "Content-Type": "application/json"},
                    )
            create_resp.raise_for_status()
            thread_id = _extract_thread_id(create_resp.text)
            if not thread_id:
                raise RuntimeError("GitHub Copilot did not return a thread id.")

            # Step 1b: upload any attached images and reference them as mediaContent.
            # The Copilot thread API rejects inline data URLs, so images must be
            # uploaded first via the browser session (ported from the BuddyAI C# app).
            media_content: list[dict[str, Any]] = []
            for data_url in _latest_user_images(messages):
                try:
                    asset_url = await auth.upload_image(thread_id, data_url)
                    header = data_url.split(";base64,", 1)[0]
                    mime = header.split(":", 1)[1] if ":" in header else "image/png"
                    media_content.append(
                        {"type": "image", "mimeType": mime, "url": asset_url, "fileName": "image"}
                    )
                except Exception:  # noqa: BLE001 - continue without the image rather than fail
                    pass

            # Step 2: post the message and consume the SSE stream.
            messages_url = f"{base_url}/github/chat/threads/{thread_id}/messages"
            payload: dict[str, Any] = {
                "responseMessageID": str(uuid.uuid4()),
                "content": prompt,
                "intent": "conversation",
                "references": [],
                "context": [],
                "currentURL": "https://github.com/copilot",
                "streaming": True,
                "confirmations": [],
                "customInstructions": [],
                "model": self._model,
                "mode": "immersive",
                "parentMessageID": "root",
                "mediaContent": media_content,
                "skillOptions": {"deepCodeSearch": False},
                "requestTrace": False,
            }
            req_headers = {
                **self._auth_headers(token),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }

            # Tool-call detection: if the response begins with JSON (a tool directive),
            # buffer it silently and parse at the end; otherwise stream tokens through.
            tools_enabled = bool(tools)
            buffering = tools_enabled
            decided = not tools_enabled  # if no tools, never buffer
            buffer = ""
            completion_chars = 0

            async with client.stream(
                "POST", messages_url, json=payload, headers=req_headers
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(
                        f"GitHub Copilot API error {resp.status_code}: {body[:500]}"
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    # Surface Copilot stream errors instead of silently ending empty.
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        evt = None
                    if isinstance(evt, dict) and evt.get("type") == "error":
                        desc = evt.get("description") or evt.get("errorType") or "unknown error"
                        etype = evt.get("errorType", "")
                        if etype == "contentTooLarge":
                            raise RuntimeError(
                                "GitHub Copilot rejected the request as too large. The Azure "
                                "tool catalog is big — try a more specific question, or use an "
                                "OpenAI/Azure OpenAI provider for broad multi-tool investigations."
                            )
                        raise RuntimeError(f"GitHub Copilot error: {desc}")

                    parts: list[str] = []
                    if evt is not None:
                        _append_delta(evt, parts)
                    else:
                        parts.append(data)
                    text = "".join(parts)
                    if not text:
                        continue
                    completion_chars += len(text)

                    if not decided:
                        buffer += text
                        lead = buffer.lstrip()
                        if not lead:
                            continue  # only whitespace so far
                        # First non-whitespace char tells us if this is a tool directive.
                        if lead[0] in "{[" or lead.startswith("```"):
                            buffering = True  # keep buffering the JSON, emit nothing yet
                        else:
                            buffering = False
                            yield StreamEvent(type="token", text=buffer)
                            buffer = ""
                        decided = True
                        continue

                    if buffering:
                        buffer += text
                    else:
                        yield StreamEvent(type="token", text=text)

            # Stream finished. If we buffered, decide tool-call vs. plain text.
            if buffering and buffer.strip():
                calls = _parse_tool_calls(buffer)
                if calls:
                    # Surface the model's restated understanding + plan before the tool
                    # call so the user sees we understood the ask (falls back to the
                    # tool's intent when no explicit "thought" was provided).
                    from app.agent.tool_protocol import thought_for_calls

                    thought = thought_for_calls(buffer, calls)
                    if thought:
                        yield StreamEvent(type="token", text=thought)
                    yield StreamEvent(type="tool_calls", tool_calls=calls)
                else:
                    # Not a valid tool directive after all — emit the buffered text.
                    yield StreamEvent(type="token", text=buffer)

        # Rough token estimate (the thread API doesn't report usage).
        yield StreamEvent(type="done", completion_tokens=max(1, completion_chars // 4))


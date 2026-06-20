"""Agent orchestrator: the tool-calling loop.

Drives the LLM, executes read tools via MCP immediately, and routes write tools
through the approval gate. Emits a stream of typed events the API turns into SSE.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.agent.factory import build_provider_for
from app.agent.provider import ToolSpec
from app.connectors.base import ConnectorToolset
from app.core.config import Settings
from app.mcp.client import DiscoveredTool, MCPClient, build_mcp_client

# Fallback tool-iteration budget; the live value comes from the dashboard setting
# `max_tool_iterations` (see app_settings.agent_runtime_params).
MAX_TOOL_ITERATIONS = 16


def _is_blank_answer(text: str | None) -> bool:
    """True when an answer has no meaningful content — empty, whitespace, or only
    punctuation/braces left behind after stripping a malformed tool-call directive
    (e.g. a stray '}'). Used to decide whether to force a real final-answer turn."""
    if not text:
        return True
    alnum = sum(1 for ch in text if ch.isalnum())
    return alnum < 8


def _summarize_result(result: dict[str, Any]) -> str:
    """Produce a short, human-readable summary of a tool result for the live
    progress timeline (e.g. 'Found 5 items', 'Error: ...')."""
    if result.get("isError"):
        content = result.get("content") or []
        first = str(content[0]) if content else "unknown error"
        return f"Error: {first[:140]}"
    content = result.get("content") or []
    text = str(content[0]) if content else ""
    # Many Azure MCP tools return JSON like {"results": {"<key>": [ ... ]}}.
    try:
        parsed = json.loads(text)
        results = parsed.get("results") if isinstance(parsed, dict) else None
        if isinstance(results, dict):
            for key, val in results.items():
                if isinstance(val, list):
                    return f"Found {len(val)} {key}"
            return "Success"
        if isinstance(results, list):
            return f"Found {len(results)} items"
        return "Success"
    except (json.JSONDecodeError, AttributeError, TypeError):
        snippet = text.strip().replace("\n", " ")
        return (snippet[:120] + "…") if len(snippet) > 120 else (snippet or "Done")


@dataclass
class AgentEvent:
    type: str  # token | reasoning | tool_start | tool_result | approval_required | done | error
    data: dict[str, Any] = field(default_factory=dict)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        provider: str | None = None,
        model: str | None = None,
        connection: dict[str, Any] | None = None,
        connector_toolset: ConnectorToolset | None = None,
        extra_instructions: str | None = None,
        write_policy_override: str | None = None,
        entra_enabled: bool = False,
    ) -> None:
        self._settings = settings
        # Per-chat provider/model override (falls back to globally-active config).
        self._provider = build_provider_for(provider, model)
        # Optional Azure connection (tenant identity) bound to this turn's MCP session.
        self._mcp = build_mcp_client(settings, connection=connection)
        # Optional EntraID (Microsoft Graph) MCP server, authenticated with the same
        # connection's service-principal identity. Built only when enabled for this turn.
        self._entra = None
        self._entra_tool_names: set[str] = set()
        if entra_enabled:
            try:
                from app.mcp.client import build_entra_mcp_client

                self._entra = build_entra_mcp_client(settings, connection=connection)
            except Exception:  # noqa: BLE001 - EntraID is optional; never block the turn
                self._entra = None
        # Optional connector tools (Teams/Outlook/Jira/Grafana) merged into the loop.
        self._connectors = connector_toolset
        # Optional custom-agent instructions prepended to the system prompt.
        self._extra_instructions = extra_instructions
        # Optional write-policy override ('off' for autonomous custom agents).
        self._write_policy_override = write_policy_override

    def close(self) -> None:
        """Release the per-turn MCP client resources (e.g. a temp cert file)."""
        for client in (self._mcp, self._entra):
            if client is None:
                continue
            try:
                client.close()
            except Exception:  # noqa: BLE001 - cleanup must never raise
                pass

    async def _load_tools(self) -> tuple[list[DiscoveredTool], dict[str, DiscoveredTool]]:
        try:
            tools = await self._mcp.list_tools()
        except Exception:
            # MCP unavailable: agent still answers, just without live Azure tools.
            tools = []
        # Merge EntraID (Microsoft Graph) tools when enabled for this turn.
        if self._entra is not None:
            try:
                entra_tools = await self._entra.list_tools()
                self._entra_tool_names = {t.name for t in entra_tools}
                # On a name clash, Azure tools win; EntraID-only names are added.
                existing = {t.name for t in tools}
                tools = tools + [t for t in entra_tools if t.name not in existing]
            except Exception:  # noqa: BLE001 - EntraID optional; proceed without it
                self._entra_tool_names = set()
        return tools, {t.name: t for t in tools}

    async def run(
        self,
        history: list[dict[str, Any]],
        scope_hint: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one assistant turn. `history` is prior messages (user/assistant/tool)
        in OpenAI message format, without the system prompt. `scope_hint`, when set,
        is an extra system instruction constraining which subscription(s) to use."""
        # Surface tool-loading as the first measured milestone — the initial message pays
        # the MCP cold-start (npx @azure/mcp spawn), which is the biggest "stuck" window.
        yield AgentEvent(type="status", data={"phase": "tools", "message": "Loading Azure tools…"})
        tools, tool_index = await self._load_tools()
        tool_specs = MCPClient.to_tool_specs(tools)
        _azure_n = len(tool_specs)
        _graph_n = len(self._entra_tool_names) if self._entra is not None else 0
        _ready = f"Ready — {_azure_n} Azure" + (f" + {_graph_n} Graph" if _graph_n else "") + " tool(s)"
        yield AgentEvent(type="status", data={"phase": "tools_ready", "message": _ready})
        # Merge in connector tools (Teams/Outlook/Jira/Grafana), if any.
        if self._connectors is not None:
            for spec in self._connectors.specs():
                tool_specs.append(
                    ToolSpec(
                        name=spec["name"],
                        description=spec["description"],
                        parameters=spec["parameters"],
                    )
                )

        from app.core.app_settings import effective_write_policy, system_prompt_additions
        from app.core.ai_prompts import get_full_prompt

        base_system = get_full_prompt("chat_system_prompt")
        system_text = base_system
        extra = system_prompt_additions()
        if extra:
            system_text = f"{base_system}\n\n{extra}"
        # Custom-agent instructions, when running as a scheduled task / custom agent.
        if self._extra_instructions:
            system_text = f"{system_text}\n\n{self._extra_instructions}"

        # Resolve the write policy: an explicit override (autonomous agent) wins, else
        # the runtime dashboard setting. 'off' => writes auto-execute; 'gated' => pause.
        write_policy_mode = self._write_policy_override or effective_write_policy(
            self._settings.agent_write_policy
        )

        # Tell the model how write actions are handled under the current policy so it
        # behaves correctly (it must not keep "awaiting approval" when writes auto-run).
        if write_policy_mode == "off":
            write_policy = (
                "WRITE POLICY: Mutating/write tools execute IMMEDIATELY when you call "
                "them — there is NO separate human-approval step and NO interactive "
                "dialog/accept-reject prompt of any kind. Never say you are 'awaiting "
                "approval' or 'waiting for a response to a dialog'. If the user has asked "
                "you to make a change, call the write tool directly and report the actual "
                "result the tool returns."
            )
        else:
            write_policy = (
                "WRITE POLICY: Mutating/write tools are GATED — calling one pauses for "
                "explicit human approval before it runs. After you request a write, it "
                "will report 'awaiting_approval' until a human approves it. Do not claim "
                "success until the tool actually returns a success result."
            )
        system_text = f"{system_text}\n\n{write_policy}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
        ]
        if scope_hint:
            messages.append({"role": "system", "content": scope_hint})
        messages.extend(history)

        total_prompt = 0
        total_completion = 0

        # Advanced tuning knobs (dashboard-configurable; read per turn).
        from app.core.app_settings import agent_runtime_params

        _rt = agent_runtime_params()
        max_iterations = _rt["max_tool_iterations"]
        data_result_cap = _rt["tool_result_limit"]
        discovery_result_cap = _rt["tool_discovery_limit"]

        for _iter in range(max_iterations):
            assistant_text = ""
            pending_calls = []

            # On follow-up rounds (after a tool result), tell the user we're going back to
            # the model with the new evidence — otherwise there's another silent gap.
            if _iter > 0:
                yield AgentEvent(type="status", data={"phase": "iterating", "message": "Sending results back to the model…"})

            async for ev in self._provider.stream(messages, tool_specs):
                if ev.type == "token":
                    assistant_text += ev.text
                    yield AgentEvent(type="token", data={"text": ev.text})
                elif ev.type == "status":
                    yield AgentEvent(type="status", data={"phase": ev.phase, "message": ev.text})
                elif ev.type == "tool_calls":
                    pending_calls = ev.tool_calls
                elif ev.type == "done":
                    total_prompt += ev.prompt_tokens
                    total_completion += ev.completion_tokens

            if not pending_calls:
                # Defense-in-depth: the streaming detector should surface tool calls,
                # but if a provider streamed a directive as prose (e.g. a preamble before
                # the JSON), recover it here so the tool actually runs instead of the
                # model hallucinating an answer with no data. Only when tools are enabled.
                if tool_specs:
                    from app.agent.tool_protocol import parse_tool_calls

                    recovered = parse_tool_calls(assistant_text)
                    if recovered:
                        pending_calls = recovered
                        # Keep only the prose BEFORE the directive as the message content
                        # (the directive itself is carried structurally as tool_calls).
                        cut = min(
                            [i for i in (assistant_text.find("{"), assistant_text.find("[")) if i != -1]
                            or [len(assistant_text)]
                        )
                        assistant_text = assistant_text[:cut]
                        # Fall through to the tool-execution path below.

            if not pending_calls:
                # Plain assistant answer; we're done. Strip any ReAct protocol leakage
                # (echoed tool-call JSON / "Tool result:" lines) and the pre-work
                # understanding/plan preamble (that belongs in the thinking panel).
                from app.agent.tool_protocol import (
                    strip_plan_preamble,
                    strip_react_artifacts,
                )

                final = strip_plan_preamble(strip_react_artifacts(assistant_text))
                # Safety net: the model sometimes emits a *malformed* text tool-call
                # (e.g. a multi-line query that won't parse) as its whole turn. Stripping
                # it leaves an empty (or punctuation-only, like a stray "}") answer —
                # which surfaced as a blank chat thread. When that happens (we had raw
                # text but nothing meaningful survived stripping), force one more no-tool
                # turn so the user always gets a real answer.
                if _is_blank_answer(final) and assistant_text.strip():
                    forced = ""
                    nudge = messages + [
                        {"role": "assistant", "content": assistant_text},
                        {
                            "role": "user",
                            "content": (
                                "Write your final answer now in plain prose. Do NOT emit "
                                "a tool-call directive or JSON — summarize what you found "
                                "and the recommended next steps."
                            ),
                        },
                    ]
                    async for ev2 in self._provider.stream(nudge, None):
                        if ev2.type == "token":
                            forced += ev2.text
                            yield AgentEvent(type="token", data={"text": ev2.text})
                        elif ev2.type == "done":
                            total_prompt += ev2.prompt_tokens
                            total_completion += ev2.completion_tokens
                    forced_final = strip_plan_preamble(strip_react_artifacts(forced))
                    if not _is_blank_answer(forced_final):
                        final = forced_final
                yield AgentEvent(
                    type="done",
                    data={
                        "content": final,
                        "prompt_tokens": total_prompt,
                        "completion_tokens": total_completion,
                    },
                )
                return

            # Record the assistant message carrying the tool calls.
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": json.dumps(c.arguments),
                            },
                        }
                        for c in pending_calls
                    ],
                }
            )

            for call in pending_calls:
                tool = tool_index.get(call.name)
                is_connector = self._connectors is not None and self._connectors.has(call.name)
                # Refine the coarse name-based kind by inspecting the call's command/
                # intent argument, so namespace tools (sql, role, …) only gate on actual
                # writes and reads run freely.
                from app.mcp.client import classify_call

                if is_connector:
                    kind = self._connectors.kind(call.name) if self._connectors else "write"
                else:
                    kind = classify_call(call.name, call.arguments)
                    if tool is not None and tool.kind == "read":
                        # Read-only server guarantees every tool is read; trust that.
                        kind = "read"

                if kind == "write" and write_policy_mode != "off":
                    # Gated: do not execute. Surface an approval requirement.
                    yield AgentEvent(
                        type="approval_required",
                        data={
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "arguments": call.arguments,
                        },
                    )
                    # Feed a tool result back so the model knows it must wait.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(
                                {
                                    "status": "awaiting_approval",
                                    "message": (
                                        "This is a mutating action and requires human "
                                        "approval before it can run."
                                    ),
                                }
                            ),
                        }
                    )
                    continue

                # Read tool: execute immediately.
                yield AgentEvent(
                    type="tool_start",
                    data={"tool_name": call.name, "arguments": call.arguments},
                )
                started = time.perf_counter()
                try:
                    if is_connector and self._connectors is not None:
                        result = await self._connectors.call(call.name, call.arguments)
                    elif call.name in self._entra_tool_names and self._entra is not None:
                        result = await self._entra.call_tool(call.name, call.arguments)
                    else:
                        result = await self._mcp.call_tool(call.name, call.arguments)
                except Exception as exc:  # surface tool failures to the model
                    result = {"isError": True, "content": [str(exc)]}
                duration_ms = int((time.perf_counter() - started) * 1000)

                yield AgentEvent(
                    type="tool_result",
                    data={
                        "tool_name": call.name,
                        "result": result,
                        "summary": _summarize_result(result),
                        "duration_ms": duration_ms,
                        "is_error": bool(result.get("isError")),
                    },
                )
                # Feed the tool result back to the model. Discovery ("learn") outputs
                # list a service's sub-commands and are large (30-50KB). We compact them
                # to command name + description + parameter NAMES (dropping the verbose
                # per-parameter schemas) — keeping everything the model needs to act
                # while cutting ~85% so the transcript stays within request-size limits
                # (critical for GitHub Copilot's small thread-API budget). Normal data
                # results keep a tighter char bound to protect the context budget.
                is_learn = bool((call.arguments or {}).get("learn"))
                if is_learn:
                    from app.agent.tool_protocol import compact_learn_result

                    result_for_model = compact_learn_result(result)
                    result_cap = discovery_result_cap
                else:
                    result_for_model = result
                    result_cap = data_result_cap
                # Defense-in-depth against prompt injection: scrub the highest-signal
                # "system: ignore previous instructions" / fake role-header markers
                # out of every string inside the tool result before it lands in the
                # model context. See app.agent.result_sanitizer for the rules. The
                # approval gate still protects writes; this just lowers the false-
                # positive surface area.
                from app.agent.result_sanitizer import sanitize_tool_result

                result_for_model = sanitize_tool_result(result_for_model)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result_for_model)[:result_cap],
                    }
                )

        # Tool-iteration budget exhausted while still calling tools. Force a final
        # answer: make one more model call WITHOUT tools so it must summarize the
        # evidence it already gathered instead of returning an empty response.
        forced_text = ""
        final_messages = messages + [
            {
                "role": "system",
                "content": (
                    "You have reached the tool-call limit for this turn. Do NOT call "
                    "any more tools. Using the evidence already gathered above, write "
                    "your best answer now: summarize findings, the most likely cause, "
                    "and concrete next steps. If the investigation is incomplete, say "
                    "what you found so far and what to check next."
                ),
            }
        ]
        async for ev in self._provider.stream(final_messages, None):
            if ev.type == "token":
                forced_text += ev.text
                yield AgentEvent(type="token", data={"text": ev.text})
            elif ev.type == "done":
                total_prompt += ev.prompt_tokens
                total_completion += ev.completion_tokens

        from app.agent.tool_protocol import (
            strip_plan_preamble as _spp,
            strip_react_artifacts as _sra,
        )

        forced_clean = _spp(_sra(forced_text))
        final_content = (
            forced_clean
            if not _is_blank_answer(forced_clean)
            else (
                "I gathered evidence with several tool calls but hit the tool-call "
                "limit before finishing. Please re-run or narrow the request to "
                "complete the analysis."
            )
        )
        yield AgentEvent(
            type="done",
            data={
                "content": final_content,
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "note": "max tool iterations reached",
            },
        )

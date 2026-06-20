"""Deep investigation: a structured, multi-phase root-cause methodology.

Mirrors Azure SRE Agent's "deep investigation" mode. Instead of a single
tool-calling turn, the agent runs four phases:

  1. Incident research  — gather context (inventory, health, metrics, logs, changes).
  2. Forming hypotheses — propose 2-4 candidate root causes from the research.
  3. Validating         — test each hypothesis with evidence; validated ones at shallow
                          depth can spawn sub-hypotheses (up to 3 levels) -> a tree.
  4. Conclusion         — synthesize the root cause, evidence, and recommended actions.

It reuses the provider + MCP client + connector toolset, but runs a *read-only* tool
loop (investigations never mutate). Progress is streamed as typed ``AgentEvent``s the
API forwards over SSE; the frontend renders an interactive hypothesis tree.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.agent.factory import build_provider_for
from app.agent.orchestrator import AgentEvent, _summarize_result
from app.agent.provider import ToolSpec
from app.connectors.base import ConnectorToolset
from app.core.ai_prompts import get_full_prompt, get_guidance
from app.core.config import Settings
from app.mcp.client import MCPClient, build_mcp_client

# Budgets (kept modest so a run finishes in a few minutes).
MAX_HYPOTHESES = 3
MIN_HYPOTHESES = 2
MAX_DEPTH = 2  # hypothesis -> sub (2 levels; deep enough, far fewer slow completions)
MAX_SUBHYPOTHESES = 1
MAX_TOTAL_NODES = 5  # hard cap on hypotheses validated across the whole tree
RESEARCH_ITERS = 5
VALIDATE_ITERS = 3
# Wall-clock budget (seconds) for the research phase so it can't run for minutes on
# simple questions. Once exceeded, the loop forces an immediate summary and moves on
# to forming hypotheses. The validation phase parallelism setting does NOT affect this.
RESEARCH_BUDGET_S = 55.0
VALIDATE_BUDGET_S = 40.0
# Max chars of the research summary fed into later phases (smaller prompts = faster
# completions on slow reasoning models).
RESEARCH_SUMMARY_CAP = 1500

# --- Admin-editable phase guidance ------------------------------------------
# Only the human-tunable prose lives here; each phase's strict JSON output contract
# is assembled in code (it carries dynamic limits) and appended automatically, so
# edits to these can't break parsing. Mirrored into the AI Prompts registry.
DEEP_RESEARCH_GUIDANCE = (
    "You are in the RESEARCH phase of a deep investigation. Use read-only "
    "tools to gather the context needed to diagnose the problem: enumerate the "
    "relevant resources, check resource health/status, recent changes/deployments, "
    "metrics, and logs. Do NOT make any changes. BE EFFICIENT: prefer a few "
    "targeted tool calls over broad enumeration, and STOP as soon as you have "
    "enough to reason about likely causes — for simple or narrowly-scoped "
    "questions one or two tool calls is often enough. Do not keep gathering data "
    "once the picture is clear."
)
DEEP_VALIDATION_GUIDANCE = (
    "You are VALIDATING one hypothesis in a deep investigation. Use "
    "read-only tools to find concrete evidence that confirms or rules out the "
    "hypothesis. Do not make changes."
)
DEEP_CONCLUSION_GUIDANCE = (
    "The deep investigation is complete. Write the final answer for the user in "
    "Markdown. Lead with the root cause, then supporting evidence (a short table "
    "or bullets), then a 'Recommended actions' list, and finish with a 'Next "
    "steps' section of 2-4 follow-ups the user can ask you to take. Be decisive "
    "and concise. Do not call any tools."
)


def _node_id() -> str:
    return uuid.uuid4().hex[:10]


class DeepInvestigator:
    """Runs a structured deep investigation, emitting AgentEvents as it progresses."""

    def __init__(
        self,
        settings: Settings,
        provider: str | None = None,
        model: str | None = None,
        connection: dict[str, Any] | None = None,
        connector_toolset: ConnectorToolset | None = None,
        focus: list[str] | None = None,
        architecture_memory: str | None = None,
    ) -> None:
        self._settings = settings
        self._provider = build_provider_for(provider, model)
        # Optional architecture "memory" (intended design, security model, known gaps,
        # diagnostic hints) injected into every phase's system prompt as expert context.
        self._architecture_memory = (architecture_memory or "").strip()
        self._mcp = build_mcp_client(settings, connection=connection)
        self._connectors = connector_toolset
        self._tool_specs: list[ToolSpec] = []
        self._nodes_validated = 0
        # Specialist agents the user picked for the war room (resolved catalog entries).
        from app.agent.deep_agents import get_agents

        self._focus = get_agents(focus or [])
        # How many hypothesis sub-agents may validate at once (from dashboard setting).
        try:
            from app.core.app_settings import deep_parallelism

            self._parallel = deep_parallelism()
        except Exception:  # noqa: BLE001 - default to sequential if unavailable
            self._parallel = 1

    def close(self) -> None:
        try:
            self._mcp.close()
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass

    async def _load_tools(self) -> None:
        try:
            tools = await self._mcp.list_tools()
        except Exception:  # noqa: BLE001 - MCP optional; investigation degrades gracefully
            tools = []
        specs = MCPClient.to_tool_specs(tools)
        if self._connectors is not None:
            for spec in self._connectors.specs():
                specs.append(
                    ToolSpec(
                        name=spec["name"],
                        description=spec["description"],
                        parameters=spec["parameters"],
                    )
                )
        self._tool_specs = specs

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._connectors is not None and self._connectors.has(name):
            # Investigations are read-only; skip connector write tools entirely.
            if self._connectors.kind(name) == "write":
                return {"isError": True, "content": ["Skipped: write tools are not used during investigation."]}
            return await self._connectors.call(name, arguments)
        return await self._mcp.call_tool(name, arguments)

    # ----------------------------------------------------------------- helpers
    async def _merge_event_streams(
        self,
        factories: list[Callable[[], AsyncIterator[AgentEvent]]],
        limit: int,
    ) -> AsyncIterator[AgentEvent]:
        """Run several event-stream factories concurrently (up to ``limit`` at once),
        interleaving their events into one stream. Used so multiple hypothesis
        sub-agents validate in parallel and their tool/status events stream live.

        Falls back to simple sequential iteration when limit<=1 or a single factory."""
        if limit <= 1 or len(factories) <= 1:
            for factory in factories:
                async for ev in factory():
                    yield ev
            return

        queue: asyncio.Queue[Any] = asyncio.Queue()
        sem = asyncio.Semaphore(limit)
        DONE = object()

        async def worker(factory: Callable[[], AsyncIterator[AgentEvent]]) -> None:
            async with sem:
                try:
                    async for ev in factory():
                        await queue.put(ev)
                except Exception as exc:  # noqa: BLE001 - surface, don't crash the run
                    from app.core.utils import format_error

                    await queue.put(
                        AgentEvent(type="tool_result", data={
                            "tool_name": "sub-agent",
                            "summary": f"Error: {format_error(exc)}",
                            "duration_ms": 0,
                            "is_error": True,
                        })
                    )
                finally:
                    await queue.put(DONE)

        tasks = [asyncio.create_task(worker(f)) for f in factories]
        remaining = len(tasks)
        try:
            while remaining > 0:
                item = await queue.get()
                if item is DONE:
                    remaining -= 1
                else:
                    yield item
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _complete(self, system: str, user: str) -> str:
        """One no-tool LLM completion; returns the full text."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        text = ""
        async for ev in self._provider.stream(messages, None):
            if ev.type == "token":
                text += ev.text
        return text

    async def _tool_loop(
        self,
        system: str,
        user: str,
        max_iters: int,
        result: dict[str, Any],
        *,
        stream_answer: bool = False,
        budget_s: float | None = None,
        tag: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """A bounded READ-ONLY tool-calling loop. Yields feed events (token/tool_start/
        tool_result). Writes the final assistant text and collected evidence into
        ``result`` (keys: ``text``, ``evidence``, ``prompt_tokens``, ``completion_tokens``).
        When ``stream_answer`` is True, token events are forwarded (for the final answer);
        otherwise tokens are accumulated silently (intermediate reasoning).
        When ``budget_s`` is set, the loop stops starting new tool iterations once that
        wall-clock budget is exceeded and forces an immediate summary instead."""
        from app.core.app_settings import agent_runtime_params

        rt = agent_runtime_params()
        data_cap = rt["tool_result_limit"]
        discovery_cap = rt["tool_discovery_limit"]
        started_loop = time.perf_counter()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        evidence: list[dict[str, Any]] = []
        text = ""
        p_tok = 0
        c_tok = 0

        for _ in range(max_iters):
            text = ""
            pending = []
            async for ev in self._provider.stream(messages, self._tool_specs):
                if ev.type == "token":
                    text += ev.text
                    if stream_answer:
                        yield AgentEvent(type="token", data={"text": ev.text})
                elif ev.type == "tool_calls":
                    pending = ev.tool_calls
                elif ev.type == "done":
                    p_tok += ev.prompt_tokens
                    c_tok += ev.completion_tokens

            if not pending:
                result["text"] = text
                result["evidence"] = evidence
                result["prompt_tokens"] = p_tok
                result["completion_tokens"] = c_tok
                return

            # Wall-clock budget guard: if we've already spent the allotted time, stop
            # starting another (potentially slow) tool round and force a summary below.
            if budget_s is not None and (time.perf_counter() - started_loop) >= budget_s:
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in pending
                    ],
                }
            )
            for call in pending:
                yield AgentEvent(
                    type="tool_start",
                    data={"tool_name": call.name, "arguments": call.arguments, **(tag or {})},
                )
                started = time.perf_counter()
                try:
                    res = await self._call_tool(call.name, call.arguments)
                except Exception as exc:  # noqa: BLE001 - surface to the model
                    res = {"isError": True, "content": [str(exc)]}
                duration_ms = int((time.perf_counter() - started) * 1000)
                summary = _summarize_result(res)
                yield AgentEvent(
                    type="tool_result",
                    data={
                        "tool_name": call.name,
                        "result": res,
                        "summary": summary,
                        "duration_ms": duration_ms,
                        **(tag or {}),
                    },
                )
                evidence.append({"tool": call.name, "summary": summary})
                is_learn = bool((call.arguments or {}).get("learn"))
                if is_learn:
                    from app.agent.tool_protocol import compact_learn_result

                    res_for_model = compact_learn_result(res)
                    cap = discovery_cap
                else:
                    res_for_model = res
                    cap = data_cap
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(res_for_model)[:cap],
                    }
                )

        # Budget exhausted while still calling tools: force a final no-tool summary.
        messages.append(
            {
                "role": "user",
                "content": (
                    "Stop investigating now. Produce your final result for what you have "
                    "found so far, in the exact output format specified in the system "
                    "message."
                ),
            }
        )
        forced = ""
        async for ev in self._provider.stream(messages, None):
            if ev.type == "token":
                forced += ev.text
                if stream_answer:
                    yield AgentEvent(type="token", data={"text": ev.text})
            elif ev.type == "done":
                p_tok += ev.prompt_tokens
                c_tok += ev.completion_tokens
        result["text"] = forced or text
        result["evidence"] = evidence
        result["prompt_tokens"] = p_tok
        result["completion_tokens"] = c_tok

    def _coerce_agent(self, value: Any) -> str:
        """Map a model-proposed agent id onto a focus agent id (or the first focus agent)."""
        if not self._focus:
            return ""
        valid = {a["id"] for a in self._focus}
        v = str(value or "").strip().lower()
        if v in valid:
            return v
        return self._focus[0]["id"]

    @staticmethod
    def _parse_json(text: str) -> Any:
        """Best-effort JSON extraction from an LLM response (handles code fences)."""
        from app.core.utils import safe_json_parse

        t = text.strip()
        if "```" in t:
            # Pull the first fenced block.
            import re

            m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
            if m:
                t = m.group(1).strip()
        # Fall back to the first {...} or [...] span.
        if not (t.startswith("{") or t.startswith("[")):
            import re

            m = re.search(r"(\[.*\]|\{.*\})", t, re.DOTALL)
            if m:
                t = m.group(1)
        return safe_json_parse(t, default=None)

    # -------------------------------------------------------------------- run
    async def run(
        self,
        history: list[dict[str, Any]],
        scope_hint: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        await self._load_tools()

        # The user's question is the last user message in history.
        question = ""
        for m in reversed(history):
            if m.get("role") == "user":
                c = m.get("content")
                question = c if isinstance(c, str) else _text_of(c)
                break

        base_system = get_full_prompt("chat_system_prompt")
        if scope_hint:
            base_system = f"{base_system}\n\n{scope_hint}"
        # Inject the architecture's memory (intended design, security model, known gaps,
        # diagnostic hints) as authoritative context for hypotheses + triage.
        if self._architecture_memory:
            base_system = (
                f"{base_system}\n\n## Architecture knowledge base (operator-authored)\n"
                "Treat the following as known facts about the intended design of the system "
                "under investigation. Use it to focus hypotheses, compare intended vs. actual "
                "state, and prioritize the documented known gaps and diagnostic hints.\n\n"
                f"{self._architecture_memory}"
            )

        total_p = 0
        total_c = 0

        # Announce the specialist agent roster up-front so the war room can render it.
        if self._focus:
            yield AgentEvent(type="agents", data={"agents": self._focus})

        # ---- Phase 1: Incident research ------------------------------------
        yield AgentEvent(
            type="phase",
            data={"phase": "research", "label": "Incident research", "summary": None},
        )
        # When the user picked specialist agents, ask the model to attribute each
        # hypothesis to one of them so the war room can route findings to the right card.
        agent_clause = ""
        if self._focus:
            ids = ", ".join(a["id"] for a in self._focus)
            agent_clause = (
                f' Also add an "agent" field to each hypothesis whose value is one of '
                f"these specialist ids: [{ids}] — pick the single most relevant one."
            )
        research_sys = (
            base_system
            + "\n\n"
            + get_guidance("deep_research_guidance")
            + "\n\nWhen you have gathered enough, STOP calling tools and output ONLY a JSON "
            'object (no prose, no code fence): {"summary": "<=120 word brief of the key '
            'facts and anomalies>", "hypotheses": [{"title": "<10 words", "description": '
            '"one or two sentences"}]}. Provide '
            f"{MIN_HYPOTHESES}-{MAX_HYPOTHESES} distinct root-cause hypotheses, most "
            "likely first." + agent_clause + " This single JSON ends the research phase."
        )
        research_user = (
            f"Investigate this problem and gather diagnostic context:\n\n{question}"
        )
        r: dict[str, Any] = {}
        async for ev in self._tool_loop(
            research_sys, research_user, RESEARCH_ITERS, r, budget_s=RESEARCH_BUDGET_S
        ):
            yield ev
        total_p += r.get("prompt_tokens", 0)
        total_c += r.get("completion_tokens", 0)

        # The research turn closes by emitting {summary, hypotheses} as one JSON object,
        # so we form hypotheses WITHOUT a second slow model call. Parse it; fall back to
        # treating the text as a plain summary (+ a separate hypotheses call) if needed.
        research_payload = self._parse_json(r.get("text") or "")
        research_summary = ""
        roots: list[dict[str, Any]] = []
        if isinstance(research_payload, dict):
            research_summary = str(research_payload.get("summary", "")).strip()[:RESEARCH_SUMMARY_CAP]
            for h in (research_payload.get("hypotheses") or [])[:MAX_HYPOTHESES]:
                if isinstance(h, dict) and h.get("title"):
                    roots.append({
                        "title": str(h["title"])[:120],
                        "description": str(h.get("description", ""))[:400],
                        "agent": self._coerce_agent(h.get("agent")),
                    })
        if not research_summary:
            research_summary = (r.get("text") or "").strip()[:RESEARCH_SUMMARY_CAP]

        yield AgentEvent(
            type="phase",
            data={"phase": "research", "label": "Incident research", "summary": research_summary[:1200]},
        )

        # ---- Phase 2: Form hypotheses --------------------------------------
        yield AgentEvent(
            type="phase",
            data={"phase": "hypotheses", "label": "Forming hypotheses", "summary": None},
        )
        if not roots:
            # Fallback: research didn't return usable hypotheses inline — do a dedicated
            # (slower) hypotheses call so the investigation still proceeds.
            hypo_sys = (
                "You form root-cause hypotheses for an Azure incident investigation. "
                f"Given the research findings, propose between {MIN_HYPOTHESES} and {MAX_HYPOTHESES} "
                "distinct, plausible root-cause hypotheses, ordered most-likely first. "
                'Respond with ONLY a JSON array: [{"title": "...", "description": "..."}]. '
                "Keep each title under 10 words and each description to one or two sentences."
            )
            hypo_user = (
                f"Problem:\n{question}\n\nResearch findings:\n{research_summary or '(limited data gathered)'}"
            )
            raw = await self._complete(hypo_sys, hypo_user)
            parsed = self._parse_json(raw)
            hypotheses = parsed if isinstance(parsed, list) else []
            for h in hypotheses[:MAX_HYPOTHESES]:
                if isinstance(h, dict) and h.get("title"):
                    roots.append({
                        "title": str(h["title"])[:120],
                        "description": str(h.get("description", ""))[:400],
                        "agent": self._coerce_agent(h.get("agent")),
                    })
        if not roots:
            roots = [{"title": "Root cause unclear from available data", "description": "Insufficient evidence was gathered to form specific hypotheses.", "agent": self._coerce_agent(None)}]

        # Emit hypothesis cards (validating) so the tree appears immediately.
        validated_nodes: list[dict[str, Any]] = []
        async for ev in self._validate_tree(roots, None, 1, base_system, question, research_summary, validated_nodes):
            yield ev

        # ---- Phase 4: Conclusion + final answer (single streamed call) -----
        # On a slow reasoning model, a separate conclusion-JSON call and a separate
        # answer call cost ~two full completions back-to-back. We merge them: the model
        # streams the user-facing Markdown answer, then emits a sentinel followed by a
        # compact JSON object for the conclusion card. We forward only the prose to the
        # chat and parse the trailing JSON for the structured conclusion.
        yield AgentEvent(
            type="phase",
            data={"phase": "conclusion", "label": "Conclusion", "summary": None},
        )
        tree_text = _tree_to_text(validated_nodes)
        sentinel = "===INVESTIGATION_JSON==="
        final_sys = (
            base_system
            + "\n\n"
            + get_guidance("deep_conclusion_guidance")
            + f"\n\nAfter the answer, output a line containing ONLY {sentinel} and then a "
            'JSON object: {"root_cause": "...", "summary": "...", "severity": "...", '
            '"evidence": ["...", "..."], "actions": ["...", "..."]}. root_cause is the '
            "single most likely cause (or 'Inconclusive' if none validated). severity is "
            "one of info|warning|error|critical reflecting how serious/urgent the finding "
            "is for the user's Azure environment. Keep the JSON small."
        )
        final_user = (
            f"Problem:\n{question}\n\nResearch summary:\n{research_summary}\n\n"
            f"Hypothesis tree results:\n{tree_text}"
        )

        full = ""
        emitted = 0
        idx = -1
        async for ev in self._provider.stream(
            [
                {"role": "system", "content": final_sys},
                {"role": "user", "content": final_user},
            ],
            None,
        ):
            if ev.type == "token":
                full += ev.text
                if idx == -1:
                    idx = full.find(sentinel)
                if idx != -1:
                    if idx > emitted:
                        yield AgentEvent(type="token", data={"text": full[emitted:idx]})
                        emitted = idx
                else:
                    # Hold back the last (len(sentinel)-1) chars so a sentinel split
                    # across chunks is never streamed to the user.
                    safe = len(full) - (len(sentinel) - 1)
                    if safe > emitted:
                        yield AgentEvent(type="token", data={"text": full[emitted:safe]})
                        emitted = safe
            elif ev.type == "done":
                total_p += ev.prompt_tokens
                total_c += ev.completion_tokens

        if idx == -1:
            # No sentinel emitted: the whole thing is the answer; flush the remainder.
            if len(full) > emitted:
                yield AgentEvent(type="token", data={"text": full[emitted:]})
            answer = full.strip()
            concl = None
        else:
            answer = full[:idx].strip()
            concl = self._parse_json(full[idx + len(sentinel):])

        if not isinstance(concl, dict):
            # Fall back to deriving a conclusion from the validated tree.
            best = next((n for n in validated_nodes if n.get("status") == "validated"), None)
            concl = {
                "root_cause": (best or {}).get("title", "Inconclusive"),
                "summary": "",
                "severity": "warning" if best else "info",
                "evidence": [],
                "actions": [],
            }
        _sev = str(concl.get("severity", "")).strip().lower()
        if _sev not in ("info", "warning", "error", "critical"):
            _sev = "info"
        conclusion = {
            "root_cause": str(concl.get("root_cause", "Inconclusive"))[:300],
            "summary": str(concl.get("summary", ""))[:1500],
            "severity": _sev,
            "evidence": [str(x)[:300] for x in (concl.get("evidence") or [])][:8],
            "actions": [str(x)[:300] for x in (concl.get("actions") or [])][:6],
        }
        yield AgentEvent(type="conclusion", data=conclusion)

        yield AgentEvent(
            type="done",
            data={
                "content": answer.strip(),
                "prompt_tokens": total_p,
                "completion_tokens": total_c,
                "investigation": {
                    "research": research_summary[:1200],
                    "hypotheses": validated_nodes,
                    "conclusion": conclusion,
                },
            },
        )

    async def _validate_tree(
        self,
        candidates: list[dict[str, Any]],
        parent_id: str | None,
        depth: int,
        base_system: str,
        question: str,
        research_summary: str,
        out_nodes: list[dict[str, Any]],
    ) -> AsyncIterator[AgentEvent]:
        """Emit + validate a level of hypotheses (in parallel up to ``self._parallel``),
        then recurse into sub-hypotheses for validated ones (up to MAX_DEPTH). Appends
        finished node dicts to ``out_nodes``."""
        # Announce all candidates at this level up-front (status: validating). Reserve
        # the budget here so parallel validators don't overshoot MAX_TOTAL_NODES.
        nodes: list[dict[str, Any]] = []
        for cand in candidates:
            if self._nodes_validated >= MAX_TOTAL_NODES:
                break
            self._nodes_validated += 1
            node = {
                "id": _node_id(),
                "parent_id": parent_id,
                "title": cand["title"],
                "description": cand.get("description", ""),
                "depth": depth,
                "status": "validating",
                "evidence": "",
                "agent": cand.get("agent", ""),
            }
            nodes.append(node)
            yield AgentEvent(type="hypothesis", data=dict(node))

        if not nodes:
            return

        if depth == 1:
            yield AgentEvent(
                type="phase",
                data={"phase": "validation", "label": "Validating hypotheses", "summary": None},
            )

        # Validate this level's nodes concurrently (sub-agents run in parallel and their
        # tool/status events interleave into the live feed).
        subs_by_node: dict[str, list[dict[str, Any]]] = {}
        factories = [
            (lambda n=node: self._validate_one(n, base_system, question, research_summary, out_nodes, subs_by_node))
            for node in nodes
        ]
        async for ev in self._merge_event_streams(factories, self._parallel):
            yield ev

        # Recurse into validated nodes that proposed sub-hypotheses (also in parallel).
        recurse_targets = [
            n
            for n in nodes
            if n["status"] == "validated" and subs_by_node.get(n["id"]) and depth < MAX_DEPTH
        ]
        if recurse_targets and self._nodes_validated < MAX_TOTAL_NODES:
            rec_factories = [
                (
                    lambda n=node: self._validate_tree(
                        subs_by_node[n["id"]], n["id"], depth + 1, base_system, question, research_summary, out_nodes
                    )
                )
                for node in recurse_targets
            ]
            async for ev in self._merge_event_streams(rec_factories, self._parallel):
                yield ev

    async def _validate_one(
        self,
        node: dict[str, Any],
        base_system: str,
        question: str,
        research_summary: str,
        out_nodes: list[dict[str, Any]],
        subs_by_node: dict[str, list[dict[str, Any]]],
    ) -> AsyncIterator[AgentEvent]:
        """Validate a single hypothesis with read-only tools, emit its status, and
        record any sub-hypotheses it proposes. Yields the validator's feed events."""
        val_sys = (
            base_system
            + "\n\n"
            + get_guidance("deep_validation_guidance")
            + "\n\nWhen done, output ONLY JSON: "
            '{"verdict": "validated|invalidated|inconclusive", "evidence": "one '
            'paragraph citing the specific evidence", "subhypotheses": [{"title": '
            '"...", "description": "..."}]}. Only include subhypotheses (at most '
            f"{MAX_SUBHYPOTHESES}) when the hypothesis is validated and a more specific "
            "underlying cause is worth drilling into; otherwise use an empty list."
        )
        val_user = (
            f"Problem:\n{question}\n\nResearch context:\n{research_summary}\n\n"
            f"Hypothesis to test:\nTitle: {node['title']}\nDescription: {node['description']}"
        )
        vr: dict[str, Any] = {}
        async for ev in self._tool_loop(
            val_sys, val_user, VALIDATE_ITERS, vr, budget_s=VALIDATE_BUDGET_S,
            tag={"node_id": node["id"], "agent": node.get("agent", "")},
        ):
            yield ev
        parsed = self._parse_json(vr.get("text", ""))
        verdict = "inconclusive"
        evidence_text = (vr.get("text") or "").strip()[:600]
        subs: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            v = str(parsed.get("verdict", "")).lower()
            if v in ("validated", "invalidated", "inconclusive"):
                verdict = v
            if parsed.get("evidence"):
                evidence_text = str(parsed["evidence"])[:600]
            if isinstance(parsed.get("subhypotheses"), list):
                for s in parsed["subhypotheses"][:MAX_SUBHYPOTHESES]:
                    if isinstance(s, dict) and s.get("title"):
                        subs.append({"title": str(s["title"])[:120], "description": str(s.get("description", ""))[:400]})
        node["status"] = verdict
        node["evidence"] = evidence_text
        yield AgentEvent(
            type="hypothesis_status",
            data={"id": node["id"], "status": verdict, "evidence": evidence_text, "agent": node.get("agent", "")},
        )
        out_nodes.append(node)
        subs_by_node[node["id"]] = subs


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return str(part.get("text", ""))
    return ""


def _tree_to_text(nodes: list[dict[str, Any]]) -> str:
    """Flatten the validated hypothesis tree into a readable summary for the LLM."""
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for n in nodes:
        by_parent.setdefault(n.get("parent_id"), []).append(n)

    lines: list[str] = []

    def walk(parent: str | None, indent: int) -> None:
        for n in by_parent.get(parent, []):
            pad = "  " * indent
            lines.append(
                f"{pad}- [{n['status'].upper()}] {n['title']}: {n.get('evidence', '')}"
            )
            walk(n["id"], indent + 1)

    walk(None, 0)
    return "\n".join(lines) if lines else "(no hypotheses validated)"

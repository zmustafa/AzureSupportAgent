"""AI-guided custom-agent designer.

Powers the "Generate with AI" wizard: a dynamic, multi-step interview (the model asks
the questions, adapting to the user's intent and prior answers) followed by a one-shot
generation of a complete, production-grade agent definition.

Both phases reuse the active LLM provider with NO tools (plain JSON completions), the
same pattern as propose_problems / auto-title. The generation phase is GROUNDED: it is
given the real connector-tool catalog and Azure connections so it can only choose tools
and a tenant that actually exist (it cannot invent tool names).
"""
from __future__ import annotations

import re
from typing import Any

from app.agent.factory import build_provider, build_provider_for
from app.core.ai_prompts import get_full_prompt
from app.core.utils import safe_json_parse

# Hard caps so a malicious/looping client can't drive an unbounded interview.
MAX_QUESTIONS_PER_STEP = 4
MAX_INTERVIEW_STEPS = 6

_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def strip_resource_guids(text: str) -> str:
    """Remove leaked Azure tenant/subscription/connection GUIDs from instruction text.

    The generator/enhancer is given the real connection list for grounding, and models
    sometimes hardcode a specific tenant/subscription/connection GUID into the agent's
    instructions (e.g. ``**khspn** (`65fd…`)``). Those defaults shouldn't live in a
    reusable persona, so we strip the GUID and its immediate backtick/paren wrapper, then
    tidy the surrounding punctuation/whitespace."""
    if not text or not _GUID_RE.search(text):
        return text
    # Drop a parenthesized, backtick-wrapped GUID and its leading space: " (`GUID`)".
    text = re.sub(r"\s*\(\s*`?" + _GUID_RE.pattern + r"`?\s*\)", "", text)
    # Drop a bare backtick-wrapped GUID: "`GUID`".
    text = re.sub(r"`\s*" + _GUID_RE.pattern + r"\s*`", "", text)
    # Drop any remaining bare GUIDs.
    text = _GUID_RE.sub("", text)
    # Tidy: collapse doubled spaces, empty backticks/parens, and space-before-punct.
    text = text.replace("``", "").replace("()", "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([;,.)])", r"\1", text)
    return text


INTERVIEW_PROMPT = """\
You are an expert assistant that DESIGNS specialized Azure automation agents by \
interviewing the person who wants one. Your job in THIS turn is to ask the next, most \
useful batch of clarifying questions — never to write the agent yet.

You are given the user's goal and the answers gathered so far. Decide what you still \
need to know to design a strong, focused agent: its scope (which resource types / \
services), the symptoms or tasks it handles, how it should deliver results (chat only, \
email, Teams, Jira, etc.), whether it should only investigate or also remediate, and \
any depth/methodology preferences.

Rules:
- Ask only what genuinely improves the design. Quality over quantity.
- Ask at most %(max_q)d questions this step. Prefer option-based questions (chips) the \
  user can click; add a free-text question only when options can't capture the answer.
- Do NOT repeat anything already answered. Build on prior answers.
- When you have enough to design a great agent, set "done": true and return an empty \
  "questions" array — do not pad with filler questions.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "questions": [
    {
      "id": "short_snake_case_id",
      "prompt": "the question text",
      "kind": "single" | "multi" | "text",
      "options": ["Option A", "Option B"],   // omit/empty for kind "text"
      "allow_custom": true                     // true if the user may add their own value
    }
  ],
  "done": false,
  "note": "one short sentence shown above the questions (optional)"
}
"""


GENERATE_PROMPT = """\
You are an expert that writes COMPLETE, production-grade definitions for specialized \
Azure automation agents. Given the design interview (goal + Q&A) and the REAL catalog of \
available connector tools and Azure tenant connections, produce the final agent.

The agent will run inside an Azure support assistant that already has read-only Azure \
investigation tools (Azure Resource Graph via an `arm` tool, resource health, metrics, \
logs, etc.). You only choose CONNECTOR tools (email/Teams/Jira/Grafana) from the catalog \
for DELIVERY/notification — never invent tool names; use only names present in the \
catalog. If the user wants chat-only output, choose no connector tools.

Write the `instructions` as a thorough operating manual for the agent, in Markdown, with \
these sections:
1. Mission — one paragraph: what it diagnoses/does and the outcome it produces.
2. Methodology — an ordered, domain-specific investigation procedure (the concrete \
   signals, metrics, and resource properties to examine for THIS domain). Be specific to \
   the user's chosen scope (e.g. for VM/App Service performance: CPU/memory/IO, app \
   service plan tier & scaling, HTTP queue length, response times, dependency latency, \
   recent deployments, autoscale rules).
3. How to use tools — prefer Azure Resource Graph for inventory; call a tool with \
   {"learn": true, "intent": "..."} first when unsure of its commands; be surgical.
4. Output format — the exact report structure the agent must always produce (summary, \
   evidence table, root cause, recommendations, validation/next steps).
5. Guardrails — read-only vs. remediation stance, never widen exposure casually, say \
   what it could not access when evidence is incomplete.

Map the run mode: "investigate only" -> "review"; "propose and execute fixes" -> \
"autonomous" (only if the user explicitly wanted execution). Default allow_all_azure to \
true for investigators.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "name": "Concise Title Case Name",
  "instructions": "full markdown operating manual",
  "connector_tools": ["exact_tool_name_from_catalog"],
  "allow_all_azure": true,
  "run_mode": "review" | "autonomous",
  "suggested_provider": "",   // "" to use the global default, else a provider id from the list
  "suggested_model": "",      // "" for provider default
  "summary": "one or two sentences describing the agent you designed",
  "rationale": "one short sentence on why these tools / run mode were chosen"
}
"""


# --------------------------------------------------------------- enhancement mode
ENHANCE_INTERVIEW_PROMPT = """\
You are a principal Azure SRE reviewing an EXISTING custom automation agent to make it \
production-grade. You are given the agent's current definition (name, instructions, \
tools, run mode) and any answers gathered so far. Your job THIS turn is to ask the next, \
most useful batch of clarifying questions that will let you MEANINGFULLY enhance it — \
never to rewrite it yet.

First, critically assess the current agent. Consider gaps such as: a thin or missing \
methodology, no explicit output format, weak or absent guardrails, missing edge cases or \
failure modes, no validation steps, unclear scope, no remediation guidance, missing \
evidence-gathering steps, or no escalation path. Then ask only what you still need to \
know to close the most impactful gaps and respect the owner's intent.

Rules:
- Ground every question in THIS agent. Pre-select / hint at sensible defaults based on \
  the current instructions so the user mostly confirms rather than re-specifies.
- Ask at most %(max_q)d questions this step. Prefer option-based questions (chips). Add \
  a free-text question only when options can't capture the answer.
- Do NOT repeat anything already answered or already clearly covered by the current \
  instructions. Build on what exists; don't restart from scratch.
- When you have enough to produce a strong enhancement, set "done": true with an empty \
  "questions" array.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "assessment": "2-3 sentences: the agent's strengths and the biggest gaps you'll close",
  "questions": [
    {
      "id": "short_snake_case_id",
      "prompt": "the question text",
      "kind": "single" | "multi" | "text",
      "options": ["Option A", "Option B"],
      "allow_custom": true
    }
  ],
  "done": false,
  "note": "one short sentence shown above the questions (optional)"
}
"""


ENHANCE_GENERATE_PROMPT = """\
You are an expert that ENHANCES an existing specialized Azure automation agent into a \
production-grade, enterprise-quality definition. You are given the agent's CURRENT \
definition, an enhancement interview (assessment + the owner's answers), and the REAL \
catalog of connector tools and Azure tenant connections.

Improve — do not discard. Preserve the agent's original intent, name (unless the owner \
clearly asked to rename), domain, and any correct existing guidance. Substantially raise \
the quality and completeness of the `instructions`: deepen the methodology with concrete, \
domain-specific signals/metrics/resource properties; add a precise required output \
format; add robust guardrails (read-only vs. remediation stance, least-privilege, never \
widen exposure casually, state what couldn't be accessed); cover edge cases, failure \
modes, validation steps, and (where appropriate) escalation. Keep it focused — longer \
only where it adds real diagnostic value.

The agent already has read-only Azure investigation tools (Azure Resource Graph via an \
`arm` tool, resource health, metrics, logs). Choose CONNECTOR tools only from the \
provided catalog for delivery/notification; never invent tool names. Keep the existing \
connector tools unless the interview indicates a change.

The `instructions` MUST be a complete Markdown operating manual with these sections: \
1. Mission, 2. Methodology, 3. How to use tools, 4. Output format, 5. Guardrails.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "name": "the agent name (keep unless asked to rename)",
  "instructions": "full enhanced markdown operating manual",
  "connector_tools": ["exact_tool_name_from_catalog"],
  "allow_all_azure": true,
  "run_mode": "review" | "autonomous",
  "summary": "one or two sentences describing what the enhanced agent now does",
  "changes": ["short bullet of a specific improvement", "another improvement"]
}
"""


def _agent_context(agent: dict[str, Any]) -> str:
    """A compact textual snapshot of the current agent for the model to reason over."""
    tools = ", ".join(agent.get("connector_tools") or []) or "(none)"
    instr = str(agent.get("instructions", "") or "")
    return (
        f"CURRENT AGENT\n"
        f"- Name: {agent.get('name', '')}\n"
        f"- Run mode: {agent.get('run_mode', 'review')}\n"
        f"- Connector tools: {tools}\n"
        f"- allow_all_azure: {agent.get('allow_all_azure', True)}\n"
        f"- Instructions length: {len(instr)} chars\n\n"
        f"CURRENT INSTRUCTIONS:\n{instr if instr.strip() else '(empty — the owner created a stub agent)'}"
    )


def _interview_transcript(goal: str, answers: list[dict[str, Any]]) -> str:
    lines = [f"User's goal: {goal.strip() or '(not provided)'}", "", "Answers so far:"]
    if not answers:
        lines.append("(none yet)")
    for a in answers:
        q = str(a.get("prompt") or a.get("id") or "question")
        val = a.get("answer")
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val)
        lines.append(f"- {q}: {val if val not in (None, '') else '(skipped)'}")
    return "\n".join(lines)


async def _complete_json(
    system: str,
    user: str,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> Any:
    """One no-tool completion, parsed leniently as JSON. Returns None on failure.
    When provider_override/model_override are given, that LLM is used instead of the
    globally-active one (lets callers target a specific provider, e.g. GitHub Copilot)."""
    provider = (
        build_provider_for(provider_override, model_override)
        if (provider_override or model_override)
        else build_provider()
    )
    text = ""
    async for ev in provider.stream(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        None,
    ):
        if ev.type == "token":
            text += ev.text
    t = text.strip()
    if "```" in t:
        import re

        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not (t.startswith("{") or t.startswith("[")):
        import re

        m = re.search(r"(\{.*\}|\[.*\])", t, re.DOTALL)
        if m:
            t = m.group(1)
    return safe_json_parse(t, default=None)


async def next_questions(goal: str, answers: list[dict[str, Any]], step: int) -> dict[str, Any]:
    """Ask the model for the next batch of clarifying questions (or signal done)."""
    # Safety: end the interview after a hard cap regardless of the model's choice.
    if step >= MAX_INTERVIEW_STEPS:
        return {"questions": [], "done": True, "note": ""}
    system = get_full_prompt("designer_interview").replace(
        "%(max_q)d", str(MAX_QUESTIONS_PER_STEP)
    )
    user = _interview_transcript(goal, answers)
    parsed = await _complete_json(system, user)
    if not isinstance(parsed, dict):
        return {"questions": [], "done": True, "note": ""}
    raw_qs = parsed.get("questions")
    questions: list[dict[str, Any]] = []
    if isinstance(raw_qs, list):
        for q in raw_qs[:MAX_QUESTIONS_PER_STEP]:
            if not isinstance(q, dict) or not q.get("prompt"):
                continue
            kind = str(q.get("kind", "single")).lower()
            if kind not in ("single", "multi", "text"):
                kind = "single"
            opts = q.get("options") if isinstance(q.get("options"), list) else []
            questions.append(
                {
                    "id": str(q.get("id") or f"q{len(questions)+1}")[:60],
                    "prompt": str(q["prompt"])[:300],
                    "kind": kind,
                    "options": [str(o)[:120] for o in opts][:8],
                    "allow_custom": bool(q.get("allow_custom", kind == "text")),
                }
            )
    done = bool(parsed.get("done")) or not questions
    return {"questions": questions, "done": done, "note": str(parsed.get("note", ""))[:200]}


async def generate_agent(
    goal: str,
    answers: list[dict[str, Any]],
    tool_catalog: list[dict[str, str]],
    connections: list[dict[str, Any]],
    providers: list[str],
    provider_override: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any] | None:
    """Generate a complete agent draft, grounded in the real tool/connection catalog."""
    catalog_lines = [
        f"- {t['name']} ({t.get('connector_name', '')}): {t.get('description', '')}"
        for t in tool_catalog
    ] or ["(no connector tools are configured)"]
    conn_lines = [
        f"- id={c.get('id')} name={c.get('display_name') or c.get('tenant_id')}"
        + (" [default]" if c.get("is_default") else "")
        for c in connections
    ] or ["(no Azure connections configured)"]
    user = (
        f"{_interview_transcript(goal, answers)}\n\n"
        f"Available connector tools (choose ONLY from these names):\n"
        + "\n".join(catalog_lines)
        + "\n\nAvailable Azure tenant connections:\n"
        + "\n".join(conn_lines)
        + "\n\nAvailable LLM providers (use '' for default): "
        + (", ".join(providers) or "(default only)")
    )
    parsed = await _complete_json(
        get_full_prompt("designer_generate"), user, provider_override, model_override
    )
    if not isinstance(parsed, dict) or not parsed.get("instructions"):
        return None
    valid_tools = {t["name"] for t in tool_catalog}
    chosen = [t for t in (parsed.get("connector_tools") or []) if t in valid_tools]
    run_mode = str(parsed.get("run_mode", "review")).lower()
    if run_mode not in ("review", "autonomous"):
        run_mode = "review"
    prov = str(parsed.get("suggested_provider", "") or "")
    if prov and prov not in providers:
        prov = ""
    from app.automations.agents import classify_category

    return {
        "name": str(parsed.get("name", "") or "New Agent")[:200],
        "instructions": strip_resource_guids(str(parsed["instructions"]))[:20000],
        "connector_tools": chosen,
        "allow_all_azure": bool(parsed.get("allow_all_azure", True)),
        "run_mode": run_mode,
        "suggested_provider": prov,
        "suggested_model": str(parsed.get("suggested_model", "") or "")[:128],
        "category": classify_category(
            str(parsed.get("name", "")), str(parsed.get("instructions", ""))
        ),
        "summary": str(parsed.get("summary", ""))[:600],
        "rationale": str(parsed.get("rationale", ""))[:400],
    }


async def enhance_questions(
    agent: dict[str, Any],
    answers: list[dict[str, Any]],
    step: int,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Ask the model for the next batch of clarifying questions to ENHANCE an existing
    agent (or signal done). Returns an assessment plus questions on the first step."""
    if step >= MAX_INTERVIEW_STEPS:
        return {"assessment": "", "questions": [], "done": True, "note": ""}
    system = get_full_prompt("designer_enhance_interview").replace(
        "%(max_q)d", str(MAX_QUESTIONS_PER_STEP)
    )
    user = _agent_context(agent) + "\n\nEnhancement answers so far:\n" + (
        "\n".join(
            f"- {a.get('prompt') or a.get('id')}: "
            + (", ".join(map(str, a["answer"])) if isinstance(a.get("answer"), list) else str(a.get("answer", "")))
            for a in answers
        )
        or "(none yet)"
    )
    parsed = await _complete_json(system, user, provider_override, model_override)
    if not isinstance(parsed, dict):
        return {"assessment": "", "questions": [], "done": True, "note": ""}
    raw_qs = parsed.get("questions")
    questions: list[dict[str, Any]] = []
    if isinstance(raw_qs, list):
        for q in raw_qs[:MAX_QUESTIONS_PER_STEP]:
            if not isinstance(q, dict) or not q.get("prompt"):
                continue
            kind = str(q.get("kind", "single")).lower()
            if kind not in ("single", "multi", "text"):
                kind = "single"
            opts = q.get("options") if isinstance(q.get("options"), list) else []
            questions.append(
                {
                    "id": str(q.get("id") or f"q{len(questions)+1}")[:60],
                    "prompt": str(q["prompt"])[:300],
                    "kind": kind,
                    "options": [str(o)[:120] for o in opts][:8],
                    "allow_custom": bool(q.get("allow_custom", kind == "text")),
                }
            )
    done = bool(parsed.get("done")) or not questions
    return {
        "assessment": str(parsed.get("assessment", ""))[:800],
        "questions": questions,
        "done": done,
        "note": str(parsed.get("note", ""))[:200],
    }


async def enhance_agent(
    agent: dict[str, Any],
    answers: list[dict[str, Any]],
    tool_catalog: list[dict[str, str]],
    connections: list[dict[str, Any]],
    provider_override: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any] | None:
    """Produce an enhanced agent draft from the current agent + the enhancement
    interview. Preserves intent; substantially deepens the instructions. Grounded in
    the real tool/connection catalog. Returns None on failure."""
    catalog_lines = [
        f"- {t['name']} ({t.get('connector_name', '')}): {t.get('description', '')}"
        for t in tool_catalog
    ] or ["(no connector tools are configured)"]
    conn_lines = [
        f"- id={c.get('id')} name={c.get('display_name') or c.get('tenant_id')}"
        + (" [default]" if c.get("is_default") else "")
        for c in connections
    ] or ["(no Azure connections configured)"]
    answers_block = "\n".join(
        f"- {a.get('prompt') or a.get('id')}: "
        + (", ".join(map(str, a["answer"])) if isinstance(a.get("answer"), list) else str(a.get("answer", "")))
        for a in answers
    ) or "(no extra answers; enhance using best judgement)"
    user = (
        f"{_agent_context(agent)}\n\n"
        f"Enhancement interview answers:\n{answers_block}\n\n"
        "Available connector tools (choose ONLY from these names):\n"
        + "\n".join(catalog_lines)
        + "\n\nAvailable Azure tenant connections:\n"
        + "\n".join(conn_lines)
    )
    parsed = await _complete_json(
        get_full_prompt("designer_enhance_generate"), user, provider_override, model_override
    )
    if not isinstance(parsed, dict) or not parsed.get("instructions"):
        return None
    valid_tools = {t["name"] for t in tool_catalog}
    # Default to keeping the agent's existing tools if the model omits them.
    chosen = [t for t in (parsed.get("connector_tools") or agent.get("connector_tools") or []) if t in valid_tools]
    run_mode = str(parsed.get("run_mode", agent.get("run_mode", "review"))).lower()
    if run_mode not in ("review", "autonomous"):
        run_mode = "review"
    changes = [str(c)[:200] for c in (parsed.get("changes") or []) if str(c).strip()][:12]
    return {
        "name": str(parsed.get("name") or agent.get("name") or "Agent")[:200],
        "instructions": strip_resource_guids(str(parsed["instructions"]))[:20000],
        "connector_tools": chosen,
        "allow_all_azure": bool(parsed.get("allow_all_azure", agent.get("allow_all_azure", True))),
        "run_mode": run_mode,
        "summary": str(parsed.get("summary", ""))[:600],
        "changes": changes,
    }


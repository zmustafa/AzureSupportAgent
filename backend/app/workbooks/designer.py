"""AI-guided Workbook designer.

Powers the "Generate with AI" wizard for Workbooks: a dynamic multi-step interview
(the model asks adaptive questions) followed by a one-shot generation of a complete,
ready-to-run workbook (runtime + body with {{param}} placeholders + params + AI'fy /
alert / tile config). Mirrors the sub-agent designer: plain JSON completions with NO
tools, grounded on the real Azure connections so it can't invent tenants.
"""
from __future__ import annotations

import re
from typing import Any

from app.agent.factory import build_provider, build_provider_for
from app.core.ai_prompts import get_full_prompt
from app.core.utils import safe_json_parse

MAX_QUESTIONS_PER_STEP = 4
MAX_INTERVIEW_STEPS = 6

_VALID_RUNTIMES = ("az", "kql", "powershell")
_VALID_SEV = ("info", "warning", "error", "critical")
_VALID_AIFY_MODES = ("summary", "severity", "extract", "diff")


INTERVIEW_PROMPT = """\
You are an expert that DESIGNS reusable Azure "workbooks" by interviewing the person who \
wants one. A workbook is a saved Azure operation — an `az` CLI command, an Azure Resource \
Graph (KQL) query, or a PowerShell snippet — whose output is automatically AI-summarized, \
severity-classified, and optionally turned into a dashboard tile or an alert. Your job in \
THIS turn is to ask the next, most useful batch of clarifying questions — never to write \
the workbook yet.

You are given the user's goal and the answers gathered so far. Decide what you still need \
to know: which runtime fits best (az vs KQL vs PowerShell), the exact resources/metrics to \
inspect, any parameters the operator should supply at run time (e.g. resource group, days \
threshold), whether the result should be summarized / scored / extracted to fields / \
diffed against the last run, and whether it should raise an alert or show as a tile.

Rules:
- Ask only what genuinely improves the design. Quality over quantity.
- Ask at most %(max_q)d questions this step. Prefer option-based questions (chips); add a \
  free-text question only when options can't capture the answer.
- Do NOT repeat anything already answered. Build on prior answers.
- When you have enough to design a great workbook, set "done": true and return an empty \
  "questions" array.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
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


GENERATE_PROMPT = """\
You are an expert that writes COMPLETE, production-ready Azure workbooks. Given the design \
interview (goal + Q&A) and the REAL list of Azure tenant connections, produce the final \
workbook definition.

Choose the best runtime:
- "kql"  — Azure Resource Graph queries for INVENTORY/posture across resources. Prefer \
  this for "find/list/count resources where ..." . The body is a KQL query, e.g. \
  `Resources | where type =~ 'microsoft.web/sites' | project name, resourceGroup`.
- "az"   — a single read-only `az` CLI command that returns JSON (use `-o json`), for \
  operations Resource Graph can't express. e.g. `az webapp list -o json`.
- "powershell" — only when az/KQL can't do it.

Use {{param}} placeholders in the body for any value the operator should supply at run \
time, and declare each in "params" with a key, label, sensible default and help text. \
Keep the workbook READ-ONLY (kind = "read") unless the user explicitly wants a change \
operation. Configure AI'fication: enable "summary" and "severity" by default; add \
"extract" (with a short schema hint of the fields to pull) when the user wants structured \
data or a numeric dashboard tile; add "diff" when they care about change over time. Set a \
dashboard tile only if the user asked for one (format "number" needs a metric_key that \
matches an extracted field; else "severity"). Set an alert only if they want to be \
notified, with a sensible min_severity.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "name": "Concise Title Case Name",
  "description": "one sentence on what it checks and why",
  "runtime": "az" | "kql" | "powershell",
  "body": "the command or query, using {{param}} placeholders",
  "params": [
    {"key": "resourceGroup", "label": "Resource group", "type": "text", "default": "", "required": false, "help": "Scope to one RG (blank = all)"}
  ],
  "kind": "read" | "write",
  "tags": ["governance", "cost"],
  "aify": {"enabled": true, "modes": ["summary", "severity"], "schema": "fields to extract, if any"},
  "alert": {"enabled": false, "min_severity": "warning"},
  "tile": {"enabled": false, "label": "", "format": "severity", "metric_key": ""},
  "summary": "one or two sentences describing the workbook you designed",
  "rationale": "one short sentence on why this runtime / config were chosen"
}
"""


ENHANCE_INTERVIEW_PROMPT = """\
You are a principal Azure engineer reviewing an EXISTING workbook to make it \
production-grade. You are given its current definition (name, runtime, body, params, AI'fy \
/ alert / tile config) and any answers gathered so far. Your job THIS turn is to ask the \
next, most useful batch of clarifying questions to MEANINGFULLY improve it — never to \
rewrite it yet.

First, critically assess the workbook. Consider gaps such as: a fragile or overly broad \
query, missing parameters, no severity scoring, no structured extraction where it would \
help, a tile/alert that should exist (or shouldn't), or an inefficient runtime choice. \
Then ask only what you still need to know to close the biggest gaps.

Rules:
- Ground every question in THIS workbook; pre-select sensible defaults so the user mostly \
  confirms. Ask at most %(max_q)d questions this step. Prefer chips.
- Don't repeat what's already answered or clearly covered. When ready, set "done": true \
  with an empty "questions" array.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "assessment": "2-3 sentences: strengths and the biggest gaps you'll close",
  "questions": [
    {"id": "id", "prompt": "...", "kind": "single|multi|text", "options": ["..."], "allow_custom": true}
  ],
  "done": false,
  "note": "optional one-liner"
}
"""


ENHANCE_GENERATE_PROMPT = """\
You are an expert that ENHANCES an existing Azure workbook into a production-grade \
definition. You are given the CURRENT workbook, an enhancement interview (assessment + \
answers), and the REAL Azure connection list. Improve — do not discard: preserve intent, \
the runtime (unless clearly wrong), and any correct existing logic. Tighten the query/ \
command, add or refine parameters with {{param}} placeholders, improve the AI'fy config \
(severity scoring, structured extraction with a schema hint where useful), and set a \
tile/alert only where it adds value. Keep it READ-ONLY unless it already was a write.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "name": "the name (keep unless asked to rename)",
  "description": "one sentence",
  "runtime": "az" | "kql" | "powershell",
  "body": "the improved command/query with {{param}} placeholders",
  "params": [{"key": "...", "label": "...", "type": "text", "default": "", "required": false, "help": "..."}],
  "kind": "read" | "write",
  "tags": ["..."],
  "aify": {"enabled": true, "modes": ["summary", "severity"], "schema": "..."},
  "alert": {"enabled": false, "min_severity": "warning"},
  "tile": {"enabled": false, "label": "", "format": "severity", "metric_key": ""},
  "summary": "what the enhanced workbook now does",
  "changes": ["specific improvement", "another improvement"]
}
"""


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


def _workbook_context(wb: dict[str, Any]) -> str:
    aify = wb.get("aify", {}) or {}
    return (
        "CURRENT WORKBOOK\n"
        f"- Name: {wb.get('name', '')}\n"
        f"- Runtime: {wb.get('runtime', 'kql')}\n"
        f"- Kind: {wb.get('kind', 'read')}\n"
        f"- Params: {', '.join(p.get('key', '') for p in (wb.get('params') or [])) or '(none)'}\n"
        f"- AI'fy modes: {', '.join(aify.get('modes') or []) or '(none)'}\n\n"
        f"CURRENT BODY:\n{wb.get('body', '') or '(empty)'}"
    )


async def _complete_json(
    system: str, user: str, provider_override: str | None = None, model_override: str | None = None
) -> Any:
    provider = (
        build_provider_for(provider_override, model_override)
        if (provider_override or model_override)
        else build_provider()
    )
    text = ""
    async for ev in provider.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], None
    ):
        if ev.type == "token":
            text += ev.text
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not (t.startswith("{") or t.startswith("[")):
        m = re.search(r"(\{.*\}|\[.*\])", t, re.DOTALL)
        if m:
            t = m.group(1)
    return safe_json_parse(t, default=None)


def _parse_questions(parsed: Any) -> list[dict[str, Any]]:
    raw_qs = parsed.get("questions") if isinstance(parsed, dict) else None
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
    return questions


def _normalize_workbook(parsed: dict[str, Any]) -> dict[str, Any]:
    runtime = str(parsed.get("runtime", "kql")).lower()
    if runtime not in _VALID_RUNTIMES:
        runtime = "kql"
    kind = str(parsed.get("kind", "read")).lower()
    if kind not in ("read", "write"):
        kind = "read"
    params = []
    for p in parsed.get("params") or []:
        if not isinstance(p, dict) or not p.get("key"):
            continue
        params.append(
            {
                "key": str(p.get("key"))[:60],
                "label": str(p.get("label", ""))[:120],
                "type": str(p.get("type", "text"))[:20] or "text",
                "default": p.get("default", ""),
                "required": bool(p.get("required", False)),
                "help": str(p.get("help", ""))[:200],
            }
        )
    aify_in = parsed.get("aify") or {}
    modes = [m for m in (aify_in.get("modes") or ["summary", "severity"]) if m in _VALID_AIFY_MODES]
    aify = {
        "enabled": bool(aify_in.get("enabled", True)),
        "modes": modes or ["summary", "severity"],
        "schema": str(aify_in.get("schema", ""))[:500],
    }
    alert_in = parsed.get("alert") or {}
    min_sev = str(alert_in.get("min_severity", "warning"))
    alert = {"enabled": bool(alert_in.get("enabled", False)), "min_severity": min_sev if min_sev in _VALID_SEV else "warning"}
    tile_in = parsed.get("tile") or {}
    fmt = str(tile_in.get("format", "severity"))
    tile = {
        "enabled": bool(tile_in.get("enabled", False)),
        "label": str(tile_in.get("label", ""))[:80],
        "format": fmt if fmt in ("severity", "number", "text") else "severity",
        "metric_key": str(tile_in.get("metric_key", ""))[:60],
    }
    tags = [str(t)[:40] for t in (parsed.get("tags") or []) if str(t).strip()][:8]
    return {
        "name": str(parsed.get("name", "") or "New Workbook")[:200],
        "description": str(parsed.get("description", ""))[:2000],
        "runtime": runtime,
        "body": str(parsed.get("body", ""))[:8000],
        "params": params,
        "kind": kind,
        "tags": tags,
        "aify": aify,
        "alert": alert,
        "tile": tile,
        "enabled": True,
    }


async def next_questions(goal: str, answers: list[dict[str, Any]], step: int) -> dict[str, Any]:
    if step >= MAX_INTERVIEW_STEPS:
        return {"questions": [], "done": True, "note": ""}
    system = get_full_prompt("workbook_designer_interview").replace("%(max_q)d", str(MAX_QUESTIONS_PER_STEP))
    parsed = await _complete_json(system, _interview_transcript(goal, answers))
    if not isinstance(parsed, dict):
        return {"questions": [], "done": True, "note": ""}
    questions = _parse_questions(parsed)
    done = bool(parsed.get("done")) or not questions
    return {"questions": questions, "done": done, "note": str(parsed.get("note", ""))[:200]}


async def generate_workbook(
    goal: str, answers: list[dict[str, Any]], connections: list[dict[str, Any]]
) -> dict[str, Any] | None:
    conn_lines = [
        f"- id={c.get('id')} name={c.get('display_name') or c.get('tenant_id')}"
        + (" [default]" if c.get("is_default") else "")
        for c in connections
    ] or ["(no Azure connections configured)"]
    user = (
        f"{_interview_transcript(goal, answers)}\n\nAvailable Azure tenant connections:\n"
        + "\n".join(conn_lines)
    )
    parsed = await _complete_json(get_full_prompt("workbook_designer_generate"), user)
    if not isinstance(parsed, dict) or not parsed.get("body"):
        return None
    result = _normalize_workbook(parsed)
    result["summary"] = str(parsed.get("summary", ""))[:600]
    result["rationale"] = str(parsed.get("rationale", ""))[:400]
    return result


async def enhance_questions(wb: dict[str, Any], answers: list[dict[str, Any]], step: int) -> dict[str, Any]:
    if step >= MAX_INTERVIEW_STEPS:
        return {"assessment": "", "questions": [], "done": True, "note": ""}
    system = get_full_prompt("workbook_designer_enhance_interview").replace("%(max_q)d", str(MAX_QUESTIONS_PER_STEP))
    user = _workbook_context(wb) + "\n\nEnhancement answers so far:\n" + (
        "\n".join(
            f"- {a.get('prompt') or a.get('id')}: "
            + (", ".join(map(str, a["answer"])) if isinstance(a.get("answer"), list) else str(a.get("answer", "")))
            for a in answers
        )
        or "(none yet)"
    )
    parsed = await _complete_json(system, user)
    if not isinstance(parsed, dict):
        return {"assessment": "", "questions": [], "done": True, "note": ""}
    questions = _parse_questions(parsed)
    done = bool(parsed.get("done")) or not questions
    return {
        "assessment": str(parsed.get("assessment", ""))[:800],
        "questions": questions,
        "done": done,
        "note": str(parsed.get("note", ""))[:200],
    }


async def enhance_workbook(
    wb: dict[str, Any], answers: list[dict[str, Any]], connections: list[dict[str, Any]]
) -> dict[str, Any] | None:
    conn_lines = [
        f"- id={c.get('id')} name={c.get('display_name') or c.get('tenant_id')}"
        for c in connections
    ] or ["(no Azure connections configured)"]
    user = (
        _workbook_context(wb)
        + "\n\nEnhancement answers:\n"
        + ("\n".join(f"- {a.get('prompt') or a.get('id')}: {a.get('answer')}" for a in answers) or "(none)")
        + "\n\nAzure connections:\n"
        + "\n".join(conn_lines)
    )
    parsed = await _complete_json(get_full_prompt("workbook_designer_enhance_generate"), user)
    if not isinstance(parsed, dict) or not parsed.get("body"):
        return None
    result = _normalize_workbook(parsed)
    result["summary"] = str(parsed.get("summary", ""))[:600]
    result["changes"] = [str(c)[:200] for c in (parsed.get("changes") or [])][:8]
    return result

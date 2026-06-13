"""AI-guided Playbook designer.

Powers the "Generate with AI" wizard for Playbooks: a dynamic interview followed by a
one-shot generation of a complete playbook (an ordered chain of steps, each invoking an
EXISTING workbook with params, optional param-mapping from a prior step's structured
output, and a severity gate). The generation is GROUNDED on the real workbook catalog so
it can only reference workbooks that actually exist; when the goal needs a workbook that
doesn't exist yet, it returns "proposed_workbooks" (handed off to the workbook designer).
"""
from __future__ import annotations

import re
from typing import Any

from app.agent.factory import build_provider, build_provider_for
from app.core.ai_prompts import get_full_prompt
from app.core.utils import safe_json_parse

MAX_QUESTIONS_PER_STEP = 4
MAX_INTERVIEW_STEPS = 6

_VALID_RUN_IF = ("always", "info", "warning", "error", "critical")


INTERVIEW_PROMPT = """\
You are an expert that DESIGNS Azure "playbooks" by interviewing the person who wants one. \
A playbook runs an ordered chain of "workbooks" (saved az/KQL/PowerShell operations). \
Each step can pass parameters, map a value from a previous step's structured output into a \
later step, and be gated on the running severity (e.g. only run a deeper step if an \
earlier step reported >= warning). Your job THIS turn is to ask the next, most useful \
batch of clarifying questions — never to write the playbook yet.

You are given the user's goal, the answers so far, and the list of workbooks that already \
exist. Decide what you still need to know: the end-to-end investigation/operation flow, \
which signals to gather first vs. only-if-something-looks-wrong, severity gating, and \
whether to alert at the end.

Rules:
- Ask only what genuinely improves the design. At most %(max_q)d questions this step. \
  Prefer option-based questions (chips). Don't repeat answered items.
- When you have enough, set "done": true with an empty "questions" array.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "questions": [
    {"id": "id", "prompt": "...", "kind": "single|multi|text", "options": ["..."], "allow_custom": true}
  ],
  "done": false,
  "note": "optional one-liner"
}
"""


GENERATE_PROMPT = """\
You are an expert that writes COMPLETE Azure playbooks. Given the design interview (goal + \
Q&A) and the REAL catalog of existing workbooks, produce the final playbook as an ordered \
chain of steps.

Each step references an EXISTING workbook by its exact id from the catalog — never invent \
a workbook id. Order steps so broad/cheap checks run first and deeper/conditional steps \
later. Use "run_if" to gate a step on the running severity ("always", or only when a \
prior step reached "warning"/"error"/"critical"). Use "param_map" to feed a value from an \
earlier step's structured output into a later step's parameter, formatted as \
"stepId.structuredKey". Use static "params" for fixed values. Set an alert only if the \
user wants notification at the end.

If the goal clearly needs a capability that NO existing workbook provides, list it under \
"proposed_workbooks" (a short title + one-line purpose) instead of inventing an id — the \
user can generate those workbooks separately and add them.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "name": "Concise Title Case Name",
  "description": "one sentence on what the playbook does",
  "steps": [
    {
      "id": "s1",
      "name": "Step label",
      "workbook_id": "<exact id from the catalog>",
      "params": {"resourceGroup": "prod-rg"},
      "param_map": {"vaultName": "s1.firstVaultName"},
      "run_if": "always"
    }
  ],
  "alert": {"enabled": false, "min_severity": "warning"},
  "proposed_workbooks": [{"title": "...", "purpose": "..."}],
  "summary": "one or two sentences describing the playbook you designed",
  "rationale": "one short sentence on the ordering / gating choices"
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


def _catalog_lines(workbooks: list[dict[str, Any]]) -> str:
    lines = [
        f"- id={w.get('id')} | {w.get('name')} [{w.get('runtime')}]: {w.get('description', '')[:120]}"
        for w in workbooks
    ]
    return "\n".join(lines) or "(no workbooks exist yet — propose new ones)"


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


async def next_questions(
    goal: str, answers: list[dict[str, Any]], step: int, workbooks: list[dict[str, Any]]
) -> dict[str, Any]:
    if step >= MAX_INTERVIEW_STEPS:
        return {"questions": [], "done": True, "note": ""}
    system = get_full_prompt("playbook_designer_interview").replace("%(max_q)d", str(MAX_QUESTIONS_PER_STEP))
    user = _interview_transcript(goal, answers) + "\n\nExisting workbooks:\n" + _catalog_lines(workbooks)
    parsed = await _complete_json(system, user)
    if not isinstance(parsed, dict):
        return {"questions": [], "done": True, "note": ""}
    questions = _parse_questions(parsed)
    done = bool(parsed.get("done")) or not questions
    return {"questions": questions, "done": done, "note": str(parsed.get("note", ""))[:200]}


async def generate_playbook(
    goal: str, answers: list[dict[str, Any]], workbooks: list[dict[str, Any]]
) -> dict[str, Any] | None:
    valid_ids = {w["id"] for w in workbooks}
    user = (
        f"{_interview_transcript(goal, answers)}\n\nExisting workbooks (use ONLY these ids):\n"
        + _catalog_lines(workbooks)
    )
    parsed = await _complete_json(get_full_prompt("playbook_designer_generate"), user)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("steps"), list):
        return None
    steps: list[dict[str, Any]] = []
    missing: list[str] = []
    for i, s in enumerate(parsed["steps"], start=1):
        if not isinstance(s, dict):
            continue
        wid = str(s.get("workbook_id", ""))
        if wid not in valid_ids:
            if wid:
                missing.append(wid)
            continue
        run_if = str(s.get("run_if", "always")).lower()
        if run_if not in _VALID_RUN_IF:
            run_if = "always"
        steps.append(
            {
                "id": str(s.get("id") or f"s{i}")[:40],
                "name": str(s.get("name", ""))[:120],
                "workbook_id": wid,
                "params": s.get("params") if isinstance(s.get("params"), dict) else {},
                "param_map": s.get("param_map") if isinstance(s.get("param_map"), dict) else {},
                "run_if": run_if,
            }
        )
    alert_in = parsed.get("alert") or {}
    min_sev = str(alert_in.get("min_severity", "warning"))
    proposed = [
        {"title": str(p.get("title", ""))[:120], "purpose": str(p.get("purpose", ""))[:200]}
        for p in (parsed.get("proposed_workbooks") or [])
        if isinstance(p, dict) and p.get("title")
    ][:6]
    if not steps and not proposed:
        return None
    return {
        "name": str(parsed.get("name", "") or "New Playbook")[:200],
        "description": str(parsed.get("description", ""))[:2000],
        "steps": steps,
        "alert": {
            "enabled": bool(alert_in.get("enabled", False)),
            "min_severity": min_sev if min_sev in ("info", "warning", "error", "critical") else "warning",
        },
        "enabled": True,
        "proposed_workbooks": proposed,
        "summary": str(parsed.get("summary", ""))[:600],
        "rationale": str(parsed.get("rationale", ""))[:400],
    }

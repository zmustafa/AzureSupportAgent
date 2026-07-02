"""AI-guided Insight Pack designer.

Powers the "Generate with AI" wizard for packs: a dynamic multi-step interview (the model
asks the questions) followed by a one-shot generation of a complete, scope-agnostic pack
definition. Both phases reuse the active LLM with NO tools (plain JSON completions), the
same pattern as the custom-agent / workbook / playbook designers.

Generation is GROUNDED in the real source catalog and the deterministic security flag
codes, so the model can only choose data sources and ``always_notify_if`` codes that
actually exist.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.core.utils import safe_json_parse
from app.insights import packfile, sources

log = logging.getLogger("app.insights.designer")

MAX_QUESTIONS_PER_STEP = 4
MAX_INTERVIEW_STEPS = 5

# Deterministic security flag codes the runtime can detect (for the notify floor). Kept in
# sync with app.changeexplorer.security.flag_event.
FLAG_CODES: list[dict[str, str]] = [
    {"code": "public_exposure", "label": "Opened to the Internet (0.0.0.0/0)"},
    {"code": "public_ip", "label": "Public IP created/modified"},
    {"code": "public_network_access", "label": "Public network access enabled"},
    {"code": "rbac_grant", "label": "Role assignment / privileged access granted"},
    {"code": "owner_grant", "label": "Owner / privileged role involved"},
    {"code": "secret_access", "label": "Secret / key material accessed"},
    {"code": "secret_change", "label": "Secret / key / certificate changed"},
    {"code": "key_listing", "label": "Account keys / SAS listed"},
    {"code": "logging_disabled", "label": "Logging / diagnostics disabled"},
    {"code": "lock_removed", "label": "Resource lock removed"},
    {"code": "policy_exemption", "label": "Policy exemption created"},
    {"code": "policy_deleted", "label": "Policy assignment / definition deleted"},
    {"code": "security_control_deleted", "label": "Network security control deleted"},
    # --- Cross-source floors (radar / cost / rbac / assessments / backup / identity / policy) ---
    {"code": "retirement_soon", "label": "Service retirement approaching (Radar)"},
    {"code": "breaking_change", "label": "Breaking change announced (Radar)"},
    {"code": "idle_or_orphaned", "label": "Idle / orphaned resource wasting spend (Cost)"},
    {"code": "eligible_grant", "label": "PIM-eligible privileged assignment (RBAC)"},
    {"code": "assessment_critical", "label": "Critical assessment finding failing"},
    {"code": "backup_unprotected", "label": "Resource not protected by backup"},
    {"code": "dr_unhealthy", "label": "Disaster-recovery pair unhealthy"},
    {"code": "cred_expiring", "label": "Secret / certificate expiring soon (Identity)"},
    {"code": "mfa_gap", "label": "Privileged user without MFA (Identity)"},
    {"code": "ownerless_app", "label": "App registration with no owner (Identity)"},
    {"code": "non_compliant", "label": "Non-compliant resources present (Policy)"},
]
_VALID_FLAGS = {f["code"] for f in FLAG_CODES}
_VALID_SOURCES = {s["id"] for s in sources.SOURCE_CATALOG}

INTERVIEW_PROMPT = """\
You are an expert that DESIGNS "AI Insight Packs" by interviewing the person who wants one. \
An insight pack runs on a schedule: it gathers deterministic Azure change/telemetry data \
for a chosen scope, an LLM reasons over it, and it notifies the owner ONLY when something \
material happened. Your job THIS turn is to ask the next, most useful batch of clarifying \
questions — never to write the pack yet.

Decide what you still need to know to design a strong, focused pack: what signal it should \
watch for, which data it needs, how noisy it should be (notify threshold), what should ALWAYS \
trigger a ping, and the default lookback window.

Rules:
- Ask only what genuinely improves the design. Quality over quantity.
- Ask at most %(max_q)d questions this step. Prefer option-based questions (chips) the user \
  can click; add a free-text question only when options can't capture the answer.
- Do NOT repeat anything already answered. Build on prior answers.
- When you have enough to design a great pack, set "done": true with an empty "questions" array.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "questions": [
    {"id": "snake_case_id", "prompt": "the question", "kind": "single"|"multi"|"text",
     "options": ["A", "B"], "allow_custom": true}
  ],
  "done": false,
  "note": "one short sentence shown above the questions (optional)"
}
"""

GENERATE_PROMPT = """\
You write COMPLETE definitions for "AI Insight Packs". Given the design interview (goal + Q&A), \
the REAL catalog of data sources, and the deterministic security flag codes available, produce \
the final pack.

A pack is SCOPE-AGNOSTIC: it is later pointed at a tenant / subscription / workload. Write the \
`instructions` as scope-neutral prose using the placeholders `{{scope_label}}` and \
`{{lookback_hours}}` (e.g. "Review the last {{lookback_hours}} hours of changes for \
{{scope_label}}..."). The instructions tell the reasoning LLM what to prioritize, what to treat \
as noise, and — critically — when to conclude "nothing_notable".

Choose `sources` ONLY from the provided catalog ids. Choose `always_notify_if` ONLY from the \
provided flag codes (these fire a notification regardless of the AI's verdict — use them for \
things that must never be missed). Set `notify_threshold` to how eager it should be: \
"urgent" (only real emergencies), "notable" (worth a glance), or "nothing_notable" (basically \
always notify).

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "name": "Concise Title Case Name",
  "icon": "a single emoji",
  "category": "security"|"change"|"identity"|"cost"|"operations"|"general",
  "description": "one sentence describing what it watches",
  "sources": ["change_explorer"],
  "supported_scopes": ["workload", "subscription", "tenant"],
  "lookback_hours": 24,
  "filters": {"categories": [], "operations": [], "min_risk": "low"|"medium"|"high"},
  "materiality": {"notify_threshold": "notable", "always_notify_if": []},
  "output": {"format": ["bullets", "table"]},
  "instructions": "scope-neutral markdown instructions with {{scope_label}}/{{lookback_hours}}",
  "summary": "one or two sentences describing the pack you designed"
}
"""


def _transcript(goal: str, answers: list[dict[str, Any]]) -> str:
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


async def _complete_json(system: str, user: str) -> Any:
    from app.agent.factory import build_provider

    provider = build_provider()
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


async def next_questions(goal: str, answers: list[dict[str, Any]], step: int) -> dict[str, Any]:
    if step >= MAX_INTERVIEW_STEPS:
        return {"questions": [], "done": True, "note": ""}
    system = INTERVIEW_PROMPT.replace("%(max_q)d", str(MAX_QUESTIONS_PER_STEP))
    parsed = await _complete_json(system, _transcript(goal, answers))
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
            questions.append({
                "id": str(q.get("id") or f"q{len(questions)+1}")[:60],
                "prompt": str(q["prompt"])[:300],
                "kind": kind,
                "options": [str(o)[:120] for o in opts][:8],
                "allow_custom": bool(q.get("allow_custom", kind == "text")),
            })
    done = bool(parsed.get("done")) or not questions
    return {"questions": questions, "done": done, "note": str(parsed.get("note", ""))[:200]}


async def generate_pack(goal: str, answers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Generate a complete pack draft, grounded in the real source + flag catalogs."""
    src_lines = [f"- {s['id']}: {s.get('description', s.get('label', ''))}" for s in sources.SOURCE_CATALOG]
    flag_lines = [f"- {f['code']}: {f['label']}" for f in FLAG_CODES]
    user = (
        f"{_transcript(goal, answers)}\n\n"
        f"Available data sources (choose ONLY these ids):\n" + "\n".join(src_lines) +
        f"\n\nAvailable always_notify_if flag codes (choose ONLY these):\n" + "\n".join(flag_lines)
    )
    parsed = await _complete_json(GENERATE_PROMPT, user)
    if not isinstance(parsed, dict) or not parsed.get("instructions"):
        return None
    # Ground the model's choices to real ids.
    chosen_sources = [s for s in (parsed.get("sources") or []) if s in _VALID_SOURCES] or ["change_explorer"]
    materiality = parsed.get("materiality") or {}
    always = [c for c in (materiality.get("always_notify_if") or []) if c in _VALID_FLAGS]
    draft = {
        "id": "",
        "name": parsed.get("name") or "New Insight Pack",
        "icon": str(parsed.get("icon") or "🧠")[:4],
        "category": parsed.get("category") or "general",
        "description": parsed.get("description") or "",
        "sources": chosen_sources,
        "supported_scopes": parsed.get("supported_scopes") or ["workload", "subscription", "tenant"],
        "lookback_hours": parsed.get("lookback_hours") or 24,
        "filters": parsed.get("filters") or {},
        "materiality": {"notify_threshold": materiality.get("notify_threshold") or "notable",
                        "always_notify_if": always},
        "output": parsed.get("output") or {"format": ["bullets", "table"]},
        "instructions": str(parsed["instructions"])[:20000],
    }
    return {"draft": packfile.normalize(draft), "summary": str(parsed.get("summary", ""))[:400]}

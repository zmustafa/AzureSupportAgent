"""Admin-editable AI prompts used behind the scenes (Autopilot, Workbook AI'fication).

Each prompt is split into an EDITABLE "guidance" part (persona + heuristics the admin may
tune) and a LOCKED "output contract" part (the strict JSON-shape instruction that keeps
parsing working). Only the guidance is user-editable; the contract is appended at runtime.

Defaults live in code so "Restore original" is always reliable. Admin overrides persist as
JSON under backend/.data, mirroring app_settings — read on each use, no restart needed.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "ai_prompts.json"


# --- Shipped defaults -------------------------------------------------------
# Each entry: id -> {label, description, group, kind, default, contract?, lower?}
# kind: "guidance" (a system-prompt body with a locked contract appended) |
#       "list"     (a comma/newline list of values; lower=False preserves case/order).
#
# The registry is assembled lazily (and cached) so it can derive its editable
# defaults from the prompt constants that live next to the code that uses them
# (chat agent, deep investigation, agent builder) without import cycles.


def _split(text: str, marker: str) -> tuple[str, str]:
    """Split `text` at the first `marker`: returns (before, marker_and_after)."""
    idx = text.find(marker)
    if idx == -1:
        return text.rstrip(), ""
    return text[:idx].rstrip(), text[idx:].strip()


def _extract(text: str, line: str) -> tuple[str, str]:
    """Remove an inline instruction `line` from `text`; return (remaining, line)."""
    return text.replace(line, "").strip(), line


@lru_cache(maxsize=1)
def _registry() -> dict[str, dict[str, Any]]:
    """The full prompt registry. Lazy + cached: the source modules import THIS module,
    so we import them at call time (after startup) to avoid circular imports."""
    from app.agent.deep_investigation import (
        DEEP_CONCLUSION_GUIDANCE,
        DEEP_RESEARCH_GUIDANCE,
        DEEP_VALIDATION_GUIDANCE,
    )
    from app.agent.prompts import (
        PROPOSE_PROBLEMS_PROMPT,
        SCOPE_CLARIFY_MG_PROMPT,
        SCOPE_CLARIFY_PROMPT,
        STARTER_SUGGESTIONS,
        SUGGESTION_SYSTEM_PROMPT,
        SYSTEM_PROMPT,
        TITLE_SYSTEM_PROMPT,
    )
    from app.automations.agent_designer import (
        ENHANCE_GENERATE_PROMPT,
        ENHANCE_INTERVIEW_PROMPT,
        GENERATE_PROMPT,
        INTERVIEW_PROMPT,
    )
    from app.playbooks.designer import (
        GENERATE_PROMPT as PB_GENERATE_PROMPT,
    )
    from app.playbooks.designer import (
        INTERVIEW_PROMPT as PB_INTERVIEW_PROMPT,
    )
    from app.workbooks.designer import (
        ENHANCE_GENERATE_PROMPT as WB_ENHANCE_GENERATE_PROMPT,
    )
    from app.workbooks.designer import (
        ENHANCE_INTERVIEW_PROMPT as WB_ENHANCE_INTERVIEW_PROMPT,
    )
    from app.workbooks.designer import (
        GENERATE_PROMPT as WB_GENERATE_PROMPT,
    )
    from app.workbooks.designer import (
        INTERVIEW_PROMPT as WB_INTERVIEW_PROMPT,
    )
    from app.architectures.designer import (
        ENHANCE_PROMPT as ARCH_ENHANCE_PROMPT,
    )
    from app.architectures.designer import (
        GENERATE_PROMPT as ARCH_GENERATE_PROMPT,
    )

    # Split each shipped prompt into an editable "guidance" body and a LOCKED
    # output/safety contract (appended verbatim at runtime) so edits can't break
    # parsing or relax safety rules.
    sys_body, sys_safety = _split(SYSTEM_PROMPT, "Safety rules (non-negotiable):")
    sug_body, sug_tail = _split(SUGGESTION_SYSTEM_PROMPT, "- Output ONLY")
    title_body, title_tail = _split(TITLE_SYSTEM_PROMPT, "- Output ONLY")
    scope_body, scope_ans = _extract(
        SCOPE_CLARIFY_PROMPT, "Answer with a single word: NEEDS_SUBSCRIPTION or OK."
    )
    mg_rest, mg_ans = _extract(
        SCOPE_CLARIFY_MG_PROMPT, "Answer with a single word: NEEDS_MANAGEMENT_GROUP or OK."
    )
    mg_body, mg_only = _extract(mg_rest, "Output ONLY the single word.")
    propose_body, propose_rules = _split(PROPOSE_PROBLEMS_PROMPT, "Rules:")
    int_body, int_contract = _split(INTERVIEW_PROMPT, "Respond with ONLY a JSON object")
    gen_body, gen_contract = _split(GENERATE_PROMPT, "Respond with ONLY a JSON object")
    eint_body, eint_contract = _split(ENHANCE_INTERVIEW_PROMPT, "Respond with ONLY a JSON object")
    egen_body, egen_contract = _split(ENHANCE_GENERATE_PROMPT, "Respond with ONLY a JSON object")

    # Workbook & Playbook designers (same guidance/contract split pattern).
    wbint_body, wbint_contract = _split(WB_INTERVIEW_PROMPT, "Respond with ONLY a JSON object")
    wbgen_body, wbgen_contract = _split(WB_GENERATE_PROMPT, "Respond with ONLY a JSON object")
    wbeint_body, wbeint_contract = _split(WB_ENHANCE_INTERVIEW_PROMPT, "Respond with ONLY a JSON object")
    wbegen_body, wbegen_contract = _split(WB_ENHANCE_GENERATE_PROMPT, "Respond with ONLY a JSON object")
    pbint_body, pbint_contract = _split(PB_INTERVIEW_PROMPT, "Respond with ONLY a JSON object")
    pbgen_body, pbgen_contract = _split(PB_GENERATE_PROMPT, "Respond with ONLY a JSON object")

    # Architecture designer (generate from a workload's resource dump + enhance).
    archgen_body, archgen_contract = _split(ARCH_GENERATE_PROMPT, "Respond with ONLY a JSON object")
    archenh_body, archenh_contract = _split(ARCH_ENHANCE_PROMPT, "Respond with ONLY a JSON object")

    _g_workloads = "Azure Workloads (Autopilot)"
    _g_workbooks = "Workbooks (AI'fication)"
    _g_chat = "Chat Agent"
    _g_deep = "Deep Investigation"
    _g_builder = "AI Agent Builder"
    _g_wbpb_builder = "Workbook & Playbook Builder"
    _g_arch = "Architecture Builder"

    return {
        # ===================== Azure Workloads (Autopilot) =====================
        "workload_discovery_guidance": {
            "label": "Workload discovery — guidance",
            "group": _g_workloads,
            "kind": "guidance",
            "description": (
                "How Autopilot groups a subscription's resources into distinct workloads. "
                "Tune the persona and the grouping heuristics. The JSON output format is fixed "
                "and appended automatically — you can't break it."
            ),
            "default": (
                "You are an Azure solutions architect. You are given a flat list of Azure "
                "resources (each with an index, name, type, resource group and key tags). Group "
                "them into distinct WORKLOADS — a workload is one application/product/solution a "
                "customer runs (e.g. a checkout app made of VMs, a database, an app gateway, a "
                "key vault and storage). Use these signals, in priority order: (1) explicit tags "
                "like app/workload/project/component/system; (2) naming conventions and shared "
                "prefixes; (3) resource group boundaries; (4) typical multi-tier app composition. "
                "Every resource should belong to exactly one workload; if some truly don't fit, "
                "omit them."
            ),
            "contract": (
                "Reply with ONLY a JSON object of this shape (no prose, no code fence):\n"
                '{"workloads": [{"name": "<short name>", "description": "<1 sentence>", '
                '"reasoning": "<why these belong together, citing the tags/names/RGs>", '
                '"confidence": <0.0-1.0>, "members": [<indices>]}]}'
            ),
        },
        "workload_discovery_tag_signals": {
            "label": "Workload discovery — tag signals",
            "group": _g_workloads,
            "kind": "list",
            "description": (
                "Resource-tag keys treated as strong workload-grouping signals (case-insensitive). "
                "One per line or comma-separated."
            ),
            "default": (
                "app, application, workload, project, service, component, system, env, "
                "environment, product, team, costcenter, cost-center"
            ),
        },
        # ===================== Workbooks (AI'fication) =====================
        "workbook_aify_guidance": {
            "label": "Workbook summarization — guidance",
            "group": _g_workbooks,
            "kind": "guidance",
            "description": (
                "How a workbook's raw command/query output is summarized, scored for severity, "
                "and extracted into structured fields. The JSON output shape is computed per "
                "workbook and appended automatically."
            ),
            "default": (
                "You are an Azure operations analyst. You are given the raw output of an Azure "
                "command or Resource Graph query run for a saved 'workbook'. Analyze it and reply "
                "with ONLY a single JSON object (no prose, no code fence) of exactly this shape:"
            ),
            "contract": "Be decisive and factual; base everything strictly on the provided output.",
        },
        # ===================== Chat Agent =====================
        "chat_system_prompt": {
            "label": "Main agent — system prompt",
            "group": _g_chat,
            "kind": "guidance",
            "description": (
                "The master persona and methodology used on every chat turn (and as the base for "
                "Deep Investigation). Tune behavior, tone and how it uses Azure tools. The "
                "non-negotiable safety rules are locked and always appended."
            ),
            "default": sys_body,
            "contract": sys_safety,
        },
        "chat_suggestions": {
            "label": "Follow-up suggestions",
            "group": _g_chat,
            "kind": "guidance",
            "description": (
                "Generates the 4 clickable follow-up suggestion chips after each answer."
            ),
            "default": sug_body,
            "contract": sug_tail,
        },
        "chat_title": {
            "label": "Chat title",
            "group": _g_chat,
            "kind": "guidance",
            "description": "Summarizes the user's first message into a short sidebar title.",
            "default": title_body,
            "contract": title_tail,
        },
        "chat_scope_subscription": {
            "label": "Scope check — subscription",
            "group": _g_chat,
            "kind": "guidance",
            "description": (
                "Decides whether a question needs the user to pick a subscription before the "
                "agent runs. Must answer with a single word; that rule is locked."
            ),
            "default": scope_body,
            "contract": scope_ans,
        },
        "chat_scope_mgmt": {
            "label": "Scope check — management group",
            "group": _g_chat,
            "kind": "guidance",
            "description": (
                "Decides whether a governance/org-wide question needs the user to pick a "
                "management group first. Must answer with a single word; that rule is locked."
            ),
            "default": mg_body,
            "contract": f"{mg_ans}\n\n{mg_only}",
        },
        "chat_propose_problems": {
            "label": "Enhance the question — problem proposals",
            "group": _g_chat,
            "kind": "guidance",
            "description": (
                "Turns a vague question into sharper, well-scoped problem statements drawn from "
                "the common-problems catalog. The output format is locked."
            ),
            "default": propose_body,
            "contract": propose_rules,
        },
        "chat_starter_suggestions": {
            "label": "Empty-chat starter prompts",
            "group": _g_chat,
            "kind": "list",
            "lower": False,
            "description": (
                "The starter buttons shown on an empty chat. One per line; order is preserved."
            ),
            "default": "\n".join(STARTER_SUGGESTIONS),
        },
        # ===================== Deep Investigation =====================
        "deep_research_guidance": {
            "label": "Research phase — guidance",
            "group": _g_deep,
            "kind": "guidance",
            "description": (
                "How the investigation gathers evidence in the research phase. The JSON output "
                "(summary + hypotheses, with its limits) is fixed and appended automatically."
            ),
            "default": DEEP_RESEARCH_GUIDANCE,
            "contract": (
                "When you have gathered enough, STOP calling tools and output ONLY a JSON object: "
                '{"summary": "...", "hypotheses": [{"title": "...", "description": "..."}]}. '
                "Provide 2-3 distinct root-cause hypotheses, most likely first."
            ),
        },
        "deep_validation_guidance": {
            "label": "Validation phase — guidance",
            "group": _g_deep,
            "kind": "guidance",
            "description": (
                "How the investigation confirms or rules out a single hypothesis. The JSON "
                "verdict shape is fixed and appended automatically."
            ),
            "default": DEEP_VALIDATION_GUIDANCE,
            "contract": (
                'When done, output ONLY JSON: {"verdict": "validated|invalidated|inconclusive", '
                '"evidence": "...", "subhypotheses": [{"title": "...", "description": "..."}]}.'
            ),
        },
        "deep_conclusion_guidance": {
            "label": "Conclusion phase — guidance",
            "group": _g_deep,
            "kind": "guidance",
            "description": (
                "How the final answer is written. The trailing machine-readable conclusion JSON "
                "(root cause, severity, evidence, actions) is fixed and appended automatically."
            ),
            "default": DEEP_CONCLUSION_GUIDANCE,
            "contract": (
                "After the answer, output a sentinel line then a small JSON object: "
                '{"root_cause": "...", "summary": "...", "severity": '
                '"info|warning|error|critical", "evidence": [...], "actions": [...]}.'
            ),
        },
        # ===================== AI Agent Builder =====================
        "designer_interview": {
            "label": "Agent builder — interview",
            "group": _g_builder,
            "kind": "guidance",
            "description": (
                "How the 'Generate with AI' wizard interviews the user when designing a new "
                "sub agent. The JSON question schema is locked. The token %(max_q)d is "
                "replaced with the max questions per step."
            ),
            "default": int_body,
            "contract": int_contract,
        },
        "designer_generate": {
            "label": "Agent builder — generate",
            "group": _g_builder,
            "kind": "guidance",
            "description": (
                "How a complete new sub-agent definition is written from the interview. The "
                "JSON output schema is locked."
            ),
            "default": gen_body,
            "contract": gen_contract,
        },
        "designer_enhance_interview": {
            "label": "Agent builder — enhance interview",
            "group": _g_builder,
            "kind": "guidance",
            "description": (
                "How the wizard interviews the owner when improving an EXISTING agent. The JSON "
                "question schema is locked. The token %(max_q)d is replaced at runtime."
            ),
            "default": eint_body,
            "contract": eint_contract,
        },
        "designer_enhance_generate": {
            "label": "Agent builder — enhance generate",
            "group": _g_builder,
            "kind": "guidance",
            "description": (
                "How an existing agent is rewritten into a production-grade definition. The JSON "
                "output schema is locked."
            ),
            "default": egen_body,
            "contract": egen_contract,
        },
        # ===================== Workbook & Playbook Builder =====================
        "workbook_designer_interview": {
            "label": "Workbook builder — interview",
            "group": _g_wbpb_builder,
            "kind": "guidance",
            "description": (
                "How the 'Generate with AI' wizard interviews the user when designing a new "
                "workbook. The JSON question schema is locked. %(max_q)d is the max questions "
                "per step."
            ),
            "default": wbint_body,
            "contract": wbint_contract,
        },
        "workbook_designer_generate": {
            "label": "Workbook builder — generate",
            "group": _g_wbpb_builder,
            "kind": "guidance",
            "description": (
                "How a complete workbook (runtime, body, params, AI'fy/alert/tile) is written "
                "from the interview. The JSON output schema is locked."
            ),
            "default": wbgen_body,
            "contract": wbgen_contract,
        },
        "workbook_designer_enhance_interview": {
            "label": "Workbook builder — enhance interview",
            "group": _g_wbpb_builder,
            "kind": "guidance",
            "description": (
                "How the wizard interviews to improve an EXISTING workbook. The JSON question "
                "schema is locked. %(max_q)d is replaced at runtime."
            ),
            "default": wbeint_body,
            "contract": wbeint_contract,
        },
        "workbook_designer_enhance_generate": {
            "label": "Workbook builder — enhance generate",
            "group": _g_wbpb_builder,
            "kind": "guidance",
            "description": (
                "How an existing workbook is rewritten into a production-grade definition. The "
                "JSON output schema is locked."
            ),
            "default": wbegen_body,
            "contract": wbegen_contract,
        },
        "playbook_designer_interview": {
            "label": "Playbook builder — interview",
            "group": _g_wbpb_builder,
            "kind": "guidance",
            "description": (
                "How the wizard interviews the user when designing a new playbook (chain of "
                "workbooks). The JSON question schema is locked. %(max_q)d is replaced at runtime."
            ),
            "default": pbint_body,
            "contract": pbint_contract,
        },
        "playbook_designer_generate": {
            "label": "Playbook builder — generate",
            "group": _g_wbpb_builder,
            "kind": "guidance",
            "description": (
                "How a complete playbook (ordered steps referencing existing workbooks, with "
                "param-mapping and severity gates) is written. The JSON output schema is locked."
            ),
            "default": pbgen_body,
            "contract": pbgen_contract,
        },
        # ===================== Architecture Builder =====================
        "architecture_generate": {
            "label": "Architecture — reverse-engineer from workload",
            "group": _g_arch,
            "kind": "guidance",
            "description": (
                "How the AI reverse-engineers an application architecture (nodes, edges, "
                "groups) from a workload's resource inventory and full Azure Resource Graph "
                "properties. The JSON output schema is locked."
            ),
            "default": archgen_body,
            "contract": archgen_contract,
        },
        "architecture_enhance": {
            "label": "Architecture — enhance an existing diagram",
            "group": _g_arch,
            "kind": "guidance",
            "description": (
                "How the AI refines an existing architecture diagram per an instruction, "
                "grounded on the real resource inventory. The JSON output schema is locked."
            ),
            "default": archenh_body,
            "contract": archenh_contract,
        },
    }


def _read() -> dict[str, str]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, str)}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(overrides: dict[str, str]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(overrides, indent=2), encoding="utf-8")


def get_guidance(prompt_id: str) -> str:
    """The active guidance text for a prompt (admin override, else shipped default)."""
    spec = _registry().get(prompt_id)
    if spec is None:
        return ""
    override = _read().get(prompt_id)
    text = override if isinstance(override, str) and override.strip() else spec["default"]
    return text


def get_full_prompt(prompt_id: str) -> str:
    """Guidance + the locked output contract (what the model actually receives)."""
    spec = _registry().get(prompt_id) or {}
    body = get_guidance(prompt_id)
    contract = spec.get("contract", "")
    return f"{body}\n{contract}".rstrip() if contract else body


def get_contract(prompt_id: str) -> str:
    """The locked output contract for a prompt (empty string if none)."""
    spec = _registry().get(prompt_id) or {}
    return spec.get("contract", "")


def get_list(prompt_id: str) -> list[str]:
    """A list-kind prompt parsed into de-duplicated items. Lowercased unless the spec
    sets lower=False (case/order preserved, e.g. starter suggestions)."""
    spec = _registry().get(prompt_id) or {}
    lower = spec.get("lower", True)
    raw = get_guidance(prompt_id)
    items: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace("\n", ",").split(","):
        v = chunk.strip()
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(key if lower else v)
    return items


def list_prompts() -> list[dict[str, Any]]:
    """All prompts with their current/default values for the Settings editor."""
    overrides = _read()
    out: list[dict[str, Any]] = []
    for pid, spec in _registry().items():
        current = overrides.get(pid)
        is_overridden = isinstance(current, str) and current.strip() != "" and current != spec["default"]
        out.append(
            {
                "id": pid,
                "label": spec["label"],
                "group": spec["group"],
                "kind": spec["kind"],
                "description": spec["description"],
                "current": current if isinstance(current, str) and current.strip() else spec["default"],
                "default": spec["default"],
                "contract": spec.get("contract", ""),
                "is_overridden": is_overridden,
            }
        )
    return out


def save_prompts(values: dict[str, str]) -> None:
    """Persist overrides. An empty/blank/default value clears the override (back to default)."""
    reg = _registry()
    overrides = _read()
    for pid, text in values.items():
        if pid not in reg:
            continue
        if not isinstance(text, str) or not text.strip() or text == reg[pid]["default"]:
            overrides.pop(pid, None)
        else:
            overrides[pid] = text
    _write(overrides)


def reset_prompt(prompt_id: str) -> bool:
    """Drop the override for one prompt (restore the shipped default)."""
    if prompt_id not in _registry():
        return False
    overrides = _read()
    if prompt_id in overrides:
        del overrides[prompt_id]
        _write(overrides)
    return True

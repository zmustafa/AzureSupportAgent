"""AI generator for an FMEA (Failure Mode and Effects Analysis) document.

Transforms an Architecture Memory (the authoritative technical source) + the workload's real
Azure scope + posture evidence into one or more FMEA tables — the way a reliability engineer
would enumerate how each subsystem can fail, how bad it is, how often, and whether we'd catch
it. Grounded on the memory; emits ``⟦TODO⟧`` for anything a human must supply (owners, due
dates) instead of inventing it. The server ALWAYS recomputes RPN — the model only proposes
the three 1-10 factors.

Mirrors ``app.knowme.generator``: a plain JSON completion via provider.stream +
loads_tolerant, with a generous max_tokens so the multi-table JSON returns whole.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from app.agent.factory import build_provider
from app.core.utils import loads_tolerant
from app.knowme import sections as km

logger = logging.getLogger("app.fmea.generator")


SYSTEM_PROMPT = """\
You are "FMEA Engineer", an assistant that converts an Architecture Memory for a single
Azure workload into a Failure Mode and Effects Analysis (FMEA) — a structured risk worksheet
a reliability/operations team uses to find and prioritise how the system can fail.

You produce one or more TABLES. Create a separate table per major subsystem, tier, or
process step you can identify from the memory (e.g. "Ingress & Front Door", "App tier",
"Data tier", "Identity & secrets", "Observability"). Each table has ROWS; each row is one
failure mode scored on three factors from 1 (best) to 10 (worst):
  - severity:   how bad the IMPACT is if this failure occurs.
  - occurrence: how LIKELY/FREQUENT this failure is.
  - detection:  how hard it is to DETECT before it causes impact (10 = nearly undetectable;
                strong monitoring/alerts lower this number).

Row columns:
  - item:                 the System / Item / Process Step (a resource, tier, or step).
  - function:             its primary function / responsibility.
  - failure_mode:         how it could potentially fail.
  - effects:              the consequential effect of that failure on the system/users.
  - causes:               the contributing cause(s).
  - control_prevention:   current controls that PREVENT the cause (design, redundancy).
  - control_detection:    current controls that DETECT the failure (alerts, health checks).
  - recommended_actions:  steps to reduce severity, occurrence, or improve detection.
  - owner:                who is responsible — ALWAYS ⟦TODO: Owner | key=owner⟧ (never invent).
  - date_due:             target date — ALWAYS ⟦TODO: Target date | key=date_due⟧.
  - severity, occurrence, detection: integers 1-10.

GROUNDING RULES:
1. Every failure mode, effect, cause and control must trace to the Architecture Memory, the
   REAL AZURE SCOPE block, or the POSTURE EVIDENCE block. Do not invent components.
2. Use the EXACT resource names/types from the scope/memory in ``item``.
3. NEVER fabricate people, emails, dates, or SLAs. Owners and due dates are ⟦TODO⟧ tokens.
4. Map evidence sensibly: a failed assessment finding raises occurrence and/or lowers the
   quality of detection; strong monitoring coverage lowers detection (easier to catch);
   a high-severity finding raises severity. Keep scores defensible from the evidence.
5. Prefer 4-10 distinct, high-signal rows per table over many trivial ones. Lead with the
   highest-risk failure modes (single points of failure, missing redundancy, weak detection).
6. If the memory is thin for a subsystem, still produce its most obvious failure modes but
   keep scores conservative and note the assumption in ``causes``.

STYLE: short, declarative, resource-first. No prose outside the JSON.

Respond with ONLY a JSON object of this EXACT shape (no prose, no code fence):
{"tables": [
  {"name": "<subsystem>", "scope_ref": "<resource group or service, optional>",
   "rows": [
     {"item": "...", "function": "...", "failure_mode": "...", "effects": "...",
      "causes": "...", "control_prevention": "...", "control_detection": "...",
      "recommended_actions": "...", "owner": "⟦TODO: Owner | key=owner⟧",
      "date_due": "⟦TODO: Target date | key=date_due⟧",
      "severity": 1-10, "occurrence": 1-10, "detection": 1-10}
   ]}
], "confidence": 0.0-1.0}
"""


def _memory_block(memory: dict[str, Any]) -> str:
    """Render the architecture memory's filled sections as the authoritative source."""
    lines: list[str] = []
    for s in memory.get("sections", []) or []:
        content = str(s.get("content") or "").strip()
        if not content:
            continue
        label = s.get("label") or km.section_label(str(s.get("key", "")))
        lines.append(f"### {label}\n{content}")
    return "\n\n".join(lines) or "(the architecture memory has no filled sections)"


async def generate_fmea(
    *,
    workload_name: str,
    memory: dict[str, Any],
    facts: dict[str, Any],
    progress: Callable[[str, str], Awaitable[None]] | None = None,
    extra_context: str = "",
    evidence_block: str = "",
    focus: str = "",
    two_pass: bool = True,
) -> dict[str, Any] | None:
    """Draft FMEA tables from the architecture memory + real scope + posture evidence, then
    (optionally) run a verification pass that keeps scores defensible and grounded.

    Returns ``{"tables": [...], "confidence": float, "passes": int}`` or None. The caller
    recomputes every RPN server-side — the model's factor numbers are proposals only.
    """
    system = SYSTEM_PROMPT
    blocks = [km.scope_facts_block(facts)]
    if evidence_block.strip():
        blocks.append(evidence_block.strip())
    if focus.strip():
        blocks.append(f"FOCUS (generate a table specifically for this subsystem): {focus.strip()[:200]}")
    if extra_context.strip():
        blocks.append(
            "ADDITIONAL HUMAN-PROVIDED CONTEXT (treat as authoritative; fold relevant facts "
            "into the analysis):\n" + extra_context.strip()[:8000]
        )
    user = (
        f"Workload: {workload_name or '(unnamed)'}\n\n"
        + "\n\n".join(blocks)
        + f"\n\nARCHITECTURE MEMORY (authoritative technical source):\n{_memory_block(memory)}"
    )

    # ---- Pass 1: draft ----
    if progress is not None:
        await progress("pass", "✏️ Pass 1 of 2 — enumerating failure modes from memory, scope & evidence…")
    text = await _stream_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        progress, compose_msg="🤖 Pass 1 — the model is building the FMEA tables…", pass_label="Pass 1/2",
    )
    draft = parse_completion(text)
    if draft is None:
        return None
    draft["passes"] = 1
    if not two_pass:
        return draft

    # ---- Pass 2: verify scoring + grounding ----
    if progress is not None:
        await progress("pass", "🔎 Pass 2 of 2 — verifying every failure mode is grounded & scored defensibly…")
    draft_json = json.dumps({"tables": draft.get("tables", [])}, ensure_ascii=False)
    review_user = (
        "Here is the DRAFT FMEA you produced (JSON). Review and RETURN A CORRECTED FULL JSON "
        "of the same shape. Apply these checks:\n"
        "1. GROUNDING: every failure mode / effect / cause / control must trace to the memory, "
        "scope or evidence below. Delete anything ungrounded.\n"
        "2. SCORING: severity/occurrence/detection must each be 1-10 and defensible from the "
        "evidence (failed findings raise occurrence/severity; strong monitoring lowers "
        "detection). Fix implausible scores.\n"
        "3. NO FABRICATION: owner and date_due MUST be ⟦TODO⟧ tokens — never invented values.\n"
        "4. COMPLETENESS: keep at least the highest-risk rows per table; merge duplicates.\n\n"
        "CONTEXT (the same authoritative sources):\n" + "\n\n".join(blocks) + "\n\n"
        "DRAFT JSON:\n" + draft_json
    )
    review_text = await _stream_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": review_user}],
        progress, compose_msg="🤖 Pass 2 — verifying grounding & refining scores…", pass_label="Pass 2/2",
    )
    refined = parse_completion(review_text)
    if refined is not None and refined.get("tables"):
        if refined.get("confidence") is None:
            refined["confidence"] = draft.get("confidence")
        refined["passes"] = 2
        return refined
    logger.info("FMEA pass 2 did not parse; returning pass-1 draft.")
    return draft


async def _stream_completion(
    messages: list[dict[str, str]],
    progress: Callable[[str, str], Awaitable[None]] | None,
    *,
    compose_msg: str,
    pass_label: str = "",
) -> str:
    """Stream one provider completion, emitting rich live progress: the current table's name
    as it appears, and a running count of failure-mode rows drafted so far."""
    provider = build_provider()
    text = ""
    first_token = False
    seen_tables = 0
    last_name = ""
    last_rows = 0
    prefix = f"{pass_label} · " if pass_label else ""
    # Completed ``"name": "<value>"`` and ``"failure_mode": "..."`` matchers (closing quote
    # present, so a half-streamed value isn't reported until it's whole).
    name_re = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')
    row_re = re.compile(r'"failure_mode"\s*:\s*"')
    # Generous output cap: multi-table FMEA JSON is large, and reasoning models spend part of
    # the budget on hidden reasoning tokens — too small a cap truncates the JSON mid-string
    # (the live "could not draft" failure). parse_completion also repairs truncation, but a
    # bigger cap means fewer rows are ever lost.
    async for ev in provider.stream(messages, None, max_tokens=32000):
        if ev.type == "token":
            text += ev.text
            if progress is not None:
                if not first_token:
                    first_token = True
                    await progress("ai", compose_msg)
                names = name_re.findall(text)
                rows = len(row_re.findall(text))
                # A new table started: announce its name.
                if len(names) > seen_tables:
                    seen_tables = len(names)
                    last_name = names[-1].strip() or f"table {seen_tables}"
                    await progress("table", f"🧱 {prefix}Table {seen_tables}: “{last_name}” — analysing failure modes…")
                # More rows landed in the current table: report the running tally.
                elif rows > last_rows and last_name:
                    await progress("row", f"📋 {prefix}“{last_name}” — {rows} failure mode{'s' if rows != 1 else ''} scored…")
                last_rows = rows
    if progress is not None and seen_tables:
        total_rows = len(row_re.findall(text))
        await progress("ai", f"🧱 {prefix}Assembled {seen_tables} table(s) · {total_rows} failure mode(s).")
    return text


def parse_completion(text: str) -> dict[str, Any] | None:
    """Parse an FMEA model completion into ``{"tables": [...], "confidence": float}``.

    Robust against the two ways large multi-table FMEA JSON breaks in practice:
    (1) a prose preamble / ```json fence (stripped), and (2) **truncation** — reasoning
    models routinely hit the output-token cap mid-string, so the raw text is a valid JSON
    *prefix* that abruptly ends. We try a strict tolerant parse first, then repair a
    truncated object by closing its open brackets at the last complete element, then finally
    salvage individual complete table/row objects. A partial FMEA is far better than the
    "could not draft" dead-end the user hit on the live site.
    """
    t = (text or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)

    parsed = loads_tolerant(t)
    if isinstance(parsed, dict) and isinstance(parsed.get("tables"), list):
        return {"tables": parsed["tables"], "confidence": _coerce_conf(parsed.get("confidence"))}

    # ---- repair a TRUNCATED completion (cut the string back to its last complete element
    # and close the open brackets) then re-parse. ----
    repaired = _repair_truncated_json(t)
    if repaired:
        parsed = loads_tolerant(repaired)
        if isinstance(parsed, dict) and isinstance(parsed.get("tables"), list) and parsed["tables"]:
            logger.info("FMEA JSON was truncated; repaired to %d table(s).", len(parsed["tables"]))
            return {"tables": parsed["tables"], "confidence": _coerce_conf(parsed.get("confidence"))}

    # ---- last resort: salvage complete table objects one-by-one from the raw text. ----
    salvaged = _salvage_tables(t)
    if salvaged:
        logger.info("FMEA JSON did not parse strictly; salvaged %d table(s) by scanning.", len(salvaged))
        return {"tables": salvaged, "confidence": None}

    logger.warning(
        "FMEA JSON completion did not parse (raw len=%d): head=%r tail=%r",
        len(text or ""), (text or "")[:200], (text or "")[-200:],
    )
    return None


def _repair_truncated_json(t: str) -> str | None:
    """Best-effort repair of a truncated JSON object: walk the text tracking string state and
    the open-bracket stack, find the last position where a container ('}' or ']') closed, cut
    there, and append the closing brackets for whatever was still open at that point. Turns a
    valid-prefix-but-cut-off completion back into parseable JSON (keeping all complete rows)."""
    start = t.find("{")
    if start < 0:
        return None
    s = t[start:]
    stack: list[str] = []
    in_str = False
    esc = False
    last_idx = -1
    last_stack: list[str] | None = None
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            last_idx = i
            last_stack = list(stack)
    if last_idx < 0 or last_stack is None:
        return None
    head = s[: last_idx + 1]
    closers = "".join("}" if c == "{" else "]" for c in reversed(last_stack))
    return head + closers


def _salvage_tables(t: str) -> list[dict[str, Any]]:
    """Scan the text for complete top-level table objects (each carrying a ``rows`` array) and
    parse each independently, tolerating a truncated final one. Used only when both the strict
    parse and the truncation-repair fail."""
    out: list[dict[str, Any]] = []
    # Find each table by its ``"name"`` key, then capture the balanced {...} object around it.
    for m in re.finditer(r'\{\s*"name"\s*:', t):
        obj = _balanced_object(t, m.start())
        if not obj:
            continue
        parsed = loads_tolerant(obj)
        if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
            out.append(parsed)
    return out


def _balanced_object(t: str, start: int) -> str | None:
    """Return the balanced ``{...}`` substring beginning at ``start`` (which must be a '{'),
    honouring string/escape state, or None if it never closes (truncated)."""
    if start >= len(t) or t[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return None



def _coerce_conf(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

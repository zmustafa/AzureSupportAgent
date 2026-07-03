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

import asyncio
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
    fanout: bool = True,
) -> dict[str, Any] | None:
    """Draft FMEA tables from the architecture memory + real scope + posture evidence, then
    (optionally) run a verification pass that keeps scores defensible and grounded.

    Returns ``{"tables": [...], "confidence": float, "passes": int}`` or None. The caller
    recomputes every RPN server-side — the model's factor numbers are proposals only.

    When ``fanout`` is set and no single-subsystem ``focus`` is requested, a whole-document
    generation is parallelised: one quick planning pass enumerates the subsystems, then each
    subsystem's table is drafted concurrently. Wall time drops from the *sum* of all tables
    (two serial passes) to roughly the *slowest single table*. A single-subsystem ``focus``
    request (per-table regen) always uses the original serial path — it is already one table.
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
    mem_block = _memory_block(memory)
    header = f"Workload: {workload_name or '(unnamed)'}\n\n"
    user = header + "\n\n".join(blocks) + f"\n\nARCHITECTURE MEMORY (authoritative technical source):\n{mem_block}"

    # ---- Fan-out: a whole-document generate is parallelised per subsystem. ----
    if fanout and not focus.strip():
        fan = await _generate_fanout(system, blocks, header, mem_block, progress)
        if fan is not None and fan.get("tables"):
            return fan
        # Planning or every worker failed → fall through to the serial path as a safety net.
        if progress is not None:
            await progress("pass", "↩️ Parallel drafting unavailable — falling back to a single pass…")

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


# ============================================================ fan-out (parallel per-subsystem)
_MAX_FANOUT = 4  # concurrent per-table completions (bounded to respect provider rate limits)

_PLAN_SYSTEM = """\
You are an FMEA planner. From the Architecture Memory, the REAL AZURE SCOPE block and the
POSTURE EVIDENCE block, identify the major subsystems / tiers / process steps that each deserve
their OWN FMEA table (e.g. "Ingress & Front Door", "App tier", "Data tier", "Identity & secrets",
"Observability"). Return 3-8 subsystems, HIGHEST-RISK first. Do NOT enumerate failure modes yet —
only the plan. Use exact resource names/types from the scope/memory in ``scope_ref`` where useful.
Respond with ONLY a JSON object of this EXACT shape (no prose, no code fence):
{"subsystems": [{"name": "<subsystem>", "scope_ref": "<resource group / service, optional>"}]}
"""


async def _generate_fanout(
    system: str,
    blocks: list[str],
    header: str,
    mem_block: str,
    progress: Callable[[str, str], Awaitable[None]] | None,
) -> dict[str, Any] | None:
    """Plan the subsystems, then draft each subsystem's table concurrently and merge. Returns
    ``{"tables": [...], "confidence": float|None, "passes": 1, "mode": "fanout"}`` or None if
    planning yielded nothing (caller falls back to the serial path)."""
    plan = await _plan_subsystems(header, blocks, mem_block, progress)
    if not plan:
        return None

    if progress is not None:
        await progress("pass", f"⚡ Drafting {len(plan)} subsystem table(s) in parallel…")

    sem = asyncio.Semaphore(_MAX_FANOUT)
    lock = asyncio.Lock()
    done = 0
    total = len(plan)

    async def _one(spec: dict[str, str]) -> dict[str, Any] | None:
        nonlocal done
        async with sem:
            table = await _generate_focused_table(system, header, blocks, mem_block, spec)
        async with lock:
            done += 1
            if progress is not None:
                if table and table.get("rows"):
                    n = len(table["rows"])
                    await progress("table", f"✅ {done}/{total} · “{spec['name']}” — {n} failure mode{'s' if n != 1 else ''} scored.")
                else:
                    await progress("table", f"⚠️ {done}/{total} · “{spec['name']}” — no rows produced.")
        return table

    results = await asyncio.gather(*[_one(s) for s in plan], return_exceptions=True)

    tables: list[dict[str, Any]] = []
    confs: list[float] = []
    for spec, res in zip(plan, results):
        if isinstance(res, Exception):
            logger.warning("FMEA fan-out worker failed for %r: %s", spec.get("name"), res)
            continue
        if isinstance(res, dict) and res.get("rows"):
            res["name"] = spec.get("name") or res.get("name") or "Subsystem"
            if spec.get("scope_ref") and not res.get("scope_ref"):
                res["scope_ref"] = spec["scope_ref"]
            c = res.pop("_confidence", None)
            if isinstance(c, (int, float)):
                confs.append(float(c))
            tables.append(res)

    if not tables:
        return None
    if progress is not None:
        total_rows = sum(len(t.get("rows", []) or []) for t in tables)
        await progress("ai", f"⚡ Parallel analysis complete — {len(tables)} table(s) · {total_rows} failure mode(s).")
    confidence = round(sum(confs) / len(confs), 3) if confs else None
    return {"tables": tables, "confidence": confidence, "passes": 1, "mode": "fanout"}


async def _plan_subsystems(
    header: str,
    blocks: list[str],
    mem_block: str,
    progress: Callable[[str, str], Awaitable[None]] | None,
) -> list[dict[str, str]]:
    """Quick, small completion that returns the subsystem plan (names + scope refs), deduped
    and capped at 8. Returns [] if nothing parseable came back."""
    if progress is not None:
        await progress("pass", "🗺️ Planning the subsystems to analyse…")
    user = header + "\n\n".join(blocks) + f"\n\nARCHITECTURE MEMORY (authoritative technical source):\n{mem_block}"
    provider = build_provider()
    parts: list[str] = []
    # Generous cap: reasoning models spend part of the budget on hidden reasoning tokens, so a
    # small cap truncates the (short) plan JSON before it closes.
    async for ev in provider.stream(
        [{"role": "system", "content": _PLAN_SYSTEM}, {"role": "user", "content": user}], None, max_tokens=8000
    ):
        if ev.type == "token":
            parts.append(ev.text)
    raw = "".join(parts)
    obj = _extract_json_obj(raw)
    subs = (obj or {}).get("subsystems") if isinstance(obj, dict) else None
    if not isinstance(subs, list):
        logger.warning("FMEA planning did not parse (raw len=%d) head=%r tail=%r",
                       len(raw), raw[:200], raw[-200:])
        return []
    plan: list[dict[str, str]] = []
    seen: set[str] = set()
    for s in subs:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        plan.append({"name": name[:120], "scope_ref": str(s.get("scope_ref") or "").strip()[:120]})
        if len(plan) >= 8:
            break
    return plan


async def _generate_focused_table(
    system: str,
    header: str,
    blocks: list[str],
    mem_block: str,
    spec: dict[str, str],
) -> dict[str, Any] | None:
    """Draft EXACTLY ONE FMEA table for a single planned subsystem (single pass). Returns the
    table dict (with a transient ``_confidence`` the caller strips) or None."""
    scope_hint = f" (scope: {spec['scope_ref']})" if spec.get("scope_ref") else ""
    focus_block = (
        f"FOCUS: Produce EXACTLY ONE FMEA table for the subsystem \"{spec['name']}\"{scope_hint}. "
        "Enumerate only THIS subsystem's highest-risk failure modes — do not cover other subsystems. "
        "Return the standard shape with a single entry in \"tables\"."
    )
    user = header + "\n\n".join(blocks + [focus_block]) + f"\n\nARCHITECTURE MEMORY (authoritative technical source):\n{mem_block}"
    text = await _stream_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        None, compose_msg="", pass_label=spec["name"],
    )
    parsed = parse_completion(text)
    if not parsed or not parsed.get("tables"):
        return None
    table = parsed["tables"][0]
    if isinstance(table, dict):
        table["_confidence"] = parsed.get("confidence")
        return table
    return None


def _extract_json_obj(text: str) -> dict[str, Any] | None:
    """Strip a ```json fence / prose preamble and parse the first JSON object, tolerantly,
    repairing a truncated tail (reasoning models routinely hit the token cap mid-object)."""
    t = (text or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)
    obj = loads_tolerant(t)
    if isinstance(obj, dict):
        return obj
    repaired = _repair_truncated_json(t)
    if repaired:
        obj = loads_tolerant(repaired)
        if isinstance(obj, dict):
            return obj
    return None


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

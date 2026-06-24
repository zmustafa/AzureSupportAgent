"""AI generator for a Workload Know-Me document.

Transforms an Architecture Memory (the authoritative technical source) plus the workload's
real Azure scope into the labelled Know-Me sections — the way a senior engineer would write
a triage-ready reference for a responder. Grounded on the memory; fills subscriptions/RGs
from real scope; emits ``⟦TODO⟧`` for anything a human must supply (people, coverage,
escalation routing, SLAs) instead of inventing it.

Mirrors ``architectures.memory_designer``: a plain JSON completion via provider.stream +
safe_json_parse, with a generous max_tokens so the multi-section JSON returns whole.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from app.agent.factory import build_provider
from app.core.utils import loads_tolerant
from app.knowme import sections as km

logger = logging.getLogger("app.knowme.generator")

# Per-section status lines streamed to the UI as the model writes each section.
_SECTION_PROGRESS: dict[str, str] = {
    "overview": "🧭 Writing the workload overview…",
    "solution_architecture": "🏗️ Describing the solution & happy-path flow…",
    "services_scope": "🧩 Enumerating Azure services in scope…",
    "subscriptions_resources": "🗂️ Filling subscriptions & resources…",
    "diagnostics_triage": "🔎 Building the first-look triage runbook…",
    "thresholds_slis": "🌡️ Listing thresholds, SLIs & monitoring…",
    "resiliency_dr": "♻️ Assessing resiliency & DR posture…",
    "known_issues": "⚠️ Prioritizing known issues & callouts…",
    "security_posture": "🛡️ Summarizing the security posture…",
    "support_handling": "📓 Capturing support & escalation handling…",
    "contacts": "📇 Laying out the contacts table…",
    "data_compliance_cost": "💾 Noting data, compliance & cost…",
    "todo_checklist": "✅ Assembling the human-completion checklist…",
}


SYSTEM_PROMPT = """\
You are "Know-Me Writer", an assistant that converts an Architecture Memory for a single
Azure workload into a support-facing Know-Me document — the reference a support engineer
reads BEFORE triaging a case on that workload, often at 2 a.m. with no prior context.

Your job is to TRANSFORM, not invent. Every technical claim must be grounded in the
supplied Architecture Memory or the REAL AZURE SCOPE block. Everything the memory cannot
know — people, contract data, phone numbers, formal coverage windows, escalation routing,
SLAs, RTO/RPO when not stated — must be emitted as an explicit placeholder token, never
guessed.

PLACEHOLDER GRAMMAR (use EXACTLY this form for any human-required field):
  ⟦TODO: <short label> | key=<field_key>⟧
e.g. ⟦TODO: On-call group + coverage window (UTC) | key=oncall_coverage⟧
Use snake_case field_key values. Reuse the same field_key if the same field recurs.

CHOICE SETS (optional, encouraged): when a field has a small, well-known set of likely
answers, append ``| choices=A; B; C`` so the UI can offer a dropdown instead of a blank box.
Order the most-likely option first; semicolon-separated; ≤6 options. Examples:
  ⟦TODO: Business criticality | key=criticality | choices=Critical; High; Medium; Low⟧
  ⟦TODO: Is geo-redundant backup enabled? | key=geo_backup | choices=Yes; No; Unknown⟧
  ⟦TODO: Storage redundancy | key=redundancy | choices=LRS; ZRS; GRS; RA-GRS⟧
Only add choices you're confident are the realistic options — never for open fields like
names, emails, IDs, or free-form notes.

GROUNDING RULES:
1. NEVER fabricate people, emails, phone numbers, contract/schedule IDs, dates, coverage
   hours, escalation routing, SLAs, or RTO/RPO. Emit ⟦TODO: …⟧ instead.
2. The REAL AZURE SCOPE block is authoritative — use those exact subscription GUIDs,
   resource-group names, regions, and resource names. Do NOT mark them ⟦TODO⟧.
3. Preserve uncertainty. Copy the memory's "not determined / inferred / planned but not
   deployed" qualifiers verbatim. Do not promote an inference to a fact.
4. Resource-name fidelity: use the exact resource names/SKUs from the memory/scope.
5. Lead with triage value. When unsure how deep to go, expand the Diagnostics, Known
   issues, and Thresholds sections — they are what the reader uses live.
6. Flag scope mismatches: if the memory cites IDs from a DIFFERENT workload, surface it as
   a verify-scope warning (⚠), not as in-scope inventory.

STYLE: Short declarative sentences, active voice, resource-first. Use ❌ for high-severity
risks, ⚠ for medium, ⟦TODO⟧ for human-required fields. Use Markdown tables for
components, services, thresholds, and contacts. Keep it skimmable.

SECTION GUIDANCE (fill each; if genuinely nothing to say, write a one-line
"*Not determined from architecture memory — to be completed by a human.*" rather than
leaving it blank):
- overview: 2–5 sentences — what it does, pattern, region, business criticality (carry any
  mixed/critical signals).
- solution_architecture: pattern; the happy-path flow as an ordered resource-named pipeline
  (producer → ingestion → landing → observability); key components & responsibilities
  (table); network topology; identity & secrets. Preserve every inferred/not-determined
  qualifier.
- services_scope: the Azure resource types/services present (Event Hub, Service Bus,
  Storage V2, Key Vault, Log Analytics, VNet/NSG, Public IP, DNS, Managed Identity, …) as
  the supported sub-workload surface area. Mark any whose role is "not determined".
- subscriptions_resources: a table of the REAL subscription GUID(s) + friendly name(s) from
  the scope block, the resource group(s), region, and named resources. Only emit ⟦TODO⟧ if
  the scope block says a subscription GUID is unresolved.
- diagnostics_triage (HIGHEST VALUE): convert the memory's diagnostic hints into an ordered
  "follow the data path" runbook. Each step: what to check → on which resource → what signal
  indicates a problem. Include any shorthand triage chain.
- thresholds_slis: SLIs + where observed (Log Analytics, diagnostic settings). Where no
  numeric target exists, write the SLI and mark the threshold ⟦TODO: define threshold⟧.
  Call out observability gaps.
- resiliency_dr: redundancy (LRS/ZRS/GRS), tier ceilings, single- vs multi-region,
  autoscale, the net read. Carry RTO/RPO/SLO as ⟦TODO⟧ when not determined. Name the
  primary bottleneck/SPOF.
- known_issues: a prioritized list (❌ high / ⚠ medium), each phrased as something a
  responder should know when handling a case. Keep orphaned-resource / scope-mismatch
  caveats.
- security_posture: public-network exposure, shared-key/SAS admin paths, Key Vault
  purge-protection/RBAC state, transport/TLS, DDoS — framed as the blast radius & access
  model, not a full audit.
- support_handling: escalation routing ⟦TODO⟧, on-call group + coverage ⟦TODO⟧; if the
  memory exposes an owner tag/cost-center surface it as an "unverified owner hint" but mark
  formal on-call ⟦TODO⟧; operational runbook notes (note PaaS = no restart), secret rotation.
- contacts: a Markdown table with ONLY ⟦TODO⟧ rows for customer key contacts, support
  contacts, escalation owner, account manager. Do not invent names/emails. (An owner tag
  from the memory may be listed once as an "unverified owner hint".)
- data_compliance_cost: data stores/redundancy/encryption/classification/backup gaps;
  tagging consistency & policy posture (carry "not determined"); idle/orphaned resources,
  uncapped log ingestion, quick savings.
- todo_checklist: a consolidated checklist of EVERY ⟦TODO⟧ a human must supply before this
  Know-Me is "Published" (subscription GUIDs if any, contract/schedule IDs & dates, on-call
  group + coverage hours, all contacts, escalation routing, formal SLAs/RTO/RPO, review).

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{"sections": {"<section_key>": "<markdown content>", ...}, "confidence": 0.0-1.0}
Use ONLY these section keys:
"""


def _catalog_block() -> str:
    return "\n".join(f"- {s['key']}: {s['label']}" for s in km.SECTION_CATALOG)


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


async def generate_know_me(
    *,
    workload_name: str,
    memory: dict[str, Any],
    facts: dict[str, Any],
    progress: Callable[[str, str], Awaitable[None]] | None = None,
    extra_context: str = "",
    known_block: str = "",
    evidence_block: str = "",
    two_pass: bool = True,
) -> dict[str, Any] | None:
    """Draft the Know-Me sections from the architecture memory + real scope facts + (A1)
    platform-known values + (A3) measured posture evidence, then (A4) run a verification /
    refinement pass that removes ungrounded claims and fills thin sections.

    Returns ``{"sections": {key: markdown}, "confidence": float, "passes": int}`` or None.
    The caller parses ⟦TODO⟧ tokens out of the returned sections (see ``sections.parse_todos``).
    """
    system = SYSTEM_PROMPT + _catalog_block()
    blocks = [km.scope_facts_block(facts)]
    if known_block.strip():
        blocks.append(known_block.strip())
    if evidence_block.strip():
        blocks.append(evidence_block.strip())
    if extra_context.strip():
        blocks.append(
            "ADDITIONAL HUMAN-PROVIDED CONTEXT (treat as authoritative; fold relevant facts "
            "into the sections):\n" + extra_context.strip()[:8000]
        )
    user = (
        f"Workload: {workload_name or '(unnamed)'}\n\n"
        + "\n\n".join(blocks)
        + f"\n\nARCHITECTURE MEMORY (authoritative technical source):\n{_memory_block(memory)}"
    )

    # ---- Pass 1: draft ----
    if progress is not None:
        await progress("pass", "✏️ Pass 1 of 2 — drafting the Know-Me from memory, scope & evidence…")
    text = await _stream_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        progress, compose_msg="🤖 Pass 1 — the model is composing the Know-Me…",
    )
    draft = parse_completion(text)
    if draft is None:
        return None
    draft["passes"] = 1
    if not two_pass:
        return draft

    # ---- Pass 2: verify grounding + refine (A4) ----
    if progress is not None:
        await progress("pass", "🔎 Pass 2 of 2 — verifying every claim is grounded & filling gaps…")
    draft_json = json.dumps({"sections": draft.get("sections", {})}, ensure_ascii=False)
    review_user = (
        "Here is the DRAFT Know-Me you produced (JSON). Review and RETURN A CORRECTED FULL "
        "JSON of the same shape. Apply these checks:\n"
        "1. GROUNDING: every technical claim must trace to the memory / scope / evidence "
        "below. Delete or soften anything not grounded.\n"
        "2. NO FABRICATION: any people, emails, phone numbers, SLAs, RTO/RPO, contract data, "
        "coverage windows or escalation routing that is not in the provided facts MUST be a "
        "⟦TODO: <label> | key=<field_key>⟧ token — never an invented value.\n"
        "3. EVIDENCE: fold the measured posture evidence into Diagnostics / Known issues / "
        "Thresholds / Resiliency with specific resource names and numbers.\n"
        "4. COMPLETENESS: fill any section that is thin or empty; keep every section present.\n"
        "5. Preserve the memory's 'not determined / inferred' qualifiers.\n\n"
        "CONTEXT (the same authoritative sources):\n" + "\n\n".join(blocks) + "\n\n"
        "DRAFT JSON:\n" + draft_json
    )
    review_text = await _stream_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": review_user}],
        progress, compose_msg="🤖 Pass 2 — verifying grounding & refining…",
    )
    refined = parse_completion(review_text)
    if refined is not None and refined.get("sections"):
        # Keep pass-2 confidence if present, else carry pass-1.
        if refined.get("confidence") is None:
            refined["confidence"] = draft.get("confidence")
        refined["passes"] = 2
        return refined
    logger.info("Know-Me pass 2 did not parse; returning pass-1 draft.")
    return draft


async def _stream_completion(
    messages: list[dict[str, str]],
    progress: Callable[[str, str], Awaitable[None]] | None,
    *,
    compose_msg: str,
) -> str:
    """Stream one provider completion, emitting a per-section progress line the first time
    each section key appears in the JSON stream."""
    provider = build_provider()
    text = ""
    seen: set[str] = set()
    first_token = False
    async for ev in provider.stream(messages, None, max_tokens=16000):
        if ev.type == "token":
            text += ev.text
            if progress is not None:
                if not first_token:
                    first_token = True
                    await progress("ai", compose_msg)
                for key in km.SECTION_KEYS:
                    if key in seen:
                        continue
                    if re.search(rf'"{re.escape(key)}"\s*:', text):
                        seen.add(key)
                        await progress("section", _SECTION_PROGRESS.get(key, f"✍️ Writing {key}…"))
    if progress is not None and seen:
        await progress("ai", f"🧱 Assembled {len(seen)} sections.")
    return text



def parse_completion(text: str) -> dict[str, Any] | None:
    """Parse a Know-Me model completion into ``{"sections": {...}, "confidence": float}``.

    Tries (1) tolerant JSON parse of the outermost ``{...}`` span (stripping any prose
    preamble or code fence the model added), then (2) a key-delimited salvage that survives
    the unescaped quotes / literal newlines large Markdown values routinely introduce."""
    t = (text or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        # Models sometimes prepend a sentence of prose before the JSON object. Grab the
        # outermost {...} span.
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)
    parsed = loads_tolerant(t)
    if isinstance(parsed, dict) and isinstance(parsed.get("sections"), dict):
        return parsed
    # Salvage: the section values are long Markdown (tables, quotes, ⟦TODO⟧, multi-line),
    # which models frequently emit with characters that make the whole object invalid JSON.
    # The section keys are a closed vocabulary, so extract each section by key boundaries —
    # robust against unescaped quotes/newlines inside a value.
    salvaged = _salvage_sections(t)
    if salvaged:
        logger.info("Know-Me JSON did not parse strictly; salvaged %d sections by key.", len(salvaged))
        conf = None
        cm = re.search(r'"confidence"\s*:\s*([0-9.]+)', t)
        if cm:
            try:
                conf = float(cm.group(1))
            except ValueError:
                conf = None
        return {"sections": salvaged, "confidence": conf}
    logger.warning(
        "Know-Me JSON completion did not parse (raw len=%d): head=%r tail=%r",
        len(text or ""), (text or "")[:200], (text or "")[-200:],
    )
    return None



def _salvage_sections(t: str) -> dict[str, str]:
    """Extract Know-Me section contents from a not-quite-valid JSON completion by using the
    known section keys as delimiters. For each ``"<key>": "<value>"`` we capture the value
    up to the next known key (or the end of the sections object) and JSON-decode just that
    string with ``strict=False`` (tolerating literal newlines/tabs). Returns ``{key: md}``."""
    import json

    keys = km.SECTION_KEYS
    key_alt = "|".join(re.escape(k) for k in keys)
    out: dict[str, str] = {}
    for key in keys:
        # Value runs (lazily) until the next known key, the confidence field, or a closing
        # brace — whichever comes first.
        pat = (
            rf'"{re.escape(key)}"\s*:\s*"(?P<val>.*?)"\s*'
            rf'(?=,\s*"(?:{key_alt})"\s*:|,\s*"confidence"\s*:|\}}\s*,\s*"confidence"|\}}\s*\}}\s*$|\}}\s*$)'
        )
        m = re.search(pat, t, re.DOTALL)
        if not m:
            continue
        raw = m.group("val")
        try:
            val = json.loads('"' + raw + '"', strict=False)
        except (json.JSONDecodeError, ValueError):
            # Last resort: manually unescape the common sequences.
            val = (
                raw.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
                .replace("\\r", "\r").replace("\\/", "/")
            )
        if str(val).strip():
            out[key] = str(val)
    return out


async def generate_section(
    *,
    section_key: str,
    workload_name: str,
    memory: dict[str, Any],
    facts: dict[str, Any],
    current_sections: list[dict[str, Any]] | None = None,
    extra_context: str = "",
    known_block: str = "",
    evidence_block: str = "",
    progress: Callable[[str, str], Awaitable[None]] | None = None,
) -> str | None:
    """Regenerate ONE Know-Me section (A5). Returns the new Markdown for that section, or
    None. Grounded on the same sources; the rest of the document is given as context so the
    section stays consistent (and so cross-references keep their resource names)."""
    label = km.section_label(section_key)
    hint = next((s.get("hint", "") for s in km.SECTION_CATALOG if s["key"] == section_key), "")
    others = "\n".join(
        f"### {s.get('label') or km.section_label(str(s.get('key','')))}\n{str(s.get('content') or '').strip()}"
        for s in (current_sections or []) if str(s.get("key")) != section_key and str(s.get("content") or "").strip()
    ) or "(no other sections yet)"
    blocks = [km.scope_facts_block(facts)]
    if known_block.strip():
        blocks.append(known_block.strip())
    if evidence_block.strip():
        blocks.append(evidence_block.strip())
    if extra_context.strip():
        blocks.append("ADDITIONAL HUMAN-PROVIDED CONTEXT:\n" + extra_context.strip()[:6000])

    system = (
        "You are 'Know-Me Writer'. Rewrite a SINGLE section of a support-facing Know-Me, "
        "grounded only in the supplied memory, scope and evidence. Use ⟦TODO: <label> | "
        "key=<field_key>⟧ for any human-only field; never fabricate people/SLAs/contacts. "
        "Use Markdown (tables, ❌/⚠). Respond with ONLY a JSON object: {\"content\": \"<markdown>\"}."
    )
    user = (
        f"Workload: {workload_name or '(unnamed)'}\n"
        f"Rewrite the section: \"{label}\" — {hint}\n\n"
        + "\n\n".join(blocks)
        + f"\n\nARCHITECTURE MEMORY:\n{_memory_block(memory)}\n\n"
        f"OTHER SECTIONS (for consistency — do not rewrite these):\n{others}"
    )
    if progress is not None:
        await progress("ai", f"🤖 Drafting “{label}” from memory, scope & evidence…")
    text = await _stream_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        progress, compose_msg=f"✍️ Writing “{label}”…",
    )
    t = (text or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    m = re.search(r"(\{.*\})", t, re.DOTALL)
    if m:
        t = m.group(1)
    parsed = loads_tolerant(t)
    if isinstance(parsed, dict) and str(parsed.get("content") or "").strip():
        return str(parsed["content"])
    # Salvage a bare "content": "..." value.
    cm = re.search(r'"content"\s*:\s*"(?P<v>.*)"\s*\}?\s*$', t, re.DOTALL)
    if cm:
        import json
        try:
            return str(json.loads('"' + cm.group("v") + '"', strict=False))
        except (json.JSONDecodeError, ValueError):
            return cm.group("v").replace('\\n', '\n').replace('\\"', '"')
    return None


async def suggest_field_choices(
    *,
    label: str,
    field_key: str = "",
    section_label: str = "",
    workload_name: str = "",
    known_block: str = "",
    evidence_block: str = "",
    max_options: int = 6,
) -> list[str]:
    """Infer a short list of realistic candidate values for ONE human-completion field, using
    the field label + its section + the workload's scope/evidence as grounding. Best-effort:
    returns [] on any failure. The values are *options a human can pick from* (still editable),
    never fabricated facts like specific people/emails."""
    import json

    ctx_parts = [p for p in (known_block, evidence_block) if p]
    ctx = ("\n\nWORKLOAD CONTEXT:\n" + "\n".join(ctx_parts)) if ctx_parts else ""
    system = (
        "You suggest realistic answer OPTIONS for a single support-runbook form field. "
        "Return ONLY a JSON array of 2–{n} short option strings (≤6 words each), most-likely "
        "first. Suggest only genuine, commonly-correct options for THIS field — categories, "
        "enums, tiers, durations, yes/no, standard values. NEVER invent specific people, "
        "emails, phone numbers, IDs, GUIDs, or contract numbers; for those return []."
    ).format(n=max_options)
    user = (
        f"Workload: {workload_name or '(unknown)'}\n"
        f"Section: {section_label or '(general)'}\n"
        f"Field label: {label}\n"
        f"Field key: {field_key or '(none)'}{ctx}\n\n"
        'Respond with ONLY a JSON array, e.g. ["Critical","High","Medium","Low"].'
    )
    try:
        provider = build_provider()
        text = ""
        async for ev in provider.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None, max_tokens=300,
        ):
            if ev.type == "token":
                text += ev.text
    except Exception:  # noqa: BLE001
        return []

    t = (text or "").strip()
    m = re.search(r"\[.*\]", t, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[str] = []
    for v in arr if isinstance(arr, list) else []:
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out[:max_options]



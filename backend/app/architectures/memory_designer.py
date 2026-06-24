"""AI generator for architecture Memory.

Given an architecture diagram, its workload's live resource inventory, and signals of
known weaknesses (assessment findings + inventory hygiene flags), the LLM drafts the
labelled Memory sections — the way a principal architect would document a system for
incident response. Grounded on real data; explicitly marks unknowns instead of inventing.

Mirrors the architecture designer: a plain JSON completion via provider.stream +
safe_json_parse (never complete_json, never bypass provider tokens).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from app.agent.factory import build_provider
from app.architectures.memory import SECTION_CATALOG
from app.core.utils import loads_tolerant

logger = logging.getLogger("app.architectures.memory_designer")


# Lively, per-section status lines streamed to the UI as the model writes each section,
# so a multi-minute draft reads like a principal architect narrating their work.
_SECTION_PROGRESS: dict[str, str] = {
    "overview": "🧭 Capturing what this system does and how critical it is…",
    "pattern": "🏗️ Identifying the architecture pattern…",
    "expected_flow": "🔀 Tracing the request & data flow (the happy path)…",
    "components": "🧩 Cataloguing components and their responsibilities…",
    "dependencies": "🔗 Mapping upstream/downstream dependencies…",
    "network_topology": "🌐 Charting the network topology — VNets, subnets, private endpoints…",
    "identity_access": "🔐 Reviewing identity & access (managed identities, RBAC)…",
    "data_storage": "🗄️ Documenting data stores, encryption & backups…",
    "security_model": "🛡️ Assessing the security model — WAF, Key Vault, exposure…",
    "compliance": "📋 Noting compliance & governance constraints…",
    "resiliency_targets": "♻️ Inferring resiliency targets — RTO/RPO, failover…",
    "scaling_performance": "📈 Reasoning about scaling & performance bottlenecks…",
    "critical_thresholds": "🌡️ Defining critical thresholds & SLIs…",
    "observability": "🔭 Locating logs, metrics, dashboards & key alerts…",
    "runbook": "📓 Writing operational runbook notes…",
    "change_management": "🔧 Capturing change management & pipelines…",
    "cost_sizing": "💰 Noting cost drivers & SKU sizing…",
    "known_gaps": "⚠️ Flagging known gaps, risks & single points of failure…",
    "known_issues": "🐞 Recording known issues & past incidents…",
    "diagnostic_hints": "🔎 Writing first-look triage & diagnostic hints…",
}


def _catalog_block() -> str:
    """The section contract the model must fill (key → label + guidance)."""
    return "\n".join(
        f"- {s['key']}: {s['label']} — {s['hint']}" for s in SECTION_CATALOG
    )


SYSTEM_PROMPT = """\
You are a principal Azure solutions architect documenting an application so an on-call \
engineer (and an AI investigator) can diagnose incidents quickly. You are given the \
application's architecture diagram, its live Azure resource inventory (with real \
properties), and signals about known weaknesses.

Write a structured "memory" with the sections listed below. Rules:
- Ground every statement in the provided data. NEVER invent resources, SLAs, or settings \
  that aren't supported by the inputs.
- When something can't be determined from the data, say so briefly (e.g. "Not determined \
  from current resources") rather than guessing.
- Be concise and operational. Prefer short Markdown: bullet lists, arrows for flows \
  (User → Front Door → App Service → SQL), and concrete resource names.
- For "expected_flow", describe the real request/data path inferred from the resources.
- For "security_model", reflect actual settings (WAF, private endpoints, public access, \
  Key Vault) you can see.
- For "known_gaps", incorporate the provided weakness signals (assessment findings, \
  unattached/idle resources, missing redundancy) — these are high priority.
- For "diagnostic_hints", give a first-look order for triage based on the topology.
- Leave a section's value as an empty string ONLY if there is genuinely nothing to say.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{"sections": {"<section_key>": "<markdown content>", ...}, "confidence": 0.0-1.0}
Use ONLY these section keys:
"""


def _arch_summary(arch: dict[str, Any]) -> str:
    nodes = arch.get("nodes", []) or []
    edges = arch.get("edges", []) or []
    name_by_id = {n.get("id"): (n.get("name") or n.get("type") or "resource") for n in nodes}
    node_lines = []
    for n in nodes[:150]:
        meta = n.get("meta") or {}
        meta_s = ", ".join(f"{k}={v}" for k, v in list(meta.items())[:5])
        node_lines.append(
            f"- {n.get('name', '?')} [{n.get('type', 'concept')}] tier={n.get('layer', '')}"
            + (f" sku={n.get('sku')}" if n.get("sku") else "")
            + (f" ({meta_s})" if meta_s else "")
        )
    edge_lines = []
    for e in edges[:200]:
        s = name_by_id.get(e.get("source"), "?")
        t = name_by_id.get(e.get("target"), "?")
        edge_lines.append(
            f"- {s} --{e.get('kind', 'connects_to')}"
            f"{f' [{e.get('label')}]' if e.get('label') else ''}--> {t}"
        )
    return (
        f"ARCHITECTURE: {arch.get('name', 'Untitled')}\n"
        f"{arch.get('description', '')}\n\n"
        f"RESOURCES ({len(nodes)}):\n" + ("\n".join(node_lines) or "(none)") + "\n\n"
        f"CONNECTIONS ({len(edges)}):\n" + ("\n".join(edge_lines) or "(none)")
    )


async def generate_memory(
    arch: dict[str, Any],
    resources: list[dict[str, Any]],
    weakness_signals: list[str],
    workload_name: str = "",
    progress: Callable[[str, str], Awaitable[None]] | None = None,
    only_keys: list[str] | None = None,
    extra_context: str = "",
) -> dict[str, Any] | None:
    """Draft memory sections from the architecture + live resources + weakness signals.

    Returns ``{"sections": {key: content}, "confidence": float}`` or None on failure.

    If ``progress`` is supplied it is awaited with ``(phase, message)`` as the model
    starts responding and as each section is detected in the streamed JSON, so callers
    can surface lively, live status while the (often multi-minute) draft is produced.

    ``only_keys`` restricts the draft to a subset of section keys (used by per-section
    "regenerate just this section"). ``extra_context`` is free-form operator-supplied
    grounding (a pasted runbook, RCA, or notes) folded into the prompt.
    """
    catalog = SECTION_CATALOG
    if only_keys:
        wanted = set(only_keys)
        catalog = [s for s in SECTION_CATALOG if s["key"] in wanted] or SECTION_CATALOG
    catalog_block = "\n".join(f"- {s['key']}: {s['label']} — {s['hint']}" for s in catalog)
    system = SYSTEM_PROMPT + catalog_block
    if only_keys:
        system += (
            f"\n\nIMPORTANT: Draft ONLY these section(s): {', '.join(only_keys)}. "
            "Return just those key(s) in the JSON."
        )
    gaps = "\n".join(f"- {g}" for g in weakness_signals[:40]) or "(none reported)"
    # Cap the raw inventory so the prompt stays within budget on large workloads.
    inv = json.dumps(resources[:120], separators=(",", ":"))
    extra_block = ""
    if extra_context.strip():
        # Bound the pasted context so it can't blow the prompt budget.
        extra_block = (
            "\n\nADDITIONAL OPERATOR-PROVIDED CONTEXT (runbooks, RCAs, notes — treat as "
            "authoritative ground truth, fold relevant facts into the sections):\n"
            + extra_context.strip()[:8000]
        )
    user = (
        f"Workload: {workload_name or arch.get('workload_name') or '(unnamed)'}\n\n"
        f"{_arch_summary(arch)}\n\n"
        f"KNOWN WEAKNESS SIGNALS (assessment findings + idle/orphaned resources):\n{gaps}\n\n"
        f"LIVE RESOURCE INVENTORY (id, type, sku, properties):\n{inv}"
        f"{extra_block}"
    )

    provider = build_provider()
    text = ""
    # Detect section keys as the JSON streams in, so we can narrate progress. A key is
    # considered "started" when its `"key":` marker appears in the accumulated text.
    section_keys = [s["key"] for s in catalog]
    seen: set[str] = set()
    first_token = False
    # Memory is many sections of markdown; the default response cap (~4k tokens) truncates
    # it mid-JSON, so the parse yields nothing and the draft "fails" (saving nothing).
    # Request a generous cap so the JSON object is returned whole, regardless of the
    # globally-configured max_tokens.
    async for ev in provider.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        None,
        max_tokens=16000,
    ):
        if ev.type == "token":
            text += ev.text
            if progress is not None:
                if not first_token:
                    first_token = True
                    await progress("ai", "🤖 The model is responding — composing the memory…")
                # Emit a status the first time each section key shows up, in catalog order.
                for key in section_keys:
                    if key in seen:
                        continue
                    if re.search(rf'"{re.escape(key)}"\s*:', text):
                        seen.add(key)
                        msg = _SECTION_PROGRESS.get(key, f"✍️ Documenting {key}…")
                        await progress("section", msg)
    if progress is not None and seen:
        await progress("ai", f"🧱 Assembling {len(seen)} sections & finalizing…")
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)
    parsed = loads_tolerant(t, default=None)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("sections"), dict):
        logger.warning(
            "Memory JSON completion did not parse (raw len=%d): head=%r tail=%r",
            len(text),
            text[:200],
            text[-200:],
        )
        return None
    # Keep only string contents; drop unknown/empty keys silently.
    valid_keys = {s["key"] for s in SECTION_CATALOG}
    sections = {
        k: str(v).strip()
        for k, v in parsed["sections"].items()
        if k in valid_keys and isinstance(v, (str, int, float)) and str(v).strip()
    }
    if not sections:
        return None
    confidence = parsed.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = None
    return {"sections": sections, "confidence": confidence}

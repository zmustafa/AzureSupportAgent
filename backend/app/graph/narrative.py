"""AI narrative + 'ask the graph' for the ``/graph`` storytelling features.

- ``narrate_graph`` writes a short plain-English summary of a (sub)graph for onboarding,
  incident review, or an executive readout. Deterministic fallback when no LLM is configured.
- ``ask_graph`` turns a natural-language question into a STRUCTURED filter over the node set
  (kinds + boolean predicates we can evaluate locally), then the API applies it. The LLM only
  emits the filter — we never let it fabricate nodes. Keyword fallback when no LLM.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("app.graph.narrative")

# Predicates the filter language supports (evaluated server-side over node.data).
_PREDICATES = (
    "internet_facing", "no_backup", "no_monitoring", "no_telemetry", "has_findings",
    "critical_findings", "public_exposure", "shared_service", "unowned", "expiring_secrets",
    "high_risk", "changed_recently", "retiring",
)


async def narrate_graph(summary: dict[str, Any]) -> dict[str, Any]:
    """Write a narrative for the current graph. ``summary`` is a compact, pre-computed dict
    (counts, top risks, drift, lenses) so we never ship the whole graph to the model."""
    from app.agent.factory import build_provider

    deterministic = _deterministic_narrative(summary)
    try:
        provider = build_provider()
    except Exception:  # noqa: BLE001
        return {"narrative": deterministic, "used_ai": False}

    system = (
        "You are an Azure cloud architect briefing a colleague from a knowledge graph of "
        "their estate. Write 2-4 short paragraphs: what this scope contains, the most "
        "important risks, what looks healthy, and a concrete next step. Be specific and "
        "use the numbers provided. Plain prose, no markdown headers."
    )
    import json as _json

    user = "Graph summary (JSON):\n" + _json.dumps(summary, default=str)[:6000]
    text = ""
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None,
            max_tokens=900,
        ):
            if ev.type == "token":
                text += ev.text
    except Exception as exc:  # noqa: BLE001
        log.warning("graph narrative LLM failed: %s", exc)
        return {"narrative": deterministic, "used_ai": False}
    text = text.strip()
    return {"narrative": text or deterministic, "used_ai": bool(text)}


def _deterministic_narrative(s: dict[str, Any]) -> str:
    counts = s.get("counts", {})
    parts = [
        f"This scope contains {counts.get('workloads', 0)} workload(s), "
        f"{counts.get('subscriptions', 0)} subscription(s), and {counts.get('resources', 0)} resource(s) "
        f"across {counts.get('architectures', 0)} documented architecture(s)."
    ]
    top = s.get("top_risks") or []
    if top:
        names = ", ".join(t.get("label", "") for t in top[:3])
        parts.append(f"Highest-risk workloads: {names}.")
    drift = s.get("drift")
    if drift and drift.get("drift_score") is not None:
        parts.append(f"Intent-vs-reality alignment is {drift['drift_score']}% for the focused workload.")
    parts.append("Open the inspector on any node for its dossier, or run blast-radius from a shared service to see what depends on it.")
    return " ".join(parts)


async def ask_graph(question: str, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Return ``{matched: [node ids], filter: {...}, explanation, used_ai}``."""
    from app.agent.factory import build_provider

    flt = None
    used_ai = False
    try:
        provider = build_provider()
        flt = await _llm_filter(provider, question)
        used_ai = flt is not None
    except Exception:  # noqa: BLE001
        flt = None
    if not flt:
        flt = _keyword_filter(question)
    matched = [n["id"] for n in nodes if _matches(n, flt)]
    return {
        "matched": matched,
        "count": len(matched),
        "filter": flt,
        "explanation": flt.get("explanation", ""),
        "used_ai": used_ai,
    }


async def _llm_filter(provider: Any, question: str) -> dict[str, Any] | None:
    from app.core.utils import safe_json_parse

    system = (
        "Translate a question about an Azure estate graph into a STRICT JSON filter. "
        "Schema: {\"kinds\": [list of node kinds], \"predicates\": [subset of "
        f"{list(_PREDICATES)}], \"text\": \"optional label substring\", "
        "\"explanation\": \"one sentence\"}. "
        "Node kinds include workload, resource, subscription, architecture, assessment_finding, "
        "rbac_principal, retirement_item. Only use predicates from the allowed list. Return "
        "ONLY the JSON object."
    )
    text = ""
    async for ev in provider.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": f"Question: {question}\nReturn only JSON."}],
        None,
        max_tokens=400,
    ):
        if ev.type == "token":
            text += ev.text
    t = text.strip()
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        t = m.group(0)
    parsed = safe_json_parse(t, default=None)
    if not isinstance(parsed, dict):
        return None
    parsed.setdefault("kinds", [])
    parsed.setdefault("predicates", [])
    parsed.setdefault("text", "")
    parsed["predicates"] = [p for p in parsed.get("predicates", []) if p in _PREDICATES]
    return parsed


def _keyword_filter(question: str) -> dict[str, Any]:
    q = (question or "").lower()
    preds: list[str] = []
    kinds: list[str] = []
    if "without backup" in q or "no backup" in q or "unprotected" in q:
        preds.append("no_backup")
    if "internet" in q or "public" in q or "exposed" in q or "facing" in q:
        preds.append("internet_facing")
    if "finding" in q or "failing" in q or "risk" in q:
        preds.append("has_findings")
    if "critical" in q:
        preds.append("critical_findings")
    if "unowned" in q or "orphan" in q:
        preds.append("unowned")
    if "shared" in q:
        preds.append("shared_service")
    if "retir" in q or "deprecat" in q:
        preds.append("retiring")
    if "changed" in q or "recent" in q:
        preds.append("changed_recently")
    if "workload" in q:
        kinds.append("workload")
    if "resource" in q:
        kinds.append("resource")
    return {"kinds": kinds, "predicates": preds, "text": "", "explanation": "Matched by keywords."}


def _matches(node: dict[str, Any], flt: dict[str, Any]) -> bool:
    kinds = flt.get("kinds") or []
    if kinds and node.get("kind") not in kinds:
        return False
    text = (flt.get("text") or "").strip().lower()
    if text and text not in (node.get("label", "") or "").lower():
        return False
    data = node.get("data", {}) or {}
    for pred in flt.get("predicates") or []:
        if not _eval_predicate(pred, node, data):
            return False
    return True


def _eval_predicate(pred: str, node: dict[str, Any], data: dict[str, Any]) -> bool:
    kind = node.get("kind", "")
    flags = data.get("flags") or []
    risk = data.get("risk") or {}
    overlay = data.get("overlay") or {}
    if pred == "internet_facing" or pred == "public_exposure":
        return any("public" in str(f).lower() or "internet" in str(f).lower() for f in flags) or bool(overlay.get("internet_facing"))
    if pred == "no_backup":
        return bool(overlay.get("no_backup"))
    if pred == "no_monitoring":
        return bool(overlay.get("no_monitoring"))
    if pred == "no_telemetry":
        return bool(overlay.get("no_telemetry"))
    if pred == "has_findings":
        return int(risk.get("failed", 0) or 0) > 0
    if pred == "critical_findings":
        return (risk.get("severity", "") or "").lower() in ("critical", "high", "error") and int(risk.get("failed", 0) or 0) > 0
    if pred == "high_risk":
        return (risk.get("level", "") or "") == "high"
    if pred == "shared_service":
        return len(data.get("workloads", []) or []) > 1
    if pred == "unowned":
        return kind == "resource" and not (data.get("workloads") or [])
    if pred == "changed_recently":
        return bool(overlay.get("changed_recently"))
    if pred == "retiring":
        return bool(overlay.get("retiring")) or kind == "retirement_item"
    if pred == "expiring_secrets":
        return bool(overlay.get("expiring_secrets")) or kind == "identity_finding"
    return True

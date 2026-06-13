"""Derived summaries for deep-investigation results.

Pure functions over the persisted ``investigation_json`` shape
(``{phases, hypotheses[], conclusion, agents, research}``) so both the history
endpoint and unit tests can compute a confidence score + hypothesis tallies without
touching the orchestrator. Confidence is a heuristic from how the hypothesis tree
resolved plus whether a concrete root cause was concluded — a signal, not a guarantee.
"""
from __future__ import annotations

from typing import Any

# Per-status weight for the confidence calculation.
_STATUS_WEIGHT = {
    "validated": 1.0,
    "inconclusive": 0.4,
    "validating": 0.3,
    "invalidated": 0.0,
}


def hypothesis_counts(inv: dict[str, Any] | None) -> dict[str, int]:
    """Tally hypotheses by status (validated / invalidated / inconclusive / validating)."""
    counts = {"validated": 0, "invalidated": 0, "inconclusive": 0, "validating": 0}
    for h in (inv or {}).get("hypotheses", []) or []:
        status = str((h or {}).get("status", "")).lower()
        if status in counts:
            counts[status] += 1
    return counts


def confidence_score(inv: dict[str, Any] | None) -> int:
    """A 0–100 confidence heuristic for an investigation's conclusion.

    Driven by the strongest validated hypothesis and the overall resolution of the
    tree, with a floor when a concrete root cause was reached and a penalty when
    everything came back invalidated/inconclusive.
    """
    if not inv:
        return 0
    hyps = inv.get("hypotheses", []) or []
    conclusion = inv.get("conclusion") or {}
    root_cause = str(conclusion.get("root_cause", "")).strip() if isinstance(conclusion, dict) else ""

    if not hyps:
        # No tree, but a stated root cause still carries moderate confidence.
        return 55 if root_cause else 0

    weights = [
        _STATUS_WEIGHT.get(str((h or {}).get("status", "")).lower(), 0.2) for h in hyps
    ]
    best = max(weights) if weights else 0.0
    avg = sum(weights) / len(weights) if weights else 0.0
    # Lean on the strongest hypothesis (root cause is usually one validated branch),
    # but let a broadly-resolved tree lift the score a little.
    score = best * 0.75 + avg * 0.25
    if root_cause:
        score = max(score, 0.5)  # a concrete conclusion floors confidence at 50%
    return max(0, min(100, round(score * 100)))


def investigation_digest(inv: dict[str, Any] | None) -> dict[str, Any]:
    """A compact summary of an investigation for list/history views."""
    conclusion = (inv or {}).get("conclusion") or {}
    if not isinstance(conclusion, dict):
        conclusion = {}
    counts = hypothesis_counts(inv)
    return {
        "root_cause": str(conclusion.get("root_cause", "")).strip(),
        "summary": str(conclusion.get("summary", "")).strip(),
        "hypothesis_counts": counts,
        "hypothesis_total": sum(counts.values()),
        "agent_count": len((inv or {}).get("agents", []) or []),
        "confidence": confidence_score(inv),
        "has_conclusion": bool(conclusion.get("root_cause") or conclusion.get("summary")),
    }

"""IAM posture score.

Two rules make this number trustworthy, and both are inherited from the Entra work because both
were learned by getting them wrong first:

1. **Blind is not zero.** A signal that could not be evaluated is EXCLUDED from the score rather
   than counted as a pass. Otherwise a tenant nobody could measure scores best.
2. **The score is never shown without its coverage**, and below ``MIN_COVERAGE_FOR_GRADE`` the
   grade is withheld entirely — a letter derived from a third of the checks is worse than no
   letter, because it will be quoted without the caveat.

A pillar with no signals registered yet reports ``not_implemented``, which is a third state
distinct from *blind* (we tried and could not) and from *ok* (we looked and it is fine).
"""
from __future__ import annotations

from typing import Any

from app.iam import signals as sig

# Letter grades. Deliberately coarse: this number moves slowly and false precision invites
# arguments about a point rather than about a finding.
GRADES: list[tuple[int, str, str]] = [
    (90, "A", "Strong"),
    (80, "B", "Good"),
    (70, "C", "Fair"),
    (60, "D", "Weak"),
    (0, "F", "Poor"),
]

# Below this coverage the grade is meaningless, so it is withheld rather than misleading.
MIN_COVERAGE_FOR_GRADE = 0.6

# What a finding costs, by severity, as a fraction of its signal's weight.
_SEVERITY_COST = {"critical": 1.0, "error": 0.8, "warning": 0.5, "info": 0.15}

# A signal that fires many times is worse than one that fires once, but not unboundedly so —
# past this many findings the marginal one adds nothing, or a single noisy check would dominate
# the whole pillar.
_SATURATION = 3


def _grade(score: int) -> tuple[str, str]:
    for floor, letter, label in GRADES:
        if score >= floor:
            return letter, label
    return "F", "Poor"


def _signal_penalty(result: sig.SignalResult) -> float:
    """0..1 — how much of this signal's weight the findings consume."""
    if not result.findings:
        return 0.0
    worst = min(result.findings, key=lambda f: sig.SEVERITY_RANK.get(f.severity, 3))
    base = _SEVERITY_COST.get(worst.severity, 0.5)
    volume = min(len(result.findings), _SATURATION) / _SATURATION
    # Half the cost is "it happened at all", half scales with how widespread it is.
    return min(1.0, base * (0.5 + 0.5 * volume))


def compute(results: list[sig.SignalResult]) -> dict[str, Any]:
    """Roll signal results up into per-pillar scores, tenant score and coverage."""
    by_pillar: dict[str, list[sig.SignalResult]] = {p["key"]: [] for p in sig.PILLARS}
    for r in results:
        by_pillar.setdefault(r.spec.pillar, []).append(r)

    pillars: list[dict[str, Any]] = []
    for meta in sig.PILLARS:
        key = meta["key"]
        rs = by_pillar.get(key, [])
        registered = sum(s.spec.weight for s in rs)
        measured = [s for s in rs if s.measured]
        measured_weight = sum(s.spec.weight for s in measured)

        if not rs:
            state, reason, score, fraction = "not_implemented", "No checks are registered for this pillar yet.", None, 0.0
        elif measured_weight == 0:
            reasons = sorted({s.reason for s in rs if s.reason})
            state, reason, score = "blind", (reasons[0] if reasons else "Nothing in this pillar could be measured."), None
            fraction = 0.0
        else:
            penalty = sum(s.spec.weight * _signal_penalty(s) for s in measured)
            score = round(100 * (1 - penalty / measured_weight))
            unmeasured = [s for s in rs if not s.measured]
            state = "partial" if unmeasured else "ok"
            reason = (
                f"{len(unmeasured)} of {len(rs)} checks could not be measured."
                if unmeasured else ""
            )
            # Weight-granular: a pillar half of whose weight could not be evaluated is half
            # covered, not fully covered on the half we could see.
            fraction = measured_weight / registered if registered else 0.0

        pillars.append({
            "key": key,
            "label": meta["label"],
            "desc": meta["desc"],
            "weight": meta["weight"],
            "score": score,
            "state": state,
            "reason": reason,
            "signals": len(rs),
            "signals_measured": len(measured),
            "findings": sum(len(s.findings) for s in rs),
            "measured_fraction": round(fraction, 3),
        })

    scored = [p for p in pillars if p["score"] is not None]
    measured_weight = sum(p["weight"] for p in scored)
    total_weight = sum(p["weight"] for p in pillars)
    score = (
        round(sum(p["weight"] * p["score"] for p in scored) / measured_weight)
        if measured_weight else None
    )
    coverage = (
        round(sum(p["weight"] * p["measured_fraction"] for p in pillars) / total_weight, 3)
        if total_weight else 0.0
    )
    show_grade = score is not None and coverage >= MIN_COVERAGE_FOR_GRADE
    letter, label = _grade(score) if score is not None else ("", "")

    return {
        "score": score,
        "coverage": coverage,
        # Withheld, not faked. The UI must render the absence, not a placeholder letter.
        "grade": letter if show_grade else None,
        "grade_label": label if show_grade else None,
        "grade_withheld_reason": (
            None if show_grade else
            "Not enough of the estate could be measured for a grade to mean anything."
            if score is not None else
            "Nothing could be measured yet."
        ),
        "min_coverage_for_grade": MIN_COVERAGE_FOR_GRADE,
        "pillars": pillars,
    }

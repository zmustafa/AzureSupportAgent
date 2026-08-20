"""Identity Posture Score — deterministic, explainable, and honest about blindness.

Three properties matter more than the number itself:

1. **Deterministic** — the same snapshot and context produce the same score, byte for byte.
   No AI in the number; AI narrates the score, it never computes it.
2. **Explainable to the object** — every lost point maps to a signal, and every signal maps
   to the specific users, applications or policies responsible.
3. **Blind is not zero** — a pillar that could not be measured (missing permission, missing
   license, collector error) is *excluded from the denominator* and reported as "not
   measured". A tenant without P2 must not look catastrophic because PIM data was
   unavailable. The score is therefore always published together with its coverage.
"""
from __future__ import annotations

from typing import Any

from app.entra import signals as sig
from app.entra.signals import EvaluationResult, SignalContext

GRADES: list[tuple[int, str, str]] = [
    (90, "A", "Strong"),
    (75, "B", "Good, gaps to close"),
    (60, "C", "Material risk"),
    (40, "D", "Weak"),
    (0, "E", "Critical"),
]

# Below this coverage the grade is meaningless, so it is withheld rather than misleading.
MIN_COVERAGE_FOR_GRADE = 0.6


def _grade(score: int) -> tuple[str, str]:
    for floor, letter, label in GRADES:
        if score >= floor:
            return letter, label
    return "E", "Critical"


def compute(
    snapshot_data: dict[str, Any],
    result: EvaluationResult,
    ctx: SignalContext,
) -> dict[str, Any]:
    """Roll findings up into pillar scores, a tenant score and the recoverable-points list."""
    specs = {s.id: s for s in sig.registry()}
    pillars: list[dict[str, Any]] = []
    wins: list[dict[str, Any]] = []

    for pillar in sig.PILLARS:
        key = pillar["key"]
        pillar_specs = [s for s in specs.values() if s.pillar == key]
        measured = [s for s in pillar_specs if s.id in result.measured]
        unmeasured = [s for s in pillar_specs if s.id not in result.measured]

        max_units = sum(sig.max_units(s) for s in measured)
        max_units_all = sum(sig.max_units(s) for s in pillar_specs)
        penalty = 0.0
        findings_count = 0
        for spec in measured:
            count = result.by_signal.get(spec.id, 0)
            findings_count += count
            units = sig.penalty_units(spec, count, snapshot_data)
            penalty += units
            if units > 0:
                wins.append({
                    "signal_id": spec.id,
                    "title": spec.title,
                    "pillar": key,
                    "severity": spec.severity,
                    "findings": count,
                    "units": units,
                    "remediation": spec.remediation,
                })

        if max_units_all <= 0:
            # No signal in the registry covers this pillar yet. Saying "not collected" would
            # imply a permission problem; the honest answer is that the model does not
            # measure it in this build.
            score: int | None = None
            state = "not_implemented"
            reason = "No checks for this pillar have shipped yet."
        elif max_units <= 0:
            score = None
            state = _pillar_state(unmeasured, result)
            reason = _pillar_reason(unmeasured, result)
        else:
            score = max(0, min(100, round(100 * (1 - penalty / max_units))))
            state = "measured" if not unmeasured else "partial"
            reason = "" if not unmeasured else _pillar_reason(unmeasured, result)

        pillars.append({
            "key": key,
            "label": pillar["label"],
            "blurb": pillar["blurb"],
            "weight": pillar["weight"],
            "score": score,
            "state": state,
            "reason": reason,
            "findings": findings_count,
            "measured_signals": len(measured),
            "total_signals": len(pillar_specs),
            # How much of THIS pillar's model we could measure, by signal weight. A pillar
            # with 2 of 21 signals readable is not "measured" in any honest sense, so this
            # is what feeds tenant coverage rather than a flat per-pillar count.
            "measured_fraction": round(max_units / max_units_all, 3) if max_units_all else 0.0,
            "penalty_units": round(penalty, 2),
            "max_units": round(max_units, 2),
            "not_measured": [
                {"signal_id": s.id, "title": s.title, "reason": result.not_measured.get(s.id, "Not measured.")}
                for s in unmeasured
            ],
        })

    measured_weight = sum(p["weight"] for p in pillars if p["score"] is not None)
    total_weight = sum(p["weight"] for p in pillars)
    if measured_weight:
        score = round(sum(p["weight"] * p["score"] for p in pillars if p["score"] is not None) / measured_weight)
    else:
        score = 0
    coverage = (
        sum(p["weight"] * p["measured_fraction"] for p in pillars) / total_weight
    ) if total_weight else 0.0

    letter, label = _grade(score)
    show_grade = coverage >= MIN_COVERAGE_FOR_GRADE and measured_weight > 0

    # Recoverable points: converting a signal's units back into tenant-score points.
    for w in wins:
        pillar = next(p for p in pillars if p["key"] == w["pillar"])
        if pillar["max_units"] and measured_weight:
            w["points"] = round(
                (w["units"] / pillar["max_units"]) * 100 * pillar["weight"] / measured_weight, 1
            )
        else:
            w["points"] = 0.0
    wins.sort(key=lambda w: (-w["points"], w["signal_id"]))

    from app.entra import model

    return {
        "score": score,
        "grade": letter if show_grade else "",
        "grade_label": label if show_grade else "",
        "grade_withheld_reason": (
            "" if show_grade else
            f"Only {coverage:.0%} of the model could be measured — a grade would be misleading."
        ),
        "coverage": round(coverage, 3),
        "measured_weight": measured_weight,
        "total_weight": total_weight,
        "pillars": pillars,
        "top_wins": wins[:8],
        "findings_by_severity": model.count_by_severity(result.findings),
        "findings_total": len(result.findings),
        "measured_signals": len(result.measured),
        "total_signals": len(specs),
        "not_measured": result.public_not_measured(),
        "registry_version": sig.registry_version(),
    }


def _pillar_state(unmeasured: list[Any], result: EvaluationResult) -> str:
    """Why a whole pillar is unmeasured.

    Classify every reason and pick the dominant one rather than the first match: a pillar
    where 17 signals are blind and 2 are unlicensed must say *blind*, because a missing
    permission is the actionable fact and a license upsell is not.
    """
    if not unmeasured:
        return "not_collected"
    counts = {"blind": 0, "unlicensed": 0, "error": 0, "not_collected": 0}
    for spec in unmeasured:
        reason = (result.not_measured.get(spec.id, "") or "").lower()
        if "requires entra id" in reason or "licence" in reason or "license" in reason:
            counts["unlicensed"] += 1
        elif "not permitted" in reason or "missing" in reason or "forbidden" in reason:
            counts["blind"] += 1
        elif "failed" in reason:
            counts["error"] += 1
        else:
            counts["not_collected"] += 1
    # Ties resolve toward the most actionable explanation.
    order = ["blind", "unlicensed", "error", "not_collected"]
    return max(order, key=lambda k: (counts[k], -order.index(k)))


def _pillar_reason(unmeasured: list[Any], result: EvaluationResult) -> str:
    """One sentence a human can act on, covering the whole unmeasured set.

    Every screen shows this instead of an empty panel — an unexplained blank is how a blind
    pillar gets mistaken for a clean one.
    """
    if not unmeasured:
        return ""
    reasons: dict[str, int] = {}
    for spec in unmeasured:
        r = (result.not_measured.get(spec.id) or "Not measured.").strip()
        reasons[r] = reasons.get(r, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    lead = top[0][0]
    if len(top) == 1:
        return lead
    return f"{lead} (+{len(top) - 1} other reason{'s' if len(top) > 2 else ''})"


def history_entry(score: dict[str, Any], snapshot_id: str, generated_at: str) -> dict[str, Any]:
    """One append-only history point. Written only after a successful FULL refresh."""
    return {
        "at": generated_at,
        "score": score["score"],
        "coverage": score["coverage"],
        "registry_version": score["registry_version"],
        "pillars": {p["key"]: p["score"] for p in score["pillars"]},
        "findings": score["findings_by_severity"],
        "measured_signals": score["measured_signals"],
        "snapshot_id": snapshot_id,
    }


def diff_findings(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> dict[str, Any]:
    """New / resolved / persisting, by fingerprint.

    This is also the notification payload: nobody reads a daily email that repeats 400
    known findings, so only the new and resolved sets are ever pushed."""
    cur = {f["fingerprint"]: f for f in current}
    prev = {f["fingerprint"]: f for f in previous}
    new_ids = sorted(set(cur) - set(prev))
    resolved_ids = sorted(set(prev) - set(cur))
    persisting_ids = sorted(set(cur) & set(prev))
    return {
        "new": [cur[i] for i in new_ids],
        "resolved": [prev[i] for i in resolved_ids],
        "persisting_count": len(persisting_ids),
        "counts": {"new": len(new_ids), "resolved": len(resolved_ids), "persisting": len(persisting_ids)},
    }

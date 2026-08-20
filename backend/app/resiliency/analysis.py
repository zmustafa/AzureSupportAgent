"""Recovery Readiness — aggregate analysis over an analyzed snapshot.

Pure. No I/O, no Azure, no snapshot store. Takes the resource rows an analysis already
produced and answers the questions a per-resource grid cannot:

* which **resource types** are weak, and at which scenario;
* how RTO classes are **distributed**, rather than what the worst one is;
* **why** — which handful of reasons account for most of the estate's exposure.

This lives apart from the exporters because the workbook and the PDF must print identical
numbers. The first time a reader cross-checks one against the other and they disagree, the
whole report stops being evidence and starts being an opinion. One function, two renderers.

Four rules are enforced here rather than left to callers, because each is a plausible
mistake that produces a confident, wrong, reassuring number:

1. **There is no average RTO.** :data:`~app.resiliency.model._RTO_RANK` deliberately
   excludes ``unknown``, so a mean over the scale is undefined. Aggregates report the worst
   class and the distribution behind it.
2. **A median RPO covers only the resources whose RPO is known**, and always travels with
   the count it excluded. A median over 41 of 44 presented as the answer for a type is a
   lie of omission.
3. **Undetermined is its own bucket** and is never folded into a percentage. A type with
   three unreadable resources must not render as "94% fine".
4. **A type whose every resource is not-applicable is excluded, not scored.** Reporting
   stateless front ends as "100% meets objective" for data corruption implies a protection
   they do not have.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from app.resiliency import derive, model

#: Reasons are grouped by their literal text, so a long tail of near-identical details would
#: fragment the index. Ranking and cutting at a limit keeps it a list of fixes.
DEFAULT_REASON_LIMIT = 25


def _applicable(verdict: dict[str, Any]) -> bool:
    return bool(verdict) and bool(verdict.get("applicable", True))


def _reason(verdict: dict[str, Any]) -> str:
    basis = verdict.get("basis") or []
    return str((basis[0] or {}).get("detail", "")) if basis else ""


def rto_distribution(rows: list[dict[str, Any]], scenario: str) -> dict[str, int]:
    """Count of resources per RTO class for one scenario, plus ``not_applicable``.

    Every class in :data:`model.RTO_CLASSES` is present even at zero, so a renderer cannot
    silently drop the empty ``none`` bucket and make a chart look complete."""
    counts = {cls: 0 for cls in model.RTO_CLASSES}
    counts["not_applicable"] = 0
    for row in rows:
        verdict = (row.get("verdicts") or {}).get(scenario) or {}
        if not _applicable(verdict):
            counts["not_applicable"] += 1
            continue
        cls = str(verdict.get("rto_class") or model.RTO_UNKNOWN)
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def rpo_distribution(rows: list[dict[str, Any]], scenario: str) -> dict[str, Any]:
    """Known RPO minutes for one scenario, with the unmeasurable ones counted separately.

    ``none`` (no recovery point exists) and ``unknown`` (a source could not be read) are
    different facts and are never merged: the first is a finding, the second is a gap in
    our own reading."""
    known: list[int] = []
    none = unknown = not_applicable = 0
    for row in rows:
        verdict = (row.get("verdicts") or {}).get(scenario) or {}
        if not _applicable(verdict):
            not_applicable += 1
            continue
        state = verdict.get("rpo_state")
        if state == model.RPO_NONE:
            none += 1
        elif state == model.RPO_KNOWN and verdict.get("rpo_minutes") is not None:
            known.append(int(verdict["rpo_minutes"]))
        else:
            unknown += 1
    known.sort()
    return {
        "known": known,
        "count_known": len(known),
        "none": none,
        "unknown": unknown,
        "not_applicable": not_applicable,
        "best_minutes": known[0] if known else None,
        "median_minutes": _median(known),
        "worst_minutes": known[-1] if known else None,
        # Carried so a renderer printing the median is forced to print what it left out.
        "excluded": none + unknown,
    }


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    # Round up: overstating data loss is the safe direction to be wrong in.
    return -((-(values[mid - 1] + values[mid])) // 2)


def by_resource_type(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per resource type per scenario, ranked worst first.

    The dominant reason is the point of this table. *"42 of 44 storage accounts have no
    region recovery, all because their vault is locally redundant"* is one fix; forty-two
    rows is a backlog."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(str(row.get("type") or "unknown"), []).append(row)

    out: list[dict[str, Any]] = []
    for rtype, group in by_type.items():
        for scenario in model.SCENARIOS:
            verdicts = [
                (row, (row.get("verdicts") or {}).get(scenario) or {}) for row in group
            ]
            applicable = [(row, v) for row, v in verdicts if _applicable(v)]
            # Rule 4: a type that cannot experience this failure is excluded, never scored.
            if not applicable:
                continue

            classes = [str(v.get("rto_class") or model.RTO_UNKNOWN) for _, v in applicable]
            worst, undetermined = model.worst_rto(classes)
            reasons = Counter(_reason(v) for _, v in applicable if _reason(v))
            dominant, dominant_count = (reasons.most_common(1) or [("", 0)])[0]
            no_path = [row for row, v in applicable
                       if v.get("rto_class") == model.RTO_NONE]
            breached = [row for row, v in applicable
                        if (v.get("breach") or {}).get("state") == "breached"]

            out.append({
                "type": rtype,
                "scenario": scenario,
                "resources": len(applicable),
                "not_applicable": len(group) - len(applicable),
                "worst_rto_class": worst,
                "undetermined": undetermined,
                "rto_counts": _class_counts(classes),
                "rpo": rpo_distribution([row for row, _ in applicable], scenario),
                "no_recovery_path": len(no_path),
                "breached": len(breached),
                "dominant_reason": dominant,
                "dominant_reason_count": dominant_count,
                "examples": [str(row.get("name") or "") for row in (no_path or breached)][:5],
            })

    out.sort(key=_type_rank)
    return out


def _class_counts(classes: list[str]) -> dict[str, int]:
    counts = {cls: 0 for cls in model.RTO_CLASSES}
    for cls in classes:
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def _type_rank(entry: dict[str, Any]) -> tuple[Any, ...]:
    """Worst first, by consequence: no recovery path, then breaches, then how bad the
    worst class is, then size. `unknown` sorts after every determined class rather than
    winning the top slot — a type we could not read is a gap in our reading, not the
    estate's biggest risk."""
    rank = model.rto_rank(entry["worst_rto_class"])
    return (
        -entry["no_recovery_path"],
        -entry["breached"],
        rank if rank is not None else 99,
        -entry["resources"],
        entry["type"],
        entry["scenario"],
    )


def reason_index(
    rows: list[dict[str, Any]], *, limit: int = DEFAULT_REASON_LIMIT,
) -> list[dict[str, Any]]:
    """Every distinct reason a verdict was reached, ranked by how much it explains.

    A reader who fixes the top five entries here moves more of the estate than one working
    down a resource list, because the same misconfiguration recurs."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        for scenario, verdict in (row.get("verdicts") or {}).items():
            if not _applicable(verdict):
                continue
            for item in verdict.get("basis") or []:
                detail = str((item or {}).get("detail") or "")
                if not detail:
                    continue
                key = (scenario, detail)
                entry = buckets.setdefault(key, {
                    "scenario": scenario,
                    "reason": detail,
                    "kind": str((item or {}).get("kind") or ""),
                    "source": str((item or {}).get("source") or ""),
                    "resources": 0,
                    "types": set(),
                    "no_recovery_path": 0,
                    "examples": [],
                })
                entry["resources"] += 1
                entry["types"].add(str(row.get("type") or ""))
                if verdict.get("rto_class") == model.RTO_NONE:
                    entry["no_recovery_path"] += 1
                if len(entry["examples"]) < 5:
                    entry["examples"].append(str(row.get("name") or ""))

    out = []
    for entry in buckets.values():
        entry["types"] = sorted(t for t in entry["types"] if t)
        out.append(entry)
    out.sort(key=lambda e: (-e["no_recovery_path"], -e["resources"], e["reason"]))
    return out[:limit]


def worst_offenders(rows: list[dict[str, Any]], *, limit: int = 50) -> list[dict[str, Any]]:
    """Resources ranked by consequence: no recovery path first, then most scenarios lost."""
    scored: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for row in rows:
        verdicts = (row.get("verdicts") or {}).items()
        applicable = [(s, v) for s, v in verdicts if _applicable(v)]
        no_path = [s for s, v in applicable if v.get("rto_class") == model.RTO_NONE]
        breached = [s for s, v in applicable
                    if (v.get("breach") or {}).get("state") == "breached"]
        unknown = [s for s, v in applicable if v.get("rto_class") == model.RTO_UNKNOWN]
        if not (no_path or breached):
            continue
        scored.append(((-len(no_path), -len(breached), str(row.get("name") or "")), {
            "id": row.get("id", ""),
            "name": row.get("name", ""),
            "type": row.get("type", ""),
            "location": row.get("location", ""),
            "no_recovery_path": [model.SCENARIO_LABEL.get(s, s) for s in no_path],
            "breached": [model.SCENARIO_LABEL.get(s, s) for s in breached],
            "undetermined": len(unknown),
            "reasons": sorted({_reason(v) for s, v in applicable
                               if s in no_path and _reason(v)}),
        }))
    scored.sort(key=lambda pair: pair[0])
    return [entry for _, entry in scored[:limit]]


#: How many RTO classes worse a logical scenario must be than the infrastructure answer
#: before the gap is worth naming. Two steps is a category change (automatic -> hours,
#: minutes -> day_plus), not a rounding difference, so the claim survives an argument.
REDUNDANCY_GAP_STEPS = 2


def redundancy_gap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resources whose redundancy makes infrastructure loss look solved while corruption or
    deletion is dramatically worse.

    The product's thesis as a list. A Cosmos account with multi-region writes recovers from
    a region loss automatically and needs a day to recover from a bad deployment; every
    zone-centric tool reports it as resilient, because every infrastructure answer is green.

    The gap is measured in RTO *classes*, not minutes, and ``unknown`` is never placed on
    the scale — a resource we could not read is not evidence of a gap.
    """
    out = []
    for row in rows:
        redundancy = row.get("redundancy") or {}
        # `if replication:` would read "LRS" as redundancy. It is the absence of it, and the
        # same shortcut once credited an un-backed-up VM with automatic instance recovery.
        redundant = (
            bool(redundancy.get("zone_redundant"))
            or str(redundancy.get("replication") or "") in derive.REDUNDANT_REPLICATIONS
            or len(redundancy.get("zones") or []) > 1
        )
        if not redundant:
            continue

        verdicts = row.get("verdicts") or {}
        infra_ranks = [
            model.rto_rank(str((verdicts.get(s) or {}).get("rto_class") or ""))
            for s in (model.SCENARIO_ZONE_LOSS, model.SCENARIO_REGION_LOSS)
            if _applicable(verdicts.get(s) or {})
        ]
        infra_ranks = [r for r in infra_ranks if r is not None]
        if not infra_ranks:
            continue
        # The WORST infrastructure answer, not the best. Taking the most flattering one
        # (a storage account that survives a zone loss automatically but needs hours for a
        # region loss) manufactures a gap against a number that was never the whole story.
        infra_worst = min(infra_ranks)

        worse: list[str] = []
        for scenario in sorted(model.LOGICAL_SCENARIOS):
            verdict = verdicts.get(scenario) or {}
            if not _applicable(verdict):
                continue
            rto_class = str(verdict.get("rto_class") or "")
            if rto_class == model.RTO_NONE or verdict.get("rpo_state") == model.RPO_NONE:
                worse.append(scenario)
                continue
            rank = model.rto_rank(rto_class)
            if rank is not None and infra_worst - rank >= REDUNDANCY_GAP_STEPS:
                worse.append(scenario)
        if not worse:
            continue

        first = verdicts.get(worse[0]) or {}
        out.append({
            "id": row.get("id", ""),
            "name": row.get("name", ""),
            "type": row.get("type", ""),
            "replication": redundancy.get("replication", ""),
            "zone_redundant": redundancy.get("zone_redundant"),
            "worse_for": [model.SCENARIO_LABEL.get(s, s) for s in worse],
            "infra_rto_class": next(
                (c for c in model.RTO_CLASSES if model.rto_rank(c) == infra_worst), ""),
            "logical_rto_class": str(first.get("rto_class") or ""),
            "unrecoverable": first.get("rto_class") == model.RTO_NONE,
            "reason": _reason(first),
        })
    out.sort(key=lambda e: (not e["unrecoverable"], -len(e["worse_for"]), e["name"]))
    return out


def analyze(snapshot: dict[str, Any], *, reason_limit: int = DEFAULT_REASON_LIMIT
            ) -> dict[str, Any]:
    """Everything the exporters and the UI lens need, from one pass over the rows."""
    rows = snapshot.get("resources") or []
    return {
        "resources": len(rows),
        "by_type": by_resource_type(rows),
        "rto_distribution": {s: rto_distribution(rows, s) for s in model.SCENARIOS},
        "rpo_distribution": {s: rpo_distribution(rows, s) for s in model.SCENARIOS},
        "reasons": reason_index(rows, limit=reason_limit),
        "worst_offenders": worst_offenders(rows),
        "redundancy_gap": redundancy_gap(rows),
    }


__all__ = [
    "analyze", "by_resource_type", "rto_distribution", "rpo_distribution",
    "reason_index", "worst_offenders", "redundancy_gap",
    "DEFAULT_REASON_LIMIT", "REDUNDANCY_GAP_STEPS",
]

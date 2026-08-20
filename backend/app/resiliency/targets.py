"""Targets and breach analysis — derived against agreed.

Two rules do most of the work here, and both have a plausible wrong answer:

* **``none`` always breaches.** No recovery path cannot meet any objective, whatever the
  tier says.
* **``unknown`` breaches nothing, and meets nothing.** It is its own bucket. Counting it as
  met hides risk; counting it as breached floods the list with items that need
  investigating rather than fixing, and those need different queues. A breach list that
  cries wolf gets ignored, which costs more than the finding was worth.
"""
from __future__ import annotations

from typing import Any

from app.resiliency import model, reference

STATE_MET = "met"
STATE_BREACHED = "breached"
STATE_UNDETERMINED = "undetermined"
STATE_NOT_APPLICABLE = "not_applicable"


def resolve_tier(
    *,
    resource_override: str = "",
    workload_override: str = "",
    workload_criticality: str = "",
    tags: dict[str, str] | None = None,
    tag_key: str = "criticality",
) -> tuple[str, str]:
    """``(tier_id, how_it_was_chosen)``.

    The chosen tier travels with every row: "breached" is not actionable if the reader
    cannot see which objective it was measured against, and a wrong tier is the most likely
    cause of a disputed finding.
    """
    if resource_override:
        return resource_override, "resource override"
    if workload_override:
        return workload_override, "workload override"
    if workload_criticality:
        return (reference.tier_for_criticality(workload_criticality),
                f"workload criticality '{workload_criticality}'")
    tag_value = (tags or {}).get(tag_key) or (tags or {}).get(tag_key.title())
    if tag_value:
        return (reference.tier_for_criticality(tag_value), f"tag {tag_key}={tag_value}")
    return reference.DEFAULT_TIER, "default tier"


def evaluate(verdict: dict[str, Any], target: dict[str, Any] | None) -> dict[str, Any]:
    """Compare one verdict against one objective."""
    if not verdict.get("applicable", True):
        return {"state": STATE_NOT_APPLICABLE, "rpo": False, "rto": False}
    if not target:
        return {"state": STATE_UNDETERMINED, "rpo": False, "rto": False,
                "reason": "No objective is set for this tier and scenario."}

    rto_class = verdict.get("rto_class")
    rpo_state = verdict.get("rpo_state")

    if rto_class == model.RTO_UNKNOWN or rpo_state == model.RPO_UNKNOWN:
        return {"state": STATE_UNDETERMINED, "rpo": False, "rto": False,
                "reason": "Not enough was readable to judge this against the objective."}

    rto_breach = False
    if rto_class == model.RTO_NONE:
        rto_breach = True
    else:
        want = model.rto_rank(str(target.get("rto_class") or ""))
        have = model.rto_rank(str(rto_class))
        if want is not None and have is not None:
            rto_breach = have < want

    rpo_breach = False
    if rpo_state == model.RPO_NONE:
        rpo_breach = True
    else:
        want_rpo = target.get("rpo_minutes")
        have_rpo = verdict.get("rpo_minutes")
        if want_rpo is not None and have_rpo is not None:
            rpo_breach = int(have_rpo) > int(want_rpo)

    return {
        "state": STATE_BREACHED if (rto_breach or rpo_breach) else STATE_MET,
        "rpo": rpo_breach,
        "rto": rto_breach,
        "target_rpo_minutes": target.get("rpo_minutes"),
        "target_rto_class": target.get("rto_class"),
    }


def apply_targets(rows: list[dict[str, Any]], *, tier_by_workload: dict[str, str] | None = None,
                  doc: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Attach target + breach to every verdict on every row."""
    doc = doc or reference.load()
    tier_by_workload = tier_by_workload or {}
    for row in rows:
        tier_id, how = resolve_tier(
            workload_criticality=tier_by_workload.get(row.get("workload_id", ""), ""),
        )
        if row.get("tier"):
            tier_id, how = row["tier"], row.get("tier_source", "row")
        tier = reference.tier_for(tier_id)
        row["tier"] = tier["id"]
        row["tier_label"] = tier.get("label", tier["id"])
        row["tier_source"] = how
        for scenario, verdict in row["verdicts"].items():
            target = (tier.get("scenarios") or {}).get(scenario)
            verdict["target"] = target
            verdict["breach"] = evaluate(verdict, target)
    return rows


# Sort by CONSEQUENCE, not count. A mission-critical database missing its RPO by an hour
# outranks forty low-tier VMs missing theirs by a day; sorting by breach count buries it.
_TIER_WEIGHT = {"mission_critical": 0, "business_critical": 1, "standard": 2, "low": 3}


def breaches(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every breached (resource, scenario) pair, worst consequence first."""
    out: list[dict[str, Any]] = []
    for row in rows:
        for scenario, verdict in row["verdicts"].items():
            breach = verdict.get("breach") or {}
            if breach.get("state") != STATE_BREACHED:
                continue
            no_path = verdict.get("rto_class") == model.RTO_NONE
            total_loss = verdict.get("rpo_state") == model.RPO_NONE
            magnitude = 0
            if verdict.get("rpo_minutes") is not None and breach.get("target_rpo_minutes"):
                magnitude = int(verdict["rpo_minutes"]) - int(breach["target_rpo_minutes"])
            out.append({
                "resource_id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "scenario": scenario,
                "tier": row.get("tier", ""),
                "rto_class": verdict.get("rto_class"),
                "rpo_minutes": verdict.get("rpo_minutes"),
                "rpo_state": verdict.get("rpo_state"),
                "target": verdict.get("target"),
                "no_recovery_path": no_path,
                "total_data_loss": total_loss,
                "basis": verdict.get("basis", []),
                "_sort": (
                    0 if no_path else (1 if total_loss else 2),
                    _TIER_WEIGHT.get(row.get("tier", ""), 9),
                    -magnitude,
                ),
            })
    out.sort(key=lambda b: b["_sort"])
    for item in out:
        item.pop("_sort", None)
    return out


def summarize_breaches(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {STATE_MET: 0, STATE_BREACHED: 0, STATE_UNDETERMINED: 0, STATE_NOT_APPLICABLE: 0}
    for row in rows:
        for verdict in row["verdicts"].values():
            state = (verdict.get("breach") or {}).get("state", STATE_UNDETERMINED)
            counts[state] = counts.get(state, 0) + 1
    return counts


__all__ = ["resolve_tier", "evaluate", "apply_targets", "breaches", "summarize_breaches",
           "STATE_MET", "STATE_BREACHED", "STATE_UNDETERMINED", "STATE_NOT_APPLICABLE"]

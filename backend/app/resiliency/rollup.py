"""Workload roll-up: one number per application, and the component that sets it.

**The weakest link is the deliverable, not the aggregate.** "Contoso Hotels: day_plus" is a
statistic; "Contoso Hotels is day_plus *because of one un-backed-up legacy VM*" is a work
item. Every rolled-up figure names its cause.

Three assumptions are stated rather than hidden, and they travel in the payload so an
exported figure carries the caveats that qualify it:

1. **Every component is treated as required.** ``max()`` is only correct when all of them
   must be recovered; for a genuinely redundant pair it should be ``min()``. We cannot
   reliably detect redundant pairs from Resource Graph, and overstating RTO is the safe
   direction — conservative-and-declared beats optimistic-and-hidden.
2. **Ordered recovery can exceed ``max()``.** Real recovery is sequenced, and sequences add.
   Computing a true critical path needs ordering metadata we do not have, so we report a
   stated lower bound rather than a fabricated precise number.
3. **Undetermined components are excluded and counted.** One unmapped Redis instance must
   not turn an application red, and it must not let a quarter-measured application look
   fully assessed either.
"""
from __future__ import annotations

from typing import Any

from app.resiliency import model

ASSUMPTIONS: tuple[str, ...] = (
    "Every component is treated as required; a genuinely redundant pair would recover faster.",
    "Components are assumed to recover in parallel — an ordered recovery can take longer.",
)


def roll_up(
    rows: list[dict[str, Any]],
    *,
    workload_id: str = "",
    workload_name: str = "",
    tier: str = "",
) -> dict[str, Any]:
    """Aggregate per-resource verdicts into one verdict per scenario for a workload."""
    scenarios: dict[str, Any] = {}

    for scenario in model.SCENARIOS:
        considered = [
            (row, row["verdicts"][scenario]) for row in rows
            if scenario in row.get("verdicts", {})
            and row["verdicts"][scenario].get("applicable", True)
        ]
        if not considered:
            scenarios[scenario] = {
                "applicable": False,
                "rto_class": model.RTO_UNKNOWN,
                "rpo_minutes": None,
                "rpo_state": model.RPO_UNKNOWN,
                "weakest_link": None,
                "coverage": {"determined": 0, "total": 0},
                "assumptions": list(ASSUMPTIONS),
            }
            continue

        classes = [v.get("rto_class", model.RTO_UNKNOWN) for _row, v in considered]
        worst_class, undetermined = model.worst_rto(classes)

        weakest = None
        for row, v in considered:
            if v.get("rto_class") == worst_class:
                weakest = {
                    "id": row["id"], "name": row["name"], "type": row["type"],
                    "reason": (v.get("basis") or [{}])[0].get("detail", ""),
                    "shared_platform": bool(row.get("shared_platform")),
                }
                break

        # RPO aggregates over what is KNOWN. `none` (total loss) beats any number.
        rpo_values = [v.get("rpo_minutes") for _r, v in considered
                      if v.get("rpo_state") == model.RPO_KNOWN and v.get("rpo_minutes") is not None]
        if any(v.get("rpo_state") == model.RPO_NONE for _r, v in considered):
            rpo_state, rpo_minutes = model.RPO_NONE, None
        elif rpo_values:
            rpo_state, rpo_minutes = model.RPO_KNOWN, max(rpo_values)
        else:
            rpo_state, rpo_minutes = model.RPO_UNKNOWN, None

        scenarios[scenario] = {
            "applicable": True,
            "rto_class": worst_class,
            "rpo_minutes": rpo_minutes,
            "rpo_state": rpo_state,
            "weakest_link": weakest,
            "coverage": {"determined": len(considered) - undetermined, "total": len(considered)},
            "assumptions": list(ASSUMPTIONS),
            "no_recovery_path": [
                {"id": row["id"], "name": row["name"]}
                for row, v in considered if v.get("rto_class") == model.RTO_NONE
            ],
        }

    return {
        "workload_id": workload_id,
        "name": workload_name,
        "tier": tier,
        "components": len(rows),
        "scenarios": scenarios,
        "worst": _headline(scenarios),
    }


def _headline(scenarios: dict[str, Any]) -> dict[str, Any]:
    """The one line an exec reads: the worst scenario and what causes it."""
    applicable = {s: v for s, v in scenarios.items() if v.get("applicable")}
    if not applicable:
        return {"scenario": "", "rto_class": model.RTO_UNKNOWN, "weakest_link": None}
    worst_scenario = min(
        applicable.items(),
        key=lambda kv: (model.rto_rank(kv[1]["rto_class"])
                        if model.rto_rank(kv[1]["rto_class"]) is not None else 99),
    )
    return {
        "scenario": worst_scenario[0],
        "rto_class": worst_scenario[1]["rto_class"],
        "weakest_link": worst_scenario[1].get("weakest_link"),
    }


def group_by_workload(
    rows: list[dict[str, Any]], *, names: dict[str, str] | None = None,
    tiers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    names, tiers = names or {}, tiers or {}
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("workload_id") or ""), []).append(row)
    out = [
        roll_up(members, workload_id=wid, workload_name=names.get(wid, wid) or "Unassigned",
                tier=tiers.get(wid, ""))
        for wid, members in buckets.items()
    ]
    out.sort(key=lambda w: w["name"].lower())
    return out


__all__ = ["roll_up", "group_by_workload", "ASSUMPTIONS"]

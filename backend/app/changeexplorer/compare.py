"""Compare two Change Explorer runs (feature E2).

Given two persisted runs (e.g. before/after a deployment window), produce a structured diff of
their changed-resource sets + risk movement so a reviewer can answer "what changed between these
two points". Pure, read-only.
"""
from __future__ import annotations

from typing import Any


def _by_resource(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in run.get("events", []) or []:
        rid = e.get("resourceId", "")
        if not rid:
            continue
        g = out.setdefault(rid, {
            "resourceId": rid, "resourceName": e.get("resourceName", ""),
            "resourceType": e.get("resourceType", ""), "changes": 0,
            "highestRiskScore": 0, "highestRiskLabel": "Informational",
        })
        g["changes"] += 1
        if int(e.get("riskScore", 0)) > g["highestRiskScore"]:
            g["highestRiskScore"] = int(e.get("riskScore", 0))
            g["highestRiskLabel"] = e.get("riskLabel", "Informational")
    return out


def compare_runs(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    """Diff run A (baseline) vs run B (later). Returns added/removed/changed resources + count deltas.

    - added:   resources changed in B but not A
    - removed: resources changed in A but not B
    - changed: resources changed in BOTH (risk may have moved)
    """
    ra, rb = _by_resource(run_a), _by_resource(run_b)
    a_ids, b_ids = set(ra), set(rb)

    added = [rb[i] for i in (b_ids - a_ids)]
    removed = [ra[i] for i in (a_ids - b_ids)]
    changed = []
    for i in (a_ids & b_ids):
        ga, gb = ra[i], rb[i]
        changed.append({
            **gb,
            "changesA": ga["changes"], "changesB": gb["changes"],
            "riskA": ga["highestRiskScore"], "riskB": gb["highestRiskScore"],
            "riskLabelA": ga["highestRiskLabel"], "riskLabelB": gb["highestRiskLabel"],
            "riskDelta": gb["highestRiskScore"] - ga["highestRiskScore"],
        })
    added.sort(key=lambda r: -r["highestRiskScore"])
    removed.sort(key=lambda r: -r["highestRiskScore"])
    changed.sort(key=lambda r: -abs(r["riskDelta"]))

    def _counts(run: dict[str, Any]) -> dict[str, Any]:
        return {
            "total": run.get("totalChanges", 0),
            "critical": run.get("criticalCount", 0), "high": run.get("highCount", 0),
            "medium": run.get("mediumCount", 0), "low": run.get("lowCount", 0),
            "window": f"{run.get('startTime','')} → {run.get('endTime','')}",
            "runId": run.get("runId", ""),
        }

    return {
        "a": _counts(run_a),
        "b": _counts(run_b),
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": {
            "added": len(added), "removed": len(removed), "changed": len(changed),
            "total_delta": run_b.get("totalChanges", 0) - run_a.get("totalChanges", 0),
            "critical_delta": run_b.get("criticalCount", 0) - run_a.get("criticalCount", 0),
            "high_delta": run_b.get("highCount", 0) - run_a.get("highCount", 0),
        },
    }

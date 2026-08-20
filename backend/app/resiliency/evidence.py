"""Recovery Readiness → an Evidence Locker snapshot.

Content, not a rendered PDF. A PDF's hash proves only that the file has not changed; the
content behind it can be diffed against a later capture, re-rendered, and checked line by
line — which is what "evidence" has to mean when somebody disputes it a year later.

Mapped into the locker's existing sections (``findings`` / ``metrics`` / ``inventory``)
rather than a new one, so diff, share and export keep working unchanged.
"""
from __future__ import annotations

from typing import Any

from app.resiliency import analysis, model

#: One finding per resource-scenario that has no recovery path or breaches its objective.
#: Bounded, because a locker entry is a record of a judgment, not a copy of the estate.
MAX_FINDINGS = 2000


def _severity(verdict: dict[str, Any]) -> str:
    if verdict.get("rto_class") == model.RTO_NONE:
        return "critical"
    if verdict.get("rto_class") == model.RTO_UNKNOWN:
        return "info"
    return "warning"


def build_evidence_content(
    snapshot: dict[str, Any], *, reference_doc: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], list[str], list[str], dict[str, Any]]:
    """``(name, scope, included, tags, content)`` for ``evidence.registry.create_snapshot``."""
    scope = snapshot.get("scope") or {}
    summary = snapshot.get("summary") or {}
    rows = snapshot.get("resources") or []
    facts = analysis.analyze(snapshot, reason_limit=100)

    findings: list[dict[str, Any]] = []
    for row in rows:
        if len(findings) >= MAX_FINDINGS:
            break
        for scenario in model.SCENARIOS:
            verdict = (row.get("verdicts") or {}).get(scenario) or {}
            if not verdict.get("applicable", True):
                continue
            no_path = verdict.get("rto_class") == model.RTO_NONE
            breached = (verdict.get("breach") or {}).get("state") == "breached"
            if not (no_path or breached):
                continue
            findings.append({
                "id": f"recovery-{row.get('id', '')}-{scenario}",
                "title": (f"{row.get('name', '')} — "
                          f"{model.SCENARIO_LABEL.get(scenario, scenario)}: "
                          f"{model.RTO_LABEL.get(verdict.get('rto_class', ''), '')}"),
                "severity": _severity(verdict),
                "status": "no_recovery_path" if no_path else "breached",
                "resource_id": row.get("id", ""),
                "resource_name": row.get("name", ""),
                "resource_type": row.get("type", ""),
                "scenario": scenario,
                "rto_class": verdict.get("rto_class", ""),
                "rpo_state": verdict.get("rpo_state", ""),
                "rpo_minutes": verdict.get("rpo_minutes"),
                "confidence": verdict.get("confidence", ""),
                # The reasoning travels with the finding: a frozen verdict whose basis was
                # left behind cannot be argued with, only believed.
                "basis": [e.get("detail", "") for e in verdict.get("basis") or []],
                "assumptions": list(verdict.get("rto_assumptions") or []),
                "feature": "resiliency",
            })
            if len(findings) >= MAX_FINDINGS:
                break

    metrics = {
        "feature": "resiliency",
        "headline_label": "Resources with no recovery path",
        "resources": summary.get("resources", 0),
        "no_recovery_path": (summary.get("worst") or {}).get("no_recovery_path", 0),
        "breaches": len(snapshot.get("breaches") or []),
        "protection": summary.get("protection") or {},
        "by_scenario": summary.get("by_scenario") or {},
        "by_type": facts["by_type"],
        "rto_distribution": facts["rto_distribution"],
        "rpo_distribution": facts["rpo_distribution"],
        "reasons": facts["reasons"],
        "redundancy_gap": facts["redundancy_gap"],
        # Without these the targets in every finding are unverifiable a year from now.
        "objectives_version": (reference_doc or {}).get("version"),
        "objectives": (reference_doc or {}).get("tiers") or [],
        "restore_rates": (reference_doc or {}).get("restore_rates") or {},
        "mechanism_minutes": (reference_doc or {}).get("mechanism_minutes") or {},
        "targets_acknowledged": bool(snapshot.get("targets_acknowledged")),
        # Carried so a later reader can tell "nothing found" from "could not look".
        "provenance": snapshot.get("provenance") or {},
        "truncation": snapshot.get("truncation") or {},
        "findings_truncated": len(findings) >= MAX_FINDINGS,
    }

    label = str(scope.get("scope_name") or scope.get("scope_id") or "scope")
    name = f"Recovery Readiness — {label} — {snapshot.get('generated_at', '')[:16]}"
    return (
        name,
        {"kind": scope.get("scope_kind", ""), "id": scope.get("scope_id", ""), "name": label},
        ["findings", "metrics", "inventory"],
        ["resiliency", "recovery"],
        {"findings": findings, "metrics": metrics, "inventory": rows},
    )


__all__ = ["build_evidence_content", "MAX_FINDINGS"]

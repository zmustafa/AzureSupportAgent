"""Recovery Readiness → the Assessments Reliability pillar.

A tenant should not score well on Reliability while holding resources that cannot be
recovered at all. This module turns the recovery analysis into a small number of
high-weight controls so it counts.

Two shapes are deliberate:

* **Aggregate, never one finding per resource.** One row per affected resource explodes on
  real data — this repo has already produced 1,262 "patterns" that way once. Each control is
  a single finding carrying a bounded sample.
* **It contributes nothing when there is no analysis.** A control that fails because nobody
  has run Recovery Readiness would be reporting our own absence as the tenant's risk. With
  no snapshot the controls report ``not_applicable`` and are excluded from the score.
"""
from __future__ import annotations

from typing import Any

from app.resiliency import model

#: Bounded so a large estate cannot make one finding dominate the payload.
MAX_SAMPLE = 20

CONTROLS: list[dict[str, Any]] = [
    {
        "id": "recovery.no_path",
        "title": "Every resource has a recovery path for each failure it can experience",
        "description": (
            "A resource with no recovery path for a scenario cannot be restored from that "
            "failure at all — it is not a question of how long. Most commonly a resource "
            "with no backup, or one whose backups live in a vault that dies with the region."
        ),
        "severity": "critical",
        "sub_category": "Disaster recovery",
        "remediation": (
            "Open Recovery Readiness, filter to 'no recovery path', and give each resource a "
            "recovery mechanism appropriate to the scenario — backup for logical loss, "
            "geo-redundancy or replication for region loss."
        ),
    },
    {
        "id": "recovery.logical_unprotected",
        "title": "Redundant resources also have point-in-time recovery",
        "description": (
            "Zone- and geo-redundancy replicate corruption and deletion, usually within "
            "seconds. A resource that is redundant but has no point-in-time copy passes "
            "every redundancy check and cannot be recovered from a bad deployment or "
            "ransomware."
        ),
        "severity": "error",
        "sub_category": "Disaster recovery",
        "remediation": (
            "Enable a point-in-time recovery mechanism: vault backup, or the platform's own "
            "(SQL PITR, Cosmos DB continuous backup, blob point-in-time restore)."
        ),
    },
    {
        "id": "recovery.target_breach",
        "title": "Resources meet their recovery objectives",
        "description": (
            "The derived recovery point or recovery time is worse than the objective set for "
            "the resource's criticality tier."
        ),
        "severity": "warning",
        "sub_category": "Disaster recovery",
        "remediation": (
            "Either raise the protection (more frequent backup, replication, geo-redundant "
            "vault) or agree a different objective for the tier in Recovery Readiness."
        ),
    },
]

_CONTROL_IDS = {c["id"] for c in CONTROLS}


def _check(spec: dict[str, Any]) -> dict[str, Any]:
    from app.assessments.catalog import SEVERITY_WEIGHT

    return {
        "id": spec["id"],
        "pillar": "reliability",
        "title": spec["title"],
        "description": spec["description"],
        "severity": spec["severity"],
        "weight": SEVERITY_WEIGHT.get(spec["severity"], 3),
        "resource_types": [],
        "kql": "",
        "remediation": spec["remediation"],
        "remediation_command": "",
        "frameworks": {},
        "kind": "recovery",
        "impact": "high",
        "effort": "medium",
        "sub_category": spec["sub_category"],
        "source": "recovery-readiness",
        "learn_more": [],
        "arg_table": "Resources",
        "expectation": "",
        "profile": "",
        "scope_mode": "",
    }


def checks() -> list[dict[str, Any]]:
    return [_check(spec) for spec in CONTROLS]


def _subject(row: dict[str, Any], scenario: str, why: str) -> dict[str, Any]:
    return {
        "id": row.get("id", ""),
        "name": row.get("name", ""),
        "type": row.get("type", ""),
        "resource_group": row.get("resource_group", ""),
        "subscription_id": row.get("subscription_id", ""),
        "location": row.get("location", ""),
        "detail": f"{model.SCENARIO_LABEL.get(scenario, scenario)}: {why}",
        "remediation_command": "",
    }


def _finding(check: dict[str, Any], subjects: list[dict[str, Any]], total: int) -> dict[str, Any]:
    base = {
        "check_id": check["id"], "pillar": check["pillar"], "title": check["title"],
        "description": check["description"], "severity": check["severity"],
        "weight": check["weight"], "frameworks": check["frameworks"],
        "remediation": check["remediation"], "remediation_command": "",
        "resource_types": [], "kind": "recovery", "impact": check["impact"],
        "effort": check["effort"], "sub_category": check["sub_category"],
        "source": check["source"], "profile": "", "learn_more": [],
        "flagged_count": total, "flagged_resources": subjects[:MAX_SAMPLE],
        "partial": total > MAX_SAMPLE, "ai_rationale": "",
        "status": "fail" if total else "pass",
    }
    return base


def findings_from(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Reliability-pillar findings derived from a Recovery Readiness snapshot.

    Returns ``not_applicable`` controls when no analysis exists — reporting our own absence
    as the tenant's risk would be worse than contributing nothing.
    """
    specs = {c["id"]: c for c in checks()}
    if not snapshot or not snapshot.get("report_exists"):
        out = []
        for check in specs.values():
            finding = _finding(check, [], 0)
            finding["status"] = "not_applicable"
            finding["error"] = ""
            finding["ai_rationale"] = (
                "Recovery Readiness has not analyzed this workload, so recovery could not be "
                "judged. This is not a statement that the workload is unrecoverable.")
            out.append(finding)
        return out

    rows = snapshot.get("resources") or []

    no_path: list[dict[str, Any]] = []
    logical: list[dict[str, Any]] = []
    breached: list[dict[str, Any]] = []

    for row in rows:
        redundant = bool((row.get("redundancy") or {}).get("zone_redundant")) or bool(
            (row.get("redundancy") or {}).get("replication"))
        for scenario, verdict in (row.get("verdicts") or {}).items():
            if not verdict.get("applicable", True):
                continue
            why = (verdict.get("basis") or [{}])[0].get("detail", "")
            if verdict.get("rto_class") == model.RTO_NONE:
                no_path.append(_subject(row, scenario, why))
                if redundant and scenario in model.LOGICAL_SCENARIOS:
                    logical.append(_subject(row, scenario, why))
                continue
            if (verdict.get("breach") or {}).get("state") == "breached":
                breached.append(_subject(row, scenario, why))

    return [
        _finding(specs["recovery.no_path"], no_path, len(no_path)),
        _finding(specs["recovery.logical_unprotected"], logical, len(logical)),
        _finding(specs["recovery.target_breach"], breached, len(breached)),
    ]


def enabled(settings: dict[str, Any] | None = None) -> bool:
    """Whether recovery contributes to the Reliability score.

    Off by default. Turning it on moves an existing tenant's Reliability score, which reads
    as a regression to anyone tracking a trend line — that is a change to announce, not to
    ship silently.
    """
    if settings is None:
        from app.core.app_settings import load_settings

        settings = load_settings()
    return bool(settings.get("assessments_include_recovery", False))


__all__ = ["checks", "findings_from", "enabled", "CONTROLS", "MAX_SAMPLE"]

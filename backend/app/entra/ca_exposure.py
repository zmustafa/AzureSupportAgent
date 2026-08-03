"""The exposure view: one row per application class, ordered by what is actually exposed.

The coverage matrix answers "what is the state of this cell?". That is the right shape for
auditing and the wrong shape for deciding what to do on a Tuesday morning — 168 cells do not
sort themselves into an order of work. This module collapses the matrix down the control axis
into one row per class, attaches the findings that fired against it and the reviewed impact
copy, and sorts by exposure so the first row is the one worth reading.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.entra import ca_taxonomy
from app.entra.ca_coverage import CELL_COVERED, CELL_NA, CELL_PARTIAL, CELL_REPORT_ONLY

_DATA = Path(__file__).resolve().parent / "data"

_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


@lru_cache(maxsize=4)
def impact_copy(version: str = "1") -> dict[str, Any]:
    path = _DATA / f"impact_copy.v{version}.json"
    if not path.exists():
        return {"copy": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def copy_for(signal_id: str, class_id: str, version: str = "1") -> dict[str, Any] | None:
    return ((impact_copy(version).get("copy") or {}).get(signal_id) or {}).get(class_id)


def build(
    coverage: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    cohort: str = "members",
) -> dict[str, Any]:
    classes = coverage.get("app_classes") or []
    controls = coverage.get("controls") or []
    row = next((r for r in coverage.get("matrix") or [] if r.get("cohort") == cohort), None)
    cells = (row or {}).get("cells") or {}

    by_class: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        oid = str(f.get("object_id") or "")
        cid = oid.split("|", 1)[0]
        by_class.setdefault(cid, []).append(f)

    rows: list[dict[str, Any]] = []
    for cls in classes:
        cid = cls["id"]
        applicable = [c for c in controls
                      if (cells.get(f"{cid}|{c['key']}") or {}).get("state") != CELL_NA]
        states = {c["key"]: (cells.get(f"{cid}|{c['key']}") or {}).get("state") for c in applicable}
        covered = sum(1 for s in states.values() if s == CELL_COVERED)
        partial = sum(1 for s in states.values() if s == CELL_PARTIAL)
        report_only = sum(1 for s in states.values() if s == CELL_REPORT_ONLY)
        total = len(applicable)

        cls_findings = by_class.get(cid, [])
        worst = max((_SEVERITY_RANK.get(str(f.get("severity")), 0) for f in cls_findings), default=0)

        # Exposure is deliberately NOT 1 - covered/total. A class with twelve applicable controls
        # and one that matters is not 92% safe. Severity of what actually fired leads; the
        # control ratio only breaks ties between classes with the same worst finding.
        uncovered_ratio = 0.0 if not total else (total - covered - 0.5 * partial) / total
        rows.append({
            "class_id": cid,
            "label": cls["label"],
            "description": cls.get("description", ""),
            "derived": bool(cls.get("derived")),
            "controls_total": total,
            "controls_covered": covered,
            "controls_partial": partial,
            "controls_report_only": report_only,
            "states": states,
            "worst_severity": next((k for k, v in _SEVERITY_RANK.items() if v == worst), "info"),
            "finding_count": len(cls_findings),
            "findings": [{
                "signal_id": f.get("signal_id"),
                "severity": f.get("severity"),
                "title": f.get("title"),
                "detail": f.get("detail"),
                "impact": copy_for(str(f.get("signal_id") or ""), cid),
            } for f in sorted(
                cls_findings,
                key=lambda f: -_SEVERITY_RANK.get(str(f.get("severity")), 0),
            )[:10]],
            "exposure": round(uncovered_ratio, 3),
            "sort_key": (worst, round(uncovered_ratio, 3)),
        })

    rows.sort(key=lambda r: (-r["sort_key"][0], -r["sort_key"][1], r["label"]))
    for r in rows:
        r.pop("sort_key", None)

    derived = coverage.get("derived") or {}
    return {
        "cohort": cohort,
        "taxonomy_version": coverage.get("taxonomy_version") or ca_taxonomy.DEFAULT_VERSION,
        "impact_copy_version": impact_copy().get("version", "1"),
        "rows": rows,
        "unattributed": derived.get("unattributed_apps") or {},
        "shadowed": derived.get("shadowed_classes") or {},
        "app_index": coverage.get("app_index") or {},
    }


def to_csv_rows(exposure: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in exposure.get("rows") or []:
        out.append({
            "Application class": r["label"],
            "Class id": r["class_id"],
            "Derived": "yes" if r["derived"] else "no",
            "Worst severity": r["worst_severity"],
            "Findings": r["finding_count"],
            "Controls covered": r["controls_covered"],
            "Controls partial": r["controls_partial"],
            "Controls report-only": r["controls_report_only"],
            "Controls applicable": r["controls_total"],
            "Exposure": r["exposure"],
            "Top finding": (r["findings"][0]["title"] if r["findings"] else ""),
        })
    return out

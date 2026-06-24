"""Pure FMEA scoring helpers (no I/O).

The Risk Priority Number (RPN) is the product of three 1-10 factors — Severity, Occurrence
and Detection. It is ALWAYS derived here (never stored as an authoritative client value) so
the grid, the export and the risk summary can never disagree. Factors are clamped to 1-10;
``0``/``None``/missing means "not scored yet" and yields a ``None`` RPN.
"""
from __future__ import annotations

from typing import Any

# Risk bands by RPN. Tuned so a maxed-out factor triple (10x10x10 = 1000) lands in
# "critical" and a benign one (1x1x1 = 1) lands in "low". These thresholds also drive the
# per-cell colour ramp in the UI and the risk summary counts.
_CRITICAL = 200
_HIGH = 120
_MEDIUM = 40


def normalize_factor(value: Any) -> int:
    """Coerce a Severity/Occurrence/Detection input to an int in 0..10.

    Returns ``0`` for anything missing/blank/non-numeric (meaning "not scored"). Real scores
    are clamped into the 1-10 band an FMEA uses.
    """
    if value is None or value == "":
        return 0
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return 10 if n > 10 else n


def rpn(severity: Any, occurrence: Any, detection: Any) -> int | None:
    """Risk Priority Number = Severity x Occurrence x Detection, or ``None`` if any factor
    is not yet scored (so a half-filled row never shows a misleading RPN)."""
    s = normalize_factor(severity)
    o = normalize_factor(occurrence)
    d = normalize_factor(detection)
    if s == 0 or o == 0 or d == 0:
        return None
    return s * o * d


def risk_band(value: int | None) -> str:
    """Bucket an RPN into ``critical|high|medium|low|none`` for colouring and summaries."""
    if value is None:
        return "none"
    if value >= _CRITICAL:
        return "critical"
    if value >= _HIGH:
        return "high"
    if value >= _MEDIUM:
        return "medium"
    return "low"


def factor_band(value: Any) -> str:
    """Bucket a single 1-10 factor into ``high|medium|low|none`` for the cell colour ramp."""
    n = normalize_factor(value)
    if n == 0:
        return "none"
    if n >= 8:
        return "high"
    if n >= 4:
        return "medium"
    return "low"


def recompute_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a row's factors and (re)derive its RPN + risk band, in place, returning it.

    Computes both the initial RPN (severity/occurrence/detection) and the post-mitigation
    "FMEA Results" RPN (severity_post/occurrence_post/detection_post).
    """
    row["severity"] = normalize_factor(row.get("severity"))
    row["occurrence"] = normalize_factor(row.get("occurrence"))
    row["detection"] = normalize_factor(row.get("detection"))
    row["severity_post"] = normalize_factor(row.get("severity_post"))
    row["occurrence_post"] = normalize_factor(row.get("occurrence_post"))
    row["detection_post"] = normalize_factor(row.get("detection_post"))
    r = rpn(row["severity"], row["occurrence"], row["detection"])
    rp = rpn(row["severity_post"], row["occurrence_post"], row["detection_post"])
    row["rpn"] = r
    row["rpn_post"] = rp
    row["risk_band"] = risk_band(r)
    row["risk_band_post"] = risk_band(rp)
    return row


def recompute_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Recompute every row's RPN across every table in a document (in place)."""
    for table in doc.get("tables", []) or []:
        for row in table.get("rows", []) or []:
            recompute_row(row)
    return doc


def summarize(doc: dict[str, Any]) -> dict[str, Any]:
    """A compact risk roll-up for the document header: per-band counts, total rows, the
    highest current RPN, and how many rows have been mitigated (an RPN_post improvement)."""
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "none": 0}
    total = 0
    scored = 0
    top_rpn = 0
    mitigated = 0
    open_actions = 0
    for table in doc.get("tables", []) or []:
        for row in table.get("rows", []) or []:
            total += 1
            r = rpn(row.get("severity"), row.get("occurrence"), row.get("detection"))
            counts[risk_band(r)] += 1
            if r is not None:
                scored += 1
                top_rpn = max(top_rpn, r)
            rp = rpn(row.get("severity_post"), row.get("occurrence_post"), row.get("detection_post"))
            if r is not None and rp is not None and rp < r:
                mitigated += 1
            actions = str(row.get("recommended_actions") or "").strip()
            if actions and rp is None:
                open_actions += 1
    return {
        "counts": counts,
        "total_rows": total,
        "scored_rows": scored,
        "top_rpn": top_rpn,
        "mitigated_rows": mitigated,
        "open_actions": open_actions,
    }

"""Composite Workload Health Score.

A single 0-100 number per workload that blends the per-signal health metrics (monitoring,
telemetry, backup/DR, performance, ownership, policy, tags). It's the "credit score" that
lets the fleet be triaged worst-first.

Design:
* Only signals that have actually been analyzed contribute. A never-computed signal is
  ``None`` and is EXCLUDED (the score is the weighted average over PRESENT signals, with the
  weights re-normalized) — so an un-analyzed workload doesn't get a misleading low score.
* Weights are admin-tunable (``workload_health_weights`` in app settings). A workload with no
  analyzed signals has a ``None`` score (UI shows "Not analyzed").

Pure / offline — no Azure, no I/O beyond reading the settings dict the caller passes in.
"""
from __future__ import annotations

from typing import Any

# The signals that feed the composite score, in display order. Each maps to a 0-100 metric
# on the WorkloadProfile.health block.
SIGNALS: tuple[str, ...] = (
    "monitoring",
    "telemetry",
    "backupdr",
    "performance",
    "ownership",
    "policy",
    "tags",
)

# Built-in default weights (mirrors app_settings DEFAULTS["workload_health_weights"]). Backup
# is weighted a little higher because an unrecoverable workload is the worst failure mode.
DEFAULT_WEIGHTS: dict[str, float] = {
    "monitoring": 1.0,
    "telemetry": 1.0,
    "backupdr": 1.5,
    "performance": 1.0,
    "ownership": 1.0,
    "policy": 1.0,
    "tags": 0.5,
}

# Score color bands (shared with the frontend): >= GOOD green, >= WARN amber, else red.
SCORE_GOOD = 80
SCORE_WARN = 50


def resolve_weights(settings: dict[str, Any] | None) -> dict[str, float]:
    """Merge admin overrides onto the defaults; ignore unknown keys / non-numeric values."""
    weights = dict(DEFAULT_WEIGHTS)
    override = (settings or {}).get("workload_health_weights") or {}
    if isinstance(override, dict):
        for k, v in override.items():
            if k in DEFAULT_WEIGHTS:
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv >= 0:
                    weights[k] = fv
    return weights


def _clamp(v: Any) -> float | None:
    """Coerce a metric to a 0-100 float, or None if missing/invalid."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, f))


def composite_score(health: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute the composite score over the PRESENT signals.

    ``health`` is the per-signal metric map ({signal: 0-100 | None}). Returns
    ``{score, band, contributing, missing, weights}`` where ``score`` is None when no signal
    has been analyzed."""
    weights = resolve_weights(settings)
    num = 0.0
    den = 0.0
    contributing: list[str] = []
    missing: list[str] = []
    for sig in SIGNALS:
        val = _clamp(health.get(sig))
        if val is None:
            missing.append(sig)
            continue
        w = weights.get(sig, 1.0)
        num += w * val
        den += w
        contributing.append(sig)
    score = round(num / den) if den > 0 else None
    return {
        "score": score,
        "band": band_for(score),
        "contributing": contributing,
        "missing": missing,
        "weights": weights,
    }


def band_for(score: int | float | None) -> str:
    """Map a score to a color band: good | warn | poor | unknown."""
    if score is None:
        return "unknown"
    if score >= SCORE_GOOD:
        return "good"
    if score >= SCORE_WARN:
        return "warn"
    return "poor"

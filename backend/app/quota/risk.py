"""Risk evaluation for quota results — configurable thresholds.

Thresholds come from app settings (``quota_threshold_watch/warning/critical``) so an operator can
retune the bands without code changes. The evaluator is a pure function over a ``QuotaResult`` so
it is trivially unit-testable."""
from __future__ import annotations

from typing import Any

from app.quota.model import QuotaResult, RiskLevel

# Defaults (percent). Healthy < watch <= Watch < warning <= Warning < critical <= Critical.
DEFAULT_THRESHOLDS: dict[str, float] = {"watch": 70.0, "warning": 85.0, "critical": 95.0}


def load_thresholds() -> dict[str, float]:
    """Read thresholds from app settings, clamped + ordered. Never raises."""
    try:
        from app.core.app_settings import load_settings

        s = load_settings()
        watch = float(s.get("quota_threshold_watch", DEFAULT_THRESHOLDS["watch"]))
        warning = float(s.get("quota_threshold_warning", DEFAULT_THRESHOLDS["warning"]))
        critical = float(s.get("quota_threshold_critical", DEFAULT_THRESHOLDS["critical"]))
    except Exception:  # noqa: BLE001
        watch, warning, critical = (
            DEFAULT_THRESHOLDS["watch"], DEFAULT_THRESHOLDS["warning"], DEFAULT_THRESHOLDS["critical"],
        )
    # Enforce ordering + sane bounds so a bad config can't invert the bands.
    watch = max(1.0, min(99.0, watch))
    warning = max(watch + 1.0, min(99.5, warning))
    critical = max(warning + 0.5, min(100.0, critical))
    return {"watch": watch, "warning": warning, "critical": critical}


def evaluate_risk(percent_used: float | None, *, has_limit: bool, thresholds: dict[str, float]) -> str:
    """Map a usage percentage to a risk band. No dynamic limit → Unknown."""
    if not has_limit or percent_used is None:
        return RiskLevel.UNKNOWN
    if percent_used >= thresholds["critical"]:
        return RiskLevel.CRITICAL
    if percent_used >= thresholds["warning"]:
        return RiskLevel.WARNING
    if percent_used >= thresholds["watch"]:
        return RiskLevel.WATCH
    return RiskLevel.HEALTHY


def apply_risk(result: QuotaResult, thresholds: dict[str, float]) -> QuotaResult:
    """Set ``risk_level`` on a result in place (preserving an already-set ThrottlingObserved /
    error Unknown). Returns the same result for chaining."""
    if result.risk_level == RiskLevel.THROTTLING:
        return result
    has_limit = result.limit is not None and result.limit > 0
    result.compute_derived()
    result.risk_level = evaluate_risk(result.percent_used, has_limit=has_limit, thresholds=thresholds)
    return result

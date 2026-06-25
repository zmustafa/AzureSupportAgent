"""By-design saturated quotas — limits that legitimately sit at (or near) 100% as a normal part
of how Azure works, so flagging them Critical is a false alarm.

Two cases:
- **Singletons** (e.g. Network Watchers): Azure auto-creates exactly one per region per subscription
  (limit 1), so a deployed region always reports 1/1 = 100%. Expected, not a capacity concern.
- **Countdown / free-trial counters** (e.g. SQL "Free to Basic Database Upgrade count-down",
  "Free Database Days Left", "Free Tokens Left"): these measure REMAINING allowance/time, where a
  full value (100%) is HEALTHY, not exhausted. Treating them as usage-vs-limit inverts the meaning.

Matching rows are kept (for completeness) but downgraded to an informational ``Unknown`` risk with
a clear explanation, so they never inflate the Critical/Warning counts."""
from __future__ import annotations

from app.quota.model import AdjustableStatus, QuotaResult, RiskLevel, SourceType

# (provider_namespace lower, quota_name substring lower) → by-design saturated singleton.
_BY_DESIGN: list[tuple[str, str]] = [
    ("microsoft.network", "network watcher"),
]

# Substrings (any provider) marking a "remaining allowance / countdown" counter where 100% is good.
_COUNTDOWN_SUBSTRINGS = (
    "count-down", "countdown", "days left", "days-left", "tokens left", "token refresh",
    "months left", "free database upgrade", "instances left", "tokens-left",
)


def is_by_design_saturated(provider_namespace: str, quota_name: str) -> bool:
    p = (provider_namespace or "").lower()
    n = (quota_name or "").lower()
    if any(p == prov and sub in n for prov, sub in _BY_DESIGN):
        return True
    return any(s in n for s in _COUNTDOWN_SUBSTRINGS)


def apply(result: QuotaResult) -> bool:
    """If the result is a by-design-saturated quota (singleton at limit, or a remaining-allowance
    countdown), downgrade it to an informational Unknown with an explanatory recommendation.
    Returns True when it adjusted the row (so the caller skips the normal recommendation)."""
    if result.source_type in (SourceType.STATIC_LIMIT, SourceType.MANUAL_REVIEW):
        return False
    p = (result.provider_namespace or "").lower()
    n = (result.quota_name or "").lower()
    is_countdown = any(s in n for s in _COUNTDOWN_SUBSTRINGS)
    is_singleton = any(p == prov and sub in n for prov, sub in _BY_DESIGN)
    if not is_countdown and not is_singleton:
        return False
    # Singletons are only informational when actually saturated; with headroom keep normal scoring.
    if is_singleton and not is_countdown and (result.percent_used or 0) < 100:
        return False
    result.risk_level = RiskLevel.UNKNOWN
    result.adjustable_status = AdjustableStatus.HARD_LIMIT
    if is_countdown:
        result.recommendation = (
            f"{result.quota_name} is a remaining-allowance counter (e.g. free-trial days/tokens "
            "left); a high value is healthy, not exhausted. Not a capacity concern."
        )
    else:
        result.recommendation = (
            f"{result.quota_name} is a by-design singleton (Azure auto-creates one per region per "
            "subscription, regional limit 1). 100% is expected and is not a capacity concern."
        )
    return True

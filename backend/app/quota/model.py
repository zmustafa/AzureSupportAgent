"""Normalized quota data model + enums.

Every collector emits ``QuotaResult`` rows in this shape, regardless of which Azure API (or
static table) they came from, so the UI, risk engine, and recommendation engine all consume one
schema. ``raw_provider_response`` is redacted/trimmed before persistence (see ``redact``)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------- enums
class SourceType:
    """How a quota figure was obtained — drives the UI 'collection method' column."""

    MICROSOFT_QUOTA = "MicrosoftQuota"
    RP_USAGE_API = "ResourceProviderUsageApi"
    RESOURCE_GRAPH = "AzureResourceGraph"
    MONITOR_METRIC = "AzureMonitorMetric"
    STATIC_LIMIT = "StaticServiceLimit"
    MANUAL_REVIEW = "ManualReviewRequired"
    NOT_SUPPORTED = "NotSupported"


class RiskLevel:
    HEALTHY = "Healthy"        # < watch threshold
    WATCH = "Watch"           # watch..warning
    WARNING = "Warning"       # warning..critical
    CRITICAL = "Critical"     # >= critical
    UNKNOWN = "Unknown"       # no dynamic limit available
    THROTTLING = "ThrottlingObserved"  # recent 429 / rate-limit detected


# Ordering used to sort/aggregate (higher = more urgent).
RISK_RANK: dict[str, int] = {
    RiskLevel.CRITICAL: 5,
    RiskLevel.THROTTLING: 4,
    RiskLevel.WARNING: 3,
    RiskLevel.WATCH: 2,
    RiskLevel.UNKNOWN: 1,
    RiskLevel.HEALTHY: 0,
}


class AdjustableStatus:
    ADJUSTABLE = "Adjustable"            # raise via Quota blade / support, self-service
    HARD_LIMIT = "HardLimit"            # per-resource/platform hard cap — redesign needed
    SUPPORT_REQUIRED = "SupportRequired"  # adjustable only via a support request
    UNKNOWN = "Unknown"


class CollectionStatus:
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"
    NOT_REGISTERED = "provider_not_registered"
    UNAUTHORIZED = "unauthorized"
    NOT_SUPPORTED = "not_supported"


# --------------------------------------------------------------------------- result
@dataclass
class QuotaResult:
    """One normalized quota / limit / throttling observation."""

    subscription_id: str = ""
    subscription_name: str = ""
    region: str = ""                       # "" for subscription-/tenant-wide limits
    provider_namespace: str = ""           # e.g. Microsoft.Compute
    service_name: str = ""                 # human label e.g. "Virtual Machines"
    quota_category: str = ""               # compute|network|storage|appservice|sql|keyvault|monitor|ai|governance|throttling
    quota_name: str = ""                   # e.g. "Total Regional vCPUs"
    sku_family: str = ""                   # e.g. "standardDv5Family" (compute) or ""
    current_usage: float | None = None
    limit: float | None = None
    remaining: float | None = None
    percent_used: float | None = None
    unit: str = "Count"
    adjustable_status: str = AdjustableStatus.UNKNOWN
    source_type: str = SourceType.RP_USAGE_API
    collection_status: str = CollectionStatus.OK
    risk_level: str = RiskLevel.UNKNOWN
    recommendation: str = ""
    last_checked_utc: str = ""
    raw_provider_response: Any = None
    error_message: str = ""
    # Tenant carried for convenience (snapshot already scopes by tenant/connection).
    tenant_id: str = ""
    tenant_name: str = ""

    def compute_derived(self) -> None:
        """Fill remaining/percent_used from usage+limit when both are known."""
        if self.current_usage is not None and self.limit is not None and self.limit > 0:
            self.remaining = max(0.0, self.limit - self.current_usage)
            self.percent_used = round(min(100.0, (self.current_usage / self.limit) * 100.0), 1)
        elif self.limit in (0, None):
            self.remaining = None
            self.percent_used = None

    def key(self) -> str:
        """Stable identity for diffing across runs."""
        return f"{self.region}|{self.provider_namespace}|{self.quota_name}|{self.sku_family}".lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "subscription_name": self.subscription_name,
            "region": self.region,
            "provider_namespace": self.provider_namespace,
            "service_name": self.service_name,
            "quota_category": self.quota_category,
            "quota_name": self.quota_name,
            "sku_family": self.sku_family,
            "current_usage": self.current_usage,
            "limit": self.limit,
            "remaining": self.remaining,
            "percent_used": self.percent_used,
            "unit": self.unit,
            "adjustable_status": self.adjustable_status,
            "source_type": self.source_type,
            "collection_status": self.collection_status,
            "risk_level": self.risk_level,
            "recommendation": self.recommendation,
            "last_checked_utc": self.last_checked_utc,
            "raw_provider_response": self.raw_provider_response,
            "error_message": self.error_message,
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
        }


# ------------------------------------------------------------------------- redaction
_SENSITIVE_KEYS = {
    "authorization", "bearer", "access_token", "refresh_token", "client_secret",
    "sas", "sastoken", "connectionstring", "primarykey", "secondarykey", "password",
    "key", "accountkey", "token",
}
_MAX_RAW_CHARS = 6000


def redact(raw: Any) -> Any:
    """Strip secret-looking fields and cap size before a raw provider response is persisted.

    Never logs/stores tokens or keys (security requirement). Keeps small structural payloads so
    the detail drawer can show the real API shape; large ones are summarized."""
    try:
        def _scrub(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {
                    k: ("***redacted***" if str(k).lower() in _SENSITIVE_KEYS else _scrub(v))
                    for k, v in obj.items()
                }
            if isinstance(obj, list):
                return [_scrub(x) for x in obj[:200]]
            return obj

        scrubbed = _scrub(raw)
        encoded = json.dumps(scrubbed, default=str)
        if len(encoded) > _MAX_RAW_CHARS:
            return {"_truncated": True, "_preview": encoded[:_MAX_RAW_CHARS]}
        return scrubbed
    except (TypeError, ValueError):
        return None

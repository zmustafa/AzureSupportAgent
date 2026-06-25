"""Static service-limit collectors (Layer 5) — emit the documented Azure hard/soft limits that
no dynamic quota API exposes (storage perf, App Service scale caps, Key Vault throttling, Monitor
query/action-group limits, ARM/governance hard limits, AI TPM defaults). Two collectors so both
subscription-wide and per-region documented limits are covered. These make ceilings visible even
when there's no live counter."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.model import QuotaResult, RiskLevel, SourceType
from app.quota.static_limits import static_results

# Categories the static tables can emit (so the collectors run whenever one is selected).
_STATIC_CATEGORIES = ("storage", "appservice", "keyvault", "monitor", "governance", "ai")


def _emit(collector: IQuotaCollector, ctx: CollectorContext, region: str | None) -> list[QuotaResult]:
    entries = static_results(ctx.selected_categories, region)
    out: list[QuotaResult] = []
    for e in entries:
        r = collector._base(
            ctx,
            provider_namespace=e["provider"],
            service_name=e["service"],
            quota_name=e["quota_name"],
            quota_category=e["category"],
            current_usage=None,
            limit=(float(e["limit"]) if e["limit"] is not None else None),
            unit=e["unit"],
            adjustable_status=e["adjustable"],
            source_type=SourceType.STATIC_LIMIT,
            risk_level=RiskLevel.UNKNOWN,  # documented ceiling, no live usage to score
            raw_provider_response={"note": e["note"], "documented_limit": e["limit"], "unit": e["unit"]},
        )
        r.region = region or ""
        out.append(r)
    return out


class StaticSubscriptionLimitsCollector(IQuotaCollector):
    name = "static_subscription"
    provider_namespace = "Microsoft.Resources"
    service_label = "Documented service limits"
    categories = _STATIC_CATEGORIES
    scope = "subscription"
    required_permissions = ()
    dynamic = False
    source_default = SourceType.STATIC_LIMIT

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        return _emit(self, ctx, region=None)


class StaticRegionLimitsCollector(IQuotaCollector):
    name = "static_region"
    provider_namespace = "Microsoft.Resources"
    service_label = "Documented service limits (regional)"
    categories = _STATIC_CATEGORIES
    scope = "region"
    required_permissions = ()
    dynamic = False
    source_default = SourceType.STATIC_LIMIT

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        return _emit(self, ctx, region=ctx.region)

"""Storage quota collector — storage accounts per region (Microsoft.Storage location usages,
Layer 2). Per-account performance ceilings have no dynamic counter and are surfaced as static
service limits by the static-limits collector."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._usage import build_usage_results
from app.quota.model import AdjustableStatus, QuotaResult, SourceType

_API = "2024-01-01"


class StorageQuotaCollector(IQuotaCollector):
    name = "storage"
    provider_namespace = "Microsoft.Storage"
    service_label = "Storage accounts"
    categories = ("storage",)
    scope = "region"
    required_permissions = ("Microsoft.Storage/locations/usages/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.SUPPORT_REQUIRED
    source_default = SourceType.RP_USAGE_API

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        path = f"/subscriptions/{ctx.subscription_id}/providers/Microsoft.Storage/locations/{ctx.region}/usages"
        data, err, status = await ctx.arm_get(path, {"api-version": _API})
        if err:
            return [self._error_result(ctx, err, status)]
        items = (data or {}).get("value", []) or []
        return build_usage_results(
            self, ctx, items,
            source_type=SourceType.RP_USAGE_API,
            adjustable=AdjustableStatus.SUPPORT_REQUIRED,
            unit_default="Count",
        )

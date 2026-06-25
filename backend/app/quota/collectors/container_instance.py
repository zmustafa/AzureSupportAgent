"""Container quota collector — Azure Container Instances (ACI) regional usages (Layer 2):
Container Groups, Standard/Spot/GPU cores, dedicated container groups. Source:
Microsoft.ContainerInstance/locations/{region}/usages.

These are subscription/region service quotas (mostly 0 usage until you deploy), so zero-usage rows
with a real limit are kept (``keep_zero_usage``) so the operator sees the ceilings — like the
portal Quotas blade. SKU rows a region doesn't offer (limit 0) are still skipped."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._usage import build_usage_results
from app.quota.model import AdjustableStatus, QuotaResult, SourceType

_API = "2023-05-01"


class ContainerInstanceQuotaCollector(IQuotaCollector):
    name = "container_instance"
    provider_namespace = "Microsoft.ContainerInstance"
    service_label = "Container Instances"
    categories = ("containers",)
    scope = "region"
    required_permissions = ("Microsoft.ContainerInstance/locations/usages/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.SUPPORT_REQUIRED
    source_default = SourceType.RP_USAGE_API

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        path = f"/subscriptions/{ctx.subscription_id}/providers/Microsoft.ContainerInstance/locations/{ctx.region}/usages"
        data, err, status = await ctx.arm_get(path, {"api-version": _API})
        if err:
            # ACI not registered/available in the region is common — surface as not-supported, not fatal.
            return [self._error_result(ctx, err, status)]
        items = (data or {}).get("value", []) or []
        return build_usage_results(
            self, ctx, items,
            source_type=SourceType.RP_USAGE_API,
            adjustable=AdjustableStatus.SUPPORT_REQUIRED,
            unit_default="Count",
            keep_zero_usage=True,
        )

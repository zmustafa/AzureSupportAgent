"""Compute quota collector — regional vCPUs, per-VM-family vCPUs, spot, VM/VMSS/availability-set
counts. Source: Microsoft.Compute regional usages (Layer 2). Most compute quotas are adjustable.

The VM SKU-family quota table is the whole point of "VM quota", so its rows are kept even at zero
usage (``keep_zero_usage``) — an operator needs to see every family's limit/headroom to plan a
deployment, exactly like the Azure portal Quotas blade. Families a region doesn't offer (limit 0)
are still skipped."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._usage import build_usage_results
from app.quota.model import AdjustableStatus, QuotaResult, SourceType

_API = "2024-07-01"


def _family(value: str) -> str:
    """Surface the VM family for SKU-family-aware rows (e.g. 'standardDv5Family')."""
    v = value or ""
    if v.lower().endswith("family"):
        return v
    return ""


class ComputeQuotaCollector(IQuotaCollector):
    name = "compute"
    provider_namespace = "Microsoft.Compute"
    service_label = "Virtual Machines / Compute"
    categories = ("compute",)
    scope = "region"
    required_permissions = ("Microsoft.Compute/locations/usages/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.ADJUSTABLE
    source_default = SourceType.RP_USAGE_API

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        path = f"/subscriptions/{ctx.subscription_id}/providers/Microsoft.Compute/locations/{ctx.region}/usages"
        data, err, status = await ctx.arm_get(path, {"api-version": _API})
        if err:
            return [self._error_result(ctx, err, status)]
        items = (data or {}).get("value", []) or []
        return build_usage_results(
            self, ctx, items,
            source_type=SourceType.RP_USAGE_API,
            adjustable=AdjustableStatus.ADJUSTABLE,
            family_fn=_family,
            unit_default="Count",
            keep_zero_usage=True,
        )

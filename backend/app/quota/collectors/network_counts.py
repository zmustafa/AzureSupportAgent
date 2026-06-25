"""Network resource-count collector (Layer 3) — Private Endpoints and Private DNS zones, which
have no Microsoft.Network *usages* row. Counted via Resource Graph and compared to documented
soft ceilings so they show up alongside the usage-API network quotas. Subscription-scoped."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._arg import arg_count
from app.quota.model import AdjustableStatus, QuotaResult, SourceType

# (quota_name, kql_type, limit, adjustable)
_PROBES = [
    ("Private Endpoints", "microsoft.network/privateendpoints", 1000, AdjustableStatus.SUPPORT_REQUIRED),
    ("Private DNS zones", "microsoft.network/privatednszones", 1000, AdjustableStatus.SUPPORT_REQUIRED),
]


class NetworkCountCollector(IQuotaCollector):
    name = "network_counts"
    provider_namespace = "Microsoft.Network"
    service_label = "Networking (resource counts)"
    categories = ("network",)
    scope = "subscription"
    required_permissions = ("Microsoft.ResourceGraph/resources/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.SUPPORT_REQUIRED
    source_default = SourceType.RESOURCE_GRAPH

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        out: list[QuotaResult] = []
        for quota_name, rtype, limit, adjustable in _PROBES:
            kql = f"resources | where type =~ '{rtype}' | summarize count_=count()"
            count, err = await arg_count(ctx, kql)
            if err:
                r = self._error_result(ctx, err)
                r.quota_name = f"{quota_name} (collection failed)"
                out.append(r)
                continue
            r = self._base(
                ctx,
                quota_name=quota_name,
                current_usage=float(count or 0),
                limit=float(limit),
                unit="Count",
                source_type=SourceType.RESOURCE_GRAPH,
                adjustable_status=adjustable,
            )
            r.compute_derived()
            out.append(r)
        return out

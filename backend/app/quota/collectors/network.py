"""Network quota collector — Public IPs, VNets, NICs, Load Balancers, App Gateways, NAT
Gateways, Route Tables, NSGs, etc. Source: Microsoft.Network regional usages (Layer 2).
Supplementary counts for resources without usage rows (Private Endpoints, Private DNS zones)
are collected by the governance/ARG path."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._usage import build_usage_results
from app.quota.model import AdjustableStatus, QuotaResult, SourceType

_API = "2024-05-01"


class NetworkQuotaCollector(IQuotaCollector):
    name = "network"
    provider_namespace = "Microsoft.Network"
    service_label = "Networking"
    categories = ("network",)
    scope = "region"
    required_permissions = ("Microsoft.Network/locations/usages/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.ADJUSTABLE
    source_default = SourceType.RP_USAGE_API

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        path = f"/subscriptions/{ctx.subscription_id}/providers/Microsoft.Network/locations/{ctx.region}/usages"
        data, err, status = await ctx.arm_get(path, {"api-version": _API})
        if err:
            return [self._error_result(ctx, err, status)]
        items = (data or {}).get("value", []) or []
        return build_usage_results(
            self, ctx, items,
            source_type=SourceType.RP_USAGE_API,
            adjustable=AdjustableStatus.ADJUSTABLE,
            unit_default="Count",
        )

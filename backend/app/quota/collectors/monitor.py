"""Azure Monitor / Log Analytics collector — counts Log Analytics workspaces and flags ones with
short retention (Resource Graph, Layer 3). Ingestion caps, query throttling, and action-group
ceilings without a live counter are surfaced as static service limits. Subscription-scoped."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._arg import arg_count
from app.quota.model import AdjustableStatus, QuotaResult, SourceType


class AzureMonitorLimitCollector(IQuotaCollector):
    name = "monitor"
    provider_namespace = "Microsoft.OperationalInsights"
    service_label = "Azure Monitor / Log Analytics"
    categories = ("monitor",)
    scope = "subscription"
    required_permissions = ("Microsoft.ResourceGraph/resources/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.UNKNOWN
    source_default = SourceType.RESOURCE_GRAPH

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        out: list[QuotaResult] = []

        ws_count, err = await arg_count(
            ctx, "resources | where type =~ 'microsoft.operationalinsights/workspaces' | summarize count_=count()"
        )
        if err:
            out.append(self._error_result(ctx, err))
        else:
            r = self._base(
                ctx, quota_name="Log Analytics workspaces (deployed)",
                current_usage=float(ws_count or 0), limit=None, unit="Count",
                source_type=SourceType.RESOURCE_GRAPH, adjustable_status=AdjustableStatus.UNKNOWN,
            )
            out.append(r)

            short_ret, rerr = await arg_count(
                ctx,
                "resources | where type =~ 'microsoft.operationalinsights/workspaces' "
                "| where toint(properties.retentionInDays) < 30 | summarize count_=count()",
            )
            if not rerr and (short_ret or 0) > 0:
                rr = self._base(
                    ctx, quota_name="Workspaces with retention < 30 days",
                    current_usage=float(short_ret), limit=float(ws_count or short_ret), unit="Count",
                    source_type=SourceType.RESOURCE_GRAPH, adjustable_status=AdjustableStatus.ADJUSTABLE,
                )
                rr.compute_derived()
                out.append(rr)
        return out

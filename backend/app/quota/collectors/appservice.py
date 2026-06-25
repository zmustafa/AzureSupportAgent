"""App Service quota collector — App Service Plans and Web Apps currently deployed (Resource
Graph counts, Layer 3) for visibility. Free/Shared CPU/memory/filesystem and Premium/Isolated
scale ceilings have no dynamic subscription counter and are surfaced as static service limits."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._arg import arg_count
from app.quota.model import AdjustableStatus, QuotaResult, SourceType


class AppServiceQuotaCollector(IQuotaCollector):
    name = "appservice"
    provider_namespace = "Microsoft.Web"
    service_label = "App Service"
    categories = ("appservice",)
    scope = "subscription"
    required_permissions = ("Microsoft.ResourceGraph/resources/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.UNKNOWN
    source_default = SourceType.RESOURCE_GRAPH

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        out: list[QuotaResult] = []
        probes = [
            ("App Service plans (deployed)", "microsoft.web/serverfarms"),
            ("Web apps & function apps (deployed)", "microsoft.web/sites"),
        ]
        any_ok = False
        last_err = ""
        last_status = 0
        for label, rtype in probes:
            kql = f"resources | where type =~ '{rtype}' | summarize count_=count()"
            count, err = await arg_count(ctx, kql)
            if err:
                last_err = err
                continue
            any_ok = True
            out.append(self._base(
                ctx,
                quota_name=label,
                current_usage=float(count or 0),
                limit=None,  # no hard subscription limit — informational headroom signal
                unit="Count",
                source_type=SourceType.RESOURCE_GRAPH,
                adjustable_status=AdjustableStatus.UNKNOWN,
            ))
        if not any_ok and last_err:
            return [self._error_result(ctx, last_err, last_status)]
        return out

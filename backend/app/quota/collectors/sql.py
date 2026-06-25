"""SQL quota collector — Microsoft.Sql subscription/location usages (Layer 2): server quota,
regional vCore quota, subnet quota, free-database counts, etc. The SQL usages API uses a NESTED
``properties`` shape (currentValue/limit/unit under ``properties``, name as a string with the
human label in ``properties.displayName``) — ``build_usage_results`` handles that transparently.

These are subscription/region service quotas (mostly at 0 usage until you deploy), so zero-usage
rows are kept (``keep_zero_usage``) — the operator needs to see the SQL server/vCore ceilings to
plan a deployment, like the portal Quotas blade."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._usage import build_usage_results
from app.quota.model import AdjustableStatus, QuotaResult, SourceType

_API = "2022-05-01-preview"


class SqlQuotaCollector(IQuotaCollector):
    name = "sql"
    provider_namespace = "Microsoft.Sql"
    service_label = "Azure SQL"
    categories = ("sql",)
    scope = "region"
    required_permissions = ("Microsoft.Sql/locations/usages/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.SUPPORT_REQUIRED
    source_default = SourceType.RP_USAGE_API

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        path = f"/subscriptions/{ctx.subscription_id}/providers/Microsoft.Sql/locations/{ctx.region}/usages"
        data, err, status = await ctx.arm_get(path, {"api-version": _API})
        if err:
            return [self._error_result(ctx, err, status)]
        items = (data or {}).get("value", []) or []
        return build_usage_results(
            self, ctx, items,
            source_type=SourceType.RP_USAGE_API,
            adjustable=AdjustableStatus.SUPPORT_REQUIRED,
            unit_default="Count",
            keep_zero_usage=True,
        )

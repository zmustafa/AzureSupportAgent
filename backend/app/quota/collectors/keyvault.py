"""Key Vault collector — counts deployed Key Vaults (Resource Graph, Layer 3) for visibility.
Key Vault transaction/crypto throttling ceilings are not exposed as quota objects; those are
surfaced as static service limits, and any ARM 429s seen during the scan appear in the throttling
lane. Subscription-scoped."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._arg import arg_count
from app.quota.model import AdjustableStatus, QuotaResult, SourceType


class KeyVaultLimitCollector(IQuotaCollector):
    name = "keyvault"
    provider_namespace = "Microsoft.KeyVault"
    service_label = "Key Vault"
    categories = ("keyvault",)
    scope = "subscription"
    required_permissions = ("Microsoft.ResourceGraph/resources/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.UNKNOWN
    source_default = SourceType.RESOURCE_GRAPH

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        count, err = await arg_count(
            ctx, "resources | where type =~ 'microsoft.keyvault/vaults' | summarize count_=count()"
        )
        if err:
            return [self._error_result(ctx, err)]
        r = self._base(
            ctx, quota_name="Key Vaults (deployed)",
            current_usage=float(count or 0), limit=None, unit="Count",
            source_type=SourceType.RESOURCE_GRAPH, adjustable_status=AdjustableStatus.UNKNOWN,
        )
        return [r]

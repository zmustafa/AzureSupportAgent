"""AI / ML quota collector — Azure OpenAI & Cognitive Services (TPM/RPM, deployments) plus
Azure ML compute quota. Sources: Microsoft.CognitiveServices and Microsoft.MachineLearningServices
regional usages (Layer 2). Where a provider isn't present/registered in the region it's skipped;
deployment-level TPM/RPM that isn't retrievable is left to the static/manual-review path."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._usage import build_usage_results
from app.quota.model import AdjustableStatus, CollectionStatus, QuotaResult, SourceType

_CS_API = "2023-05-01"
_ML_API = "2024-04-01"


def _ml_family(value: str) -> str:
    """Surface the ML cluster VM family for the per-family dedicated-vCPU quota rows."""
    v = value or ""
    return v if v.lower().endswith("family") else ""


class AiQuotaCollector(IQuotaCollector):
    name = "ai"
    provider_namespace = "Microsoft.CognitiveServices"
    service_label = "Azure OpenAI / AI & ML"
    categories = ("ai",)
    scope = "region"
    required_permissions = (
        "Microsoft.CognitiveServices/locations/usages/read",
        "Microsoft.MachineLearningServices/locations/usages/read",
    )
    dynamic = True
    adjustable_default = AdjustableStatus.SUPPORT_REQUIRED
    source_default = SourceType.RP_USAGE_API

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        out: list[QuotaResult] = []
        statuses: list[int] = []

        cs_path = f"/subscriptions/{ctx.subscription_id}/providers/Microsoft.CognitiveServices/locations/{ctx.region}/usages"
        cs, cerr, cstat = await ctx.arm_get(cs_path, {"api-version": _CS_API})
        if not cerr:
            out += build_usage_results(
                self, ctx, (cs or {}).get("value", []) or [],
                source_type=SourceType.RP_USAGE_API,
                adjustable=AdjustableStatus.SUPPORT_REQUIRED,
                unit_default="Count",
            )
        else:
            statuses.append(cstat)

        ml_path = f"/subscriptions/{ctx.subscription_id}/providers/Microsoft.MachineLearningServices/locations/{ctx.region}/usages"
        ml, merr, mstat = await ctx.arm_get(ml_path, {"api-version": _ML_API})
        if not merr:
            ml_rows = build_usage_results(
                self, ctx, (ml or {}).get("value", []) or [],
                source_type=SourceType.RP_USAGE_API,
                adjustable=AdjustableStatus.SUPPORT_REQUIRED,
                family_fn=_ml_family,
                unit_default="Count",
                keep_zero_usage=True,  # ML cluster vCPU families are a quota table (like compute)
            )
            for r in ml_rows:
                r.provider_namespace = "Microsoft.MachineLearningServices"
                r.service_name = "Azure Machine Learning"
            out += ml_rows
        else:
            statuses.append(mstat)

        # Both unavailable AND nothing collected → emit a manual-review marker so the operator
        # knows AI quota wasn't silently dropped (e.g. neither provider registered in the region).
        if not out and statuses and all(s in (403, 404, 409) for s in statuses):
            out.append(self._base(
                ctx,
                quota_name="Azure OpenAI / ML quota",
                source_type=SourceType.MANUAL_REVIEW,
                collection_status=CollectionStatus.NOT_SUPPORTED,
                adjustable_status=AdjustableStatus.SUPPORT_REQUIRED,
                error_message="No AI/ML usage API available in this region — review the Quota blade manually.",
            ))
        return out

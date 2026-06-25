"""Governance / ARM limits collector — counts current resource groups, role assignments, custom
roles, and policy assignments (Resource Graph, Layer 3) and compares them to documented ARM
limits so an operator sees real headroom against the hard caps. Subscription-scoped."""
from __future__ import annotations

from app.quota.base import CollectorContext, IQuotaCollector
from app.quota.collectors._arg import arg_count
from app.quota.model import AdjustableStatus, QuotaResult, SourceType

# (service, provider, quota_name, kql, limit, unit, adjustable)
_PROBES = [
    (
        "Resource Manager", "Microsoft.Resources", "Resource groups per subscription",
        "resourcecontainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups' | summarize count_=count()",
        980, "Count", AdjustableStatus.HARD_LIMIT,
    ),
    (
        "RBAC", "Microsoft.Authorization", "Role assignments per subscription",
        "authorizationresources | where type =~ 'microsoft.authorization/roleassignments' | summarize count_=count()",
        4000, "Count", AdjustableStatus.SUPPORT_REQUIRED,
    ),
    (
        "RBAC", "Microsoft.Authorization", "Custom role definitions",
        "authorizationresources | where type =~ 'microsoft.authorization/roledefinitions' "
        "| where tostring(properties.type) =~ 'CustomRole' | summarize count_=count()",
        5000, "Count", AdjustableStatus.HARD_LIMIT,
    ),
    (
        "Azure Policy", "Microsoft.Authorization", "Policy assignments per scope",
        "policyresources | where type =~ 'microsoft.authorization/policyassignments' | summarize count_=count()",
        200, "Count", AdjustableStatus.HARD_LIMIT,
    ),
]


class GovernanceLimitCollector(IQuotaCollector):
    name = "governance"
    provider_namespace = "Microsoft.Resources"
    service_label = "Governance & ARM limits"
    categories = ("governance",)
    scope = "subscription"
    required_permissions = ("Microsoft.ResourceGraph/resources/read",)
    dynamic = True
    adjustable_default = AdjustableStatus.HARD_LIMIT
    source_default = SourceType.RESOURCE_GRAPH

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:
        out: list[QuotaResult] = []
        for service, provider, quota_name, kql, limit, unit, adjustable in _PROBES:
            count, err = await arg_count(ctx, kql)
            if err:
                r = self._error_result(ctx, err)
                r.quota_name = f"{quota_name} (collection failed)"
                r.service_name = service
                r.provider_namespace = provider
                out.append(r)
                continue
            r = self._base(
                ctx,
                service_name=service,
                provider_namespace=provider,
                quota_name=quota_name,
                current_usage=float(count or 0),
                limit=float(limit),
                unit=unit,
                source_type=SourceType.RESOURCE_GRAPH,
                adjustable_status=adjustable,
            )
            r.compute_derived()
            out.append(r)
        return out

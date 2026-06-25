"""Deterministic demo snapshot for the Quota Monitor — no Azure calls.

Synthesizes a realistic spread across categories, regions, source types, and risk levels so the
UI (and screenshots/tests) can be exercised without a live connection. Mirrors the shape produced
by ``scan.run_scan``."""
from __future__ import annotations

from datetime import datetime, timezone

from app.quota.model import (
    AdjustableStatus,
    CollectionStatus,
    QuotaResult,
    RiskLevel,
    SourceType,
)
from app.quota.recommend import CAPACITY_NOTE, recommend_for_result
from app.quota.risk import apply_risk, load_thresholds

DEMO_SCOPE_ID = "__demo__"
_SUB = "00000000-0000-0000-0000-0000000000de"
_SUB_NAME = "CONTOSO-Prod (demo)"


def _r(**kw) -> QuotaResult:
    base = dict(
        subscription_id=_SUB, subscription_name=_SUB_NAME, unit="Count",
        last_checked_utc=datetime.now(timezone.utc).isoformat(),
    )
    base.update(kw)
    r = QuotaResult(**base)
    r.compute_derived()
    return r


def _rows() -> list[QuotaResult]:
    rows = [
        # Compute — a critical family + a healthy regional total.
        _r(region="eastus2", provider_namespace="Microsoft.Compute", service_name="Virtual Machines / Compute",
           quota_category="compute", quota_name="Standard Dv5 Family vCPUs", sku_family="standardDv5Family",
           current_usage=190, limit=200, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.ADJUSTABLE),
        _r(region="eastus2", provider_namespace="Microsoft.Compute", service_name="Virtual Machines / Compute",
           quota_category="compute", quota_name="Total Regional vCPUs",
           current_usage=240, limit=350, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.ADJUSTABLE),
        # Zero-usage SKU families with headroom — shown so the operator can plan a deployment
        # (like the portal Quotas blade). These exercise the SKU-family column + "VM families" filter.
        _r(region="eastus2", provider_namespace="Microsoft.Compute", service_name="Virtual Machines / Compute",
           quota_category="compute", quota_name="Standard Ev5 Family vCPUs", sku_family="standardEv5Family",
           current_usage=0, limit=100, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.ADJUSTABLE),
        _r(region="eastus2", provider_namespace="Microsoft.Compute", service_name="Virtual Machines / Compute",
           quota_category="compute", quota_name="Standard FSv2 Family vCPUs", sku_family="standardFSv2Family",
           current_usage=0, limit=50, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.ADJUSTABLE),
        _r(region="westeurope", provider_namespace="Microsoft.Compute", service_name="Virtual Machines / Compute",
           quota_category="compute", quota_name="Standard NC Family vCPUs (GPU)", sku_family="standardNCFamily",
           current_usage=22, limit=24, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.SUPPORT_REQUIRED),
        # Network — warning public IPs + healthy NICs.
        _r(region="eastus2", provider_namespace="Microsoft.Network", service_name="Networking",
           quota_category="network", quota_name="Public IP Addresses",
           current_usage=88, limit=100, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.ADJUSTABLE),
        _r(region="eastus2", provider_namespace="Microsoft.Network", service_name="Networking",
           quota_category="network", quota_name="Network Interfaces",
           current_usage=120, limit=1000, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.ADJUSTABLE),
        _r(provider_namespace="Microsoft.Network", service_name="Networking (resource counts)",
           quota_category="network", quota_name="Private Endpoints",
           current_usage=910, limit=1000, source_type=SourceType.RESOURCE_GRAPH, adjustable_status=AdjustableStatus.SUPPORT_REQUIRED),
        # Storage — watch on account count.
        _r(region="eastus2", provider_namespace="Microsoft.Storage", service_name="Storage accounts",
           quota_category="storage", quota_name="Storage Accounts",
           current_usage=180, limit=250, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.SUPPORT_REQUIRED),
        # Governance — role assignments warning.
        _r(provider_namespace="Microsoft.Authorization", service_name="RBAC",
           quota_category="governance", quota_name="Role assignments per subscription",
           current_usage=3650, limit=4000, source_type=SourceType.RESOURCE_GRAPH, adjustable_status=AdjustableStatus.SUPPORT_REQUIRED),
        _r(provider_namespace="Microsoft.Resources", service_name="Resource Manager",
           quota_category="governance", quota_name="Resource groups per subscription",
           current_usage=210, limit=980, source_type=SourceType.RESOURCE_GRAPH, adjustable_status=AdjustableStatus.HARD_LIMIT),
        # SQL — healthy.
        _r(region="eastus2", provider_namespace="Microsoft.Sql", service_name="Azure SQL",
           quota_category="sql", quota_name="Regional Server Quota",
           current_usage=4, limit=250, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.SUPPORT_REQUIRED),
        # Containers (ACI) — Container Groups headroom.
        _r(region="eastus2", provider_namespace="Microsoft.ContainerInstance", service_name="Container Instances",
           quota_category="containers", quota_name="Container Groups",
           current_usage=12, limit=100, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.SUPPORT_REQUIRED),
        # AI — TPM critical.
        _r(region="eastus2", provider_namespace="Microsoft.CognitiveServices", service_name="Azure OpenAI / AI & ML",
           quota_category="ai", quota_name="Tokens Per Minute (GPT-4o)", unit="TPM",
           current_usage=480000, limit=500000, source_type=SourceType.RP_USAGE_API, adjustable_status=AdjustableStatus.SUPPORT_REQUIRED),
        # Static service limits (Unknown risk — documented ceilings, no live usage).
        _r(provider_namespace="Microsoft.KeyVault", service_name="Key Vault",
           quota_category="keyvault", quota_name="Secrets/keys GET transactions", unit="ops/10s",
           current_usage=None, limit=4000, source_type=SourceType.STATIC_LIMIT, adjustable_status=AdjustableStatus.HARD_LIMIT),
        _r(provider_namespace="Microsoft.Storage", service_name="Storage account",
           quota_category="storage", quota_name="Max request rate per account", unit="requests/s",
           current_usage=None, limit=20000, source_type=SourceType.STATIC_LIMIT, adjustable_status=AdjustableStatus.HARD_LIMIT),
        # Manual review (AI deployment).
        _r(region="westeurope", provider_namespace="Microsoft.CognitiveServices", service_name="Azure OpenAI",
           quota_category="ai", quota_name="Azure OpenAI / ML quota", unit="TPM",
           current_usage=None, limit=None, source_type=SourceType.MANUAL_REVIEW,
           collection_status=CollectionStatus.NOT_SUPPORTED, adjustable_status=AdjustableStatus.SUPPORT_REQUIRED,
           error_message="No AI/ML usage API available in this region — review the Quota blade manually."),
        # Throttling lane.
        _r(region="eastus2", provider_namespace="Microsoft.Resources", service_name="ARM API",
           quota_category="throttling", quota_name="ARM read throttling (HTTP 429)", unit="events",
           current_usage=3, limit=None, source_type=SourceType.MONITOR_METRIC, risk_level=RiskLevel.THROTTLING,
           adjustable_status=AdjustableStatus.HARD_LIMIT),
        # An unauthorized error row to exercise the partial-failure UI.
        _r(region="westeurope", provider_namespace="Microsoft.Sql", service_name="Azure SQL",
           quota_category="sql", quota_name="Azure SQL (collection failed)",
           current_usage=None, limit=None, source_type=SourceType.MANUAL_REVIEW,
           collection_status=CollectionStatus.UNAUTHORIZED, adjustable_status=AdjustableStatus.UNKNOWN,
           error_message="ARM 403: The client does not have authorization to perform action 'Microsoft.Sql/locations/usages/read'."),
    ]
    return rows


def seed_demo() -> dict:
    thresholds = load_thresholds()
    rows = _rows()
    error_statuses = {
        CollectionStatus.ERROR, CollectionStatus.UNAUTHORIZED,
        CollectionStatus.NOT_SUPPORTED, CollectionStatus.NOT_REGISTERED,
    }
    for r in rows:
        if r.collection_status not in error_statuses and r.risk_level != RiskLevel.THROTTLING:
            apply_risk(r, thresholds)
        r.recommendation = recommend_for_result(r)

    counts = {
        RiskLevel.CRITICAL: 0, RiskLevel.WARNING: 0, RiskLevel.WATCH: 0,
        RiskLevel.HEALTHY: 0, RiskLevel.UNKNOWN: 0, RiskLevel.THROTTLING: 0,
        "total": 0, "errors": 0,
    }
    by_provider: dict[str, dict[str, int]] = {}
    provider_errors: list[dict[str, Any]] = []
    for r in rows:
        counts["total"] += 1
        counts[r.risk_level] = counts.get(r.risk_level, 0) + 1
        bucket = by_provider.setdefault(r.provider_namespace, {"ok": 0, "error": 0})
        if r.collection_status in error_statuses:
            counts["errors"] += 1
            bucket["error"] += 1
            provider_errors.append({
                "provider": r.provider_namespace, "service": r.service_name, "region": r.region,
                "status": r.collection_status, "message": r.error_message,
            })
        else:
            bucket["ok"] += 1

    registration = [
        {"namespace": ns, "state": "Registered", "registered": True, "remediation": ""}
        for ns in ["Microsoft.Compute", "Microsoft.Network", "Microsoft.Storage", "Microsoft.Sql",
                   "Microsoft.Authorization", "Microsoft.CognitiveServices"]
    ]
    registration.append({
        "namespace": "Microsoft.Quota", "state": "NotRegistered", "registered": False,
        "remediation": f"az provider register --namespace Microsoft.Quota --subscription {_SUB}",
    })

    return {
        "source": "demo",
        "demo": True,
        "connection_configured": True,
        "never_loaded": False,
        "error": "",
        "status": "partial",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "subscription_id": _SUB,
        "subscription_name": _SUB_NAME,
        "regions_scanned": ["eastus2", "westeurope"],
        "categories_scanned": ["compute", "network", "storage", "sql", "ai", "keyvault", "governance", "throttling"],
        "thresholds": thresholds,
        "counts": counts,
        "by_provider": by_provider,
        "provider_registration": registration,
        "provider_errors": provider_errors,
        "throttling": {"events": 3, "min_remaining_reads": 1180},
        "results": [r.to_dict() for r in rows],
        "ai_summary": "",
        "used_ai": False,
        "capacity_note": CAPACITY_NOTE,
    }

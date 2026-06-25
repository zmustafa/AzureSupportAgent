"""Unit + integration tests for the Quota Monitor feature.

Covers the normalized model, risk bands (incl. configurable thresholds), recommendation
generation, static service-limit metadata, usage-API mapping, collector error mapping, the demo
seed, and an end-to-end ``run_scan`` with mocked Azure responses exercising partial-provider
failure, provider-not-registered, throttling capture, and aggregation."""
from __future__ import annotations

import pytest

from app.quota.base import CollectorContext, IQuotaCollector, ThrottleTracker
from app.quota.collectors._usage import build_usage_results
from app.quota.model import (
    AdjustableStatus,
    CollectionStatus,
    QuotaResult,
    RiskLevel,
    SourceType,
    redact,
)
from app.quota.recommend import recommend_for_result
from app.quota.risk import apply_risk, evaluate_risk, load_thresholds
from app.quota.static_limits import static_results


# --------------------------------------------------------------------------- model
def test_compute_derived_and_key():
    r = QuotaResult(current_usage=190, limit=200, region="eastus2",
                    provider_namespace="Microsoft.Compute", quota_name="Dv5 vCPUs", sku_family="standardDv5Family")
    r.compute_derived()
    assert r.remaining == 10
    assert r.percent_used == 95.0
    assert r.key() == "eastus2|microsoft.compute|dv5 vcpus|standarddv5family"


def test_compute_derived_no_limit():
    r = QuotaResult(current_usage=5, limit=None)
    r.compute_derived()
    assert r.percent_used is None and r.remaining is None


def test_redact_strips_secrets_and_caps():
    raw = {"Authorization": "Bearer abc", "nested": {"client_secret": "s", "ok": 1}, "list": [1, 2, 3]}
    out = redact(raw)
    assert out["Authorization"] == "***redacted***"
    assert out["nested"]["client_secret"] == "***redacted***"
    assert out["nested"]["ok"] == 1
    # Oversized payload is summarized.
    big = {"x": "y" * 20000}
    summarized = redact(big)
    assert summarized.get("_truncated") is True


# --------------------------------------------------------------------------- risk
def test_evaluate_risk_bands():
    t = {"watch": 70.0, "warning": 85.0, "critical": 95.0}
    assert evaluate_risk(50, has_limit=True, thresholds=t) == RiskLevel.HEALTHY
    assert evaluate_risk(72, has_limit=True, thresholds=t) == RiskLevel.WATCH
    assert evaluate_risk(88, has_limit=True, thresholds=t) == RiskLevel.WARNING
    assert evaluate_risk(99, has_limit=True, thresholds=t) == RiskLevel.CRITICAL
    assert evaluate_risk(99, has_limit=False, thresholds=t) == RiskLevel.UNKNOWN
    assert evaluate_risk(None, has_limit=True, thresholds=t) == RiskLevel.UNKNOWN


def test_apply_risk_preserves_throttling():
    r = QuotaResult(risk_level=RiskLevel.THROTTLING, current_usage=1, limit=None)
    apply_risk(r, {"watch": 70.0, "warning": 85.0, "critical": 95.0})
    assert r.risk_level == RiskLevel.THROTTLING


def test_load_thresholds_orders_and_clamps(monkeypatch):
    import app.core.app_settings as appset

    monkeypatch.setattr(
        appset, "load_settings",
        lambda: {"quota_threshold_watch": 90, "quota_threshold_warning": 50, "quota_threshold_critical": 60},
        raising=False,
    )
    # Ordering is enforced even when the settings invert the bands.
    t = load_thresholds()
    assert t["watch"] < t["warning"] < t["critical"]


# ----------------------------------------------------------------------- recommend
def test_recommend_critical_adjustable():
    r = QuotaResult(region="eastus2", service_name="Compute", quota_name="vCPUs", sku_family="Dv5",
                    current_usage=96, limit=100, adjustable_status=AdjustableStatus.ADJUSTABLE,
                    source_type=SourceType.RP_USAGE_API)
    apply_risk(r, load_thresholds())
    msg = recommend_for_result(r)
    assert "increase" in msg.lower() and "eastus2" in msg


def test_recommend_hard_limit_static():
    r = QuotaResult(service_name="Storage account", quota_name="Max request rate", limit=20000,
                    unit="requests/s", adjustable_status=AdjustableStatus.HARD_LIMIT,
                    source_type=SourceType.STATIC_LIMIT)
    msg = recommend_for_result(r)
    assert "hard limit" in msg.lower()


def test_recommend_manual_and_throttling_and_unregistered():
    manual = QuotaResult(quota_name="OpenAI TPM", source_type=SourceType.MANUAL_REVIEW)
    assert "manual review" in recommend_for_result(manual).lower()

    thr = QuotaResult(risk_level=RiskLevel.THROTTLING, quota_name="ARM 429")
    assert "backoff" in recommend_for_result(thr).lower()

    notreg = QuotaResult(provider_namespace="Microsoft.Quota", collection_status=CollectionStatus.NOT_REGISTERED)
    assert "register" in recommend_for_result(notreg).lower()


# --------------------------------------------------------------------- static limits
def test_static_results_scope_and_category_filter():
    sub = static_results({"governance"}, region=None)
    assert sub and all(e["category"] == "governance" and e["scope"] == "subscription" for e in sub)
    reg = static_results({"ai"}, region="eastus2")
    assert all(e["scope"] == "region" for e in reg)
    # None category → everything for the scope.
    assert len(static_results(None, region=None)) > len(sub)


# ----------------------------------------------------------------------- usage map
class _Dummy(IQuotaCollector):
    name = "dummy"
    provider_namespace = "Microsoft.Compute"
    service_label = "Compute"
    categories = ("compute",)
    scope = "region"


def _ctx() -> CollectorContext:
    return CollectorContext(
        token="t", connection={}, tenant_id="default", tenant_name="", subscription_id="sub",
        subscription_name="Sub", region="eastus2",
    )


def test_build_usage_results_maps_and_skips_empty():
    items = [
        {"name": {"value": "standardDv5Family", "localizedValue": "Standard Dv5 Family vCPUs"},
         "currentValue": 190, "limit": 200, "unit": "Count"},
        {"name": {"value": "cores", "localizedValue": "Total Regional vCPUs"}, "currentValue": 0, "limit": 0},
        {"name": {"value": "x", "localizedValue": "X"}, "currentValue": None, "limit": None},
    ]
    rows = build_usage_results(_Dummy(), _ctx(), items, source_type=SourceType.RP_USAGE_API,
                               adjustable=AdjustableStatus.ADJUSTABLE,
                               family_fn=lambda v: v if v.endswith("Family") else "")
    # Only the meaningful row survives (zero-limit + empty are skipped).
    assert len(rows) == 1
    assert rows[0].sku_family == "standardDv5Family"
    assert rows[0].percent_used == 95.0


def test_build_usage_results_hides_zero_usage_when_enabled():
    items = [
        {"name": {"value": "cores", "localizedValue": "Total Regional vCPUs"}, "currentValue": 0, "limit": 350},
        {"name": {"value": "used", "localizedValue": "Used vCPUs"}, "currentValue": 5, "limit": 350},
    ]
    ctx = _ctx()
    ctx.hide_zero_usage = True
    rows = build_usage_results(_Dummy(), ctx, items, source_type=SourceType.RP_USAGE_API,
                               adjustable=AdjustableStatus.ADJUSTABLE)
    # The zero-usage row is hidden; the used one is kept.
    assert len(rows) == 1 and rows[0].quota_name == "Used vCPUs"

    ctx.hide_zero_usage = False
    rows2 = build_usage_results(_Dummy(), ctx, items, source_type=SourceType.RP_USAGE_API,
                                adjustable=AdjustableStatus.ADJUSTABLE)
    assert len(rows2) == 2  # both kept when not hiding


def test_build_usage_results_nested_properties_shape():
    # SQL-style nested shape: name is a string, currentValue/limit/unit live under properties.
    items = [{
        "name": "ServerQuota",
        "properties": {"displayName": "Regional Server Quota for eastus",
                       "currentValue": 4, "limit": 250, "unit": "Count"},
    }]
    rows = build_usage_results(_Dummy(), _ctx(), items, source_type=SourceType.RP_USAGE_API,
                               adjustable=AdjustableStatus.SUPPORT_REQUIRED)
    assert len(rows) == 1
    r = rows[0]
    assert r.quota_name == "Regional Server Quota for eastus"  # displayName used as the label
    assert r.current_usage == 4 and r.limit == 250 and r.unit == "Count"
    assert r.percent_used == 1.6


def test_informational_downgrades_countdown_counter():
    from app.quota import informational

    # SQL free-trial countdown at 365/365 must NOT be Critical (high value = healthy).
    r = QuotaResult(provider_namespace="Microsoft.Sql",
                    quota_name="Free to Basic Database Upgrade count-down in eastus",
                    current_usage=365, limit=365, source_type=SourceType.RP_USAGE_API)
    r.compute_derived()
    r.risk_level = RiskLevel.CRITICAL
    assert informational.apply(r) is True
    assert r.risk_level == RiskLevel.UNKNOWN
    assert "remaining-allowance" in r.recommendation.lower()


def test_build_usage_results_keep_zero_usage_overrides_hide():
    items = [
        {"name": {"value": "standardDv5Family", "localizedValue": "Standard Dv5 Family vCPUs"},
         "currentValue": 0, "limit": 20},
    ]
    ctx = _ctx()
    ctx.hide_zero_usage = True
    # keep_zero_usage=True (compute path) keeps the VM family quota row even at 0 usage.
    rows = build_usage_results(_Dummy(), ctx, items, source_type=SourceType.RP_USAGE_API,
                               adjustable=AdjustableStatus.ADJUSTABLE,
                               family_fn=lambda v: v if v.endswith("Family") else "",
                               keep_zero_usage=True)
    assert len(rows) == 1 and rows[0].sku_family == "standardDv5Family" and rows[0].limit == 20
    # But a SKU not offered in the region (limit 0, usage 0) is still skipped.
    items2 = [{"name": {"value": "standardXFamily", "localizedValue": "X"}, "currentValue": 0, "limit": 0}]
    assert build_usage_results(_Dummy(), ctx, items2, source_type=SourceType.RP_USAGE_API,
                               adjustable=AdjustableStatus.ADJUSTABLE, keep_zero_usage=True) == []


def test_include_unused_overrides_hide_for_all():
    items = [{"name": {"value": "cores", "localizedValue": "Total Regional vCPUs"}, "currentValue": 0, "limit": 350}]
    ctx = _ctx()
    ctx.hide_zero_usage = True
    ctx.include_unused = True
    rows = build_usage_results(_Dummy(), ctx, items, source_type=SourceType.RP_USAGE_API,
                               adjustable=AdjustableStatus.ADJUSTABLE)
    assert len(rows) == 1  # include_unused forces zero rows in even without keep_zero_usage


def test_informational_downgrades_network_watchers():
    from app.quota import informational

    r = QuotaResult(provider_namespace="Microsoft.Network", quota_name="Network Watchers",
                    current_usage=1, limit=1, source_type=SourceType.RP_USAGE_API)
    r.compute_derived()
    r.risk_level = RiskLevel.CRITICAL  # would otherwise scream Critical at 1/1
    adjusted = informational.apply(r)
    assert adjusted is True
    assert r.risk_level == RiskLevel.UNKNOWN
    assert r.adjustable_status == AdjustableStatus.HARD_LIMIT
    assert "by-design" in r.recommendation.lower()

    # With headroom it is NOT treated as informational.
    r2 = QuotaResult(provider_namespace="Microsoft.Network", quota_name="Network Watchers",
                     current_usage=0, limit=1, source_type=SourceType.RP_USAGE_API)
    r2.compute_derived()
    assert informational.apply(r2) is False
    # A normal quota is never matched.
    r3 = QuotaResult(provider_namespace="Microsoft.Compute", quota_name="vCPUs",
                     current_usage=10, limit=10, source_type=SourceType.RP_USAGE_API)
    r3.compute_derived()
    assert informational.apply(r3) is False


def test_collector_error_result_status_mapping():
    d = _Dummy()
    ctx = _ctx()
    assert d._error_result(ctx, "x", 403).collection_status == CollectionStatus.UNAUTHORIZED
    assert d._error_result(ctx, "x", 404).collection_status == CollectionStatus.NOT_SUPPORTED
    assert d._error_result(ctx, "x", 409).collection_status == CollectionStatus.NOT_REGISTERED
    assert d._error_result(ctx, "x", 500).collection_status == CollectionStatus.ERROR


# --------------------------------------------------------------------------- demo
def test_demo_seed_shape():
    from app.quota.demo import seed_demo

    snap = seed_demo()
    assert snap["demo"] is True
    assert snap["counts"]["total"] == len(snap["results"]) == 19
    assert snap["counts"]["Critical"] >= 1
    assert snap["counts"]["ThrottlingObserved"] >= 1
    # Every result carries a recommendation.
    assert all(r["recommendation"] for r in snap["results"])
    # VM SKU-family quota rows are present (incl. zero-usage headroom rows).
    fam = [r for r in snap["results"] if r["sku_family"]]
    assert len(fam) >= 3
    assert any(r["current_usage"] == 0 and (r["limit"] or 0) > 0 for r in fam)
    # The container quota category is represented.
    assert any(r["quota_category"] == "containers" for r in snap["results"])


# ----------------------------------------------------------------------- throttling
def test_throttle_tracker_records_429():
    class _Resp:
        def __init__(self, status, headers):
            self.status_code = status
            self.headers = headers

    tr = ThrottleTracker()
    tr.note_response("eastus2", "/x", _Resp(200, {"x-ms-ratelimit-remaining-subscription-reads": "1180"}))
    tr.note_response("eastus2", "/y", _Resp(429, {"retry-after": "12"}))
    assert tr.min_remaining_reads == 1180
    assert len(tr.events) == 1 and tr.events[0].retry_after == 12.0


# ----------------------------------------------------------------- run_scan (mocked)
async def test_run_scan_partial_failure(monkeypatch):
    import app.azure.credentials as creds
    import app.quota.providers as prov
    import app.quota.base as basemod
    import app.azure.arm as armmod

    async def fake_token(conn):
        return "tok", None

    async def fake_regions(token, sub, use_cache=True):
        return [{"name": "eastus2", "display_name": "East US 2"}], None

    async def fake_reg(token, sub):
        return [{"namespace": "Microsoft.Compute", "state": "Registered", "registered": True, "remediation": ""}], None

    async def fake_arg(token, query, subs, page_size=1, max_rows=1, max_retries=4):
        return [{"count_": 7}], None, True, 7

    # Stub ARM GET: compute usages with a critical family; SQL returns 403 (unauthorized).
    async def fake_arm_get(self, path, params):
        if "Microsoft.Compute/locations" in path:
            return ({"value": [
                {"name": {"value": "standardDv5Family", "localizedValue": "Dv5 vCPUs"},
                 "currentValue": 98, "limit": 100, "unit": "Count"},
            ]}, None, 200)
        if "Microsoft.Sql/locations" in path:
            return (None, "ARM 403: not authorized", 403)
        # Everything else: empty usage set.
        return ({"value": []}, None, 200)

    monkeypatch.setattr(creds, "get_arm_token", fake_token, raising=False)
    monkeypatch.setattr(prov, "list_regions", fake_regions, raising=False)
    monkeypatch.setattr(prov, "provider_registration", fake_reg, raising=False)
    monkeypatch.setattr(armmod, "query_resource_graph_paged", fake_arg, raising=False)
    monkeypatch.setattr(basemod.CollectorContext, "arm_get", fake_arm_get, raising=False)

    from app.quota.scan import run_scan

    snap = await run_scan(
        {"id": "c1"}, "sub-123", "Sub 123",
        regions=["eastus2"], categories=["compute", "sql", "governance"],
        tenant_id="default",
    )

    assert snap["subscription_id"] == "sub-123"
    assert snap["status"] == "partial"  # SQL failed but the scan still returned

    results = snap["results"]
    # Critical compute row present.
    crit = [r for r in results if r["risk_level"] == "Critical" and r["provider_namespace"] == "Microsoft.Compute"]
    assert crit and crit[0]["percent_used"] == 98.0
    # SQL unauthorized error surfaced (not fatal).
    sql_err = [r for r in results if r["collection_status"] == "unauthorized"]
    assert sql_err and sql_err[0]["provider_namespace"] == "Microsoft.Sql"
    # Governance ARG count compared to a documented limit.
    gov = [r for r in results if r["quota_category"] == "governance" and r["current_usage"] == 7]
    assert gov
    # provider_errors captured for audit.
    assert any(e["status"] == "unauthorized" for e in snap["provider_errors"])
    # No AI executive summary is produced.
    assert snap["ai_summary"] == "" and snap["used_ai"] is False


async def test_run_scan_no_connection():
    from app.quota.scan import run_scan

    snap = await run_scan(None, "sub", "Sub")
    assert snap["connection_configured"] is False
    assert snap["results"] == []

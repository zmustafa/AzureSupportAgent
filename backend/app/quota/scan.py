"""Quota scan orchestration.

``run_scan`` resolves the connection's ARM token, discovers (or accepts a subset of) regions,
checks resource-provider registration, then runs every selected collector across the subscription
and each region with bounded concurrency. Results are risk-scored and given a deterministic
recommendation; an optional AI executive summary is layered on top. ARM throttling observed during
the scan is surfaced as a separate lane. Returns a normalized snapshot dict (the API layer handles
caching + run-history persistence). Never raises — failures fold into the snapshot."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.quota.base import CollectorContext, ThrottleTracker, registry
from app.quota.model import (
    AdjustableStatus,
    CollectionStatus,
    QuotaResult,
    RiskLevel,
    SourceType,
)
from app.quota.recommend import CAPACITY_NOTE, recommend_for_result
from app.quota.risk import apply_risk, load_thresholds

# Importing the collectors package registers the built-in collectors into ``registry``.
import app.quota.collectors  # noqa: F401

log = logging.getLogger("app.quota.scan")

_ARM = "https://management.azure.com"
_ERROR_STATUSES = {
    CollectionStatus.ERROR,
    CollectionStatus.UNAUTHORIZED,
    CollectionStatus.NOT_SUPPORTED,
    CollectionStatus.NOT_REGISTERED,
}


def _concurrency() -> int:
    try:
        from app.core.app_settings import load_settings

        return max(1, min(16, int(load_settings().get("quota_scan_concurrency", 8) or 8)))
    except Exception:  # noqa: BLE001
        return 5


def _hide_zero_usage() -> bool:
    try:
        from app.core.app_settings import load_settings

        return bool(load_settings().get("quota_hide_zero_usage", True))
    except Exception:  # noqa: BLE001
        return True


def empty_snapshot(*, connection_configured: bool, error: str = "", never_loaded: bool = False) -> dict[str, Any]:
    return {
        "source": "azure",
        "demo": False,
        "connection_configured": connection_configured,
        "never_loaded": never_loaded,
        "error": error,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "subscription_id": "",
        "subscription_name": "",
        "regions_scanned": [],
        "categories_scanned": [],
        "thresholds": load_thresholds(),
        "counts": _zero_counts(),
        "by_provider": {},
        "provider_registration": [],
        "provider_errors": [],
        "throttling": {"events": 0, "min_remaining_reads": None},
        "results": [],
        "ai_summary": "",
        "used_ai": False,
        "capacity_note": CAPACITY_NOTE,
    }


def _zero_counts() -> dict[str, int]:
    return {
        RiskLevel.CRITICAL: 0, RiskLevel.WARNING: 0, RiskLevel.WATCH: 0,
        RiskLevel.HEALTHY: 0, RiskLevel.UNKNOWN: 0, RiskLevel.THROTTLING: 0,
        "total": 0, "errors": 0,
    }


async def run_scan(
    connection: dict[str, Any] | None,
    subscription_id: str,
    subscription_name: str,
    *,
    regions: list[str] | None = None,
    categories: list[str] | None = None,
    tenant_id: str = "default",
    tenant_name: str = "",
    include_unused: bool = False,
) -> dict[str, Any]:
    """Run a full scan and return the final snapshot (non-streaming wrapper)."""
    snapshot: dict[str, Any] = empty_snapshot(connection_configured=connection is not None)
    async for kind, payload in scan_events(
        connection, subscription_id, subscription_name,
        regions=regions, categories=categories, tenant_id=tenant_id, tenant_name=tenant_name,
        include_unused=include_unused,
    ):
        if kind == "snapshot":
            snapshot = payload
    return snapshot


async def scan_events(
    connection: dict[str, Any] | None,
    subscription_id: str,
    subscription_name: str,
    *,
    regions: list[str] | None = None,
    categories: list[str] | None = None,
    tenant_id: str = "default",
    tenant_name: str = "",
    include_unused: bool = False,
):
    """Run a scan as an async generator, yielding progress events so the UI can show a live
    activity log (like FMEA). Yields tuples:

    - ``("status", {"phase", "message"})`` — incremental progress.
    - ``("snapshot", <snapshot dict>)`` — the final result (always last on success).

    Never raises — terminal failures are emitted as a status + a snapshot carrying ``error``."""

    def _status(phase: str, message: str):
        return ("status", {"phase": phase, "message": message})

    if connection is None:
        yield _status("error", "❌ No Azure connection configured.")
        yield ("snapshot", empty_snapshot(connection_configured=False))
        return
    if not subscription_id:
        yield _status("error", "❌ No subscription selected.")
        yield ("snapshot", empty_snapshot(connection_configured=True, error="No subscription selected."))
        return

    from app.azure.credentials import get_arm_token
    from app.quota import providers as prov

    yield _status("auth", "🔑 Acquiring ARM token…")
    token, terr = await get_arm_token(connection)
    if terr or not token:
        yield _status("error", f"❌ {terr or 'No ARM token.'}")
        yield ("snapshot", empty_snapshot(connection_configured=True, error=terr or "No ARM token."))
        return
    yield _status("auth", f"🔑 Token acquired · subscription {subscription_name or subscription_id}.")

    cat_filter: set[str] | None = set(categories) if categories else None
    thresholds = load_thresholds()

    # --- discover regions (all, unless the operator picked a subset) -------------------
    if regions:
        scan_regions = [r for r in regions if r]
        yield _status("regions", f"🌍 Scanning {len(scan_regions)} selected region(s).")
    else:
        yield _status("regions", "🌍 Discovering regions…")
        discovered, rerr = await prov.list_regions(token, subscription_id)
        if rerr and not discovered:
            yield _status("error", f"❌ Region discovery failed: {rerr}")
            yield ("snapshot", empty_snapshot(connection_configured=True, error=f"Region discovery failed: {rerr}"))
            return
        scan_regions = [r["name"] for r in discovered]
        yield _status("regions", f"🌍 {len(scan_regions)} region(s) reported by the subscription.")

    # --- provider registration (best-effort; non-fatal) -------------------------------
    yield _status("providers", "🧩 Checking resource-provider registration…")
    registration, _perr = await prov.provider_registration(token, subscription_id)
    n_reg = sum(1 for p in registration if p.get("registered"))
    unreg = [p["namespace"] for p in registration if not p.get("registered")]
    msg = f"🧩 {n_reg}/{len(registration)} required providers registered."
    if unreg:
        msg += f" Not registered: {', '.join(unreg)}."
    yield _status("providers", msg)

    collectors = registry.for_categories(cat_filter)
    region_collectors = [c for c in collectors if c.scope == "region"]
    sub_collectors = [c for c in collectors if c.scope == "subscription"]

    tracker = ThrottleTracker()
    sem = asyncio.Semaphore(_concurrency())
    hide_zero = _hide_zero_usage()
    results: list[QuotaResult] = []

    def _make_ctx(client, region: str) -> CollectorContext:
        return CollectorContext(
            token=token, connection=connection, tenant_id=tenant_id, tenant_name=tenant_name,
            subscription_id=subscription_id, subscription_name=subscription_name,
            region=region, client=client, throttle=tracker, thresholds=thresholds,
            selected_categories=cat_filter, hide_zero_usage=hide_zero, include_unused=include_unused,
        )

    async def _collect_one(collector, client, region: str) -> list[QuotaResult]:
        ctx = _make_ctx(client, region)
        async with sem:
            try:
                return await collector.collect(ctx)
            except Exception as exc:  # noqa: BLE001 - collector must never sink the scan
                log.warning("Quota collector %s failed (%s/%s): %s", collector.name, subscription_id, region, exc)
                return [collector._error_result(ctx, f"Unhandled collector error: {exc}")]

    async def _collect_region(client, region: str) -> tuple[str, list[QuotaResult]]:
        rows: list[QuotaResult] = []
        out = await asyncio.gather(*[_collect_one(c, client, region) for c in region_collectors])
        for r in out:
            rows.extend(r)
        return region, rows

    n_cat = len(cat_filter) if cat_filter else len(registry.categories())
    yield _status(
        "collect",
        f"📡 Querying {len(sub_collectors) + len(region_collectors)} collectors across "
        f"{len(scan_regions)} region(s) · {n_cat} categor{'y' if n_cat == 1 else 'ies'}…",
    )

    async with httpx.AsyncClient(timeout=45, base_url=_ARM) as client:
        # Subscription-wide collectors run once.
        if sub_collectors:
            sub_out = await asyncio.gather(*[_collect_one(c, client, "") for c in sub_collectors])
            for r in sub_out:
                results.extend(r)
            yield _status("collect", f"🗂️ Subscription-wide checks done ({sum(len(r) for r in sub_out)} item(s)).")

        # Per-region collectors run concurrently; emit a line as each region finishes.
        region_tasks = [asyncio.ensure_future(_collect_region(client, region)) for region in scan_regions]
        done_count = 0
        for fut in asyncio.as_completed(region_tasks):
            region, rows = await fut
            results.extend(rows)
            done_count += 1
            near = sum(
                1 for x in rows
                if x.risk_level in (RiskLevel.CRITICAL, RiskLevel.WARNING)
            )
            tail = f" · {near} at risk" if near else ""
            yield _status(
                "collect",
                f"✓ [{done_count}/{len(scan_regions)}] {region}: {len(rows)} quota row(s){tail}.",
            )

    # --- risk + recommendation per row ------------------------------------------------
    from app.quota import informational

    yield _status("score", "📐 Scoring risk & generating recommendations…")
    for r in results:
        if r.collection_status not in _ERROR_STATUSES:
            apply_risk(r, thresholds)
        # By-design saturated quotas (e.g. Network Watchers 1/1) are downgraded to informational
        # and get their own explanation; everything else gets the standard recommendation.
        if not informational.apply(r):
            r.recommendation = recommend_for_result(r)

    # --- throttling lane --------------------------------------------------------------
    results.extend(_throttling_results(tracker, subscription_id, subscription_name, tenant_id, tenant_name))

    counts = _count_risks(results)
    by_provider = _provider_rollup(results)
    provider_errors = [
        {
            "provider": r.provider_namespace,
            "service": r.service_name,
            "region": r.region,
            "status": r.collection_status,
            "message": r.error_message,
        }
        for r in results if r.collection_status in _ERROR_STATUSES
    ]

    crit = counts.get(RiskLevel.CRITICAL, 0)
    warn = counts.get(RiskLevel.WARNING, 0)

    status = "succeeded"
    if provider_errors:
        status = "partial"
    if counts["total"] == counts["errors"] and counts["errors"] > 0:
        status = "failed"

    yield _status(
        "done",
        f"✅ Scan complete — {counts['total']} quota(s) across {len(scan_regions)} region(s) · "
        f"{crit} critical, {warn} warning.",
    )

    yield ("snapshot", {
        "source": "azure",
        "demo": False,
        "connection_configured": True,
        "never_loaded": False,
        "error": "",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "subscription_id": subscription_id,
        "subscription_name": subscription_name,
        "regions_scanned": scan_regions,
        "categories_scanned": sorted(cat_filter) if cat_filter else registry.categories(),
        "thresholds": thresholds,
        "counts": counts,
        "by_provider": by_provider,
        "provider_registration": registration,
        "provider_errors": provider_errors,
        "throttling": {
            "events": len(tracker.events),
            "min_remaining_reads": tracker.min_remaining_reads,
        },
        "results": [r.to_dict() for r in results],
        "ai_summary": "",
        "used_ai": False,
        "capacity_note": CAPACITY_NOTE,
    })



def _throttling_results(
    tracker: ThrottleTracker, sub_id: str, sub_name: str, tenant_id: str, tenant_name: str,
) -> list[QuotaResult]:
    """Surface ARM throttling observed during the scan as a separate lane (not a quota object)."""
    out: list[QuotaResult] = []
    if not tracker.events:
        return out
    by_region: dict[str, int] = {}
    for ev in tracker.events:
        by_region[ev.region] = by_region.get(ev.region, 0) + 1
    for region, n in by_region.items():
        r = QuotaResult(
            subscription_id=sub_id, subscription_name=sub_name,
            region="" if region == "-" else region,
            provider_namespace="Microsoft.Resources", service_name="ARM API",
            quota_category="throttling", quota_name="ARM read throttling (HTTP 429)",
            current_usage=float(n), limit=None, unit="events",
            adjustable_status=AdjustableStatus.HARD_LIMIT, source_type=SourceType.MONITOR_METRIC,
            collection_status=CollectionStatus.OK, risk_level=RiskLevel.THROTTLING,
            tenant_id=tenant_id, tenant_name=tenant_name,
            last_checked_utc=datetime.now(timezone.utc).isoformat(),
            raw_provider_response={"events": n, "min_remaining_reads": tracker.min_remaining_reads},
        )
        r.recommendation = recommend_for_result(r)
        out.append(r)
    return out


def _count_risks(results: list[QuotaResult]) -> dict[str, int]:
    counts = _zero_counts()
    for r in results:
        counts["total"] += 1
        if r.collection_status in _ERROR_STATUSES:
            counts["errors"] += 1
        counts[r.risk_level] = counts.get(r.risk_level, 0) + 1
    return counts


def _provider_rollup(results: list[QuotaResult]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = out.setdefault(r.provider_namespace or "unknown", {"ok": 0, "error": 0})
        if r.collection_status in _ERROR_STATUSES:
            bucket["error"] += 1
        else:
            bucket["ok"] += 1
    return out

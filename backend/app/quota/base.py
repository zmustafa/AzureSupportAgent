"""Collector framework: the ``IQuotaCollector`` contract, a throttling-aware ARM client
(``CollectorContext``), and the ``QuotaCollectorRegistry``.

Each collector declares its metadata (provider namespace, categories, scope, required
permissions, whether quota is dynamic/adjustable) and implements ``collect(ctx)`` returning a
list of normalized ``QuotaResult`` rows. Collectors MUST be fail-soft: catch their own errors and
return an error-status result rather than raising, so one failing provider never sinks a scan."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.quota.model import (
    AdjustableStatus,
    CollectionStatus,
    QuotaResult,
    RiskLevel,
    SourceType,
)

log = logging.getLogger("app.quota.collector")

_ARM = "https://management.azure.com"
_RETRYABLE = {429, 500, 502, 503, 504}


# ----------------------------------------------------------------------- throttling
@dataclass
class ThrottleEvent:
    """A captured ARM rate-limit signal (429 / Retry-After / low remaining header)."""

    region: str
    path: str
    status: int
    retry_after: float | None
    remaining_reads: int | None
    at: str


class ThrottleTracker:
    """Records ARM throttling observed during a scan so the throttling lane can surface it
    separately from resource quota (ARM throttling is NOT a quota object)."""

    def __init__(self) -> None:
        self.events: list[ThrottleEvent] = []
        self.min_remaining_reads: int | None = None

    def note_response(self, region: str, path: str, resp: "httpx.Response") -> None:
        remaining = _header_int(resp, "x-ms-ratelimit-remaining-subscription-reads")
        if remaining is None:
            remaining = _header_int(resp, "x-ms-ratelimit-remaining-tenant-reads")
        if remaining is not None:
            self.min_remaining_reads = (
                remaining if self.min_remaining_reads is None else min(self.min_remaining_reads, remaining)
            )
        if resp.status_code == 429:
            self.events.append(
                ThrottleEvent(
                    region=region,
                    path=path,
                    status=429,
                    retry_after=_retry_after(resp),
                    remaining_reads=remaining,
                    at=datetime.now(timezone.utc).isoformat(),
                )
            )


def _header_int(resp: "httpx.Response", name: str) -> int | None:
    val = resp.headers.get(name)
    if val is None:
        return None
    try:
        # The header can be a comma list across resource types; take the smallest.
        return min(int(x) for x in str(val).split(",") if x.strip().isdigit())
    except (ValueError, TypeError):
        return None


def _retry_after(resp: "httpx.Response") -> float | None:
    val = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------- context
@dataclass
class CollectorContext:
    """Per-(subscription[, region]) execution context passed to every collector.

    Holds the ARM token, the shared httpx client, identity metadata, the resolved risk
    thresholds, and the scan-wide throttle tracker. Exposes ``arm_get`` — a single ARM REST GET
    with bounded retry/backoff that records throttling — so collectors never build their own."""

    token: str
    connection: dict[str, Any]
    tenant_id: str
    tenant_name: str
    subscription_id: str
    subscription_name: str
    region: str = ""
    client: httpx.AsyncClient | None = None
    throttle: ThrottleTracker | None = None
    thresholds: dict[str, float] = field(default_factory=dict)
    # The category filter the operator selected for this scan (None = all). Used by the static
    # service-limit collectors to emit only the relevant documented limits.
    selected_categories: set[str] | None = None
    # Hide usage-API rows whose current usage is exactly 0 (full headroom = not actionable for a
    # quota monitor, and the per-model AI/compute tables are otherwise hundreds of zero rows).
    hide_zero_usage: bool = True
    # When True, include zero-usage rows for EVERY collector (operator asked for the full quota
    # table, e.g. to see all VM SKU families with headroom). Overrides hide_zero_usage.
    include_unused: bool = False
    max_retries: int = 3

    async def arm_get(self, path: str, params: dict[str, Any]) -> tuple[Any, str | None, int]:
        """ARM REST GET with retry/backoff + throttle capture.

        Returns ``(json_or_none, error_or_none, status_code)``. ``status_code`` is surfaced so a
        collector can distinguish 403 (unauthorized) / 404 (not supported) / 409 (not registered)
        and stamp the right collection_status. Never raises."""
        client = self.client
        headers = {"Authorization": f"Bearer {self.token}"}
        region = self.region or "-"
        owns_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=45, base_url=_ARM)
            owns_client = True
        try:
            last_status = 0
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.get(path, headers=headers, params=params)
                except httpx.HTTPError as exc:  # noqa: BLE001 - transient network
                    if attempt >= self.max_retries:
                        return None, f"ARM request error: {exc}", 0
                    await asyncio.sleep(min(20.0, (2 ** attempt) + random.uniform(0, 0.4)))
                    continue
                last_status = resp.status_code
                if self.throttle is not None:
                    self.throttle.note_response(region, path, resp)
                if resp.status_code in _RETRYABLE and attempt < self.max_retries:
                    delay = _retry_after(resp) or ((2 ** attempt) + random.uniform(0, 0.4))
                    await asyncio.sleep(min(45.0, delay))
                    continue
                if resp.status_code == 200:
                    try:
                        return resp.json(), None, 200
                    except (ValueError, AttributeError) as exc:
                        return None, f"ARM response parse error: {exc}", 200
                try:
                    detail = resp.json().get("error", {}).get("message", resp.text)
                except (ValueError, AttributeError):
                    detail = resp.text
                return None, f"ARM {resp.status_code}: {str(detail)[:300]}", resp.status_code
            return None, "ARM request failed after retries.", last_status
        finally:
            if owns_client:
                await client.aclose()


# ----------------------------------------------------------------------- contract
class IQuotaCollector:
    """Base class for a quota collector. Subclasses set the class metadata and implement
    ``collect``. Keep ``collect`` fail-soft — return an error result, do not raise."""

    name: str = ""
    provider_namespace: str = ""
    service_label: str = ""
    categories: tuple[str, ...] = ()
    # "subscription" → run once per subscription; "region" → run once per selected region.
    scope: str = "subscription"
    required_permissions: tuple[str, ...] = ()
    dynamic: bool = True
    adjustable_default: str = AdjustableStatus.UNKNOWN
    source_default: str = SourceType.RP_USAGE_API

    async def collect(self, ctx: CollectorContext) -> list[QuotaResult]:  # pragma: no cover - interface
        raise NotImplementedError

    # -- helpers shared by concrete collectors --------------------------------------
    def _base(self, ctx: CollectorContext, **over: Any) -> QuotaResult:
        """A QuotaResult pre-filled with identity + collector defaults."""
        r = QuotaResult(
            subscription_id=ctx.subscription_id,
            subscription_name=ctx.subscription_name,
            region=ctx.region if self.scope == "region" else "",
            provider_namespace=self.provider_namespace,
            service_name=self.service_label,
            quota_category=self.categories[0] if self.categories else "",
            adjustable_status=self.adjustable_default,
            source_type=self.source_default,
            tenant_id=ctx.tenant_id,
            tenant_name=ctx.tenant_name,
            last_checked_utc=datetime.now(timezone.utc).isoformat(),
        )
        for k, v in over.items():
            setattr(r, k, v)
        return r

    def _error_result(self, ctx: CollectorContext, error: str, status_code: int = 0) -> QuotaResult:
        """A single error-status row representing this collector failing for this scope."""
        coll = CollectionStatus.ERROR
        if status_code in (401, 403):
            coll = CollectionStatus.UNAUTHORIZED
        elif status_code == 404:
            coll = CollectionStatus.NOT_SUPPORTED
        elif status_code == 409:
            coll = CollectionStatus.NOT_REGISTERED
        return self._base(
            ctx,
            quota_name=f"{self.service_label} (collection failed)",
            collection_status=coll,
            risk_level=RiskLevel.UNKNOWN,
            source_type=SourceType.MANUAL_REVIEW,
            error_message=error[:400],
        )


# ----------------------------------------------------------------------- registry
class QuotaCollectorRegistry:
    """Holds the registered collectors and exposes metadata for the UI/meta endpoint."""

    def __init__(self) -> None:
        self._collectors: list[IQuotaCollector] = []

    def register(self, collector: IQuotaCollector) -> IQuotaCollector:
        self._collectors.append(collector)
        return collector

    def all(self) -> list[IQuotaCollector]:
        return list(self._collectors)

    def for_categories(self, categories: set[str] | None) -> list[IQuotaCollector]:
        if not categories:
            return self.all()
        return [c for c in self._collectors if set(c.categories) & categories]

    def categories(self) -> list[str]:
        seen: list[str] = []
        for c in self._collectors:
            for cat in c.categories:
                if cat not in seen:
                    seen.append(cat)
        return seen

    def meta(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "provider_namespace": c.provider_namespace,
                "service_label": c.service_label,
                "categories": list(c.categories),
                "scope": c.scope,
                "required_permissions": list(c.required_permissions),
                "dynamic": c.dynamic,
                "adjustable_default": c.adjustable_default,
                "source_default": c.source_default,
            }
            for c in self._collectors
        ]


# Singleton registry populated by importing app.quota.collectors (see that package's __init__).
registry = QuotaCollectorRegistry()

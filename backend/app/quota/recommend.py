"""Recommendation engine.

Two layers:
- ``recommend_for_result`` — a deterministic, plain-English recommendation for EVERY quota row,
  keyed by (category, risk, adjustable_status, source_type). Always present, instant, no LLM.
- ``ai_executive_summary`` — an optional AI pass (wired to the app's provider) that produces a
  prioritized, executive action list over the risky rows. Best-effort: on any failure it falls
  back to a deterministic summary, so the scan never depends on the LLM being available."""
from __future__ import annotations

import logging
from typing import Any

from app.quota.model import (
    AdjustableStatus,
    CollectionStatus,
    QuotaResult,
    RiskLevel,
    SourceType,
)

log = logging.getLogger("app.quota.recommend")

CAPACITY_NOTE = (
    "Quota approval does not guarantee real-time Azure regional/SKU capacity — a granted "
    "quota can still fail to allocate if the region/SKU is capacity-constrained."
)


def recommend_for_result(r: QuotaResult) -> str:
    """Deterministic recommendation for a single result. Never empty."""
    where = r.region or "the subscription"
    label = r.sku_family or r.quota_name or r.service_name

    if r.collection_status == CollectionStatus.NOT_REGISTERED:
        return (
            f"Resource provider {r.provider_namespace} is not registered in this subscription. "
            f"Register it (az provider register --namespace {r.provider_namespace}) then re-scan."
        )
    if r.collection_status == CollectionStatus.UNAUTHORIZED:
        return (
            f"The connection's identity lacks permission to read {r.service_name} quota. Grant "
            f"Reader (or the specific usages/read action) at the subscription scope and re-scan."
        )
    if r.collection_status == CollectionStatus.ERROR:
        return f"Could not collect {r.service_name} quota: {r.error_message or 'unknown error'}. Re-scan or review manually."

    if r.risk_level == RiskLevel.THROTTLING:
        return (
            "Recent ARM/service throttling (HTTP 429) observed. Add retry with exponential "
            "backoff, honor Retry-After, and reduce polling frequency / batch your calls."
        )

    if r.source_type == SourceType.MANUAL_REVIEW:
        return (
            f"Dynamic quota data is not available for {label}. Manual review required — check the "
            f"Azure portal Quota blade or the service's documented limits."
        )
    if r.source_type == SourceType.STATIC_LIMIT:
        if r.adjustable_status == AdjustableStatus.HARD_LIMIT:
            return f"{label} is a documented hard limit ({_fmt_limit(r)}). Design within it or split across resources/accounts."
        if r.adjustable_status == AdjustableStatus.SUPPORT_REQUIRED:
            return f"{label} is a documented limit ({_fmt_limit(r)}); raising it requires an Azure support request."
        return f"{label}: documented service limit ({_fmt_limit(r)}). Monitor usage against it."

    # Dynamic, with a real limit + percentage.
    pct = r.percent_used
    if pct is None:
        return f"{label}: no limit reported by Azure for {where}. Treat as unknown and review manually."
    if r.risk_level == RiskLevel.CRITICAL:
        if r.adjustable_status in (AdjustableStatus.ADJUSTABLE, AdjustableStatus.SUPPORT_REQUIRED):
            return (
                f"{label} in {where} is at {pct:.0f}% ({_fmt_usage(r)}). Request a quota increase "
                f"now before deployments fail. {CAPACITY_NOTE}"
            )
        return f"{label} in {where} is at {pct:.0f}% and is a hard limit. Redesign or split resources before you hit it."
    if r.risk_level == RiskLevel.WARNING:
        return (
            f"{label} in {where} is at {pct:.0f}% ({_fmt_usage(r)}). Review upcoming deployments "
            f"and request a quota increase if more headroom is needed."
        )
    if r.risk_level == RiskLevel.WATCH:
        return f"{label} in {where} is at {pct:.0f}%. Watch this — plan a quota increase if growth continues."
    return f"{label} in {where} is healthy at {pct:.0f}% ({_fmt_usage(r)}). No action needed."


def _fmt_usage(r: QuotaResult) -> str:
    if r.current_usage is None or r.limit is None:
        return ""
    return f"{_num(r.current_usage)}/{_num(r.limit)} {r.unit}"


def _fmt_limit(r: QuotaResult) -> str:
    if r.limit is None:
        return f"limit not published, {r.unit}"
    return f"{_num(r.limit)} {r.unit}"


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


# --------------------------------------------------------------------- AI summary
def _deterministic_summary(results: list[QuotaResult], counts: dict[str, int]) -> str:
    crit = counts.get(RiskLevel.CRITICAL, 0)
    warn = counts.get(RiskLevel.WARNING, 0)
    watch = counts.get(RiskLevel.WATCH, 0)
    thr = counts.get(RiskLevel.THROTTLING, 0)
    lines = [f"Quota scan summary: {crit} critical, {warn} warning, {watch} watch, {thr} throttling signals."]
    risky = sorted(
        [r for r in results if r.risk_level in (RiskLevel.CRITICAL, RiskLevel.WARNING)],
        key=lambda r: (r.percent_used or 0),
        reverse=True,
    )[:8]
    for r in risky:
        lines.append(f"- {r.region or 'sub'} · {r.service_name} · {r.quota_name}: {r.recommendation}")
    if not risky and not thr:
        lines.append("No quotas are near exhaustion. Estate has healthy headroom.")
    lines.append(CAPACITY_NOTE)
    return "\n".join(lines)


async def ai_executive_summary(
    results: list[QuotaResult],
    counts: dict[str, int],
    *,
    subscription_name: str,
) -> dict[str, Any]:
    """Return {summary, used_ai}. Best-effort AI prioritization over the risky rows; falls back
    to a deterministic summary on any failure."""
    fallback = _deterministic_summary(results, counts)
    risky = [
        r for r in results
        if r.risk_level in (RiskLevel.CRITICAL, RiskLevel.WARNING, RiskLevel.WATCH, RiskLevel.THROTTLING)
    ]
    if not risky:
        return {"summary": fallback, "used_ai": False}
    try:
        from app.agent.factory import build_provider

        provider = build_provider()
    except Exception:  # noqa: BLE001
        return {"summary": fallback, "used_ai": False}

    risky_sorted = sorted(risky, key=lambda r: (r.percent_used or 0), reverse=True)[:30]
    block = "\n".join(
        f"- region={r.region or 'sub'} service={r.service_name} quota={r.quota_name} "
        f"family={r.sku_family or '-'} used={r.percent_used if r.percent_used is not None else '?'}% "
        f"({_fmt_usage(r) or 'n/a'}) adjustable={r.adjustable_status} risk={r.risk_level}"
        for r in risky_sorted
    )
    system = (
        "You are an Azure capacity & quota engineer. Given a list of near-limit quotas for one "
        "subscription, write a concise executive summary (Markdown) for an operations team: a "
        "one-paragraph overview, then a prioritized, numbered action list (most urgent first) of "
        "concrete steps — which quota to raise in which region, which are hard limits requiring "
        "redesign, and which need a support request. Be specific and do not invent quotas not "
        "listed. End with a one-line reminder that quota approval does not guarantee capacity."
    )
    user = (
        f"SUBSCRIPTION: {subscription_name}\n"
        f"RISK COUNTS: {counts}\n\nNEAR-LIMIT QUOTAS:\n{block}"
    )

    # Stream the model; retry once on a transient failure (the GitHub Copilot / network path can
    # blip after a long scan), then fall back to the deterministic summary.
    for attempt in range(2):
        text = ""
        try:
            async for ev in provider.stream(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                None,
                max_tokens=2000,
            ):
                if getattr(ev, "type", "") == "token":
                    text += ev.text
        except Exception as exc:  # noqa: BLE001
            log.warning("Quota AI summary attempt %d failed: %r", attempt + 1, exc)
            if attempt == 0:
                continue
            return {"summary": fallback, "used_ai": False}
        text = text.strip()
        if len(text) >= 60:
            return {"summary": text, "used_ai": True}
        if attempt == 0:
            continue
    return {"summary": fallback, "used_ai": False}

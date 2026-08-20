"""One analysis pass that produces every Backup Manager tab's payload.

Before this module each tab endpoint independently re-derived its view from the estate, so
opening nine tabs meant nine sweeps of the same Azure data and nine chances for the numbers to
disagree with each other. :func:`build_snapshot` runs the pipeline exactly once and returns a
single document that every tab reads, which is both faster and internally consistent: the job
counts on the overview are by construction the same rows the job inbox lists.

It reuses the same pure functions the endpoints call — :mod:`jobs`, :mod:`posture`,
:mod:`policies`, :mod:`gaps`, :mod:`dr`, :mod:`cost` — so there is one implementation of each
algorithm, not a snapshot copy that can drift from the live one.

Every stage reports through ``progress(phase, message)`` so the UI can show what is being
fetched and how far along it is. Phases, in order:
``scope`` → ``query`` → ``orphans`` → ``vaults`` → ``analyze`` → ``cost`` → ``save`` → ``done``.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.backup_manager import cost as cost_ops
from app.backup_manager import costmgmt
from app.backup_manager import dr as dr_ops
from app.backup_manager import gaps as gap_ops
from app.backup_manager import inventory as inventory_ops
from app.backup_manager import jobs as job_ops
from app.backup_manager import policies as policy_ops
from app.backup_manager import posture as posture_ops
from app.backup_manager import pricing, reference
from app.backup_manager import reports as report_ops
from app.backup_manager import snapshot as snapshot_store

log = logging.getLogger("app.backup_manager.analysis")

ProgressFn = Callable[[str, str], Awaitable[None]]

#: The cost period a snapshot is built for. The Cost tab can ask for other periods on demand;
#: those are explicit user actions and are fetched live rather than snapshotted.
DEFAULT_MONTHS_BACK = 1
DEFAULT_COST_TYPE = "AmortizedCost"


class CollectionFailed(RuntimeError):
    """Every Resource Graph source failed, so there is no estate to analyze.

    Raised rather than returning an empty snapshot: an expired token or a revoked role would
    otherwise produce a perfectly well-formed analysis saying the tenant has no backups at
    all, overwrite the last good one, and look exactly like a real answer.
    """


def price_region(estate: dict[str, Any], connection: dict[str, Any]) -> str:
    """Region whose list prices best represent this estate — the most common vault location."""
    counts: dict[str, int] = {}
    for vault in estate.get("vaults", []) or []:
        location = str(vault.get("location") or "").strip().lower()
        if location:
            counts[location] = counts.get(location, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    configured = str(reference.cost_rates().get("price_region") or "").strip().lower()
    return configured or str(connection.get("default_region") or "") or "eastus"


def match_report_storage(estate: dict[str, Any], by_item: dict[str, Any]) -> dict[str, float]:
    """Map Log Analytics per-item consumed GB onto our protected-item ids.

    Backup Reports keys rows by its own item identifier, so matching is by friendly name.
    Ambiguous matches are dropped rather than guessed — a wrong weight silently misallocates
    real money."""
    lowered = {str(k).lower(): v for k, v in (by_item or {}).items()}
    out: dict[str, float] = {}
    for instance in estate.get("instances", []):
        name = str(instance.get("friendly_name") or "").strip().lower()
        if not name:
            continue
        hits = [gb for key, gb in lowered.items() if name in key]
        if len(hits) == 1:
            try:
                out[str(instance.get("id") or "")] = float(hits[0])
            except (TypeError, ValueError):
                continue
    return out


def resolve_currency(connection: dict[str, Any], tenant_id: str) -> str:
    """The tenant's billing currency when known, else the configured default.

    Reads cache only, so callers that must stay fast never pay for a Cost Management call."""
    return (
        costmgmt.known_currency(connection, tenant_id=tenant_id)
        or str(reference.cost_rates().get("currency") or "USD")
    )


async def build_cost(
    connection: dict[str, Any],
    estate: dict[str, Any],
    *,
    tenant_id: str,
    is_demo: bool,
    use_reports: bool = True,
    use_actuals: bool = True,
    months_back: int = DEFAULT_MONTHS_BACK,
    cost_type: str = DEFAULT_COST_TYPE,
    force: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Backup cost from the best available source.

    Layers three inputs, each labeled so the caller knows what it is looking at: live retail
    list prices (forward-looking), Log Analytics consumption (per-item truth), and Cost
    Management actuals (authoritative, but only ever attributed to the vault).
    """
    subscriptions = [s for s in (estate.get("scope", {}).get("subscriptions") or []) if s]

    # 1. Actual spend first — it is the only source that knows the billing currency.
    actuals: dict[str, Any] = {"available": False, "by_vault": {}, "by_meter": {}, "currency": "",
                               "total": 0.0, "daily": [], "reason": "", "remedy": ""}
    if use_actuals and not is_demo and subscriptions:
        if progress:
            await progress("cost", f"Querying Cost Management actuals across {len(subscriptions)} subscription(s)…")
        actuals = await costmgmt.cached_actuals(
            connection, subscriptions, tenant_id=tenant_id,
            months_back=months_back, cost_type=cost_type, daily=True, force=force,
        )
        if progress:
            await progress(
                "cost",
                f"Actual spend {actuals.get('total', 0):,.2f} {actuals.get('currency', '')} "
                f"across {len(actuals.get('by_meter') or {})} meter(s); "
                f"{actuals.get('subscriptions_succeeded', len(subscriptions))}/{len(subscriptions)} subscription queries succeeded."
                if actuals.get("available")
                else f"Cost Management unavailable — {str(actuals.get('reason') or '')[:120]}",
            )
    elif is_demo:
        actuals["reason"] = "Demo mode does not query Azure Cost Management."

    # 2. Rate card in the billing currency where we know it, so estimate and actual compare.
    currency = str(actuals.get("currency") or "") or str(reference.cost_rates().get("currency") or "USD")
    region = price_region(estate, connection)
    regions = sorted({str(vault.get("location") or "").lower() for vault in estate.get("vaults", []) if vault.get("location")})
    rate_card = cost_ops.reference_rate_card()
    if not is_demo:
        try:
            live_card = await pricing.get_rate_card(region, currency, force=force)
            if live_card.get("instance_meters"):
                rate_card = live_card
        except (ValueError, KeyError, TypeError):  # noqa: BLE001 - fall back to the seeded table
            rate_card = cost_ops.reference_rate_card()
        if progress:
            await progress(
                "cost",
                f"Retail rate card for {region} in {currency}: "
                f"{len(rate_card.get('instance_meters') or {})} instance meter(s), "
                f"{len(rate_card.get('storage_gb_month') or {})} storage rate(s).",
            )

    # 3. Per-item consumed GB from Backup Reports — the allocation weights.
    storage_by_instance: dict[str, float] = {}
    report_note = ""
    report: dict[str, Any] = {}
    if use_reports and not is_demo:
        if progress:
            await progress("cost", "Asking Log Analytics for per-item consumed storage…")
        try:
            report = await report_ops.build_report(connection, estate, days=30)
            if report.get("available"):
                storage_by_instance = match_report_storage(estate, report.get("storage_by_item", {}))
                report_note = str(report.get("reason") or "")
            else:
                report_note = report.get("reason", "")
        except report_ops.ReportsUnavailable as exc:
            report_note = exc.reason
        if progress:
            await progress(
                "cost",
                f"Measured storage for {len(storage_by_instance)} item(s)." if storage_by_instance
                else f"No measured storage — {report_note[:120] or 'no reporting workspace'}.",
            )

    estimate = cost_ops.estimate(estate, rate_card=rate_card, storage_by_instance=storage_by_instance)
    allocation = (
        {
            "rows": [], "currency": "", "allocated_total": 0.0,
            "unattributed_total": 0.0, "vaults_allocated": 0,
            "vaults_unattributed": len(actuals.get("by_vault") or {}),
            "basis_counts": {},
            "note": "Allocation is unavailable because the management group spans multiple billing currencies.",
        }
        if actuals.get("mixed_currency")
        else cost_ops.allocate(
            estate, actuals, estimate_rows=estimate.get("top_rows", []),
            storage_by_instance=storage_by_instance,
        )
    )
    cost_by_instance = {r["instance_id"]: r["allocated_cost"] for r in allocation.get("rows", [])}

    estimate["actuals"] = {k: v for k, v in actuals.items() if k != "rows"}
    estimate["allocation"] = allocation
    estimate["variance"] = cost_ops.variance(estimate, actuals)
    estimate["waste"] = cost_ops.waste(
        estate, rate_card=rate_card, cost_by_instance=cost_by_instance or None,
    )
    estimate["report_note"] = report_note
    estimate["price_regions"] = regions
    estimate["representative_price_region"] = len(regions) > 1
    if len(regions) > 1:
        estimate["rate_error"] = (
            f"The scope spans {len(regions)} vault regions; list-price estimates use representative region {region}."
        )
    estimate["partial"] = bool(
        actuals.get("mixed_currency")
        or (actuals.get("reason") and not actuals.get("available"))
        or report_note
        or report.get("partial")
        or len(regions) > 1
    ) if not is_demo else False
    estimate["price_region"] = region
    estimate["demo"] = bool(estate.get("demo"))
    estimate["months_back"] = months_back
    estimate["cost_type"] = cost_type
    return estimate


# --------------------------------------------------------------------------- sections
def _facets(estate: dict[str, Any]) -> dict[str, Any]:
    instances = estate.get("instances", [])
    return {
        "datasource_types": sorted({r.get("datasource_type", "") for r in instances if r.get("datasource_type")}),
        "states": sorted({r.get("protection_state", "") for r in instances if r.get("protection_state")}),
        "vaults": [
            {"id": v["id"], "name": v["name"], "kind": v["kind"], "count": v.get("instance_count", 0)}
            for v in estate.get("vaults", [])
        ],
    }


def compose(
    estate: dict[str, Any],
    *,
    enriched_jobs: list[dict[str, Any]],
    posture: dict[str, Any],
    policies: dict[str, Any],
    compliance: dict[str, Any],
    gaps: dict[str, Any],
    readiness: dict[str, Any],
    rpo: dict[str, Any],
    cost: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the per-tab document from already-computed sections.

    Pure, so the demo estate and the tests build a snapshot through exactly this code."""
    instances = estate.get("instances", [])
    job_summary = job_ops.summarize(enriched_jobs)
    chronic = job_ops.chronic_failures(enriched_jobs, instances)
    capacity = posture_ops.capacity(estate.get("vaults", []))
    waste = cost.get("waste") or {}

    summary = {
        "generated_at": estate.get("generated_at"),
        "demo": bool(estate.get("demo")),
        "scope": estate.get("scope", {}),
        "errors": estate.get("errors", {}),
        "warnings": estate.get("warnings", {}),
        "source_details": estate.get("source_details", {}),
        "protection": {
            "vaults": len(estate.get("vaults", [])),
            "protected_items": len(instances),
            "stopped": sum(1 for i in instances if i.get("protection_stopped")),
            "orphaned": sum(1 for i in instances if i.get("orphaned")),
            "policies": len(estate.get("policies", [])),
        },
        "jobs": job_summary,
        "chronic_failures": len(chronic),
        "rpo": {
            "attainment_pct": rpo["attainment_pct"], "breached": rpo["breached"],
            "at_risk": rpo["at_risk"], "unknown": rpo["unknown"],
        },
        "posture": {
            "average_score": posture["average_score"], "band": posture["band"],
            "red_vaults": posture["red_vaults"], "actionable_count": posture["actionable_count"],
        },
        "dr": readiness["summary"],
        "cost": {
            "monthly_total": cost.get("monthly_total", 0.0), "currency": cost.get("currency", ""),
            "confidence": cost.get("confidence", ""),
            "recoverable_monthly": waste.get("recoverable_monthly", 0.0),
        },
        # The ledger lives in the database and changes without an analysis, so the live value
        # is layered on by the API rather than frozen here.
        "actionable_changes": 0,
        "job_window_days": estate.get("job_window_days"),
    }

    return {
        "report_exists": True,
        "generated_at": estate.get("generated_at"),
        "demo": bool(estate.get("demo")),
        "scope": estate.get("scope", {}),
        "errors": estate.get("errors", {}),
        "warnings": estate.get("warnings", {}),
        "source_details": estate.get("source_details", {}),
        "job_window_days": estate.get("job_window_days"),
        "summary": summary,
        "inventory": {
            "rows": instances,
            "facets": _facets(estate),
            "total_count": sum(
                int((estate.get("source_details", {}).get(name, {}) or {}).get("source_total") or 0)
                for name in ("rsv_items", "dp_instances")
            ) or len(instances),
            "truncated": any(
                bool((estate.get("source_details", {}).get(name, {}) or {}).get("partial"))
                for name in ("rsv_items", "dp_instances")
            ),
        },
        "jobs": {
            "rows": enriched_jobs,
            "summary": job_summary,
            "total_count": sum(
                int((estate.get("source_details", {}).get(name, {}) or {}).get("source_total") or 0)
                for name in ("rsv_jobs", "dp_jobs")
            ) or len(enriched_jobs),
            "job_window_days": estate.get("job_window_days"),
            "truncated": any(
                bool((estate.get("source_details", {}).get(name, {}) or {}).get("partial"))
                for name in ("rsv_jobs", "dp_jobs")
            ),
        },
        "job_analysis": {
            "clusters": job_ops.cluster_failures(enriched_jobs),
            "chronic": chronic,
            "congestion": job_ops.congestion(enriched_jobs),
            "summary": job_summary,
            "job_window_days": estate.get("job_window_days"),
        },
        "policies": policies,
        "compliance": compliance,
        "posture": {**posture, "capacity": capacity, "generated_at": estate.get("generated_at")},
        "vaults": {"vaults": estate.get("vaults", []), "capacity": capacity},
        "gaps": gaps,
        "dr": {**readiness, "rpo": rpo},
        "cost": cost,
        "counts": {
            "vaults": len(estate.get("vaults", [])),
            "protected_items": len(instances),
            "policies": len(estate.get("policies", [])),
            "jobs": len(enriched_jobs),
            "gaps": len(gaps.get("gaps") or []),
            "failed_jobs": int(job_summary.get("failed", 0) or 0),
        },
    }


# --------------------------------------------------------------------------- entry point
async def build_snapshot(
    connection: dict[str, Any],
    *,
    tenant_id: str,
    scope_kind: str,
    scope_id: str,
    workload_id: str = "",
    subscription_id: str = "",
    management_group_id: str = "",
    progress: ProgressFn | None = None,
    resolved_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the whole Backup Manager pipeline once and return every tab's payload."""
    async def emit(phase: str, message: str) -> None:
        if progress is not None:
            await progress(phase, message)

    estate = await inventory_ops.collect_estate(
        connection, tenant_id=tenant_id, workload_id=workload_id or None,
        subscription_id=subscription_id or None,
        management_group_id=management_group_id or None,
        force=True, progress=progress, resolved_scope=resolved_scope,
    )
    is_demo = bool(estate.get("demo"))

    errors = estate.get("errors") or {}
    if set(inventory_ops.SOURCE_LABELS).issubset(set(errors)):
        first = next(iter(errors.values()), "")
        await emit("error", f"Every Resource Graph source failed — {first}")
        raise CollectionFailed(
            f"No backup data could be read from Azure, so the previous analysis was kept. {first}"
        )

    if estate.get("vaults"):
        await inventory_ops.enrich_vaults(connection, estate["vaults"], progress=progress)

    await emit("analyze", f"Joining {len(estate.get('jobs', [])):,} job(s) to the failure knowledge base…")
    enriched_jobs = job_ops.enrich(estate.get("jobs", []))

    await emit("analyze", f"Scoring ransomware posture across {len(estate.get('vaults', []))} vault(s)…")
    posture = posture_ops.build_posture(estate.get("vaults", []))

    await emit("analyze", f"Analyzing {len(estate.get('policies', []))} policy/policies for sprawl and drift…")
    policies = policy_ops.analyze(estate.get("policies", []), estate.get("instances", []))

    await emit("analyze", "Grading protected items against their retention tiers…")
    compliance = policy_ops.compliance(estate.get("instances", []), estate.get("policies", []))
    compliance["tiers"] = reference.load_reference().get("tiers", [])

    await emit("analyze", "Detecting unprotected but backup-eligible resources…")
    subscriptions = set(estate.get("scope", {}).get("subscriptions") or [])
    gaps = await gap_ops.detect(connection, estate, subscriptions=subscriptions)
    coverage_gaps, coverage_status = gap_ops.ingest_coverage_gaps_for_scope(
        tenant_id, scope_kind, scope_id, sorted(subscriptions),
    ) if scope_id else ([], {"available_snapshots": 0, "missing_snapshots": 0, "partial": False})
    gaps["coverage_gaps"] = coverage_gaps
    gaps["coverage_status"] = coverage_status
    # The remediation form needs somewhere to send the work, so carry the target lists along.
    gaps["vaults"] = [
        {"id": v["id"], "name": v["name"], "kind": v["kind"], "location": v["location"],
         "subscription_id": v["subscription_id"], "redundancy": v.get("redundancy", "")}
        for v in estate.get("vaults", [])
    ]
    gaps["policies"] = [
        {"id": p["id"], "arm_id": p.get("arm_id", ""), "name": p["name"], "vault_id": p["vault_id"],
         "vault_kind": p["vault_kind"], "backup_management_type": p.get("backup_management_type", ""),
         "retention_days": p.get("retention_days")}
        for p in estate.get("policies", [])
    ]
    await emit("analyze", f"Found {len(gaps.get('gaps') or []):,} unprotected eligible resource(s).")

    await emit("analyze", "Building Site Recovery readiness and RPO attainment…")
    readiness = dr_ops.build_readiness(estate)
    rpo = dr_ops.rpo_attainment(estate.get("instances", []))

    cost = await build_cost(
        connection, estate, tenant_id=tenant_id, is_demo=is_demo, progress=progress,
    )

    snapshot = compose(
        estate, enriched_jobs=enriched_jobs, posture=posture, policies=policies,
        compliance=compliance, gaps=gaps, readiness=readiness, rpo=rpo, cost=cost,
    )
    snapshot["scope"] = {**snapshot.get("scope", {}), "scope_kind": scope_kind, "scope_id": scope_id}
    snapshot["partial"] = bool(
        errors or estate.get("warnings") or gaps.get("source_detail", {}).get("partial")
        or coverage_status.get("partial")
        or cost.get("partial")
    )
    return snapshot_store.bound(snapshot)

"""Inventory endpoints: a unified, read-only resource grid across Azure Workloads (or the
whole tenant), with workload attribution, facets for filtering, server-side caching, and
AI natural-language search. Admin-only; all Azure access is read-only.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.azure_connections import resolve_connection
from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.inventory import ai, cache, cost, service, snapshots
from app.inventory import optimization as optimization_mod
from app.models import AssessmentRun
from app.policy import collector as policy_collector

router = APIRouter(prefix="/inventory", tags=["inventory"])
logger = logging.getLogger("app.api.inventory")

# Small in-process cache of the (slow) policy inventory per tenant+connection, so the
# Governance drawer tab doesn't re-query Azure Policy on every resource open.
_POLICY_CACHE: dict[str, dict[str, Any]] = {}
_POLICY_TTL = 300.0


def _conn(connection_id: str | None) -> dict[str, Any] | None:
    return resolve_connection(connection_id)


def _actor(p: Principal) -> str:
    return p.display_name or p.email or p.subject


async def _policy_inventory(tenant_id: str, connection_id: str) -> dict[str, Any]:
    key = f"{tenant_id}|{connection_id}"
    hit = _POLICY_CACHE.get(key)
    if hit and (time.time() - hit["ts"]) < _POLICY_TTL:
        return hit["inv"]
    inv = await policy_collector.collect_inventory(_conn(connection_id or None))
    _POLICY_CACHE[key] = {"inv": inv, "ts": time.time()}
    return inv


@router.get("")
async def get_inventory(
    connection_id: str | None = None,
    force: int = 0,
    principal: Principal = Depends(require_admin),
):
    """Full resource inventory (resources + facets + summary), attributed to workloads.

    Server-cached PERMANENTLY per tenant + connection so the many Resource Graph queries run
    only once until refreshed. ``force=1`` bypasses the cache and re-collects from Azure."""
    tid = principal.tenant_id
    cid = connection_id or ""
    if not force:
        hit = cache.get(tid, cid)
        if hit:
            return {**hit["payload"], "cached": True, "fetched_at": hit["fetched_at"], "age_seconds": hit["age_seconds"]}

    conn = _conn(connection_id)
    payload = await service.collect(conn)
    fetched_at = cache.set_(tid, cid, payload)
    return {**payload, "cached": False, "fetched_at": fetched_at, "age_seconds": 0}


@router.get("/optimization")
async def get_optimization(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
):
    """Cost-optimization report: orphaned / idle resources (unattached disks, idle public
    IPs, orphaned NICs) joined with trailing-30-day cost as an estimated monthly saving.

    Read-only and cache-only — it never triggers a fresh Resource Graph or Cost query, so
    it returns instantly. If inventory hasn't been collected yet, ``available`` is false and
    the UI prompts the user to load the Inventory grid first."""
    tid = principal.tenant_id
    cid = connection_id or ""
    hit = cache.get(tid, cid)
    if not hit:
        return {"available": False, "categories": [], "items": [], "total_count": 0}
    resources = (hit.get("payload") or {}).get("resources") or []
    cost_payload = cost.peek_cost(tid, cid)
    report = optimization_mod.analyze_resources(resources, cost_payload)
    return {
        "available": True,
        "inventory_fetched_at": hit.get("fetched_at"),
        **report,
    }


class NlSearchReq(BaseModel):
    query: str
    connection_id: str = ""
    # Context so the AI maps friendly names to exact values present in the inventory.
    types: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    workloads: list[str] = Field(default_factory=list)
    subscriptions: list[str] = Field(default_factory=list)


@router.post("/nl-search")
async def post_nl_search(req: NlSearchReq, principal: Principal = Depends(require_admin)):
    """Translate a natural-language query into a structured filter (applied client-side) or
    a read-only KQL query (executed here; returns the matching resource ids). Read-only."""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query is required.")
    result = await ai.nl_to_query(
        req.query,
        {"types": req.types, "locations": req.locations, "workloads": req.workloads, "subscriptions": req.subscriptions},
    )
    # For KQL mode, run the validated query and return the matched resource ids so the
    # frontend can filter the already-loaded grid to them.
    if result.get("mode") == "kql" and result.get("kql"):
        conn = _conn(req.connection_id or None)
        ids, err = await service.run_id_query(result["kql"], conn)
        if err:
            return {"mode": "filter", "filter": {"text": req.query}, "explanation": f"Query failed ({err}); using text search.", "error": err}
        result["matched_ids"] = ids
        result["match_count"] = len(ids)
    return result


class ExplainReq(BaseModel):
    resource: dict[str, Any]


@router.post("/explain")
async def post_explain(req: ExplainReq, _: Principal = Depends(require_admin)):
    """A short, plain-language explanation of a single resource (AI). Read-only. Degrades to
    a friendly message (not an error) when the AI provider is unavailable."""
    if not req.resource:
        raise HTTPException(status_code=400, detail="resource is required.")
    try:
        text = await ai.explain_resource(req.resource)
    except Exception:  # noqa: BLE001 — never surface a 5xx for an optional AI nicety
        logger.exception("inventory explain failed")
        text = ""
    if not text:
        text = "The AI provider is currently unavailable, so an explanation couldn't be generated. Check the configured AI provider in Settings and try again."
    return {"explanation": text}


# ============================================================ AI estate insights (Theme 5)
@router.get("/insights")
async def get_insights(connection_id: str | None = None, principal: Principal = Depends(require_admin)):
    """AI-generated, actionable insights over the whole inventory roll-up (concentration,
    tag governance, cleanup, unassigned). Degrades to deterministic insights if AI is down."""
    hit = cache.get(principal.tenant_id, connection_id or "")
    if hit:
        payload = hit["payload"]
    else:
        payload = await service.collect(_conn(connection_id))
        cache.set_(principal.tenant_id, connection_id or "", payload)
    return await ai.estate_insights(payload.get("summary", {}), payload.get("facets", {}))


# ============================================================ snapshots + drift (Theme 3)
@router.get("/snapshots")
async def get_snapshots(connection_id: str | None = None, principal: Principal = Depends(require_admin)):
    return {"snapshots": snapshots.list_snapshots(principal.tenant_id, connection_id)}


@router.post("/snapshots")
async def post_snapshot(connection_id: str | None = None, principal: Principal = Depends(require_admin)):
    """Capture a point-in-time snapshot of the live inventory and return it plus the drift
    since the previous snapshot. Read-only — records a fingerprint, applies nothing."""
    payload = await service.collect(_conn(connection_id))
    cache.set_(principal.tenant_id, connection_id or "", payload)
    prev = snapshots.latest_snapshot(principal.tenant_id, connection_id or "")
    snap = snapshots.save_snapshot(principal.tenant_id, connection_id or "", payload, _actor(principal))
    drift = snapshots.compute_drift(prev, payload.get("resources", [])) if prev else None
    return {"snapshot": snap, "drift_since_previous": drift}


@router.get("/drift")
async def get_drift(connection_id: str | None = None, baseline_id: str | None = None, principal: Principal = Depends(require_admin)):
    """Drift of the current live inventory against a baseline snapshot (or the latest one)."""
    baseline = (
        snapshots.get_snapshot(principal.tenant_id, baseline_id) if baseline_id
        else snapshots.latest_snapshot(principal.tenant_id, connection_id or "")
    )
    if not baseline:
        return {"drift": None, "reason": "No snapshot yet. Take a snapshot to start tracking drift."}
    hit = cache.get(principal.tenant_id, connection_id or "")
    payload = hit["payload"] if hit else await service.collect(_conn(connection_id))
    if not hit:
        cache.set_(principal.tenant_id, connection_id or "", payload)
    return {"drift": snapshots.compute_drift(baseline, payload.get("resources", []))}


@router.delete("/snapshots/{snapshot_id}")
async def delete_snapshot(snapshot_id: str, principal: Principal = Depends(require_admin)):
    if not snapshots.delete_snapshot(principal.tenant_id, snapshot_id):
        raise HTTPException(status_code=404, detail="Snapshot not found.")
    return {"ok": True}


# ============================================================ governance (Theme 2)
class GovernanceReq(BaseModel):
    resource_id: str
    connection_id: str = ""


@router.post("/governance")
async def post_governance(req: GovernanceReq, principal: Principal = Depends(require_admin)):
    """Effective Azure Policy at a resource's scope (assignments inherited from its RG /
    subscription / management groups), plus a compliance hint. Read-only."""
    if not req.resource_id:
        raise HTTPException(status_code=400, detail="resource_id is required.")
    inv = await _policy_inventory(principal.tenant_id, req.connection_id)
    eff = policy_collector.resolve_effective(req.resource_id, inv.get("assignments", []), inv.get("exemptions", []))
    return {"effective": eff}


# ============================================================ assessment findings (Theme 2)
class FindingsReq(BaseModel):
    resource_id: str


@router.post("/findings")
async def post_findings(req: FindingsReq, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Open Well-Architected assessment findings whose flagged resources include this
    resource id, drawn from the latest assessment run per workload. Read-only."""
    rid = (req.resource_id or "").lower()
    if not rid:
        raise HTTPException(status_code=400, detail="resource_id is required.")
    rows = (
        await db.execute(
            select(AssessmentRun)
            .where(AssessmentRun.tenant_id == principal.tenant_id, AssessmentRun.deleted_at.is_(None))
            .order_by(desc(AssessmentRun.started_at))
            .limit(200)
        )
    ).scalars().all()

    # Keep only the most recent run per workload.
    latest: dict[str, AssessmentRun] = {}
    for r in rows:
        if r.workload_id not in latest:
            latest[r.workload_id] = r

    findings: list[dict[str, Any]] = []
    for run in latest.values():
        for f in (run.findings_json or []):
            flagged = f.get("flagged_resources") or []
            if any((fr.get("id") or "").lower() == rid for fr in flagged):
                findings.append({
                    "run_id": run.id,
                    "workload_id": run.workload_id,
                    "workload_name": run.workload_name,
                    "check_id": f.get("check_id"),
                    "title": f.get("title"),
                    "pillar": f.get("pillar"),
                    "severity": f.get("severity"),
                    "status": f.get("status"),
                    "ai_rationale": f.get("ai_rationale", ""),
                    "remediation": f.get("remediation", ""),
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                })
    sev_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    findings.sort(key=lambda x: sev_order.get(x.get("severity", "info"), 3))
    return {"findings": findings, "count": len(findings)}


# ============================================================ cost / FinOps (Theme 4)
@router.get("/cost")
async def get_cost(connection_id: str | None = None, force: int = 0, cached_only: int = 0, principal: Principal = Depends(require_admin)):
    """Best-effort trailing-30-days Azure cost per resource (Cost Management). Returns an empty,
    'unavailable' result (not an error) when Cost Management isn't accessible. ``cached_only=1``
    returns the permanently-cached cost without ever running the slow query (auto-restore on a
    fresh page load); a ``not_loaded`` marker is returned when nothing is cached yet. Read-only."""
    tid = principal.tenant_id
    cid = connection_id or ""
    if cached_only and not force:
        hit = cost.peek_cost(tid, cid)
        if hit is None:
            return {"available": False, "not_loaded": True, "currency": "USD", "period": "",
                    "fetched_at": "", "cached": False, "by_resource": {}, "by_subscription": {},
                    "total": 0, "errors": []}
        return hit
    conn = _conn(connection_id)
    subs = await policy_collector.discover_subscriptions(conn)
    return await cost.get_cost(conn, subs, tid, cid, force=bool(force))


@router.get("/cost-rollup")
async def get_cost_rollup(connection_id: str | None = None, force: int = 0, cached_only: int = 0, principal: Principal = Depends(require_admin)):
    """Cost rolled up by workload / resource type / region / subscription / resource group,
    plus the most expensive resources — joining the PERMANENT cost cache onto the inventory.

    ``force=1`` re-runs the (slow) Azure Cost Management query and refreshes the permanent
    cost cache; otherwise the cached cost is reused indefinitely. ``cached_only=1`` returns the
    cached rollup if one exists and NEVER runs the slow query — used to auto-restore cost on a
    fresh page load. The inventory itself uses its own 5-minute cache. Read-only."""
    tid = principal.tenant_id
    cid = connection_id or ""
    conn = _conn(connection_id)

    # Cost. cached_only takes precedence (even if force is also passed): peek the permanent
    # cache without ever querying Azure; if there's no cached cost yet, return a "not loaded"
    # marker so the UI shows its load prompt.
    if cached_only:
        cost_payload = cost.peek_cost(tid, cid)
        if cost_payload is None:
            return {"available": False, "not_loaded": True, "currency": "USD", "period": "",
                    "fetched_at": "", "cached": False, "total": 0, "by_workload": [], "by_type": [],
                    "by_location": [], "by_subscription": [], "by_resource_group": [], "top_resources": [],
                    "unassigned_cost": 0, "attributed_total": 0, "unattributed_total": 0, "errors": []}
    else:
        subs = await policy_collector.discover_subscriptions(conn)
        cost_payload = await cost.get_cost(conn, subs, tid, cid, force=bool(force))

    # Inventory (5-min cache; collect on miss).
    hit = cache.get(tid, cid)
    if hit:
        inv = hit["payload"]
    else:
        inv = await service.collect(conn)
        cache.set_(tid, cid, inv)

    rollup = cost.build_rollup(cost_payload, inv.get("resources", []))

    # Decorate subscription buckets with friendly names from the inventory facets.
    sub_names = {f.get("key"): f.get("name", f.get("key")) for f in inv.get("facets", {}).get("subscriptions", [])}
    for row in rollup.get("by_subscription", []):
        row["name"] = sub_names.get(row["key"], row["key"])
    return rollup


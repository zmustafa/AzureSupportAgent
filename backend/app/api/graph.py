"""Central knowledge-graph API for the ``/graph`` visualization surface.

Read-only, **admin-gated**, and **cache-only** on load: the overview / expand / build /
search / node endpoints read the file-backed registries (workloads, architectures,
architecture memory), the server-side inventory cache, and the assessment-run history —
never a live Azure scan. Expensive estate refresh stays behind the existing Inventory and
Assessment refresh buttons. Aggregating existing knowledge (not re-querying Azure) is what
keeps the graph instant.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.azure_connections import (
    get_default_connection,
    public_connection,
    resolve_connection,
)
from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.graph import analytics as AN
from app.graph import assembler as A
from app.graph import drift as DR
from app.graph import narrative as NAR
from app.graph import overlays as OV
from app.graph import views as VIEWS
from app.inventory import cache as inv_cache
from app.models import AssessmentRun, AuditLog

router = APIRouter(prefix="/graph", tags=["graph"])
log = logging.getLogger("app.api.graph")


# --------------------------------------------------------------------- data loading
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve(connection_id: str | None) -> tuple[dict[str, Any] | None, str]:
    """Pick the connection (explicit id → default) and its id for cache lookups."""
    conn = resolve_connection(connection_id) or get_default_connection()
    cid = (conn or {}).get("id") or (connection_id or "")
    return conn, cid


def _load_inventory(tenant_id: str, connection_id_param: str | None, resolved_cid: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Read inventory resources from the server cache without ever scanning Azure.

    Inventory is keyed ``(principal.tenant_id | connection_id)``. We try the resolved
    connection id and the raw query param ONLY — never the legacy empty-connection key,
    which could surface a DIFFERENT connection's resources (a cross-tenant leak). If the
    selected connection has no cached inventory, resources come back empty and the graph
    reports 'inventory not scanned'."""
    for cid in _dedupe_keep_order([resolved_cid, connection_id_param or ""]):
        if not cid:
            continue
        hit = inv_cache.get(tenant_id, cid)
        if hit and hit.get("payload"):
            payload = hit["payload"]
            return (payload.get("resources", []) or [], payload)
    return ([], None)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def _subscriptions(payload: dict[str, Any] | None, workloads: list[dict[str, Any]], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``[{id, name, resource_count}]`` from the inventory facets, falling back to the
    subscriptions the workloads span (and the connection's default subscription)."""
    if payload:
        facets = (payload.get("facets") or {}).get("subscriptions") or []
        if facets:
            return [
                {"id": s.get("key", ""), "name": s.get("name", "") or s.get("key", ""), "resource_count": int(s.get("count", 0) or 0)}
                for s in facets
                if s.get("key")
            ]
    seen: dict[str, str] = {}
    for wl in workloads:
        for sid in A._workload_subscription_ids(wl):
            seen.setdefault(sid, sid)
    default_sub = (connection or {}).get("default_subscription") or ""
    if default_sub:
        seen.setdefault(default_sub.lower(), default_sub)
    return [{"id": sid, "name": name, "resource_count": 0} for sid, name in seen.items()]


async def _risk_by_workload(db: AsyncSession, tenant_id: str) -> dict[str, dict[str, Any]]:
    """Latest succeeded assessment run per workload → a compact risk rollup. Best-effort."""
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = (
            await db.execute(
                select(AssessmentRun)
                .where(AssessmentRun.tenant_id == tenant_id, AssessmentRun.status == "succeeded")
                .order_by(desc(AssessmentRun.started_at))
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        log.warning("risk rollup query failed", exc_info=True)
        return out
    for run in rows:
        if run.workload_id in out:
            continue
        totals = run.totals_json or {}
        out[run.workload_id] = {
            "run_id": run.id,
            "score": run.overall_score,
            "failed": int(totals.get("failed", 0) or 0),
            "passed": int(totals.get("passed", 0) or 0),
            "severity": run.severity or "",
            "completed_at": run.started_at.isoformat() if run.started_at else "",
        }
    return out


async def _latest_run(db: AsyncSession, tenant_id: str, workload_id: str) -> AssessmentRun | None:
    try:
        return (
            await db.execute(
                select(AssessmentRun)
                .where(
                    AssessmentRun.tenant_id == tenant_id,
                    AssessmentRun.workload_id == workload_id,
                    AssessmentRun.status == "succeeded",
                )
                .order_by(desc(AssessmentRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        log.warning("latest run query failed", exc_info=True)
        return None


def _connection_public(conn: dict[str, Any] | None, cid: str) -> dict[str, Any]:
    if conn:
        return public_connection(conn)
    return {"id": cid or "default", "display_name": "No Azure connection", "tenant_id": "", "status": "unconfigured", "is_default": False}


def _scoped_workloads(cid: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    """Workloads belonging to the SELECTED connection.

    A named connection NEVER shows another connection's workloads — this is the
    cross-tenant leak fix (selecting tenant A previously showed tenant B's workloads,
    because the registry list is global). Connection-less workloads (demo / unassigned,
    e.g. the seeded Contoso/Zava demos) carry no estate identity, so they're shown under
    every connection rather than vanishing entirely. When no connection resolves (cid
    empty) everything is returned (single-tenant / unconfigured fallback)."""
    from app.workloads.registry import list_workloads

    wls = list_workloads(include_deleted=include_deleted)
    if not cid:
        return wls
    return [w for w in wls if (w.get("connection_id") or "") in ("", cid)]


def _scoped_architectures(tenant_id: str, cid: str) -> list[dict[str, Any]]:
    """Architectures for the principal tenant, further scoped to the SELECTED connection.

    Same rationale as ``_scoped_workloads``: ``list_architectures(tenant_id)`` only filters
    by the *principal* tenant (e.g. ``default``), so without this every connection showed the
    same architecture set. Connection-less architectures are shown everywhere."""
    from app.architectures.registry import list_architectures

    archs = list_architectures(tenant_id)
    if not cid:
        return archs
    return [a for a in archs if (a.get("connection_id") or "") in ("", cid)]


def _workload_in_scope(workload: dict[str, Any] | None, cid: str) -> bool:
    """True if a single workload belongs to the selected connection (or is connection-less).
    Guards the single-workload build/node/drift paths against cross-connection access."""
    if workload is None:
        return False
    if not cid:
        return True
    return (workload.get("connection_id") or "") in ("", cid)


# --------------------------------------------------------------------- overview
@router.get("/overview")
async def overview(
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The landing graph: connection → subscriptions → workloads → architectures."""
    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(connection_id)
    conn_pub = _connection_public(conn, cid)

    workloads = _scoped_workloads(cid)
    architectures = _scoped_architectures(tenant_id, cid)
    resources, payload = _load_inventory(tenant_id, connection_id, cid)
    subscriptions = _subscriptions(payload, workloads, conn)
    risk = await _risk_by_workload(db, tenant_id)

    graph = A.build_overview(
        connection=conn_pub,
        subscriptions=subscriptions,
        workloads=workloads,
        architectures=architectures,
        risk_by_workload=risk,
    )
    graph.update(
        {
            "connection": conn_pub,
            "inventory_loaded": payload is not None,
            "generated_at": _now(),
            "counts": {
                "subscriptions": len(subscriptions),
                "workloads": len(workloads),
                "architectures": len(architectures),
                "resources": len(resources),
            },
        }
    )
    return graph


# --------------------------------------------------------------------- expand
class ExpandRequest(BaseModel):
    node_id: str
    connection_id: str | None = None


@router.post("/expand")
async def expand(
    body: ExpandRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Lazily expand one node's children/neighbours (one hop)."""
    from app.workloads.registry import get_workload

    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(body.connection_id)
    resources, _payload = _load_inventory(tenant_id, body.connection_id, cid)
    prefix, value = A.decode_node_id(body.node_id)

    if prefix == "conn":
        # Same shape as the overview (re-list subs + workloads + archs).
        workloads = _scoped_workloads(cid)
        architectures = _scoped_architectures(tenant_id, cid)
        subscriptions = _subscriptions(_payload, workloads, conn)
        risk = await _risk_by_workload(db, tenant_id)
        return A.build_overview(
            connection=_connection_public(conn, cid),
            subscriptions=subscriptions,
            workloads=workloads,
            architectures=architectures,
            risk_by_workload=risk,
        )

    if prefix == "sub":
        workloads = _scoped_workloads(cid)
        return A.expand_subscription(
            subscription_id=value, name=value, resources=resources, workloads=workloads
        )

    if prefix == "rg":
        sub, _, rg = value.partition("|")
        return A.expand_resource_group(subscription_id=sub, resource_group=rg, resources=resources)

    if prefix == "wl":
        workload = get_workload(value)
        if not _workload_in_scope(workload, cid):
            return {"nodes": [], "edges": [], "stats": {"node_count": 0, "edge_count": 0, "by_kind": {}}}
        architectures = _scoped_architectures(tenant_id, cid)
        memory = _load_memory(value, architectures)
        run = await _latest_run(db, tenant_id, value)
        risk = None
        findings: list[dict[str, Any]] = []
        if run:
            totals = run.totals_json or {}
            risk = {"run_id": run.id, "severity": run.severity or "", "score": run.overall_score, "failed": int(totals.get("failed", 0) or 0)}
            findings = run.findings_json or []
        return A.expand_workload(
            workload=workload,
            resources=resources,
            architectures=architectures,
            memory=memory,
            risk=risk,
            findings=findings,
        )

    return {"nodes": [], "edges": [], "stats": {"node_count": 0, "edge_count": 0, "by_kind": {}}}


def _load_memory(workload_id: str, architectures: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best-effort architecture-memory summary for a workload's architecture."""
    try:
        from app.architectures.memory import get_memory
    except Exception:  # noqa: BLE001
        return None
    for arch in architectures:
        if arch.get("workload_id") != workload_id:
            continue
        try:
            mem = get_memory(arch.get("id", ""))
        except Exception:  # noqa: BLE001
            mem = None
        if mem:
            sections = mem.get("sections") or []
            return {
                "architecture_id": arch.get("id", ""),
                "sections": len(sections) if isinstance(sections, list) else 0,
                "confidence": (mem.get("ai") or {}).get("confidence"),
            }
    return None


# --------------------------------------------------------------------- build (scoped)
class BuildRequest(BaseModel):
    scope_kind: str = Field(default="workload")  # workload | subscription
    scope_id: str
    connection_id: str | None = None
    overlays: list[str] = Field(default_factory=list)  # cost|retirement|coverage|rbac|change
    drift: bool = False


@router.post("/build")
async def build(
    body: BuildRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Assemble a focused subgraph for a workload or subscription, optionally enriched with
    overlays (cost/retirement/coverage/rbac/change) and intent-vs-reality drift tagging."""
    from app.workloads.registry import get_workload

    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(body.connection_id)
    resources, _payload = _load_inventory(tenant_id, body.connection_id, cid)

    if body.scope_kind == "subscription":
        workloads = _scoped_workloads(cid)
        sub_graph = A.expand_subscription(
            subscription_id=body.scope_id, name=body.scope_id, resources=resources, workloads=workloads
        )
        # Seed with the subscription node itself so the focus is rooted.
        snode = A.subscription_node(body.scope_id, body.scope_id, connection_id=cid)
        sub_graph["nodes"] = [snode, *sub_graph["nodes"]]
        sub_graph["stats"] = A._stats(sub_graph["nodes"], sub_graph["edges"])
        _apply_build_overlays(sub_graph, body.overlays, tenant_id=tenant_id, connection_id=cid, workloads=workloads, subscriptions=[{"id": body.scope_id}], resources=resources)
        sub_graph["generated_at"] = _now()
        return sub_graph

    workload = get_workload(body.scope_id)
    if not _workload_in_scope(workload, cid):
        return {"nodes": [], "edges": [], "stats": {"node_count": 0, "edge_count": 0, "by_kind": {}}, "error": "Workload not found."}
    architectures = _scoped_architectures(tenant_id, cid)
    memory = _load_memory(body.scope_id, architectures)
    run = await _latest_run(db, tenant_id, body.scope_id)
    risk = None
    findings: list[dict[str, Any]] = []
    if run:
        totals = run.totals_json or {}
        risk = {"run_id": run.id, "severity": run.severity or "", "score": run.overall_score, "failed": int(totals.get("failed", 0) or 0), "passed": int(totals.get("passed", 0) or 0)}
        findings = run.findings_json or []
    graph = A.expand_workload(
        workload=workload,
        resources=resources,
        architectures=architectures,
        memory=memory,
        risk=risk,
        findings=findings,
    )
    wnode = A.workload_node(workload, risk=risk)
    graph["nodes"] = [wnode, *graph["nodes"]]
    graph["stats"] = A._stats(graph["nodes"], graph["edges"])

    # Intent-vs-reality drift tagging.
    if body.drift:
        member_resources = [r for r in resources if any((w or {}).get("id") == body.scope_id for w in (r.get("workloads") or []))]
        arch = next((a for a in architectures if a.get("workload_id") == body.scope_id), None)
        d = DR.compute_drift(architecture=arch, member_resources=member_resources)
        classes = DR.drift_classification(d)
        for n in graph["nodes"]:
            if n["kind"] == A.KIND_RESOURCE:
                arm = (n["data"].get("arm_id", "") or "").lower()
                if arm in classes:
                    n["data"]["drift"] = classes[arm]
        graph["drift"] = d

    _apply_build_overlays(graph, body.overlays, tenant_id=tenant_id, connection_id=cid, workloads=[workload], subscriptions=[], resources=resources)
    graph["generated_at"] = _now()
    return graph


def _apply_build_overlays(
    graph: dict[str, Any],
    overlay_names: list[str],
    *,
    tenant_id: str,
    connection_id: str,
    workloads: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    resources: list[dict[str, Any]],
) -> None:
    """Load + merge the requested overlays into ``graph`` (best-effort)."""
    if not overlay_names:
        return
    loaded: list[dict[str, Any]] = []
    if "cost" in overlay_names:
        loaded.append(OV.cost_overlay(tenant_id=tenant_id, connection_id=connection_id, subscriptions=subscriptions))
    if "retirement" in overlay_names:
        loaded.append(OV.retirement_overlay(tenant_id=tenant_id, workloads=workloads))
    if "coverage" in overlay_names:
        loaded.append(OV.coverage_overlay(tenant_id=tenant_id, workloads=workloads))
    if "rbac" in overlay_names:
        loaded.append(OV.rbac_overlay(tenant_id=tenant_id, connection_id=connection_id))
    if "change" in overlay_names:
        loaded.append(OV.change_overlay(resources=resources, changes=_load_changes(tenant_id, connection_id)))
    if loaded:
        OV.apply_overlays(graph, loaded)


def _load_changes(tenant_id: str, connection_id: str) -> list[dict[str, Any]]:
    """Best-effort recent change rows from the Change Explorer run history (cache-only)."""
    try:
        from app.changeexplorer import store as ce_store
    except Exception:  # noqa: BLE001
        return []
    for fn_name in ("latest_changes", "recent_changes", "list_changes"):
        fn = getattr(ce_store, fn_name, None)
        if callable(fn):
            try:
                rows = fn(tenant_id) if fn.__code__.co_argcount >= 1 else fn()
                if isinstance(rows, list):
                    return rows
            except Exception:  # noqa: BLE001
                continue
    return []


# --------------------------------------------------------------------- multi-workload focus
class WorkloadsRequest(BaseModel):
    workload_ids: list[str] = Field(default_factory=list)
    connection_id: str | None = None
    overlays: list[str] = Field(default_factory=list)  # cost|retirement|coverage|rbac|change
    drift: bool = False


@router.post("/workloads")
async def build_workloads(
    body: WorkloadsRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Focus on ONE OR MORE workloads at once: merges each workload's subgraph (member
    resources, architecture, memory, top findings, reverse-engineered dependency edges) into
    a single canvas, optionally with overlays + intent-vs-reality drift tagging.

    Cross-connection ids are silently dropped (only in-scope workloads are built), so a stray
    id from another tenant can never leak in."""
    from app.workloads.registry import get_workload

    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(body.connection_id)
    resources, _payload = _load_inventory(tenant_id, body.connection_id, cid)
    architectures = _scoped_architectures(tenant_id, cid)

    # In-scope, de-duplicated workloads in the requested order.
    seen: set[str] = set()
    wls: list[dict[str, Any]] = []
    for wid in body.workload_ids:
        if wid in seen:
            continue
        seen.add(wid)
        wl = get_workload(wid)
        if _workload_in_scope(wl, cid):
            wls.append(wl)

    if not wls:
        return {"nodes": [], "edges": [], "stats": {"node_count": 0, "edge_count": 0, "by_kind": {}}, "workload_ids": [], "error": "No matching workloads in this connection."}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    drift_by_workload: dict[str, Any] = {}
    drift_classes: dict[str, str] = {}

    for wl in wls:
        wid = wl.get("id", "")
        memory = _load_memory(wid, architectures)
        run = await _latest_run(db, tenant_id, wid)
        risk = None
        findings: list[dict[str, Any]] = []
        if run:
            totals = run.totals_json or {}
            risk = {"run_id": run.id, "severity": run.severity or "", "score": run.overall_score, "failed": int(totals.get("failed", 0) or 0), "passed": int(totals.get("passed", 0) or 0)}
            findings = run.findings_json or []
        sub = A.expand_workload(
            workload=wl, resources=resources, architectures=architectures, memory=memory, risk=risk, findings=findings
        )
        nodes.append(A.workload_node(wl, risk=risk))
        nodes.extend(sub["nodes"])
        edges.extend(sub["edges"])
        if body.drift:
            members = [r for r in resources if any((w or {}).get("id") == wid for w in (r.get("workloads") or []))]
            arch = next((a for a in architectures if a.get("workload_id") == wid), None)
            d = DR.compute_drift(architecture=arch, member_resources=members)
            drift_by_workload[wid] = d
            drift_classes.update(DR.drift_classification(d))

    # Dedupe (resources shared by 2+ focused workloads collapse to one node, keeping both
    # belongs_to edges — that's exactly how shared services surface).
    nodes, edges = A._dedupe(nodes, edges)

    if body.drift:
        for n in nodes:
            if n["kind"] == A.KIND_RESOURCE:
                arm = (n["data"].get("arm_id", "") or "").lower()
                if arm in drift_classes:
                    n["data"]["drift"] = drift_classes[arm]

    graph: dict[str, Any] = {"nodes": nodes, "edges": edges, "stats": A._stats(nodes, edges)}
    _apply_build_overlays(graph, body.overlays, tenant_id=tenant_id, connection_id=cid, workloads=wls, subscriptions=[], resources=resources)
    graph["workload_ids"] = [w.get("id", "") for w in wls]
    graph["workload_count"] = len(wls)
    if drift_by_workload:
        graph["drift_by_workload"] = drift_by_workload
    graph["generated_at"] = _now()
    return graph


# --------------------------------------------------------------------- search
@router.get("/search")
async def search(
    q: str = Query(default=""),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(connection_id)
    workloads = _scoped_workloads(cid)
    architectures = _scoped_architectures(tenant_id, cid)
    resources, payload = _load_inventory(tenant_id, connection_id, cid)
    subscriptions = _subscriptions(payload, workloads, conn)
    risk = await _risk_by_workload(db, tenant_id)
    nodes = A.search(
        query=q,
        subscriptions=subscriptions,
        workloads=workloads,
        architectures=architectures,
        resources=resources,
        risk_by_workload=risk,
    )
    return {"nodes": nodes, "query": q, "count": len(nodes)}


# --------------------------------------------------------------------- node dossier
@router.get("/node")
async def node_detail(
    node_id: str = Query(...),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Full dossier for the selected node (workload/resource/architecture/sub/finding)."""
    from app.architectures.registry import get_architecture
    from app.workloads.registry import get_workload

    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(connection_id)
    prefix, value = A.decode_node_id(node_id)
    resources, payload = _load_inventory(tenant_id, connection_id, cid)

    if prefix == "wl":
        workload = get_workload(value)
        if not _workload_in_scope(workload, cid):
            return {"found": False, "detail": "Workload not found."}
        architectures = [a for a in _scoped_architectures(tenant_id, cid) if a.get("workload_id") == value]
        run = await _latest_run(db, tenant_id, value)
        members = [r for r in resources if any((w or {}).get("id") == value for w in (r.get("workloads") or []))]
        risk = None
        if run:
            totals = run.totals_json or {}
            risk = {
                "run_id": run.id,
                "score": run.overall_score,
                "failed": int(totals.get("failed", 0) or 0),
                "passed": int(totals.get("passed", 0) or 0),
                "na": int(totals.get("na", 0) or 0),
                "severity": run.severity or "",
                "completed_at": run.started_at.isoformat() if run.started_at else "",
                "summary": run.summary or "",
            }
        node = A.workload_node(workload, risk=risk)
        return {
            "found": True,
            "node": node,
            "dossier": {
                "kind": "workload",
                "description": workload.get("description", ""),
                "reasoning": workload.get("reasoning", ""),
                "member_resources": len(members),
                "resource_types": (workload.get("summary") or {}).get("types", []),
                "architectures": [{"id": a.get("id"), "name": a.get("name"), "state": a.get("state")} for a in architectures],
                "risk": risk,
                "links": {
                    "workload": f"/workloads/{value}",
                    "architecture": f"/architectures/{architectures[0]['id']}" if architectures else "",
                    "assessment": f"/assessments/{risk['run_id']}" if risk and risk.get("run_id") else "/assessments",
                    "inventory": "/inventory",
                },
            },
        }

    if prefix == "res":
        match = next((r for r in resources if r.get("id") == value), None)
        if not match:
            return {"found": True, "node": A.resource_node({"id": value, "name": value, "type": ""}), "dossier": {"kind": "resource", "note": "Resource not in the loaded inventory cache. Refresh Inventory to load it."}}
        return {
            "found": True,
            "node": A.resource_node(match),
            "dossier": {
                "kind": "resource",
                "arm_id": match.get("id", ""),
                "type": match.get("type", ""),
                "location": match.get("location", ""),
                "resource_group": match.get("resource_group", ""),
                "subscription_id": match.get("subscription_id", ""),
                "sku": match.get("sku", ""),
                "tier": match.get("tier", ""),
                "tags": match.get("tags", {}),
                "flags": match.get("flags", []),
                "workloads": match.get("workloads", []),
                "links": {"inventory": "/inventory"},
            },
        }

    if prefix == "arch":
        arch = get_architecture(value)
        if not arch:
            return {"found": False, "detail": "Architecture not found."}
        return {
            "found": True,
            "node": A.architecture_node(arch),
            "dossier": {
                "kind": "architecture",
                "description": arch.get("description", ""),
                "workload_id": arch.get("workload_id", ""),
                "workload_name": arch.get("workload_name", ""),
                "state": arch.get("state", ""),
                "source": arch.get("source", ""),
                "node_count": len(arch.get("nodes") or []),
                "edge_count": len(arch.get("edges") or []),
                "ai": arch.get("ai", {}),
                "links": {"architecture": f"/architectures/{value}", "memory": f"/architectures/{value}/memory"},
            },
        }

    if prefix == "sub":
        sid = value.lower()
        in_sub = [r for r in resources if (r.get("subscription_id", "") or "").lower() == sid]
        rgs = sorted({(r.get("resource_group", "") or "") for r in in_sub})
        name = value
        if payload:
            for s in (payload.get("facets") or {}).get("subscriptions") or []:
                if (s.get("key", "") or "").lower() == sid:
                    name = s.get("name", "") or value
                    break
        return {
            "found": True,
            "node": A.subscription_node(value, name, connection_id=cid, resource_count=len(in_sub)),
            "dossier": {
                "kind": "subscription",
                "subscription_id": value,
                "resource_count": len(in_sub),
                "resource_group_count": len([r for r in rgs if r]),
                "links": {"inventory": "/inventory"},
            },
        }

    if prefix == "conn":
        return {
            "found": True,
            "node": A.connection_node(_connection_public(conn, cid)),
            "dossier": {"kind": "tenant_connection", "connection": _connection_public(conn, cid), "links": {"tenants": "/admin/tenants"}},
        }

    if prefix == "finding":
        run_id, _, check = value.partition("|")
        run = None
        try:
            run = (await db.execute(select(AssessmentRun).where(AssessmentRun.id == run_id))).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            run = None
        finding = None
        if run:
            finding = next((f for f in (run.findings_json or []) if (f.get("check_id") or f.get("id")) == check), None)
        if not finding:
            return {"found": False, "detail": "Finding not found."}
        return {
            "found": True,
            "node": A.finding_node(run_id, finding),
            "dossier": {
                "kind": "assessment_finding",
                "check_id": check,
                "title": finding.get("title", ""),
                "pillar": finding.get("pillar", ""),
                "severity": finding.get("severity", ""),
                "status": finding.get("status", ""),
                "rationale": finding.get("ai_rationale", "") or finding.get("rationale", ""),
                "remediation": finding.get("remediation", ""),
                "flagged_resources": finding.get("flagged_resources", []),
                "links": {"assessment": f"/assessments/{run_id}"},
            },
        }

    return {"found": False, "detail": "Unknown node type."}


# ===================================================================== full graph helper
async def _full_graph(
    db: AsyncSession,
    tenant_id: str,
    connection_id_param: str | None,
    cid: str,
    conn: dict[str, Any] | None,
    *,
    overlay_names: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A comprehensive, cache-only estate graph: overview + every workload expanded with its
    member resources and reverse-engineered dependency edges (findings omitted to stay lean).
    Returns ``(graph, raw)`` where ``raw`` carries the source lists for analytics."""
    workloads = _scoped_workloads(cid)
    architectures = _scoped_architectures(tenant_id, cid)
    resources, payload = _load_inventory(tenant_id, connection_id_param, cid)
    subscriptions = _subscriptions(payload, workloads, conn)
    risk = await _risk_by_workload(db, tenant_id)

    graph = A.build_overview(
        connection=_connection_public(conn, cid),
        subscriptions=subscriptions,
        workloads=workloads,
        architectures=architectures,
        risk_by_workload=risk,
    )
    seen_n = {n["id"] for n in graph["nodes"]}
    seen_e = {e["id"] for e in graph["edges"]}
    dependency_edges: list[dict[str, Any]] = []
    for wl in workloads:
        sub = A.expand_workload(
            workload=wl, resources=resources, architectures=architectures, memory=None, risk=None, findings=None
        )
        for n in sub["nodes"]:
            if n["id"] not in seen_n:
                graph["nodes"].append(n)
                seen_n.add(n["id"])
        for e in sub["edges"]:
            if e["id"] not in seen_e:
                graph["edges"].append(e)
                seen_e.add(e["id"])
            if e["kind"] in A._DEP_EDGE_KINDS:
                dependency_edges.append(e)
    graph["stats"] = A._stats(graph["nodes"], graph["edges"])

    if overlay_names:
        _apply_build_overlays(
            graph, list(overlay_names), tenant_id=tenant_id, connection_id=cid,
            workloads=workloads, subscriptions=subscriptions, resources=resources,
        )
    raw = {"workloads": workloads, "architectures": architectures, "resources": resources, "dependency_edges": dependency_edges, "risk": risk}
    return graph, raw


# ===================================================================== path / blast-radius
class GraphElements(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class PathRequest(GraphElements):
    source: str
    target: str
    directed: bool = False


@router.post("/path")
async def path(body: PathRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Shortest/dependency path between two nodes on the current canvas."""
    return AN.shortest_path(body.nodes, body.edges, body.source, body.target, directed=body.directed)


class BlastRequest(GraphElements):
    source: str
    max_depth: int = 3
    directed: bool = False


@router.post("/blast-radius")
async def blast_radius(body: BlastRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Direct + indirect impact set reachable from a node on the current canvas."""
    return AN.blast_radius(body.nodes, body.edges, body.source, max_depth=max(1, min(6, body.max_depth)), directed=body.directed)


# ===================================================================== analytics
@router.get("/analytics")
async def analytics(
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Estate-wide analytics: centrality / concentration risk, communities, orphan detection,
    and candidate-workload clustering. Computed over the full cache-only graph."""
    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(connection_id)
    graph, raw = await _full_graph(db, tenant_id, connection_id, cid, conn)
    nodes, edges = graph["nodes"], graph["edges"]
    communities = AN.connected_components(nodes, edges)
    by_id = {n["id"]: n for n in nodes}
    community_summary = [
        {
            "size": len(c),
            "kinds": _kind_counts(c, by_id),
            "sample": [by_id[i].get("label", "") for i in c[:5]],
        }
        for c in communities[:10]
    ]
    orphans = AN.detect_orphans(resources=raw["resources"], workloads=raw["workloads"], architectures=raw["architectures"])
    candidates = AN.candidate_workloads(resources=raw["resources"], dependency_edges=raw["dependency_edges"])
    concentration = AN.concentration_risk(nodes, edges, top=12)
    return {
        "stats": graph["stats"],
        "concentration_risk": concentration,
        "communities": community_summary,
        "community_count": len(communities),
        "orphans": orphans,
        "candidate_workloads": candidates,
        "generated_at": _now(),
    }


def _kind_counts(ids: list[str], by_id: dict[str, dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in ids:
        k = by_id.get(i, {}).get("kind", "")
        if k:
            out[k] = out.get(k, 0) + 1
    return out


# ===================================================================== drift (standalone)
@router.get("/drift")
async def drift(
    workload_id: str = Query(...),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Intent-vs-reality drift for a single workload (documented architecture vs live inventory)."""
    from app.workloads.registry import get_workload

    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(connection_id)
    workload = get_workload(workload_id)
    if not _workload_in_scope(workload, cid):
        return {"found": False, "detail": "Workload not found."}
    resources, _payload = _load_inventory(tenant_id, connection_id, cid)
    members = [r for r in resources if any((w or {}).get("id") == workload_id for w in (r.get("workloads") or []))]
    arch = next((a for a in _scoped_architectures(tenant_id, cid) if a.get("workload_id") == workload_id), None)
    d = DR.compute_drift(architecture=arch, member_resources=members)
    return {"found": True, "workload_id": workload_id, "workload_name": workload.get("name", ""), "drift": d}


# ===================================================================== compare
class CompareRequest(BaseModel):
    scope_kind: str = "workload"
    left_id: str
    right_id: str
    connection_id: str | None = None


@router.post("/compare")
async def compare(
    body: CompareRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Compare two scopes (two workloads, or two subscriptions): added / removed / shared nodes."""
    left = await build(BuildRequest(scope_kind=body.scope_kind, scope_id=body.left_id, connection_id=body.connection_id), principal, db)
    right = await build(BuildRequest(scope_kind=body.scope_kind, scope_id=body.right_id, connection_id=body.connection_id), principal, db)
    left_ids = {n["id"] for n in left.get("nodes", [])}
    right_ids = {n["id"] for n in right.get("nodes", [])}
    by_id = {n["id"]: n for n in (left.get("nodes", []) + right.get("nodes", []))}

    def _proj(ids: set[str]) -> list[dict[str, Any]]:
        return [{"id": i, "label": by_id[i].get("label", ""), "kind": by_id[i].get("kind", "")} for i in ids]

    return {
        "left": {"id": body.left_id, "node_count": len(left_ids)},
        "right": {"id": body.right_id, "node_count": len(right_ids)},
        "only_left": _proj(left_ids - right_ids),
        "only_right": _proj(right_ids - left_ids),
        "shared": _proj(left_ids & right_ids),
        "generated_at": _now(),
    }


# ===================================================================== narrative / ask
class NarrativeRequest(BaseModel):
    connection_id: str | None = None
    scope_kind: str = "overview"
    scope_id: str = ""


@router.post("/narrative")
async def narrative(
    body: NarrativeRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """AI (or deterministic) narrative summarising the current scope."""
    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(body.connection_id)
    graph, raw = await _full_graph(db, tenant_id, body.connection_id, cid, conn)
    risk = raw["risk"]
    top_risks = sorted(
        ({"label": w.get("name", ""), "score": risk.get(w.get("id", ""), {}).get("score"), "failed": risk.get(w.get("id", ""), {}).get("failed", 0)} for w in raw["workloads"] if w.get("id") in risk),
        key=lambda r: (r.get("failed") or 0), reverse=True,
    )[:5]
    summary = {
        "counts": {
            "workloads": len(raw["workloads"]),
            "subscriptions": graph["stats"]["by_kind"].get("subscription", 0),
            "resources": len(raw["resources"]),
            "architectures": len(raw["architectures"]),
        },
        "top_risks": top_risks,
        "orphans": AN.detect_orphans(resources=raw["resources"], workloads=raw["workloads"], architectures=raw["architectures"])["unowned_count"],
    }
    if body.scope_kind == "workload" and body.scope_id:
        d = await drift(workload_id=body.scope_id, connection_id=body.connection_id, principal=principal, db=db)
        if d.get("found"):
            summary["drift"] = d.get("drift")
    result = await NAR.narrate_graph(summary)
    return {**result, "summary": summary}


class AskRequest(BaseModel):
    question: str
    connection_id: str | None = None


@router.post("/ask")
async def ask(
    body: AskRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Natural-language query → matched node ids over the full cache-only estate graph,
    enriched with coverage/retirement/change overlays so predicates like 'without backup' work."""
    tenant_id = principal.tenant_id or "default"
    conn, cid = _resolve(body.connection_id)
    graph, _raw = await _full_graph(
        db, tenant_id, body.connection_id, cid, conn, overlay_names=("coverage", "retirement", "change")
    )
    result = await NAR.ask_graph(body.question, graph["nodes"])
    matched_set = set(result["matched"])
    result["nodes"] = [n for n in graph["nodes"] if n["id"] in matched_set]
    return result


# ===================================================================== view preferences
class GraphPrefsRequest(BaseModel):
    tenant_id: str = ""
    layout: str = "organic"


@router.get("/prefs")
async def get_view_prefs(
    tenant_id: str = Query(default=""),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """The remembered graph layout ("view") for an Azure tenant (defaults to Organic)."""
    from app.graph import prefs

    return prefs.get_prefs(tenant_id)


@router.put("/prefs")
async def put_view_prefs(
    body: GraphPrefsRequest,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Remember the chosen graph layout per Azure tenant (server-side)."""
    from app.graph import prefs

    return prefs.set_prefs(body.tenant_id, layout=body.layout)


# ===================================================================== saved views
class ViewSaveRequest(BaseModel):
    id: str | None = None
    name: str
    connection_id: str = ""
    scope_kind: str = "overview"
    scope_id: str = ""
    lens: str = "none"
    layout: str = "breadthfirst"
    hidden_kinds: list[str] = Field(default_factory=list)
    expanded: list[str] = Field(default_factory=list)
    camera: dict[str, Any] = Field(default_factory=dict)
    overlays: list[str] = Field(default_factory=list)


@router.get("/views")
async def list_views(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    return {"views": VIEWS.list_views(principal.tenant_id or "default")}


@router.post("/views")
async def save_view(
    body: ViewSaveRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    view = body.model_dump()
    view["tenant_id"] = principal.tenant_id or "default"
    saved = VIEWS.save_view(view, actor=principal.subject)
    db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="graph.view.save", target=saved["id"], metadata_json={"name": saved.get("name")}))
    await db.commit()
    return {"view": saved}


@router.delete("/views/{view_id}")
async def delete_view(
    view_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    existing = VIEWS.get_view(view_id)
    if not existing or (existing.get("tenant_id") or "") not in ("", principal.tenant_id or "default"):
        return {"ok": False, "detail": "View not found."}
    ok = VIEWS.delete_view(view_id)
    if ok:
        db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="graph.view.delete", target=view_id, metadata_json={}))
        await db.commit()
    return {"ok": ok}


# ===================================================================== SSE build
@router.post("/build/stream")
async def build_stream(
    body: BuildRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """SSE progress wrapper around ``/build`` for large graph assembly."""
    import json as _json

    from sse_starlette.sse import EventSourceResponse

    async def _gen():
        try:
            yield {"event": "start", "data": _json.dumps({"scope_kind": body.scope_kind, "scope_id": body.scope_id})}
            yield {"event": "progress", "data": _json.dumps({"phase": "resolving scope"})}
            yield {"event": "progress", "data": _json.dumps({"phase": "loading inventory"})}
            graph = await build(body, principal, db)
            yield {"event": "progress", "data": _json.dumps({"phase": "assembling graph", "nodes": graph.get("stats", {}).get("node_count", 0)})}
            yield {"event": "done", "data": _json.dumps(graph, default=str)}
        except Exception as exc:  # noqa: BLE001
            yield {"event": "error", "data": _json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


"""Azure Workload endpoints: CRUD over the registry + resource-picker discovery.

Per the product governance, any authenticated user may build and manage workloads.
Discovery (tree/search/facets) is read-only Azure data via ARM + Resource Graph.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.azure_connections import resolve_connection
from app.core.security import Principal, get_principal
from app.workloads import discovery
from app.workloads import registry as wl_registry
from app.workloads.autopilot import discover_workloads
from app.workloads.cache import discovery_cache

router = APIRouter(prefix="/workloads", tags=["workloads"])
logger = logging.getLogger("app.api.workloads")


class WorkloadNode(BaseModel):
    kind: str  # mg | subscription | resource_group | resource
    id: str
    name: str = ""
    subscription_id: str | None = None
    resource_group: str | None = None
    resource_type: str | None = None
    location: str | None = None
    excludes: list[str] = Field(default_factory=list)


class WorkloadUpsert(BaseModel):
    id: str | None = None
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    connection_id: str = ""
    tenant_id: str = ""
    nodes: list[WorkloadNode] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


@router.get("")
async def list_workloads_endpoint(_: Principal = Depends(get_principal)):
    # NOTE: a workload's ``tenant_id`` is the AZURE AD tenant of its connection (an admin-
    # managed boundary), not the app principal's tenant, so we do not filter by the app
    # tenant here — that would hide admin-configured workloads. Workloads are governed by
    # the admin-managed Azure connections they reference.
    return {"workloads": wl_registry.list_workloads()}


@router.put("")
async def upsert_workload_endpoint(
    payload: WorkloadUpsert, principal: Principal = Depends(get_principal)
):
    data = payload.model_dump()
    if not payload.id:
        data["created_by"] = principal.subject
    # Resolve tenant_id from the connection when available (the Azure AD tenant).
    conn = resolve_connection(payload.connection_id or None)
    if conn and not payload.tenant_id:
        data["tenant_id"] = conn.get("tenant_id", "")
    saved = wl_registry.upsert_workload(data)
    return {"workload": saved}


@router.get("/trash")
async def list_trashed_workloads_endpoint(_: Principal = Depends(get_principal)):
    """List workloads currently in the Trash (soft-deleted, restorable)."""
    return {"workloads": wl_registry.list_trashed_workloads()}


@router.post("/trash/empty")
async def empty_workload_trash_endpoint(_: Principal = Depends(get_principal)):
    """Permanently delete every workload in the Trash."""
    deleted = wl_registry.empty_trash()
    return {"ok": True, "deleted": deleted}


@router.delete("/{workload_id}")
async def delete_workload_endpoint(workload_id: str, _: Principal = Depends(get_principal)):
    """Soft-delete a workload: move it to the Trash (restorable until purged)."""
    if not wl_registry.delete_workload(workload_id):
        raise HTTPException(status_code=404, detail="Workload not found.")
    return {"ok": True}


@router.post("/{workload_id}/restore")
async def restore_workload_endpoint(workload_id: str, _: Principal = Depends(get_principal)):
    """Restore a trashed workload back into the active list."""
    wl = wl_registry.restore_workload(workload_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="Workload not in trash.")
    return {"workload": wl}


@router.delete("/{workload_id}/purge")
async def purge_workload_endpoint(workload_id: str, _: Principal = Depends(get_principal)):
    """Permanently delete a single trashed workload."""
    if not wl_registry.purge_workload(workload_id):
        raise HTTPException(status_code=404, detail="Workload not in trash.")
    return {"ok": True}


# ----------------------------------------------------------------- discovery
def _cache_meta(cached_at: float, from_cache: bool) -> dict:
    import time

    return {
        "cached_at": datetime.fromtimestamp(cached_at, tz=timezone.utc).isoformat(),
        "age_seconds": max(0.0, time.time() - cached_at),
        "from_cache": from_cache,
    }


class TreeRequest(BaseModel):
    connection_id: str = ""
    group_by: str = "subscription"  # subscription | mg
    kind: str = ""  # empty = top level; else mg|subscription|resource_group
    node_id: str = ""
    refresh: bool = False  # bypass + overwrite the cache for this node


@router.post("/tree")
async def tree_endpoint(payload: TreeRequest, _: Principal = Depends(get_principal)):
    conn = resolve_connection(payload.connection_id or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Pick an Azure connection first.")

    # Subscription expansion is special: a cold expand fetches the RG list AND all of the
    # subscription's resources in ONE session, then caches each child RG's resources too —
    # so expanding any resource group underneath is an instant cache hit (no live call).
    if payload.kind == "subscription":
        sub_key = discovery_cache.key(payload.connection_id, "tree:subscription", payload.node_id)
        if not payload.refresh:
            entry_nodes, cached_at, from_cache = await discovery_cache.get_or_compute(
                sub_key,
                lambda: discovery.expand_node(conn, "subscription", payload.node_id),
                force=False,
            )
            if from_cache:
                return {"nodes": entry_nodes, **_cache_meta(cached_at, from_cache)}
        # Cold (or forced): compute RG list + all resources together, then cache both.
        rg_nodes = await _warm_subscription(payload.connection_id, conn, payload.node_id)
        cached_at = discovery_cache.put(sub_key, rg_nodes)
        return {"nodes": rg_nodes, **_cache_meta(cached_at, False)}

    if not payload.kind:
        namespace = f"tree:top:{payload.group_by}"
        subkey = ""

        async def _compute():
            return await discovery.list_top_level(conn, payload.group_by)
    else:
        namespace = f"tree:{payload.kind}"
        subkey = payload.node_id

        async def _compute():
            return await discovery.expand_node(conn, payload.kind, payload.node_id)

    key = discovery_cache.key(payload.connection_id, namespace, subkey)
    nodes, cached_at, from_cache = await discovery_cache.get_or_compute(
        key, _compute, force=payload.refresh
    )
    return {"nodes": nodes, **_cache_meta(cached_at, from_cache)}


async def _warm_subscription(
    connection_id: str, conn: dict, subscription_id: str
) -> list[dict]:
    """Under ONE service-principal login, fetch the subscription's RG list + all its
    resources concurrently, populate each RG's resource cache, and return the RG list."""
    import asyncio

    from app.exec.command_runner import close_sp_session, open_sp_session

    def _lc(s: str) -> str:
        return (s or "").lower()

    session_dir: str | None = None
    try:
        session_dir, sess_err = await open_sp_session(conn)
        if sess_err:
            # Login failed; fall back to a plain (own-login) RG-list expand.
            logger.warning("Subscription warm login failed: %s", sess_err)
            return await discovery.expand_node(conn, "subscription", subscription_id)
        rg_nodes, all_res = await asyncio.gather(
            discovery.expand_node(conn, "subscription", subscription_id, session_config_dir=session_dir),
            discovery.resources_in_subscriptions(conn, [subscription_id], cap=1000, session_config_dir=session_dir),
        )
        by_rg: dict[str, list] = {}
        for r in all_res:
            by_rg.setdefault(_lc(r.get("resource_group", "")), []).append(r)
        for rg in rg_nodes:
            rid = rg.get("id", "")
            if rid:
                discovery_cache.put(
                    discovery_cache.key(connection_id, "tree:resource_group", rid),
                    by_rg.get(_lc(rg.get("name", "")), []),
                )
        return rg_nodes
    except Exception as exc:  # noqa: BLE001
        logger.warning("Subscription warm failed for %s: %s", subscription_id, exc)
        return await discovery.expand_node(conn, "subscription", subscription_id)
    finally:
        close_sp_session(session_dir)


class SearchRequest(BaseModel):
    connection_id: str = ""
    query: str = ""
    subscription_id: str = ""
    types: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    skip: int = 0
    top: int = 200


@router.post("/search")
async def search_endpoint(payload: SearchRequest, _: Principal = Depends(get_principal)):
    conn = resolve_connection(payload.connection_id or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Pick an Azure connection first.")
    result = await discovery.search_resources(
        conn,
        query=payload.query,
        subscription_id=payload.subscription_id,
        types=payload.types,
        locations=payload.locations,
        skip=payload.skip,
        top=payload.top,
    )
    return result


class FacetsRequest(BaseModel):
    connection_id: str = ""
    subscription_id: str = ""
    refresh: bool = False


@router.post("/facets")
async def facets_endpoint(payload: FacetsRequest, _: Principal = Depends(get_principal)):
    conn = resolve_connection(payload.connection_id or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Pick an Azure connection first.")

    async def _compute():
        return await discovery.facets(conn, payload.subscription_id)

    key = discovery_cache.key(payload.connection_id, "facets", payload.subscription_id)
    result, cached_at, from_cache = await discovery_cache.get_or_compute(
        key, _compute, force=payload.refresh
    )
    return {**result, **_cache_meta(cached_at, from_cache)}


class CacheInvalidateRequest(BaseModel):
    connection_id: str = ""


@router.post("/cache/invalidate")
async def cache_invalidate_endpoint(
    payload: CacheInvalidateRequest, _: Principal = Depends(get_principal)
):
    """Drop all cached discovery data for a connection (the picker's Refresh button)."""
    removed = discovery_cache.invalidate_connection(payload.connection_id)
    return {"ok": True, "removed": removed}


class PrefetchRequest(BaseModel):
    connection_id: str = ""
    group_by: str = "subscription"
    refresh: bool = False  # when True, bypass + overwrite existing cache entries


# Safety cap so a huge tenant can't make prefetch run unbounded.
_PREFETCH_MAX_SUBS = 60


@router.post("/cache/prefetch")
async def cache_prefetch_endpoint(
    payload: PrefetchRequest, _: Principal = Depends(get_principal)
):
    """Warm the discovery cache by walking subscriptions → resource groups → resources,
    streaming live progress. Optimized: each subscription's resources are fetched in ONE
    Resource Graph query (not one-per-RG) and subscriptions are scanned concurrently, so
    a large tenant warms in seconds instead of minutes. Subsequent picker expands are then
    instant (served from the same cache keys)."""
    import asyncio

    conn = resolve_connection(payload.connection_id or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Pick an Azure connection first.")
    cid = payload.connection_id
    force = payload.refresh

    async def warm(namespace: str, subkey: str, compute):
        key = discovery_cache.key(cid, namespace, subkey)
        value, _cached_at, _from_cache = await discovery_cache.get_or_compute(
            key, compute, force=force
        )
        return value

    def _lc(s: str) -> str:
        return (s or "").lower()

    async def _gen():
        from app.exec.command_runner import close_sp_session, open_sp_session

        subs = 0
        rgs = 0
        resources = 0
        session_dir: str | None = None
        try:
            yield {"event": "status", "data": json.dumps({"phase": "subs", "message": "Authenticating & discovering subscriptions…"})}

            # ONE service-principal login reused across every prefetch query (skips the
            # slow per-query `az login`). No-op for non-SP connections.
            session_dir, sess_err = await open_sp_session(conn)
            if sess_err:
                yield {"event": "error", "data": json.dumps({"message": sess_err})}
                return

            # Warm everything concurrently in ONE wave: top-level views, facets, all RGs
            # and all resources. The two tenant-wide queries replace ~2-per-subscription.
            sub_nodes, _mg, _facets, all_rgs, all_res = await asyncio.gather(
                warm("tree:top:subscription", "", lambda: discovery.list_top_level(conn, "subscription")),
                warm("tree:top:mg", "", lambda: discovery.list_top_level(conn, "mg")),
                warm("facets", "", lambda: discovery.facets(conn, "", session_config_dir=session_dir)),
                discovery.all_resource_groups(conn, session_config_dir=session_dir),
                discovery.all_resources(conn, cap=1000, session_config_dir=session_dir),
            )
            sub_list = [n for n in sub_nodes if n.get("kind") == "subscription"][:_PREFETCH_MAX_SUBS]
            subs = len(sub_list)
            rgs = len(all_rgs)
            resources = len(all_res)
            yield {"event": "status", "data": json.dumps({"phase": "scanning", "message": f"Found {subs} subscriptions, {rgs} resource groups, {resources} resources. Caching…", "subscriptions": subs, "resource_groups": rgs, "resources": resources})}

            # Group RGs by subscription → cache tree:subscription:{sid}.
            rgs_by_sub: dict[str, list] = {}
            for rg in all_rgs:
                rgs_by_sub.setdefault(rg.get("subscription_id", ""), []).append(rg)
            for sid, group in rgs_by_sub.items():
                discovery_cache.put(discovery_cache.key(cid, "tree:subscription", sid), group)
            # Subscriptions with no RGs still get an empty cached list.
            for s in sub_list:
                discovery_cache.put(
                    discovery_cache.key(cid, "tree:subscription", s["id"]),
                    rgs_by_sub.get(s["id"], []),
                )

            # Group resources by (subscription, RG name) → cache tree:resource_group:{rid}.
            res_by_key: dict[tuple[str, str], list] = {}
            for r in all_res:
                res_by_key.setdefault((r.get("subscription_id", ""), _lc(r.get("resource_group", ""))), []).append(r)
            for rg in all_rgs:
                rid = rg.get("id", "")
                members = res_by_key.get((rg.get("subscription_id", ""), _lc(rg.get("name", ""))), [])
                discovery_cache.put(discovery_cache.key(cid, "tree:resource_group", rid), members)

            yield {"event": "done", "data": json.dumps({
                "subscriptions": subs, "resource_groups": rgs, "resources": resources,
            })}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Prefetch failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}
        finally:
            close_sp_session(session_dir)

    return EventSourceResponse(_gen())


# ----------------------------------------------------------------- autopilot
class AutopilotRequest(BaseModel):
    connection_id: str = ""
    scope_kind: str = "subscription"  # subscription | mg
    scope_id: str = ""
    scope_name: str = ""


@router.post("/autopilot/discover")
async def autopilot_discover_endpoint(
    payload: AutopilotRequest, _: Principal = Depends(get_principal)
):
    """Stream AI Workload Autopilot discovery progress + candidates over SSE."""
    conn = resolve_connection(payload.connection_id or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Pick an Azure connection first.")

    async def _gen():
        try:
            async for ev in discover_workloads(
                conn, payload.scope_kind, payload.scope_id, payload.scope_name
            ):
                ev_type = ev.pop("type")
                yield {"event": ev_type, "data": json.dumps(ev)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Autopilot discovery failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


class CandidateSave(BaseModel):
    name: str
    description: str = ""
    reasoning: str = ""
    confidence: float = 0.0
    nodes: list[WorkloadNode] = Field(default_factory=list)


class AutopilotSaveRequest(BaseModel):
    connection_id: str = ""
    scope_kind: str = ""
    scope_id: str = ""
    scope_name: str = ""
    candidates: list[CandidateSave] = Field(default_factory=list)


@router.post("/autopilot/save")
async def autopilot_save_endpoint(
    payload: AutopilotSaveRequest, principal: Principal = Depends(get_principal)
):
    """Persist selected Autopilot candidates as Azure Workloads."""
    conn = resolve_connection(payload.connection_id or None)
    tenant_id = conn.get("tenant_id", "") if conn else ""
    origin = (
        {"kind": payload.scope_kind, "id": payload.scope_id, "name": payload.scope_name}
        if payload.scope_kind
        else {}
    )
    saved = []
    for cand in payload.candidates:
        wl = wl_registry.upsert_workload(
            {
                "name": cand.name,
                "description": cand.description,
                "connection_id": payload.connection_id,
                "tenant_id": tenant_id,
                "nodes": [n.model_dump() for n in cand.nodes],
                "origin": origin,
                "reasoning": cand.reasoning,
                "confidence": cand.confidence,
                "created_by": principal.subject,
            }
        )
        saved.append(wl)
    return {"saved": saved, "count": len(saved)}


# ----------------------------------------------------------------- refresh
@router.post("/{workload_id}/refresh")
async def refresh_workload_endpoint(
    workload_id: str, _: Principal = Depends(get_principal)
):
    """Re-scan a workload's scope: drop deleted resources, pick up newly-added ones in
    the workload's resource groups, and recompute the type-breakdown summary."""
    from datetime import datetime, timezone

    wl = wl_registry.get_workload(workload_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="Workload not found.")
    conn = resolve_connection(wl.get("connection_id") or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Workload has no resolvable Azure connection.")

    nodes = wl.get("nodes", [])
    scope_nodes = [n for n in nodes if n.get("kind") in ("mg", "subscription", "resource_group")]
    resource_nodes = [n for n in nodes if n.get("kind") == "resource"]

    # Resource groups this workload's individual resources live in → the "scan scope".
    rg_pairs = sorted(
        {
            (n.get("subscription_id", ""), n.get("resource_group", ""))
            for n in resource_nodes
            if n.get("resource_group")
        }
    )

    added: list[dict] = []
    removed: list[dict] = []
    kept_resources = resource_nodes

    if rg_pairs:
        current = await discovery.resources_in_resource_groups(conn, list(rg_pairs))
        current_by_id = {r["id"].lower(): r for r in current if r.get("id")}
        existing_ids = {n.get("id", "").lower() for n in resource_nodes}

        kept_resources = [
            n for n in resource_nodes if n.get("id", "").lower() in current_by_id
        ]
        removed = [n for n in resource_nodes if n.get("id", "").lower() not in current_by_id]
        for rid, r in current_by_id.items():
            if rid not in existing_ids:
                added.append(
                    {
                        "kind": "resource",
                        "id": r["id"],
                        "name": r.get("name", ""),
                        "resource_type": r.get("resource_type", ""),
                        "location": r.get("location", ""),
                        "resource_group": r.get("resource_group", ""),
                        "subscription_id": r.get("subscription_id", ""),
                        "excludes": [],
                    }
                )

    new_nodes = scope_nodes + kept_resources + added
    wl_saved = wl_registry.upsert_workload(
        {
            "id": workload_id,
            "name": wl["name"],
            "nodes": new_nodes,
            "last_refreshed": datetime.now(timezone.utc).isoformat(),
        }
    )
    return {
        "workload": wl_saved,
        "diff": {
            "added": added,
            "removed": removed,
            "added_count": len(added),
            "removed_count": len(removed),
            "scanned_resource_groups": len(rg_pairs),
        },
    }

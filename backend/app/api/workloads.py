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
from app.core.security import Principal, require_permission
from app.workloads import discovery, discovery_profiles
from app.workloads import registry as wl_registry
from app.workloads.autopilot import compute_estimate, discover_workloads, survey_estate
from app.workloads.cache import discovery_cache

router = APIRouter(prefix="/workloads", tags=["workloads"])

# Viewing workloads + running discovery/search analysis requires workloads.read; creating,
# editing, deleting, restoring, purging and saving discovered workloads requires
# workloads.write. The `get_principal` alias is the read tier (so existing call sites stay
# correct); write endpoints opt into `_write`. Admins always pass via require_permission.
get_principal = require_permission("workloads.read")
_write = require_permission("workloads.write")
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
    workload_type: str = ""
    environment: str = ""
    criticality: str = ""
    data_classification: str = ""


@router.get("")
async def list_workloads_endpoint(_: Principal = Depends(get_principal)):
    # NOTE: a workload's ``tenant_id`` is the AZURE AD tenant of its connection (an admin-
    # managed boundary), not the app principal's tenant, so we do not filter by the app
    # tenant here — that would hide admin-configured workloads. Workloads are governed by
    # the admin-managed Azure connections they reference.
    return {"workloads": wl_registry.list_workloads()}


@router.put("")
async def upsert_workload_endpoint(
    payload: WorkloadUpsert, principal: Principal = Depends(_write)
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
async def empty_workload_trash_endpoint(_: Principal = Depends(_write)):
    """Permanently delete every workload in the Trash."""
    deleted = wl_registry.empty_trash()
    return {"ok": True, "deleted": deleted}


class MergeRequest(BaseModel):
    workload_ids: list[str] = Field(default_factory=list)
    name: str = Field(default="", max_length=200)


@router.post("/merge")
async def merge_workloads_endpoint(payload: MergeRequest, _: Principal = Depends(_write)):
    """Merge two or more workloads into one new workload (sources moved to Trash). The
    merged workload is a normal workload, so architecture, missions and assessments can be
    re-run against it. Its name gets a trailing ``MERGED`` marker."""
    ids = [i for i in payload.workload_ids if i]
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="Select at least two workloads to merge.")
    merged = wl_registry.merge_workloads(ids, payload.name)
    if merged is None:
        raise HTTPException(
            status_code=404, detail="Need at least two valid (active) workloads to merge."
        )
    return {"workload": merged}


# ----------------------------------------------------------------- groups (applications)
# A Workload Group is a lightweight, NON-destructive association ("application" / service
# family) over workloads that keep their own identity — e.g. "CRM PROD" + "CRM DEV" under a
# "CRM" group. Membership lives as ``group_id`` on each workload (see registry.py). These
# routes are registered BEFORE the ``/{workload_id}`` routes so ``groups`` is never captured
# as a workload id.
class GroupUpsert(BaseModel):
    id: str | None = None
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    color: str = Field(default="", max_length=32)
    owner: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list)


class GroupAssign(BaseModel):
    group_id: str = ""  # existing group to assign to (ignored when `name` creates one)
    name: str = Field(default="", max_length=200)  # create a new group with this name
    workload_ids: list[str] = Field(default_factory=list)
    mode: str = "add"  # add | remove


def _members_by_group(workloads: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for w in workloads:
        gid = w.get("group_id") or ""
        if gid:
            out.setdefault(gid, []).append(w)
    return out


def _member_ref(w: dict) -> dict:
    return {
        "id": w["id"],
        "name": w.get("name", ""),
        "environment": w.get("environment", ""),
        "criticality": w.get("criticality", ""),
        "connection_id": w.get("connection_id", ""),
    }


@router.put("/groups")
async def upsert_group_endpoint(payload: GroupUpsert, principal: Principal = Depends(_write)):
    """Create or update a Workload Group's metadata (name/description/owner/color/tags).
    Membership is managed separately via ``POST /groups/assign``."""
    from app.workloads import groups as wl_groups

    data = payload.model_dump()
    if not payload.id:
        data["created_by"] = principal.subject
        data["tenant_id"] = principal.tenant_id or ""
    return {"group": wl_groups.upsert_group(data)}


@router.get("/groups")
async def list_groups_endpoint(principal: Principal = Depends(get_principal)):
    """List Workload Groups, each with its member refs + a cache-only rollup (aggregate
    health, resources, worst criticality, environment mix, risk). One request powers the
    grouped fleet view."""
    from app.core.app_settings import load_settings
    from app.workloads import groups as wl_groups
    from app.workloads import profile as wl_profile

    settings = load_settings()
    tenant = _profile_tenant(principal)
    all_wl = wl_registry.list_workloads()
    by_group = _members_by_group(all_wl)
    out = []
    for g in wl_groups.list_groups():
        members = by_group.get(g["id"], [])
        profiles = wl_profile.build_profiles(members, tenant, settings)
        out.append({
            **g,
            "member_ids": [m["id"] for m in members],
            "members": [_member_ref(m) for m in members],
            "member_count": len(members),
            "rollup": wl_groups.rollup_from_profiles(profiles),
        })
    ungrouped = sum(1 for w in all_wl if not w.get("group_id"))
    return {"groups": out, "ungrouped": ungrouped, "total_workloads": len(all_wl)}


@router.post("/groups/assign")
async def assign_group_endpoint(payload: GroupAssign, principal: Principal = Depends(_write)):
    """Assign or unassign workloads to a group. Provide ``group_id`` for an existing group,
    or ``name`` to create a new group on the fly. ``mode=remove`` detaches the workloads
    (ignores group_id/name). Non-destructive — the workloads themselves are untouched."""
    from app.workloads import groups as wl_groups

    if payload.mode == "remove":
        changed = wl_registry.assign_group(payload.workload_ids, "")
        return {"ok": True, "updated": changed, "group": None}

    gid = payload.group_id
    group: dict | None
    if gid:
        group = wl_groups.get_group(gid)
        if group is None:
            raise HTTPException(status_code=404, detail="Group not found.")
    elif payload.name.strip():
        group = wl_groups.upsert_group({
            "name": payload.name.strip(),
            "created_by": principal.subject,
            "tenant_id": principal.tenant_id or "",
        })
        gid = group["id"]
    else:
        raise HTTPException(
            status_code=400, detail="Provide an existing group_id or a name for a new group."
        )
    changed = wl_registry.assign_group(payload.workload_ids, gid)
    return {"ok": True, "updated": changed, "group": group}


@router.post("/groups/suggest")
async def suggest_groups_endpoint(_: Principal = Depends(get_principal)):
    """Suggest Workload Groups from environment-family name patterns ("CRM PROD" + "CRM DEV"
    → "CRM"). Only considers currently-ungrouped workloads. Read-only, no Azure calls."""
    from app.workloads import groups as wl_groups

    return {"suggestions": wl_groups.suggest_groups(wl_registry.list_workloads())}


@router.get("/groups/{group_id}")
async def group_detail_endpoint(group_id: str, principal: Principal = Depends(get_principal)):
    """Full detail for one group: metadata, member workloads, their cache-only profiles and
    the aggregate rollup — powering the group command-center page."""
    from app.core.app_settings import load_settings
    from app.workloads import groups as wl_groups
    from app.workloads import profile as wl_profile

    group = wl_groups.get_group(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found.")
    settings = load_settings()
    tenant = _profile_tenant(principal)
    members = [w for w in wl_registry.list_workloads() if w.get("group_id") == group_id]
    profiles = wl_profile.build_profiles(members, tenant, settings)
    return {
        "group": group,
        "members": members,
        "profiles": profiles,
        "rollup": wl_groups.rollup_from_profiles(profiles),
    }


@router.get("/groups/{group_id}/compare")
async def group_compare_endpoint(group_id: str, principal: Principal = Depends(get_principal)):
    """PROD-vs-DEV drift comparison across a group's members (cache-only): aligned member
    summaries plus resource-type / category / health-signal coverage drift and human-readable
    highlights ("PROD has a WAF that DEV lacks"). Powers the group's Compare tab."""
    from app.core.app_settings import load_settings
    from app.workloads import groups as wl_groups
    from app.workloads import profile as wl_profile

    group = wl_groups.get_group(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found.")
    settings = load_settings()
    tenant = _profile_tenant(principal)
    members = [w for w in wl_registry.list_workloads() if w.get("group_id") == group_id]
    profiles = wl_profile.build_profiles(members, tenant, settings)
    return {"group": group, "compare": wl_groups.compare_profiles(profiles)}


@router.delete("/groups/{group_id}")
async def delete_group_endpoint(group_id: str, _: Principal = Depends(_write)):
    """Delete a group. Its member workloads are detached (their ``group_id`` cleared) but NOT
    deleted."""
    from app.workloads import groups as wl_groups

    if not wl_groups.delete_group(group_id):
        raise HTTPException(status_code=404, detail="Group not found.")
    return {"ok": True}


@router.delete("/{workload_id}")
async def delete_workload_endpoint(workload_id: str, _: Principal = Depends(_write)):
    """Soft-delete a workload: move it to the Trash (restorable until purged)."""
    if not wl_registry.delete_workload(workload_id):
        raise HTTPException(status_code=404, detail="Workload not found.")
    return {"ok": True}


@router.post("/{workload_id}/restore")
async def restore_workload_endpoint(workload_id: str, _: Principal = Depends(_write)):
    """Restore a trashed workload back into the active list."""
    wl = wl_registry.restore_workload(workload_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="Workload not in trash.")
    return {"workload": wl}


@router.delete("/{workload_id}/purge")
async def purge_workload_endpoint(workload_id: str, _: Principal = Depends(_write)):
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
    strategy: str = "ai"   # ai | resource_group | subscription | tag
    mode: str = "full"     # full | delta (skip already-organized resources)
    tag_key: str = ""      # for strategy=tag (empty = auto-pick a signal tag)
    # ---- Scope Sculptor controls (pre-flight input shaping) ----
    preset: str = ""                          # fast | balanced | thorough (recorded)
    granularity: str = "resource"             # resource | resource_group | sample
    exclude_noise: bool = True                # drop low-signal children (re-attached after)
    exclude_system_rgs: bool = True           # drop MC_*/NetworkWatcherRG/etc (re-attached)
    rg_globs: list[str] = Field(default_factory=list)   # custom system-RG globs (empty=default)
    tag_seed_keys: list[str] = Field(default_factory=list)  # deterministic pre-bucket tags
    include_types: list[str] = Field(default_factory=list)
    exclude_types: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    subscriptions: list[str] = Field(default_factory=list)
    name_contains: str = ""
    confidence_floor: float = 0.0             # hide candidates below this confidence
    max_ai_calls: int = 0                     # budget cap (0 = unbounded)
    naming_hint: str = ""                     # naming convention pattern for the prompt


class SurveyRequest(BaseModel):
    connection_id: str = ""
    scope_kind: str = "subscription"
    scope_id: str = ""
    scope_name: str = ""


@router.post("/autopilot/survey")
async def autopilot_survey_endpoint(
    payload: SurveyRequest, _: Principal = Depends(get_principal)
):
    """Pre-flight survey: stream the estate facet tallies + default cost estimate (no AI)."""
    conn = resolve_connection(payload.connection_id or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Pick an Azure connection first.")

    async def _gen():
        try:
            async for ev in survey_estate(
                conn, payload.scope_kind, payload.scope_id, payload.scope_name
            ):
                ev_type = ev.pop("type")
                yield {"event": ev_type, "data": json.dumps(ev)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Autopilot survey failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


class EstimateRequest(BaseModel):
    connection_id: str = ""
    scope_kind: str = "subscription"
    scope_id: str = ""
    config: dict = Field(default_factory=dict)


@router.post("/autopilot/estimate")
async def autopilot_estimate_endpoint(
    payload: EstimateRequest, _: Principal = Depends(get_principal)
):
    """Live re-estimate cost + filter preview for a sculpt config against the cached survey
    (no Azure call). Returns ``{needs_survey: true}`` when the survey cache has expired."""
    conn = resolve_connection(payload.connection_id or None)
    tenant_id = conn.get("tenant_id", "") if conn else ""
    result = compute_estimate(
        tenant_id, payload.connection_id, payload.scope_kind, payload.scope_id, payload.config
    )
    if result is None:
        return {"needs_survey": True}
    return result


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
                conn, payload.scope_kind, payload.scope_id, payload.scope_name,
                strategy=payload.strategy, mode=payload.mode, tag_key=payload.tag_key,
                preset=payload.preset, granularity=payload.granularity,
                exclude_noise=payload.exclude_noise, exclude_system_rgs=payload.exclude_system_rgs,
                rg_globs=payload.rg_globs, tag_seed_keys=payload.tag_seed_keys,
                include_types=payload.include_types, exclude_types=payload.exclude_types,
                environments=payload.environments, regions=payload.regions,
                subscriptions=payload.subscriptions, name_contains=payload.name_contains,
                confidence_floor=payload.confidence_floor, max_ai_calls=payload.max_ai_calls,
                naming_hint=payload.naming_hint,
            ):
                ev_type = ev.pop("type")
                yield {"event": ev_type, "data": json.dumps(ev)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Autopilot discovery failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


# ----------------------------------------------------------------- discovery profiles
class ProfileSaveRequest(BaseModel):
    connection_id: str = ""
    name: str = ""
    config: dict = Field(default_factory=dict)
    scope_kind: str = ""
    scope_id: str = ""
    scope_name: str = ""
    profile_id: str = ""


@router.get("/autopilot/profiles")
async def list_profiles_endpoint(
    connection_id: str = "", _: Principal = Depends(get_principal)
):
    """Saved discovery profiles for a connection (newest first)."""
    conn = resolve_connection(connection_id or None)
    tenant_id = conn.get("tenant_id", "") if conn else ""
    return {"profiles": discovery_profiles.list_profiles(tenant_id, connection_id)}


@router.post("/autopilot/profiles")
async def save_profile_endpoint(
    payload: ProfileSaveRequest, principal: Principal = Depends(_write)
):
    """Create or update a discovery profile."""
    conn = resolve_connection(payload.connection_id or None)
    tenant_id = conn.get("tenant_id", "") if conn else ""
    profile = discovery_profiles.save_profile(
        tenant_id, payload.connection_id,
        name=payload.name, config=payload.config,
        scope_kind=payload.scope_kind, scope_id=payload.scope_id, scope_name=payload.scope_name,
        profile_id=payload.profile_id, actor=principal.subject,
    )
    return {"profile": profile}


@router.delete("/autopilot/profiles/{profile_id}")
async def delete_profile_endpoint(
    profile_id: str, connection_id: str = "", principal: Principal = Depends(_write)
):
    """Delete a discovery profile."""
    conn = resolve_connection(connection_id or None)
    tenant_id = conn.get("tenant_id", "") if conn else ""
    ok = discovery_profiles.delete_profile(tenant_id, connection_id, profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Profile not found.")
    return {"ok": True}



class CandidateSave(BaseModel):
    name: str
    description: str = ""
    reasoning: str = ""
    confidence: float = 0.0
    nodes: list[WorkloadNode] = Field(default_factory=list)
    workload_type: str = ""
    environment: str = ""
    criticality: str = ""
    data_classification: str = ""
    evidence: list[dict] = Field(default_factory=list)


class GroupingDecision(BaseModel):
    action: str  # accept | reject | rename | exclude | split | merge
    name: str = ""
    from_: str = Field(default="", alias="from")
    to: str = ""
    excluded: str = ""

    model_config = {"populate_by_name": True}


class AutopilotSaveRequest(BaseModel):
    connection_id: str = ""
    scope_kind: str = ""
    scope_id: str = ""
    scope_name: str = ""
    candidates: list[CandidateSave] = Field(default_factory=list)
    # Corrections the user made while reviewing (feed the grouping memory so the next run
    # respects them) + decisions about candidates they rejected.
    decisions: list[GroupingDecision] = Field(default_factory=list)
    # Discover -> Act: optionally kick off a Mission Control sweep + architecture/memory
    # generation for each saved workload right away.
    auto_assess: bool = False
    auto_architecture: bool = False


@router.post("/autopilot/save")
async def autopilot_save_endpoint(
    payload: AutopilotSaveRequest, principal: Principal = Depends(_write)
):
    """Persist selected Autopilot candidates as Azure Workloads. Records the user's
    grouping corrections into memory and (optionally) launches a Mission Control sweep +
    architecture generation for each new workload."""
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
                "workload_type": cand.workload_type,
                "environment": cand.environment,
                "criticality": cand.criticality,
                "data_classification": cand.data_classification,
                "evidence": cand.evidence,
                "created_by": principal.subject,
            }
        )
        saved.append(wl)

    # Learn from this review: persist accepted candidates + explicit corrections so the
    # next discovery honors them.
    try:
        from app.workloads import grouping_memory

        decisions = [{"action": "accept", "name": c.name} for c in payload.candidates]
        decisions += [d.model_dump(by_alias=True) for d in payload.decisions]
        if decisions:
            grouping_memory.record_decisions(tenant_id or "default", payload.connection_id, decisions)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to record autopilot grouping memory", exc_info=True)

    # Discover -> Act: fire-and-forget downstream generation (never blocks the save).
    launched: dict[str, list[str]] = {"missions": [], "architectures": []}
    if payload.auto_assess or payload.auto_architecture:
        for wl in saved:
            if payload.auto_architecture:
                try:
                    from app.architectures.jobs import manager as arch_jobs

                    job = arch_jobs.create(
                        tenant_id=principal.tenant_id,
                        workload_id=wl["id"],
                        workload_name=wl["name"],
                        connection_id=wl.get("connection_id", ""),
                        created_by=principal.subject,
                    )
                    launched["architectures"].append(job.get("id", ""))
                except Exception:  # noqa: BLE001
                    logger.warning("Auto-architecture launch failed for %s", wl["id"], exc_info=True)
            if payload.auto_assess:
                try:
                    from app.missions import orchestrator as missions

                    m = missions.manager.create(
                        tenant_id=principal.tenant_id,
                        workload_id=wl["id"],
                        workload_name=wl["name"],
                        connection_id=wl.get("connection_id", ""),
                        actor=principal.subject,
                        force=False,
                        trigger="autopilot",
                        system_keys=[],
                    )
                    launched["missions"].append(m.get("id", ""))
                except Exception:  # noqa: BLE001
                    logger.warning("Auto-assess launch failed for %s", wl["id"], exc_info=True)

    return {"saved": saved, "count": len(saved), "launched": launched}


# ----------------------------------------------------------------- estate coverage
@router.get("/estate-coverage")
async def estate_coverage_endpoint(
    connection_id: str = "", _: Principal = Depends(get_principal)
):
    """How much of the Azure estate (under a connection) is organized into workloads.

    Returns the organized %, the total/organized/orphaned counts, and a sample of orphaned
    resources (those belonging to no workload) for triage. Read-only; cached enumeration."""
    conn = resolve_connection(connection_id or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Pick an Azure connection first.")
    cid = conn.get("id", "")

    # All resource ids already claimed by a workload on this connection.
    claimed: set[str] = set()
    for wl in wl_registry.list_workloads():
        if cid and wl.get("connection_id") and wl.get("connection_id") != cid:
            continue
        for n in wl.get("nodes", []):
            if n.get("kind") == "resource" and n.get("id"):
                claimed.add(str(n["id"]).lower())

    # Enumerate the estate (paged to 5000) across all subscriptions visible to the connection.
    subs = await discovery.list_top_level(conn, "subscription")
    sub_ids = [
        (s["id"].split("/")[-1] if "/" in s.get("id", "") else s.get("id", ""))
        for s in subs
    ]
    sub_ids = [s for s in sub_ids if s]
    resources, truncated = await discovery.enumerate_resources_paged(conn, sub_ids, cap=5000)

    total = len(resources)
    orphans = [r for r in resources if str(r.get("id", "")).lower() not in claimed]
    organized = total - len(orphans)
    pct = round(100 * organized / total) if total else 100

    # Group orphans by resource group for triage; sample the first 200.
    by_rg: dict[str, int] = {}
    for r in orphans:
        by_rg[r.get("resource_group", "") or "(none)"] = by_rg.get(r.get("resource_group", "") or "(none)", 0) + 1
    orphan_rgs = sorted(({"resource_group": k, "count": v} for k, v in by_rg.items()), key=lambda x: -x["count"])

    return {
        "connection_id": cid,
        "total": total,
        "organized": organized,
        "orphaned": len(orphans),
        "organized_pct": pct,
        "truncated": truncated,
        "orphan_resource_groups": orphan_rgs[:50],
        "orphans": [
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "resource_type": r.get("resource_type", ""),
                "resource_group": r.get("resource_group", ""),
                "subscription_id": r.get("subscription_id", ""),
                "location": r.get("location", ""),
            }
            for r in orphans[:200]
        ],
    }


# ----------------------------------------------------------------- overlaps (duplicates)
def _sub_id_of(value: str) -> str:
    """Extract the bare subscription GUID from an ARM id or scope value."""
    v = value or ""
    if "/subscriptions/" in v.lower():
        # /subscriptions/<guid>/...
        parts = v.split("/")
        for i, p in enumerate(parts):
            if p.lower() == "subscriptions" and i + 1 < len(parts):
                return parts[i + 1]
    return v


@router.get("/overlaps")
async def overlaps_endpoint(
    connection_id: str = "",
    deep: bool = False,
    principal: Principal = Depends(get_principal),
):
    """Report resources that belong to MORE THAN ONE workload.

    Tier 1 (default, instant, no Azure calls): resources EXPLICITLY listed as ``resource``
    nodes in 2+ workloads. Tier 2 (``deep=true``): also detect SCOPE-IMPLIED overlaps — a
    resource explicitly in one workload while another workload includes its whole resource
    group / subscription (excludes honored). Read-only."""
    from datetime import datetime, timezone

    cid = connection_id or None
    if not deep:
        result = wl_registry.find_overlaps(cid)
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["deep"] = False
        result["truncated"] = False
        return result

    # ---- Deep scan: enumerate the estate, then attribute resources to scope nodes. ----
    conn = resolve_connection(connection_id or None)
    if not conn:
        raise HTTPException(status_code=400, detail="Pick an Azure connection first for a deep scan.")

    workloads = [
        w for w in wl_registry.list_workloads()
        if (not cid) or (not w.get("connection_id")) or w.get("connection_id") == cid
    ]

    # Enumerate all resources across the connection's subscriptions (cached, paged).
    subs = await discovery.list_top_level(conn, "subscription")
    sub_ids = [_sub_id_of(s.get("id", "")) or s.get("id", "") for s in subs]
    sub_ids = [s for s in sub_ids if s]
    resources, truncated = await discovery.enumerate_resources_paged(conn, sub_ids, cap=5000)

    # Index estate resources by subscription and by (sub, rg) for fast scope attribution.
    res_by_sub: dict[str, list[dict[str, Any]]] = {}
    res_by_rg: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in resources:
        s = (r.get("subscription_id") or "").lower()
        rg = (r.get("resource_group") or "").lower()
        res_by_sub.setdefault(s, []).append(r)
        res_by_rg.setdefault((s, rg), []).append(r)

    def _member(wl: dict[str, Any], via: str, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "workload_id": wl["id"],
            "workload_name": wl.get("name", ""),
            "via": via,
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "resource_type": r.get("resource_type", ""),
            "resource_group": r.get("resource_group", ""),
            "subscription_id": r.get("subscription_id", ""),
            "location": r.get("location", ""),
        }

    # Build scope-implied memberships keyed by lowercased ARM id.
    scope_members: dict[str, list[dict[str, Any]]] = {}
    for wl in workloads:
        for n in wl.get("nodes", []):
            kind = n.get("kind")
            if kind not in ("subscription", "resource_group"):
                continue  # mg expansion omitted (rare for workloads)
            excludes = {str(x).lower() for x in (n.get("excludes") or [])}
            covered: list[dict[str, Any]] = []
            if kind == "subscription":
                sub = (_sub_id_of(n.get("id", "")) or n.get("subscription_id", "")).lower()
                covered = res_by_sub.get(sub, [])
            else:  # resource_group
                sub = (n.get("subscription_id") or _sub_id_of(n.get("id", ""))).lower()
                rg = (n.get("resource_group") or n.get("name") or "").lower()
                covered = res_by_rg.get((sub, rg), [])
            for r in covered:
                rid = (r.get("id") or "").lower()
                if not rid or rid in excludes:
                    continue
                scope_members.setdefault(rid, []).append(_member(wl, kind, r))

    result = wl_registry.find_overlaps_with_memberships(cid, scope_members)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["deep"] = True
    result["truncated"] = truncated
    return result





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


# =============================================================== Command center: profiles
class ProfilesRequest(BaseModel):
    # Empty ids = profile EVERY active workload (the fleet list). Otherwise just these.
    ids: list[str] = Field(default_factory=list)


def _profile_tenant(principal: Principal) -> str:
    # Per-workload feature caches (amba/telemetry/backupdr/perf/ownership/radar) are written
    # under principal.tenant_id or "default" — read the profile under the same key.
    return principal.tenant_id or "default"


@router.get("/{workload_id}/profile")
async def workload_profile_endpoint(
    workload_id: str, principal: Principal = Depends(get_principal)
):
    """The cache-only command-center rollup for ONE workload (composition + health signals +
    risk + activity). Never scans Azure — a never-analyzed signal reports null."""
    from app.core.app_settings import load_settings
    from app.workloads import profile as wl_profile

    wl = wl_registry.get_workload(workload_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="Workload not found.")
    settings = load_settings()
    return {"profile": wl_profile.build_profile(wl, _profile_tenant(principal), settings)}


@router.post("/profiles")
async def workload_profiles_endpoint(
    payload: ProfilesRequest, principal: Principal = Depends(get_principal)
):
    """Batch cache-only profiles for the fleet list — ONE request powers all the cards."""
    from app.core.app_settings import load_settings
    from app.workloads import profile as wl_profile

    settings = load_settings()
    if payload.ids:
        wanted = set(payload.ids)
        workloads = [w for w in wl_registry.list_workloads() if w["id"] in wanted]
    else:
        workloads = wl_registry.list_workloads()
    profiles = wl_profile.build_profiles(workloads, _profile_tenant(principal), settings)
    return {"profiles": profiles, "total": len(profiles)}


@router.get("/health-weights")
async def workload_health_weights_endpoint(_: Principal = Depends(get_principal)):
    """The composite-score signal list + the (admin-tunable) weights currently in effect —
    so the UI can explain the score and the Settings page can edit them."""
    from app.core.app_settings import load_settings
    from app.workloads import health as wl_health

    settings = load_settings()
    return {
        "signals": list(wl_health.SIGNALS),
        "weights": wl_health.resolve_weights(settings),
        "bands": {"good": wl_health.SCORE_GOOD, "warn": wl_health.SCORE_WARN},
        "nightly_refresh": bool(settings.get("workload_nightly_refresh")),
    }


@router.post("/{workload_id}/trend/record")
async def workload_trend_record_endpoint(
    workload_id: str, principal: Principal = Depends(get_principal)
):
    """Record a composite-score trend point for this workload (call after Analyze). No-op when
    nothing has been analyzed yet."""
    from app.core.app_settings import load_settings
    from app.workloads import profile as wl_profile

    wl = wl_registry.get_workload(workload_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="Workload not found.")
    score = wl_profile.record_trend(wl, _profile_tenant(principal), load_settings())
    return {"recorded": score is not None, "score": score}


@router.get("/{workload_id}/trend")
async def workload_trend_endpoint(
    workload_id: str, principal: Principal = Depends(get_principal)
):
    """The composite-score trend series for this workload (chart-ready)."""
    from app.core import coverage_trends

    return coverage_trends.trend("workload", _profile_tenant(principal), "workload", workload_id)


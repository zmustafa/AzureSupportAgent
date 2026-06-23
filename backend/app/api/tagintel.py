"""Azure Tag Intelligence endpoints.

A read-first toolkit over the existing inventory scan. The expensive Resource Graph work is
shared with the Inventory screen: Tag Intelligence reads the same per-(tenant, connection,
scope) inventory cache, and ``force=1`` triggers one fresh ``inventory.service.collect`` that
both screens then reuse. The analysis layer (census, hygiene, coverage, FinOps, drift, policy
generation, remediation preview) is pure Python over that cached payload — no extra Azure
calls. Admin-gated; everything is read-only except remediation, which only ever returns a
plan + scripts (it never writes to Azure).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.azure_connections import resolve_connection
from app.core.security import Principal, require_permission
from app.inventory import cache as inv_cache
from app.inventory import cost as inv_cost
from app.inventory import service as inv_service
from app.tagintel import (
    analysis,
    ask as ask_mod,
    catalog,
    coverage as coverage_mod,
    drift,
    finops,
    policygen,
    rbac_advice,
    remediation,
    scale,
)

router = APIRouter(prefix="/tagintel", tags=["tagintel"])

# Viewing tag intelligence + generating previews/scripts requires tagintel.read; persisting
# catalog entries, changesets, snapshots and APPLYING tag remediation to Azure requires
# tagintel.write. The `require_admin` alias is the read tier; mutating endpoints opt into
# `_write`. Admins pass either way. See app.auth.permissions for the catalog.
require_admin = require_permission("tagintel.read")
_write = require_permission("tagintel.write")
logger = logging.getLogger("app.api.tagintel")

# Resource types exempt from required-tag enforcement by default (shared/platform telemetry
# that customers rarely tag). Overridable per request.
_DEFAULT_EXEMPT = ["microsoft.insights/", "microsoft.alertsmanagement/", "microsoft.security/"]


def _conn(connection_id: str | None) -> dict[str, Any] | None:
    return resolve_connection(connection_id)


def _sub_names(payload: dict[str, Any]) -> dict[str, str]:
    return {f.get("key"): f.get("name", f.get("key")) for f in (payload.get("facets", {}) or {}).get("subscriptions", [])}


def _normalize_dump(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map ``reverse.dump_resources`` rows (camelCase, tags may be None) into the snake_case
    shape the analysis layer expects (matching ``inventory.service`` output)."""
    out = []
    for r in resources:
        tags = r.get("tags")
        out.append({
            "id": r.get("id", ""), "name": r.get("name", ""), "type": (r.get("type", "") or "").lower(),
            "kind": r.get("kind") or "", "location": (r.get("location") or "").lower(),
            "resource_group": r.get("resourceGroup") or "", "subscription_id": r.get("subscriptionId") or "",
            "tags": tags if isinstance(tags, dict) else {}, "workloads": [],
        })
    return out


async def _load(tid: str, cid: str, scope: str, force: bool, workload_id: str = "") -> dict[str, Any] | None:
    """Return the resource payload for a scope, sharing the Inventory cache.

    A ``workload_id`` scopes precisely to one workload's resources (via the same
    ``dump_resources`` path Mission Control uses), cached under a synthetic ``wl:<id>`` scope.
    Otherwise the standard inventory scope (``""`` / ``sub:`` / ``mg:``) is used. ``force`` runs
    a fresh scan; a cache miss without ``force`` returns None (caller shows 'not loaded yet')."""
    if workload_id:
        from app.architectures.reverse import dump_resources
        from app.core.azure_connections import connection_for_workload
        from app.workloads.registry import get_workload

        wl = get_workload(workload_id)
        if wl is None:
            return None
        wl_scope = f"wl:{workload_id}"
        if not force:
            hit = inv_cache.get(tid, cid, scope=wl_scope)
            if not hit:
                return None
            return {"payload": hit["payload"], "fetched_at": hit.get("fetched_at", ""), "age_seconds": hit.get("age_seconds", 0)}
        # Demo workloads serve their synthetic (intentionally messy) tag inventory from the shared
        # catalog instead of querying Azure — so Tag Intelligence is fully explorable offline.
        from app.demo_catalog import is_demo_workload, resources_for

        if is_demo_workload(workload_id):
            payload = {
                "resources": _normalize_dump(resources_for(workload_id)),
                "facets": {"subscriptions": []}, "summary": {}, "errors": [],
            }
            fetched_at = inv_cache.set_(tid, cid, payload, scope=wl_scope)
            return {"payload": payload, "fetched_at": fetched_at, "age_seconds": 0}
        conn = _conn(cid or None) or connection_for_workload(wl)
        dump = await dump_resources(wl, conn)
        payload = {
            "resources": _normalize_dump(dump.get("resources", []) or []),
            "facets": {"subscriptions": []}, "summary": {},
            "errors": ([dump["error"]] if dump.get("error") else []),
        }
        fetched_at = inv_cache.set_(tid, cid, payload, scope=wl_scope)
        return {"payload": payload, "fetched_at": fetched_at, "age_seconds": 0}

    if force:
        payload = await inv_service.collect(_conn(cid or None), scope=scope)
        fetched_at = inv_cache.set_(tid, cid, payload, scope=scope)
        return {"payload": payload, "fetched_at": fetched_at, "age_seconds": 0}
    hit = inv_cache.get(tid, cid, scope=scope)
    if not hit:
        return None
    return {"payload": hit["payload"], "fetched_at": hit.get("fetched_at", ""), "age_seconds": hit.get("age_seconds", 0)}


def _capped(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    return scale.cap_estate(payload.get("resources", []) or [])


def _not_loaded() -> dict[str, Any]:
    return {"available": False, "never_loaded": True, "fetched_at": "", "age_seconds": 0}


# --------------------------------------------------------------------------- F1 census + F10 ask


@router.get("/census")
async def get_census(connection_id: str | None = None, scope: str = "", workload_id: str = "", force: int = 0,
                     principal: Principal = Depends(require_admin)):
    """Estate-wide tag census (F1). Cache-only on a page visit; ``force=1`` scans."""
    loaded = await _load(principal.tenant_id, connection_id or "", scope, bool(force), workload_id)
    if not loaded:
        return _not_loaded()
    resources, truncated = _capped(loaded["payload"])
    cen = analysis.census(resources, _sub_names(loaded["payload"]))
    return {"available": True, "never_loaded": False, "fetched_at": loaded["fetched_at"],
            "age_seconds": loaded["age_seconds"], "truncated": truncated, "estate_cap": scale.MAX_ESTATE,
            "census": cen}


class AskReq(BaseModel):
    question: str
    connection_id: str = ""
    scope: str = ""
    workload_id: str = ""


@router.post("/ask")
async def post_ask(req: AskReq, principal: Principal = Depends(require_admin)):
    """Natural-language tag console (F10). Answers from the cached census/resources."""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required.")
    loaded = await _load(principal.tenant_id, req.connection_id or "", req.scope, False, req.workload_id)
    if not loaded:
        return {"available": False, "answer": "Load the tag census first (press Refresh on the Census tab)."}
    resources, _ = _capped(loaded["payload"])
    cen = analysis.census(resources, _sub_names(loaded["payload"]))
    det = ask_mod.answer(req.question, cen, resources)
    # Compound / free-form questions the deterministic templates can't parse fall back to the
    # AI NL→ARG path, which generates a real Resource Graph query + the matching rows.
    if det.get("needs_ai"):
        ai = await ask_mod.answer_ai(req.question, cen, resources)
        if ai is not None:
            return {"available": True, **ai}
    return {"available": True, **det}


# --------------------------------------------------------------------------- F2 hygiene + F3 grouping


@router.get("/hygiene")
async def get_hygiene(connection_id: str | None = None, scope: str = "", workload_id: str = "",
                      principal: Principal = Depends(require_admin)):
    """Key/value normalization clusters (F2) + inferred workload grouping (F3)."""
    loaded = await _load(principal.tenant_id, connection_id or "", scope, False, workload_id)
    if not loaded:
        return _not_loaded()
    resources, truncated = _capped(loaded["payload"])
    return {
        "available": True, "fetched_at": loaded["fetched_at"], "truncated": truncated,
        "key_clusters": analysis.key_clusters(resources),
        "value_clusters": analysis.value_clusters(resources),
        "grouping": analysis.workload_inference(resources),
    }


# --------------------------------------------------------------------------- F2 catalog


class CatalogEntry(BaseModel):
    id: str | None = None
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    category: str | None = None
    purpose: str = ""
    required: bool = False
    inherited: bool = False
    scope: str = "resource"
    allowed_values: list[str] = Field(default_factory=list)
    example_values: list[str] = Field(default_factory=list)
    owner: str = ""
    description: str = ""


@router.get("/catalog")
async def get_catalog(principal: Principal = Depends(require_admin)):
    return {"entries": catalog.list_catalog(principal.tenant_id)}


@router.post("/catalog")
async def post_catalog(entry: CatalogEntry, principal: Principal = Depends(_write)):
    try:
        saved = catalog.upsert(principal.tenant_id, entry.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return saved


@router.delete("/catalog/{entry_id}")
async def delete_catalog(entry_id: str, principal: Principal = Depends(_write)):
    return {"deleted": catalog.delete(principal.tenant_id, entry_id)}


class SeedReq(BaseModel):
    connection_id: str = ""
    scope: str = ""
    workload_id: str = ""
    limit: int = 12


@router.post("/catalog/seed")
async def post_catalog_seed(req: SeedReq, principal: Principal = Depends(_write)):
    """Create draft catalog entries from the most-used discovered keys (F2)."""
    loaded = await _load(principal.tenant_id, req.connection_id or "", req.scope, False, req.workload_id)
    if not loaded:
        return {"available": False, "created": []}
    resources, _ = _capped(loaded["payload"])
    cen = analysis.census(resources)
    created = catalog.seed_from_census(principal.tenant_id, cen["keys"], analysis.key_clusters(resources), limit=req.limit)
    return {"available": True, "created": created, "entries": catalog.list_catalog(principal.tenant_id)}


# --------------------------------------------------------------------------- F6 coverage


@router.get("/coverage")
async def get_coverage(connection_id: str | None = None, scope: str = "", workload_id: str = "", required: str = "",
                       principal: Principal = Depends(require_admin)):
    """Required-tag coverage + 'missing only one tag' queue (F6). Required keys come from the
    catalog unless overridden with a comma-separated ``required`` list."""
    loaded = await _load(principal.tenant_id, connection_id or "", scope, False, workload_id)
    if not loaded:
        return _not_loaded()
    resources, truncated = _capped(loaded["payload"])
    req_keys = [k.strip() for k in required.split(",") if k.strip()] or catalog.required_keys(principal.tenant_id)
    if not req_keys:
        return {"available": True, "needs_required": True, "fetched_at": loaded["fetched_at"],
                "message": "No required tags defined. Add required tags in the catalog (Hygiene tab) or pass ?required=Key1,Key2."}
    cov = coverage_mod.coverage(resources, req_keys, _DEFAULT_EXEMPT, _sub_names(loaded["payload"]))
    return {"available": True, "needs_required": False, "fetched_at": loaded["fetched_at"], "truncated": truncated, **cov}


# --------------------------------------------------------------------------- F4 billing + F5 cost


def _cost_map(tid: str, cid: str, scope: str) -> tuple[dict[str, float], dict[str, Any] | None]:
    payload = inv_cost.peek_cost(tid, cid, scope=scope)
    if not payload:
        return {}, None
    return {str(k): float(v) for k, v in (payload.get("by_resource") or {}).items()}, payload


@router.get("/cost")
async def get_cost_allocation(connection_id: str | None = None, scope: str = "", workload_id: str = "", dimension: str = "workload",
                              principal: Principal = Depends(require_admin)):
    """Cost allocation / showback (F5). Cache-only; needs the Inventory Cost tab loaded once."""
    loaded = await _load(principal.tenant_id, connection_id or "", scope, False, workload_id)
    if not loaded:
        return _not_loaded()
    cost_by_resource, cost_payload = _cost_map(principal.tenant_id, connection_id or "", scope)
    if cost_payload is None:
        return {"available": True, "cost_available": False,
                "message": "Load Cost once on the Inventory → Cost tab (needs Cost Management Reader)."}
    resources, truncated = _capped(loaded["payload"])
    alloc = finops.cost_allocation(resources, cost_by_resource, dimension)
    alloc["currency"] = cost_payload.get("currency", "")
    return {"available": True, "cost_available": True, "truncated": truncated, **alloc}


@router.get("/billing-map")
async def get_billing_map(connection_id: str | None = None, scope: str = "", workload_id: str = "",
                          principal: Principal = Depends(require_admin)):
    """Billing-code -> workload -> owner -> cost map (F4)."""
    loaded = await _load(principal.tenant_id, connection_id or "", scope, False, workload_id)
    if not loaded:
        return _not_loaded()
    cost_by_resource, cost_payload = _cost_map(principal.tenant_id, connection_id or "", scope)
    resources, truncated = _capped(loaded["payload"])
    bm = finops.billing_map(resources, cost_by_resource)
    bm["cost_available"] = cost_payload is not None
    bm["currency"] = (cost_payload or {}).get("currency", "")
    bm["available"] = True
    bm["truncated"] = truncated
    return bm


class CmdbReq(BaseModel):
    connection_id: str = ""
    scope: str = ""
    workload_id: str = ""
    cmdb_codes: list[str] = Field(default_factory=list)


@router.post("/cmdb-reconcile")
async def post_cmdb_reconcile(req: CmdbReq, principal: Principal = Depends(require_admin)):
    """Diff discovered billing codes against an imported CMDB list (F4)."""
    loaded = await _load(principal.tenant_id, req.connection_id or "", req.scope, False, req.workload_id)
    if not loaded:
        return _not_loaded()
    resources, _ = _capped(loaded["payload"])
    bm = finops.billing_map(resources, {})
    discovered = [r["billing_code"] for r in bm["rows"] if not r["unallocated"]]
    return {"available": True, **finops.reconcile_cmdb(discovered, req.cmdb_codes)}


# --------------------------------------------------------------------------- F7 drift


@router.post("/drift/snapshot")
async def post_drift_snapshot(connection_id: str | None = None, scope: str = "", workload_id: str = "",
                              principal: Principal = Depends(_write)):
    loaded = await _load(principal.tenant_id, connection_id or "", scope, False, workload_id)
    if not loaded:
        return _not_loaded()
    resources, _ = _capped(loaded["payload"])
    cen = analysis.census(resources)
    snap = drift.save_snapshot(principal.tenant_id, connection_id or "", scope, resources,
                               coverage_pct=cen["tag_coverage_pct"], actor=principal.subject)
    return {"available": True, "snapshot": snap}


@router.get("/drift")
async def get_drift(connection_id: str | None = None, scope: str = "",
                    principal: Principal = Depends(require_admin)):
    return {"snapshots": drift.list_snapshots(principal.tenant_id, connection_id or "", scope)}


@router.get("/drift/diff")
async def get_drift_diff(base: str, head: str, connection_id: str | None = None, scope: str = "",
                         principal: Principal = Depends(require_admin)):
    return drift.diff(principal.tenant_id, connection_id or "", scope, base, head)


# --------------------------------------------------------------------------- F8 policy


class PolicyGenReq(BaseModel):
    selections: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/policygen")
async def post_policygen(req: PolicyGenReq, principal: Principal = Depends(require_admin)):
    """Generate tag policy definitions + an initiative from selections (F8). Read-only."""
    return policygen.generate(req.selections)


@router.get("/policy/ladder")
async def get_policy_ladder(_: Principal = Depends(require_admin)):
    return {"ladder": policygen.rollout_ladder()}


# --------------------------------------------------------------------------- F9 remediation + F11 rbac


class RemediateReq(BaseModel):
    connection_id: str = ""
    scope: str = ""
    workload_id: str = ""
    # A change-set is one or more operations applied together. ``op`` (single) stays for
    # back-compat; ``operations`` (list) is the saved/preloaded change-set form.
    op: dict[str, Any] = Field(default_factory=dict)
    operations: list[dict[str, Any]] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)


def _plan_from_req(req: "RemediateReq", resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a dry-run plan from either a single ``op`` or a multi-op ``operations`` change-set."""
    ops = req.operations or ([req.op] if req.op.get("type") else [])
    if not ops:
        raise ValueError("provide an operation or a change-set")
    resource_ids = req.resource_ids or (req.op.get("resource_ids") if req.op else None)
    return remediation.build_plan_ops(resources, ops, resource_ids)


@router.post("/remediate/preview")
async def post_remediate_preview(req: RemediateReq, principal: Principal = Depends(require_admin)):
    """Dry-run a tag change-set: per-resource before->after diff (F9). No writes."""
    loaded = await _load(principal.tenant_id, req.connection_id or "", req.scope, False, req.workload_id)
    if not loaded:
        return _not_loaded()
    resources, _ = _capped(loaded["payload"])
    try:
        plan = _plan_from_req(req, resources)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"available": True, **plan}


@router.post("/remediate/scripts")
async def post_remediate_scripts(req: RemediateReq, principal: Principal = Depends(require_admin)):
    """Generate PowerShell / CLI / ARG / rollback scripts for a change-set (F9). No writes."""
    loaded = await _load(principal.tenant_id, req.connection_id or "", req.scope, False, req.workload_id)
    if not loaded:
        return _not_loaded()
    resources, _ = _capped(loaded["payload"])
    try:
        plan = _plan_from_req(req, resources)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    scripts = remediation.generate_scripts(plan)
    remediation.save_plan(principal.tenant_id, plan, actor=principal.subject)
    return {"available": True, "count": plan["count"], "overwrites": plan["overwrites"], "scripts": scripts}


@router.get("/remediate/plans")
async def get_remediate_plans(principal: Principal = Depends(require_admin)):
    return {"plans": remediation.list_plans(principal.tenant_id)}


def _resolve_write_connection(connection_id: str, workload_id: str) -> dict[str, Any] | None:
    """The connection a remediation write should run under — the workload's own connection for a
    workload scope (unless overridden), otherwise the selected/default connection."""
    if workload_id:
        from app.core.azure_connections import connection_for_workload
        from app.workloads.registry import get_workload

        wl = get_workload(workload_id)
        return resolve_connection(connection_id or None) or (connection_for_workload(wl) if wl else None)
    return resolve_connection(connection_id or None)


class ApplyReq(BaseModel):
    connection_id: str = ""
    scope: str = ""
    workload_id: str = ""
    operations: list[dict[str, Any]] = Field(default_factory=list)
    op: dict[str, Any] = Field(default_factory=dict)
    resource_ids: list[str] = Field(default_factory=list)
    approved: bool = False
    changeset_id: str = ""


@router.post("/remediate/apply")
async def post_remediate_apply(req: ApplyReq, principal: Principal = Depends(_write)):
    """Apply a tag change-set to Azure (the actual write). Requires explicit approval and a
    writable connection; the command runner enforces connection read-only + admin command-exec
    governance. Returns per-resource results. Nothing runs without ``approved=True``."""
    if not req.approved:
        raise HTTPException(status_code=400, detail="Explicit approval is required to apply changes.")
    loaded = await _load(principal.tenant_id, req.connection_id or "", req.scope, False, req.workload_id)
    if not loaded:
        return _not_loaded()
    resources, _ = _capped(loaded["payload"])
    plan_req = RemediateReq(connection_id=req.connection_id, scope=req.scope, workload_id=req.workload_id,
                            op=req.op, operations=req.operations, resource_ids=req.resource_ids)
    try:
        plan = _plan_from_req(plan_req, resources)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = _resolve_write_connection(req.connection_id or "", req.workload_id or "")
    actor = principal.display_name or principal.email or principal.subject
    result = await remediation.apply_plan(plan, conn, actor=actor)
    remediation.save_plan(principal.tenant_id, plan, actor=actor, approved=True,
                          applied=not result.get("blocked"), result=result)
    # Stamp the saved change-set's last-run audit trail when this apply came from one.
    if req.changeset_id and not result.get("blocked"):
        remediation.record_changeset_run(principal.tenant_id, req.changeset_id, {
            "scope": req.workload_id or req.scope or "tenant", "actor": actor,
            "applied": result.get("applied", 0), "failed": result.get("failed", 0),
            "total": result.get("total", 0),
        })
    return {"available": True, "count": plan["count"], "overwrites": plan["overwrites"], **result}


@router.post("/remediate/apply/stream")
async def post_remediate_apply_stream(req: ApplyReq, principal: Principal = Depends(_write)):
    """Apply a tag change-set to Azure over SSE, emitting a live per-resource status feed:
    ``start`` → (``item_start`` → ``item_done``)* → ``done``. Same governance, approval and
    audit-trail rules as :func:`post_remediate_apply` — the plan + result are persisted (and the
    change-set's run is stamped) before ``done`` reaches the client, so the audit record survives
    a dropped stream."""
    if not req.approved:
        raise HTTPException(status_code=400, detail="Explicit approval is required to apply changes.")
    loaded = await _load(principal.tenant_id, req.connection_id or "", req.scope, False, req.workload_id)
    if not loaded:
        return _not_loaded()
    resources, _ = _capped(loaded["payload"])
    plan_req = RemediateReq(connection_id=req.connection_id, scope=req.scope, workload_id=req.workload_id,
                            op=req.op, operations=req.operations, resource_ids=req.resource_ids)
    try:
        plan = _plan_from_req(plan_req, resources)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = _resolve_write_connection(req.connection_id or "", req.workload_id or "")
    actor = principal.display_name or principal.email or principal.subject
    tenant_id = principal.tenant_id
    changeset_id = req.changeset_id

    async def _gen():
        try:
            async for ev in remediation.apply_plan_stream(plan, conn, actor=actor):
                etype = ev.get("event", "message")
                data = {k: v for k, v in ev.items() if k != "event"}
                if etype == "done":
                    # Persist the audit trail BEFORE telling the client we're done.
                    remediation.save_plan(tenant_id, plan, actor=actor, approved=True,
                                          applied=not data.get("blocked"), result=data)
                    if changeset_id and not data.get("blocked"):
                        remediation.record_changeset_run(tenant_id, changeset_id, {
                            "scope": req.workload_id or req.scope or "tenant", "actor": actor,
                            "applied": data.get("applied", 0), "failed": data.get("failed", 0),
                            "total": data.get("total", 0),
                        })
                    payload = {"available": True, "count": plan["count"],
                               "overwrites": plan["overwrites"], **data}
                    yield {"event": "done", "data": json.dumps(payload)}
                else:
                    yield {"event": etype, "data": json.dumps(data)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("tag remediation apply stream failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())



# --------------------------------------------------------------------------- saved change-sets


class ChangeSetReq(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    group_id: str = ""
    labels: list[str] = Field(default_factory=list)
    operations: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/changesets")
async def get_changesets(principal: Principal = Depends(require_admin)):
    """List saved change-sets + their groups (the cloud-ops change-set library, F9)."""
    return {
        "changesets": remediation.list_changesets(principal.tenant_id),
        "groups": remediation.list_groups(principal.tenant_id),
    }


@router.post("/changesets")
async def post_changeset(cs: ChangeSetReq, principal: Principal = Depends(_write)):
    """Create or update a named change-set so it can be re-loaded for preview/dry-run/apply."""
    try:
        saved = remediation.save_changeset(principal.tenant_id, cs.model_dump(), actor=principal.subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return saved


@router.delete("/changesets/{cs_id}")
async def delete_changeset(cs_id: str, principal: Principal = Depends(_write)):
    return {"deleted": remediation.delete_changeset(principal.tenant_id, cs_id)}


@router.post("/changesets/{cs_id}/duplicate")
async def post_duplicate_changeset(cs_id: str, principal: Principal = Depends(_write)):
    dup = remediation.duplicate_changeset(principal.tenant_id, cs_id, actor=principal.subject)
    if dup is None:
        raise HTTPException(status_code=404, detail="Change-set not found.")
    return dup


class MoveReq(BaseModel):
    group_id: str = ""


@router.post("/changesets/{cs_id}/move")
async def post_move_changeset(cs_id: str, req: MoveReq, principal: Principal = Depends(_write)):
    moved = remediation.move_changeset(principal.tenant_id, cs_id, req.group_id)
    if moved is None:
        raise HTTPException(status_code=404, detail="Change-set or group not found.")
    return moved


@router.get("/changesets/export")
async def export_changesets(ids: str = "", principal: Principal = Depends(require_admin)):
    """Export saved change-sets (and the groups they use) as a portable JSON bundle. Pass a
    comma-separated ``ids`` to export a subset; omit it to export the whole library."""
    id_list = [i for i in (ids.split(",") if ids else []) if i] or None
    return remediation.export_changesets(principal.tenant_id, id_list)


class ImportReq(BaseModel):
    kind: str = ""
    version: int = 1
    groups: list[dict[str, Any]] = Field(default_factory=list)
    changesets: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/changesets/import")
async def import_changesets(req: ImportReq, principal: Principal = Depends(_write)):
    """Import a change-set bundle produced by the export endpoint. Adds change-sets as new
    records (never overwrites); referenced groups are matched by name or created."""
    try:
        result = remediation.import_changesets(principal.tenant_id, req.model_dump(), actor=principal.subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


class GroupReq(BaseModel):
    id: str | None = None
    name: str
    color: str = ""
    description: str = ""
    order: int = 0


@router.get("/changeset-groups")
async def get_changeset_groups(principal: Principal = Depends(require_admin)):
    return {"groups": remediation.list_groups(principal.tenant_id)}


@router.post("/changeset-groups")
async def post_changeset_group(group: GroupReq, principal: Principal = Depends(_write)):
    try:
        saved = remediation.save_group(principal.tenant_id, group.model_dump(), actor=principal.subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return saved


@router.delete("/changeset-groups/{group_id}")
async def delete_changeset_group(group_id: str, principal: Principal = Depends(_write)):
    return {"deleted": remediation.delete_group(principal.tenant_id, group_id)}


@router.get("/rbac-advice")
async def get_rbac_advice(_: Principal = Depends(require_admin)):
    return rbac_advice.advice()


# --------------------------------------------------------------------------- summary (overview / mission)


@router.get("/summary")
async def get_summary(connection_id: str | None = None, scope: str = "", workload_id: str = "",
                      principal: Principal = Depends(require_admin)):
    """A compact headline used by the screen header and the Tag Intelligence mission (F12)."""
    loaded = await _load(principal.tenant_id, connection_id or "", scope, False, workload_id)
    if not loaded:
        return _not_loaded()
    resources, truncated = _capped(loaded["payload"])
    cen = analysis.census(resources)
    req_keys = catalog.required_keys(principal.tenant_id)
    cov = coverage_mod.coverage(resources, req_keys, _DEFAULT_EXEMPT) if req_keys else None
    return {
        "available": True, "fetched_at": loaded["fetched_at"], "truncated": truncated,
        "total_resources": cen["total_resources"],
        "tag_coverage_pct": cen["tag_coverage_pct"],
        "distinct_keys": cen["distinct_keys"],
        "untagged_count": cen["untagged_count"],
        "required_coverage_pct": cov["coverage_pct"] if cov else None,
        "missing_one_total": cov["missing_one_total"] if cov else None,
        "high_cardinality": cen["flags"]["high_cardinality"],
    }

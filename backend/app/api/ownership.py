"""Ownership API — owners + teams directory, owner↔subject assignments, the federated
people-picker, and the effective-owner resolver.

RBAC: reads require ``ownership.read`` (granted to every role incl. ``user``); writes
require ``ownership.write`` (admin + operator). Every mutation writes an ``AuditLog`` row.

Route ordering note: the literal ``/owners/trash`` + ``/assignments/trash`` collections are
declared BEFORE the ``/{id}`` parameter routes so "trash" is never captured as an id (the
same gotcha handled in workloads/architectures/evidence)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog
from app.ownership import cache, coverage, directory, registry, resolve

router = APIRouter(prefix="/ownership", tags=["ownership"])
log = logging.getLogger("app.api.ownership")

read_dep = require_permission("ownership.read")
write_dep = require_permission("ownership.write")


async def _audit(db: AsyncSession, principal: Principal, action: str, target: str, **meta: Any) -> None:
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action=action, target=target[:512], metadata_json=meta or {},
    ))
    await db.commit()


# =============================================================== Pydantic payloads
class OwnerLink(BaseModel):
    user_id: str = ""
    idp_id: str = ""
    external_id: str = ""
    entra_object_id: str = ""
    upn: str = ""


class OwnerIn(BaseModel):
    id: str = ""
    kind: str = "person"            # person | team | service
    display_name: str = ""
    email: str = ""
    source: str = "manual"
    link: dict[str, Any] = Field(default_factory=dict)
    members: list[str] = Field(default_factory=list)
    group_ref: dict[str, Any] = Field(default_factory=dict)
    delegate: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    tags: list[str] = Field(default_factory=list)


class DirectoryOwnerIn(BaseModel):
    """Materialize a people-picker hit into an owner record."""
    source: str = "manual"          # app_user | entra | oidc_group | rbac | manual
    kind: str = "person"
    display_name: str = ""
    email: str = ""
    link: dict[str, Any] = Field(default_factory=dict)
    group_ref: dict[str, Any] = Field(default_factory=dict)


class AssignmentIn(BaseModel):
    id: str = ""
    owner_id: str
    subject_kind: str               # mg | subscription | resource_group | resource | workload | architecture
    subject_id: str
    subject_name: str = ""
    subscription_id: str = ""
    resource_group: str = ""
    role: str = "technical"
    primary: bool = False
    source: str = "manual"
    confidence: float = 1.0
    notes: str = ""


class BulkAssignIn(BaseModel):
    owner_id: str
    role: str = "technical"
    primary: bool = False
    subjects: list[AssignmentIn] = Field(default_factory=list)


class TransferIn(BaseModel):
    from_owner_id: str
    to_owner_id: str


def _validate_owner(body: OwnerIn | DirectoryOwnerIn) -> None:
    if body.kind not in registry.OWNER_KINDS:
        raise HTTPException(400, f"kind must be one of {registry.OWNER_KINDS}")
    if not (body.display_name or body.email).strip():
        raise HTTPException(400, "An owner needs a display name or email.")


def _validate_assignment(body: AssignmentIn) -> None:
    if body.subject_kind not in registry.SUBJECT_KINDS:
        raise HTTPException(400, f"subject_kind must be one of {registry.SUBJECT_KINDS}")
    if body.role not in registry.OWNER_ROLES:
        raise HTTPException(400, f"role must be one of {registry.OWNER_ROLES}")
    if not body.subject_id.strip():
        raise HTTPException(400, "subject_id is required.")
    if not body.owner_id.strip():
        raise HTTPException(400, "owner_id is required.")


def _enrich_assignment(a: dict[str, Any], owners: dict[str, dict[str, Any]]) -> dict[str, Any]:
    o = owners.get(a.get("owner_id", ""))
    return {**a, "owner": {
        "id": o["id"], "display_name": o["display_name"], "email": o["email"], "kind": o["kind"],
    } if o else None}


# =============================================================== Owners directory
@router.get("/owners")
async def list_owners(principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    owners = registry.list_owners(principal.tenant_id)
    # Annotate each owner with how many subjects they're assigned to.
    assignments = registry.list_assignments(principal.tenant_id)
    counts: dict[str, int] = {}
    for a in assignments:
        counts[a["owner_id"]] = counts.get(a["owner_id"], 0) + 1
    for o in owners:
        o["assignment_count"] = counts.get(o["id"], 0)
    return {"owners": owners, "total": len(owners)}


@router.get("/owners/trash")
async def owners_trash(principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    return {"owners": registry.list_trashed_owners(principal.tenant_id)}


@router.post("/owners/trash/empty")
async def empty_owners_trash(principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n = registry.empty_owner_trash(principal.tenant_id)
    await _audit(db, principal, "ownership.owner.trash_empty", "owners", count=n)
    return {"purged": n}


@router.post("/owners")
async def upsert_owner(body: OwnerIn, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    _validate_owner(body)
    rec = body.model_dump()
    rec["created_by"] = principal.subject
    owner = registry.upsert_owner(principal.tenant_id, rec)
    await _audit(db, principal, "ownership.owner.upsert", owner["id"], name=owner["display_name"], kind=owner["kind"])
    return owner


@router.post("/owners/from-directory")
async def owner_from_directory(body: DirectoryOwnerIn, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Create an owner from a people-picker hit (SSO user / Entra principal / OIDC group)."""
    _validate_owner(body)
    rec = body.model_dump()
    rec["created_by"] = principal.subject
    owner = registry.upsert_owner(principal.tenant_id, rec)
    await _audit(db, principal, "ownership.owner.from_directory", owner["id"], source=body.source, name=owner["display_name"])
    return owner


@router.get("/owners/{owner_id}")
async def get_owner(owner_id: str, principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    owner = registry.get_owner(principal.tenant_id, owner_id)
    if owner is None:
        raise HTTPException(404, "Owner not found.")
    owner["assignments"] = registry.list_assignments(principal.tenant_id, owner_id=owner_id)
    return owner


@router.delete("/owners/{owner_id}")
async def delete_owner(owner_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not registry.delete_owner(principal.tenant_id, owner_id, actor=principal.subject):
        raise HTTPException(404, "Owner not found or already trashed.")
    await _audit(db, principal, "ownership.owner.delete", owner_id)
    return {"ok": True}


@router.post("/owners/{owner_id}/restore")
async def restore_owner(owner_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    owner = registry.restore_owner(principal.tenant_id, owner_id)
    if owner is None:
        raise HTTPException(404, "Owner not in trash.")
    await _audit(db, principal, "ownership.owner.restore", owner_id)
    return owner


@router.delete("/owners/{owner_id}/purge")
async def purge_owner(owner_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not registry.purge_owner(principal.tenant_id, owner_id):
        raise HTTPException(404, "Owner not in trash.")
    await _audit(db, principal, "ownership.owner.purge", owner_id)
    return {"ok": True}


# =============================================================== Assignments
@router.get("/assignments")
async def list_assignments(
    principal: Principal = Depends(read_dep),
    subject_kind: str = Query(default=""),
    subject_id: str = Query(default=""),
    owner_id: str = Query(default=""),
) -> dict[str, Any]:
    owners = {o["id"]: o for o in registry.list_owners(principal.tenant_id)}
    items = registry.list_assignments(
        principal.tenant_id, subject_kind=subject_kind, subject_id=subject_id, owner_id=owner_id,
    )
    return {"assignments": [_enrich_assignment(a, owners) for a in items], "total": len(items)}


@router.get("/assignments/trash")
async def assignments_trash(principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    return {"assignments": registry.list_trashed_assignments(principal.tenant_id)}


@router.post("/assignments/trash/empty")
async def empty_assignments_trash(principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n = registry.empty_assignment_trash(principal.tenant_id)
    await _audit(db, principal, "ownership.assignment.trash_empty", "assignments", count=n)
    return {"purged": n}


@router.post("/assignments")
async def upsert_assignment(body: AssignmentIn, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    _validate_assignment(body)
    if registry.get_owner(principal.tenant_id, body.owner_id) is None:
        raise HTTPException(400, "owner_id does not reference a known owner.")
    rec = body.model_dump()
    rec["created_by"] = principal.subject
    a = registry.upsert_assignment(principal.tenant_id, rec)
    await _audit(db, principal, "ownership.assignment.upsert", a["subject_id"],
                 owner_id=a["owner_id"], subject_kind=a["subject_kind"], role=a["role"])
    return a


@router.post("/assignments/bulk")
async def bulk_assign(body: BulkAssignIn, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if registry.get_owner(principal.tenant_id, body.owner_id) is None:
        raise HTTPException(400, "owner_id does not reference a known owner.")
    created: list[dict[str, Any]] = []
    for s in body.subjects:
        s.owner_id = body.owner_id
        s.role = body.role
        s.primary = body.primary
        _validate_assignment(s)
        rec = s.model_dump()
        rec["created_by"] = principal.subject
        created.append(registry.upsert_assignment(principal.tenant_id, rec))
    await _audit(db, principal, "ownership.assignment.bulk", body.owner_id, count=len(created))
    return {"created": created, "count": len(created)}


@router.post("/assignments/transfer")
async def transfer(body: TransferIn, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Reassign every subject owned by ``from_owner`` to ``to_owner`` (org reorg)."""
    if registry.get_owner(principal.tenant_id, body.to_owner_id) is None:
        raise HTTPException(400, "to_owner_id does not reference a known owner.")
    moved = 0
    for a in registry.list_assignments(principal.tenant_id, owner_id=body.from_owner_id):
        a["owner_id"] = body.to_owner_id
        registry.upsert_assignment(principal.tenant_id, a)
        moved += 1
    await _audit(db, principal, "ownership.assignment.transfer", body.from_owner_id, to=body.to_owner_id, moved=moved)
    return {"moved": moved}


@router.delete("/assignments/{assignment_id}")
async def delete_assignment(assignment_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not registry.delete_assignment(principal.tenant_id, assignment_id, actor=principal.subject):
        raise HTTPException(404, "Assignment not found or already trashed.")
    await _audit(db, principal, "ownership.assignment.delete", assignment_id)
    return {"ok": True}


@router.post("/assignments/{assignment_id}/restore")
async def restore_assignment(assignment_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    a = registry.restore_assignment(principal.tenant_id, assignment_id)
    if a is None:
        raise HTTPException(404, "Assignment not in trash.")
    await _audit(db, principal, "ownership.assignment.restore", assignment_id)
    return a


@router.delete("/assignments/{assignment_id}/purge")
async def purge_assignment(assignment_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not registry.purge_assignment(principal.tenant_id, assignment_id):
        raise HTTPException(404, "Assignment not in trash.")
    await _audit(db, principal, "ownership.assignment.purge", assignment_id)
    return {"ok": True}


# =============================================================== People-picker
@router.get("/directory/search")
async def directory_search(
    principal: Principal = Depends(read_dep),
    db: AsyncSession = Depends(get_db),
    q: str = Query(default=""),
    connection_id: str | None = Query(default=None),
    include_entra: bool = Query(default=True),
) -> dict[str, Any]:
    from app.core.azure_connections import resolve_connection

    connection = resolve_connection(connection_id)
    return await directory.search_directory(
        db, connection, principal.tenant_id, q, include_entra=include_entra,
    )


# =============================================================== Resolver
@router.get("/resolve")
async def resolve_one(
    principal: Principal = Depends(read_dep),
    subject_kind: str = Query(...),
    subject_id: str = Query(...),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
) -> dict[str, Any]:
    return resolve.resolve_owner(
        principal.tenant_id, subject_kind, subject_id,
        subscription_id=subscription_id, resource_group=resource_group,
    )


class ResolveBatchIn(BaseModel):
    subjects: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/resolve/batch")
async def resolve_batch(body: ResolveBatchIn, principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    ctx = resolve.build_context(principal.tenant_id)
    out: list[dict[str, Any]] = []
    for s in body.subjects:
        out.append(resolve.resolve_owner(
            principal.tenant_id, s.get("subject_kind", "resource"), s.get("subject_id", ""),
            tags=s.get("tags"), subscription_id=s.get("subscription_id", ""),
            resource_group=s.get("resource_group", ""), ctx=ctx,
        ))
    return {"results": out}


# =============================================================== Ownable subjects overview
def _arch_in_sub(arch: dict[str, Any], sub: str) -> bool:
    """True if any architecture node touches the given subscription guid."""
    for n in arch.get("nodes", []) or []:
        meta = n.get("meta") or n.get("data") or {}
        guid = resolve.sub_guid(
            str(meta.get("subscription_id", "")) or str(meta.get("arm_id", "")) or str(n.get("id", ""))
        )
        if guid and guid == sub:
            return True
    return False


def _scope_predicate(tenant_id: str, scope_kind: str, workload_id: str, subscription_id: str):
    """Return a predicate ``(subject_kind, subject_id, sub_hint="") -> bool`` selecting subjects
    in the chosen scope. ``tenant`` (or no concrete pick) selects everything.

    * workload scope → only that one workload subject.
    * subscription scope → workloads whose nodes touch the sub + architectures touching the sub
      + resource subjects whose ARM id is in the sub.
    """
    if scope_kind == "workload" and workload_id:
        wid = workload_id
        return lambda kind, sid, sub_hint="": kind == "workload" and sid == wid

    if scope_kind == "subscription" and subscription_id:
        sub = subscription_id.lower()
        wl_ids: set[str] = set()
        arch_ids: set[str] = set()
        try:
            from app.workloads.registry import list_workloads
            for wl in list_workloads():
                for node in wl.get("nodes", []) or []:
                    guid = resolve.sub_guid(str(node.get("subscription_id", ""))) or resolve.sub_guid(str(node.get("id", "")))
                    if guid and guid == sub:
                        wl_ids.add(wl["id"])
                        break
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.architectures.registry import list_architectures
            for a in list_architectures(tenant_id):
                if _arch_in_sub(a, sub):
                    arch_ids.add(a["id"])
        except Exception:  # noqa: BLE001
            pass

        def _pred(kind: str, sid: str, sub_hint: str = "") -> bool:
            if kind == "workload":
                return sid in wl_ids
            if kind == "architecture":
                return sid in arch_ids
            g = resolve.sub_guid(str(sid)) or (sub_hint or "").lower()
            return g == sub

        return _pred

    return lambda kind, sid, sub_hint="": True


@router.get("/subjects")
async def subjects(
    principal: Principal = Depends(read_dep),
    scope_kind: str = Query(default="tenant"),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
) -> dict[str, Any]:
    """Workloads + architectures with their resolved primary owner — the "what's owned"
    overview that anchors the Assignments tab. Filterable by the section scope."""
    ctx = resolve.build_context(principal.tenant_id)
    in_scope = _scope_predicate(principal.tenant_id, scope_kind, workload_id, subscription_id)
    out: list[dict[str, Any]] = []
    try:
        from app.workloads.registry import list_workloads
        for wl in list_workloads():
            if not in_scope("workload", wl["id"]):
                continue
            res = resolve.resolve_owner(principal.tenant_id, "workload", wl["id"], ctx=ctx)
            out.append({
                "subject_kind": "workload", "subject_id": wl["id"], "subject_name": wl.get("name", ""),
                "owners": res["owners"], "source": res["source"], "unowned": res["unowned"],
            })
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.architectures.registry import list_architectures
        for arch in list_architectures(principal.tenant_id):
            if not in_scope("architecture", arch["id"]):
                continue
            res = resolve.resolve_owner(principal.tenant_id, "architecture", arch["id"], ctx=ctx)
            out.append({
                "subject_kind": "architecture", "subject_id": arch["id"], "subject_name": arch.get("name", ""),
                "owners": res["owners"], "source": res["source"], "unowned": res["unowned"],
            })
    except Exception:  # noqa: BLE001
        pass
    owned = sum(1 for s in out if not s["unowned"])
    return {
        "subjects": out, "total": len(out), "owned": owned,
        "unowned": len(out) - owned,
    }


# =============================================================== Coverage + policy
_CACHE_TTL_S = 21600  # 6h


def _resolve_scope_inputs(scope_kind: str, workload_id: str, scope_id: str):
    """Return (scope_id, workload, connection) for the chosen scope."""
    from app.core.azure_connections import connection_for_workload, get_default_connection

    if scope_kind == "workload":
        from app.workloads.registry import get_workload

        workload = get_workload(workload_id)
        if workload is None:
            raise HTTPException(404, "Workload not found.")
        return workload_id, workload, connection_for_workload(workload)
    return scope_id, None, get_default_connection()


@router.get("/coverage")
async def get_coverage(
    principal: Principal = Depends(read_dep),
    scope_kind: str = Query(default="workload"),
    workload_id: str = Query(default=""),
    scope_id: str = Query(default=""),
) -> dict[str, Any]:
    """Read-cache-only: returns the last computed coverage snapshot, or a ``never_loaded``
    placeholder. Computing is done only by POST /refresh (no Azure scan on a page visit)."""
    sid = workload_id if scope_kind == "workload" else scope_id
    if not sid:
        return coverage.empty_snapshot(scope_kind, "", never_loaded=True)
    snap = cache.read_snapshot(principal.tenant_id, scope_kind, sid)
    if snap is None:
        return coverage.empty_snapshot(scope_kind, sid, never_loaded=True)
    return snap


@router.post("/refresh")
async def refresh_coverage(
    principal: Principal = Depends(read_dep),
    scope_kind: str = Query(default="workload"),
    workload_id: str = Query(default=""),
    scope_id: str = Query(default=""),
) -> dict[str, Any]:
    """Recompute coverage for the scope (read-only Azure scan), cache it, record a trend point."""
    import asyncio

    from app.core import coverage_trends

    sid, workload, connection = _resolve_scope_inputs(scope_kind, workload_id, scope_id)
    if not sid:
        raise HTTPException(400, "A workload_id or scope_id is required.")
    lock = cache.get_lock(principal.tenant_id, scope_kind, sid)
    async with lock:
        fresh = await asyncio.shield(coverage.collect_coverage(
            connection, scope_kind=scope_kind, scope_id=sid, workload=workload,
            tenant_id=principal.tenant_id,
        ))
        cache.write_snapshot(principal.tenant_id, scope_kind, sid, fresh)
        try:
            coverage_trends.record(
                "ownership", principal.tenant_id, scope_kind, sid,
                pct=fresh.get("coverage_pct"),
                extra={"owned": fresh["kpis"]["owned"], "total": fresh["kpis"]["total"]},
            )
        except Exception:  # noqa: BLE001
            pass
    return fresh


@router.get("/trend")
async def get_trend(
    principal: Principal = Depends(read_dep),
    scope_kind: str = Query(default="workload"),
    workload_id: str = Query(default=""),
    scope_id: str = Query(default=""),
) -> dict[str, Any]:
    from app.core import coverage_trends

    sid = workload_id if scope_kind == "workload" else scope_id
    return coverage_trends.trend("ownership", principal.tenant_id, scope_kind, sid)


# =============================================================== My Estate (owner cockpit)
def _estate_for_owner(tenant_id: str, owner: dict[str, Any]) -> dict[str, Any]:
    """A lightweight per-owner scorecard (no Azure scan): assignments grouped by subject
    kind + role, plus directory-linkage health."""
    assignments = registry.list_assignments(tenant_id, owner_id=owner["id"])
    by_kind: dict[str, int] = {}
    by_role: dict[str, int] = {}
    for a in assignments:
        by_kind[a["subject_kind"]] = by_kind.get(a["subject_kind"], 0) + 1
        by_role[a["role"]] = by_role.get(a["role"], 0) + 1
    return {
        "owner": {
            "id": owner["id"], "display_name": owner["display_name"], "email": owner["email"],
            "kind": owner["kind"], "source": owner["source"], "link": owner.get("link", {}),
        },
        "total": len(assignments),
        "by_kind": by_kind,
        "by_role": by_role,
        "assignments": assignments,
        "linked": owner["source"] != "manual",
    }


@router.get("/estate")
async def my_estate(
    principal: Principal = Depends(read_dep),
    owner_id: str = Query(default=""),
) -> dict[str, Any]:
    """Without ``owner_id``: the SIGNED-IN user's estate (owners linked to them by user id or
    email). With ``owner_id``: that owner's scorecard."""
    owners = registry.list_owners(principal.tenant_id)
    if owner_id:
        owner = next((o for o in owners if o["id"] == owner_id), None)
        if owner is None:
            raise HTTPException(404, "Owner not found.")
        return {"scope": "owner", "estates": [_estate_for_owner(principal.tenant_id, owner)]}
    email = (principal.email or "").lower()
    mine = [
        o for o in owners
        if (o.get("link", {}).get("user_id") == principal.subject)
        or (email and o.get("email", "").lower() == email)
    ]
    estates = [_estate_for_owner(principal.tenant_id, o) for o in mine]
    return {
        "scope": "me",
        "principal": {"email": principal.email, "display_name": principal.display_name},
        "estates": estates,
        "total_subjects": sum(e["total"] for e in estates),
        "matched_owners": len(estates),
    }


# =============================================================== Suggestions (AI/heuristic)
@router.get("/suggestions")
async def list_suggestions(
    principal: Principal = Depends(read_dep),
    scope_kind: str = Query(default="tenant"),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
) -> dict[str, Any]:
    """Heuristic owner suggestions for unowned subjects: owner TAGS on member resources
    (from cached inventory) + RBAC owners on the workload's subscriptions + orphan-tag
    promotions. Cache-only — no Azure scan. Filterable by the section scope."""
    from app.ownership import suggest

    tag_based = suggest.inventory_tag_suggestions(principal.tenant_id)
    rbac = suggest.suggest_for_tenant(principal.tenant_id)
    orphans = suggest.orphan_tag_suggestions(principal.tenant_id)
    in_scope = _scope_predicate(principal.tenant_id, scope_kind, workload_id, subscription_id)
    # De-dupe by (subject_id, candidate name) — a tag and RBAC signal for the same pairing
    # collapse to the higher-confidence one.
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for s in tag_based + rbac + orphans:
        if not in_scope(s.get("subject_kind", ""), s.get("subject_id", ""), s.get("subscription_id", "")):
            continue
        key = (s["subject_id"], (s["candidate"]["display_name"] or "").lower())
        if key not in seen or s["confidence"] > seen[key]["confidence"]:
            seen[key] = s
    items = sorted(seen.values(), key=lambda s: -s["confidence"])
    note = ""
    if not items:
        note = ("No owner signals found yet. Scan inventory (so resource owner-tags can be read) "
                "or run an RBAC access scan, then check back.")
    return {"suggestions": items, "total": len(items), "note": note}



class AcceptSuggestionIn(BaseModel):
    subject_kind: str
    subject_id: str
    subject_name: str = ""
    candidate: dict[str, Any]
    role: str = "technical"
    primary: bool = True


@router.post("/suggestions/accept")
async def accept_suggestion(body: AcceptSuggestionIn, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Materialise a suggestion: create (or reuse) the candidate owner, then assign it."""
    cand = body.candidate or {}
    name = str(cand.get("display_name") or "").strip()
    email = str(cand.get("email") or "").strip()
    if not (name or email):
        raise HTTPException(400, "Candidate needs a display name or email.")
    # Reuse an existing owner with the same Entra object id or email; else create one.
    link = cand.get("link") or {}
    existing = None
    for o in registry.list_owners(principal.tenant_id):
        same_oid = link.get("entra_object_id") and o.get("link", {}).get("entra_object_id") == link["entra_object_id"]
        same_email = email and o.get("email", "").lower() == email.lower()
        if same_oid or same_email:
            existing = o
            break
    if existing:
        owner = existing
    else:
        owner = registry.upsert_owner(principal.tenant_id, {
            "kind": cand.get("kind", "person"), "display_name": name or email,
            "email": email, "source": cand.get("source", "rbac"), "link": link,
            "created_by": principal.subject,
        })
    a = registry.upsert_assignment(principal.tenant_id, {
        "owner_id": owner["id"], "subject_kind": body.subject_kind, "subject_id": body.subject_id,
        "subject_name": body.subject_name, "role": body.role, "primary": body.primary,
        "source": "ai", "confidence": 1.0, "created_by": principal.subject,
    })
    await _audit(db, principal, "ownership.suggestion.accept", body.subject_id, owner_id=owner["id"])
    return {"owner": owner, "assignment": a}


# =============================================================== Attestation / recertification
_ATTEST_STALE_DAYS = 90


def _days_since(iso: str) -> int | None:
    if not iso:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - dt).total_seconds() // 86400)


@router.get("/attestation")
async def attestation(
    principal: Principal = Depends(read_dep),
    scope_kind: str = Query(default="tenant"),
    workload_id: str = Query(default=""),
    subscription_id: str = Query(default=""),
) -> dict[str, Any]:
    """Assignments grouped by attestation status: never attested / stale (>90d) / fresh.
    Filterable by the section scope.

    ``confidence decay``: the longer since an assignment was last confirmed, the less we
    trust it — surfaced so owners can recertify their estate."""
    owners = {o["id"]: o for o in registry.list_owners(principal.tenant_id)}
    in_scope = _scope_predicate(principal.tenant_id, scope_kind, workload_id, subscription_id)
    items: list[dict[str, Any]] = []
    never = stale = fresh = 0
    for a in registry.list_assignments(principal.tenant_id):
        if not in_scope(a.get("subject_kind", ""), a.get("subject_id", ""), a.get("subscription_id", "")):
            continue
        # Days since attestation, else since creation (never-attested baseline).
        att = a.get("attested_at", "")
        days = _days_since(att) if att else _days_since(a.get("created_at", ""))
        if not att:
            status = "never"
            never += 1
        elif (days or 0) >= _ATTEST_STALE_DAYS:
            status = "stale"
            stale += 1
        else:
            status = "fresh"
            fresh += 1
        o = owners.get(a["owner_id"])
        items.append({
            **a,
            "owner": {"id": o["id"], "display_name": o["display_name"], "email": o["email"], "kind": o["kind"]} if o else None,
            "attestation_status": status,
            "days_since": days,
        })
    items.sort(key=lambda x: {"never": 0, "stale": 1, "fresh": 2}[x["attestation_status"]])
    return {
        "items": items,
        "summary": {"total": len(items), "never": never, "stale": stale, "fresh": fresh, "stale_days": _ATTEST_STALE_DAYS},
    }


@router.post("/assignments/{assignment_id}/attest")
async def attest(assignment_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    a = registry.attest_assignment(principal.tenant_id, assignment_id, actor=principal.subject)
    if a is None:
        raise HTTPException(404, "Assignment not found.")
    await _audit(db, principal, "ownership.assignment.attest", a["subject_id"], owner_id=a["owner_id"])
    return a


# =============================================================== JML / leaver detection
@router.get("/leavers")
async def leavers(principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Owners linked to a disabled/removed app User (joiner-mover-leaver risk). Their estate
    needs reassignment. Cross-references owner.link.user_id against the users table."""
    from sqlalchemy import select

    from app.models.auth import User

    owners = registry.list_owners(principal.tenant_id)
    linked = {o["link"]["user_id"]: o for o in owners if o.get("link", {}).get("user_id")}
    at_risk: list[dict[str, Any]] = []
    if linked:
        rows = (await db.execute(select(User).where(User.id.in_(list(linked.keys()))))).scalars().all()
        found = {u.id: u for u in rows}
        for uid, owner in linked.items():
            u = found.get(uid)
            reason = ""
            if u is None:
                reason = "linked app user no longer exists"
            elif u.status != "active":
                reason = f"linked app user is {u.status}"
            if reason:
                assignments = registry.list_assignments(principal.tenant_id, owner_id=owner["id"])
                at_risk.append({
                    "owner": {"id": owner["id"], "display_name": owner["display_name"], "email": owner["email"]},
                    "reason": reason,
                    "orphaned_subjects": len(assignments),
                    "assignments": assignments,
                })
    return {"at_risk": at_risk, "count": len(at_risk)}


# =============================================================== Tag write-back (gated) + IaC
class TagWritebackIn(BaseModel):
    resource_id: str
    owner: str = ""
    owner_email: str = ""
    connection_id: str | None = None


@router.get("/writeback/status")
async def writeback_status(principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    from app.ownership import writeback

    return {"enabled": writeback.writeback_enabled()}


@router.post("/writeback/iac")
async def writeback_iac(body: TagWritebackIn, principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    """Generate review-only IaC (Bicep + Policy) that stamps the owner tag. No Azure write."""
    from app.ownership import writeback

    owner = body.owner
    if not owner:
        res = resolve.resolve_owner(principal.tenant_id, "resource", body.resource_id)
        if not res["unowned"] and res["owners"]:
            primary = next((o for o in res["owners"] if o["primary"]), res["owners"][0])
            owner = primary.get("display_name") or primary.get("email") or ""
    return {
        "bicep": writeback.bicep_for(body.resource_id, owner or "<owner>", body.owner_email),
        "policy": writeback.policy_for(owner or "<owner>"),
    }


@router.post("/writeback/apply")
async def writeback_apply(body: TagWritebackIn, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Gated Azure write: merge the owner tag onto a resource (preserves other tags)."""
    from app.core.azure_connections import resolve_connection
    from app.ownership import writeback

    owner = body.owner
    owner_email = body.owner_email
    if not owner:
        # Resolve the effective owner if the caller didn't pass one explicitly.
        res = resolve.resolve_owner(principal.tenant_id, "resource", body.resource_id)
        if not res["unowned"] and res["owners"]:
            primary = next((o for o in res["owners"] if o["primary"]), res["owners"][0])
            owner = primary.get("display_name") or primary.get("email") or ""
            owner_email = owner_email or primary.get("email", "")
    connection = resolve_connection(body.connection_id)
    result = await writeback.apply_owner_tag(
        connection, resource_id=body.resource_id, owner=owner, owner_email=owner_email,
    )
    if result["ok"]:
        await _audit(db, principal, "ownership.tag.writeback", body.resource_id, owner=owner)
    return result






"""Evidence Locker endpoints — create/list/detail/diff/attach/share/export snapshots.

Snapshots are immutable once written; their SHA-256 is recorded in the audit log and shown
in the UI, and re-verified on read. Every create/view/export is audited with the actor +
scope. RBAC: reads require ``evidence.read``; writes require ``evidence.write``."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.evidence import demo, registry, share
from app.evidence.diff import diff_snapshots
from app.models import AuditLog

router = APIRouter(prefix="/evidence", tags=["evidence"])
log = logging.getLogger("app.api.evidence")

read_dep = require_permission("evidence.read")
write_dep = require_permission("evidence.write")


async def _audit(db: AsyncSession, principal: Principal, action: str, target: str, **meta: Any) -> None:
    db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action=action, target=target[:512], metadata_json=meta or {}))


# --------------------------------------------------------------------------- create
class Scope(BaseModel):
    kind: str = "workload"  # workload | subscription | resources
    id: str = ""
    resource_ids: list[str] = Field(default_factory=list)


class CreateRequest(BaseModel):
    name: str = "Snapshot"
    scope: Scope
    included: list[str] = Field(default_factory=lambda: ["inventory", "findings"])
    retention_class: str = "standard"
    tags: list[str] = Field(default_factory=list)


@router.post("")
async def create(payload: CreateRequest, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from app.core.azure_connections import get_default_connection
    from app.evidence.collector import collect_content

    connection = get_default_connection()
    content = await collect_content(
        tenant_id=principal.tenant_id, scope=payload.scope.model_dump(),
        included=payload.included, connection=connection,
    )
    # Link any failing findings for locker filtering.
    finding_links: list[str] = []
    for run in (content.get("findings", {}) or {}).get("runs", []) or []:
        for f in run.get("findings", []) or []:
            if str(f.get("status", "")).lower() in ("fail", "failed", "error") and f.get("check_id"):
                finding_links.append(f["check_id"])
    meta = registry.create_snapshot(
        tenant_id=principal.tenant_id, name=payload.name, scope=payload.scope.model_dump(),
        included=payload.included, retention_class=payload.retention_class, tags=payload.tags,
        content=content, created_by=principal.subject, finding_links=sorted(set(finding_links)),
    )
    await _audit(db, principal, "evidence.create", meta["id"], sha256=meta["sha256"], scope=payload.scope.model_dump(), name=meta["name"])
    await db.commit()
    return {"ok": True, "snapshot": meta}


# --------------------------------------------------------------------------- list / detail
@router.get("")
async def list_snapshots(
    workload_id: str | None = Query(default=None),
    creator: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    finding: str | None = Query(default=None),
    retention_class: str | None = Query(default=None),
    principal: Principal = Depends(read_dep),
) -> dict[str, Any]:
    return {"snapshots": registry.list_snapshots(
        principal.tenant_id, workload_id=workload_id, creator=creator, tag=tag, finding=finding, retention_class=retention_class)}


# --------------------------------------------------------------------------- trash
# Registered BEFORE /{snapshot_id} so "trash" isn't matched as a snapshot id.
@router.get("/trash")
async def list_trash(principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    return {"snapshots": registry.list_trashed(principal.tenant_id)}


@router.post("/trash/empty")
async def empty_trash(principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n = registry.empty_trash(principal.tenant_id)
    await _audit(db, principal, "evidence.trash.empty", "trash", count=n)
    await db.commit()
    return {"ok": True, "purged": n}


@router.get("/{snapshot_id}")
async def detail(snapshot_id: str, principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    meta = registry.get_meta(principal.tenant_id, snapshot_id)
    if meta is None:
        return {"ok": False, "detail": "Snapshot not found."}
    verified = registry.verify_sha(meta)
    await _audit(db, principal, "evidence.view", snapshot_id, sha256=meta["sha256"], verified=verified)
    await db.commit()
    return {"ok": True, "snapshot": meta, "sha_verified": verified}


@router.get("/{snapshot_id}/content")
async def content(snapshot_id: str, tab: str | None = Query(default=None), principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    meta = registry.get_meta(principal.tenant_id, snapshot_id)
    if meta is None:
        return {"ok": False, "detail": "Snapshot not found."}
    full = registry.get_content(snapshot_id) or {}
    if tab and tab in full:
        return {"ok": True, "tab": tab, "content": full[tab]}
    return {"ok": True, "content": full}


# --------------------------------------------------------------------------- diff
class DiffRequest(BaseModel):
    a: str
    b: str
    type_filter: str = ""
    tag_filter: str = ""
    finding_filter: str = ""


@router.post("/diff")
async def diff(payload: DiffRequest, principal: Principal = Depends(read_dep)) -> dict[str, Any]:
    ma = registry.get_meta(principal.tenant_id, payload.a)
    mb = registry.get_meta(principal.tenant_id, payload.b)
    if ma is None or mb is None:
        return {"ok": False, "detail": "One or both snapshots not found."}
    ca = registry.get_content(payload.a) or {}
    cb = registry.get_content(payload.b) or {}
    result = diff_snapshots(ca, cb, type_filter=payload.type_filter, tag_filter=payload.tag_filter, finding_filter=payload.finding_filter)
    return {"ok": True, "a": {"id": ma["id"], "name": ma["name"], "created_at": ma["created_at"]},
            "b": {"id": mb["id"], "name": mb["name"], "created_at": mb["created_at"]}, "diff": result}


# --------------------------------------------------------------------------- attach
class AttachRequest(BaseModel):
    target: str = "ticket"  # ticket | rca
    connector_id: str = ""
    note: str = ""


@router.post("/{snapshot_id}/attach")
async def attach(snapshot_id: str, payload: AttachRequest, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    meta = registry.get_meta(principal.tenant_id, snapshot_id)
    if meta is None:
        return {"ok": False, "detail": "Snapshot not found."}
    body = (
        f"Evidence snapshot: {meta['name']}\n"
        f"SHA-256: {meta['sha256']}\n"
        f"Scope: {meta['scope'].get('kind')}:{meta['scope'].get('id')}\n"
        f"Captured: {meta['created_at']} by {meta['created_by']}\n"
        f"Sections: {', '.join(meta['included'])}\n"
        + (f"\n{payload.note}" if payload.note else "")
    )
    if payload.target == "ticket" and payload.connector_id:
        from app.assessments.tickets import create_ticket

        finding = {"severity": "info", "title": f"Evidence snapshot — {meta['name']}", "check_id": meta["id"],
                   "pillar": "Evidence", "description": body, "remediation": ""}
        result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=meta["scope"].get("id", ""))
        if result.get("ok"):
            registry.add_attachment(principal.tenant_id, snapshot_id, {"type": "ticket", "connector": result.get("connector_type", ""),
                                    "ticket_id": result.get("ticket_id", ""), "ticket_url": result.get("ticket_url", ""), "by": principal.subject})
            await _audit(db, principal, "evidence.attach", snapshot_id, attach_target="ticket", ticket=result.get("ticket_id", ""), sha256=meta["sha256"])
            await db.commit()
        return result
    # RCA draft attach (records the linkage; the SHA is carried in the body).
    registry.add_attachment(principal.tenant_id, snapshot_id, {"type": "rca", "note": payload.note, "by": principal.subject})
    await _audit(db, principal, "evidence.attach", snapshot_id, attach_target="rca", sha256=meta["sha256"])
    await db.commit()
    return {"ok": True, "attached": "rca", "body": body}


# --------------------------------------------------------------------------- share
class ShareRequest(BaseModel):
    ttl_days: int = 30


@router.post("/{snapshot_id}/share")
async def make_share(snapshot_id: str, payload: ShareRequest, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = share.create_share(principal.tenant_id, snapshot_id, actor=principal.subject, ttl_days=payload.ttl_days)
    if s is None:
        return {"ok": False, "detail": "Snapshot not found."}
    await _audit(db, principal, "evidence.share", snapshot_id, token=s["token"][:8] + "…")
    await db.commit()
    return {"ok": True, "share": s}


@router.get("/shared/{token}")
async def shared(token: str, principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    meta = share.resolve_share(token)
    if meta is None:
        return {"ok": False, "detail": "Share link invalid or expired."}
    await _audit(db, principal, "evidence.view_shared", meta["id"], sha256=meta.get("sha256", ""))
    await db.commit()
    return {"ok": True, "snapshot": meta, "content": registry.get_content(meta["id"]) or {}}


# --------------------------------------------------------------------------- export
@router.get("/{snapshot_id}/export")
async def export(snapshot_id: str, principal: Principal = Depends(read_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    meta = registry.get_meta(principal.tenant_id, snapshot_id)
    if meta is None:
        return {"ok": False, "detail": "Snapshot not found."}
    await _audit(db, principal, "evidence.export", snapshot_id, sha256=meta["sha256"])
    await db.commit()
    return {"ok": True, "bundle": {"meta": meta, "content": registry.get_content(snapshot_id) or {}, "sha_verified": registry.verify_sha(meta)}}


# --------------------------------------------------------------------------- delete / restore / purge
@router.delete("/{snapshot_id}")
async def delete_snapshot(snapshot_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Soft-delete: move the snapshot to Trash (content + SHA preserved)."""
    m = registry.soft_delete(principal.tenant_id, snapshot_id, actor=principal.subject)
    if m is None:
        return {"ok": False, "detail": "Snapshot not found."}
    await _audit(db, principal, "evidence.delete", snapshot_id, sha256=m.get("sha256"))
    await db.commit()
    return {"ok": True, "snapshot": m}


@router.post("/{snapshot_id}/restore")
async def restore_snapshot(snapshot_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = registry.restore(principal.tenant_id, snapshot_id)
    if m is None:
        return {"ok": False, "detail": "Trashed snapshot not found."}
    await _audit(db, principal, "evidence.restore", snapshot_id)
    await db.commit()
    return {"ok": True, "snapshot": m}


@router.delete("/{snapshot_id}/purge")
async def purge_snapshot(snapshot_id: str, principal: Principal = Depends(write_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Permanently delete a snapshot (metadata + content blob)."""
    ok = registry.purge(principal.tenant_id, snapshot_id)
    if ok:
        await _audit(db, principal, "evidence.purge", snapshot_id)
        await db.commit()
    return {"ok": ok}


# --------------------------------------------------------------------------- demo
@router.post("/demo/seed")
async def seed_demo_endpoint(principal: Principal = Depends(write_dep)) -> dict[str, Any]:
    ids = demo.seed_demo(tenant_id=principal.tenant_id)
    return {"ok": True, **ids}

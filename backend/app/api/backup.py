"""Admin endpoints for whole-tenant Backup & Restore.

Exposes the backup section catalog, a secret-free export ZIP (with a JSON manifest and
an optional nested chats archive), a dry-run import preview, and the import apply
(with skip|overwrite|merge conflict handling). All endpoints are admin-only and write
an audit-log row.
"""
from __future__ import annotations

import io
import json
import logging
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.backup import registry as backup
from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.models import AuditLog

router = APIRouter(prefix="/admin/backup", tags=["backup"])
logger = logging.getLogger("app.api.backup")


class ExportRequest(BaseModel):
    # When omitted/empty, export every available section.
    sections: list[str] = Field(default_factory=list)
    # When true, also include the export-only chats HTML archive in the ZIP.
    include_chats: bool = False


class ImportRequest(BaseModel):
    # The parsed JSON of a backup manifest.
    data: Any
    # Conflict handling for existing local entries.
    mode: str = "merge"  # skip | overwrite | merge
    # When omitted/empty, apply every section present in the manifest.
    sections: list[str] = Field(default_factory=list)


# Hard upper bounds on a single backup payload. A malicious admin (or a leaked
# admin session) could otherwise POST a 100 GB JSON or a zip-bomb-like manifest
# and exhaust container memory / disk. The numbers are generous for the largest
# realistic deployment but bounded.
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024     # 64 MB serialized
_MAX_MANIFEST_ITEMS = 200_000              # total elements across all sections
_MAX_MANIFEST_DEPTH = 20                   # JSON nesting depth


def _enforce_manifest_limits(data: Any) -> None:
    """Reject obviously oversized or pathologically nested import payloads.

    Runs in O(n) over the materialized JSON tree so it can't itself become a DoS.
    Raised as ``HTTPException(413)`` so the client sees a clear "payload too large".
    """
    try:
        serialized = json.dumps(data, default=str)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Backup payload is not JSON-serializable: {exc}") from exc
    if len(serialized) > _MAX_MANIFEST_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Backup payload too large ({len(serialized) // (1024 * 1024)} MB > {_MAX_MANIFEST_BYTES // (1024 * 1024)} MB).",
        )

    items = 0
    max_depth_seen = 0
    # Iterative traversal so a deeply-nested manifest can't blow the recursion stack.
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        max_depth_seen = max(max_depth_seen, depth)
        if depth > _MAX_MANIFEST_DEPTH:
            raise HTTPException(
                status_code=400,
                detail=f"Backup payload nesting too deep (> {_MAX_MANIFEST_DEPTH} levels).",
            )
        if isinstance(node, dict):
            items += len(node)
            if items > _MAX_MANIFEST_ITEMS:
                raise HTTPException(
                    status_code=413,
                    detail=f"Backup payload has too many items (> {_MAX_MANIFEST_ITEMS}).",
                )
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
        elif isinstance(node, list):
            items += len(node)
            if items > _MAX_MANIFEST_ITEMS:
                raise HTTPException(
                    status_code=413,
                    detail=f"Backup payload has too many items (> {_MAX_MANIFEST_ITEMS}).",
                )
            for v in node:
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))


@router.get("/sections")
async def list_sections_endpoint(
    principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    """Catalog of backup-able sections with current counts."""
    return {"sections": await backup.list_sections(principal.tenant_id, db)}


@router.post("/export")
async def export_endpoint(
    payload: ExportRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Build a secret-free backup ZIP for the chosen sections (download in the UI)."""
    manifest = await backup.build_backup(payload.sections or None, principal.tenant_id, db)
    chats_bytes = await backup.build_chat_archive(principal.tenant_id, db) if payload.include_chats else None
    archive = io.BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        if chats_bytes is not None:
            zf.writestr("chats.zip", chats_bytes)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="backup.export",
            target=None,
            metadata_json={
                "sections": manifest["meta"]["sections"],
                "secrets_redacted": len(manifest["meta"]["secrets_required"]),
            },
        )
    )
    await db.commit()
    filename = f"azsupagent-backup-{principal.tenant_id}-{manifest['exported_at'][:19].replace(':', '-')}.zip"
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import/preview")
async def import_preview_endpoint(
    payload: ImportRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Dry-run an import: report per-section create/update/skip counts. Writes nothing."""
    _enforce_manifest_limits(payload.data)
    try:
        return await backup.preview_import(payload.data, principal.tenant_id, payload.mode, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import")
async def import_endpoint(
    payload: ImportRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Apply a backup with the chosen conflict mode. DB writes commit atomically; file
    sections are written atomically per file."""
    _enforce_manifest_limits(payload.data)
    if payload.mode not in backup.CONFLICT_MODES:
        raise HTTPException(status_code=400, detail=f"Unknown conflict mode '{payload.mode}'.")
    try:
        result = await backup.apply_import(
            payload.data, principal.tenant_id, payload.mode, db, payload.sections or None
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="backup.import",
            target=None,
            metadata_json={"mode": result["mode"], "sections": [s["id"] for s in result["sections"]]},
        )
    )
    await db.commit()
    return result

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

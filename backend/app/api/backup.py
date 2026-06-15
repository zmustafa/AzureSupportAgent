"""Admin endpoints for whole-tenant Backup & Restore.

Exposes the backup section catalog, a secret-free export (downloadable JSON manifest),
a dry-run import preview, and the import apply (with skip|overwrite|merge conflict
handling). All endpoints are admin-only and write an audit-log row.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
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
    """Build a secret-free backup manifest for the chosen sections (download in the UI)."""
    manifest = await backup.build_backup(payload.sections or None, principal.tenant_id, db)
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
    return manifest


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

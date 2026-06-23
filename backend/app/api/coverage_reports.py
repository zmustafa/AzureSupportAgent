"""Combined estate coverage report.

Stitches the latest Monitoring (AMBA), Telemetry and Backup & DR coverage snapshots for one
scope into a single branded PDF — the natural companion to the dashboard's Coverage row.
Admin-gated. Renders the latest cached snapshots (no forced re-scan)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.core.security import Principal, require_permission

router = APIRouter(prefix="/coverage-reports", tags=["coverage-reports"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("coverage.read")


@router.get("/estate/pdf")
async def estate_pdf(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> Any:
    from app.api import amba, backupdr, telemetry
    from app.core import coverage_trends
    from app.core.coverage_pdf import build_estate_pdf
    from app.core.coverage_report_helpers import safe_filename

    tenant_id = principal.tenant_id or "default"
    items: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    scope_name = ""
    for feature, mod in (("amba", amba), ("telemetry", telemetry), ("backupdr", backupdr)):
        scope_kind, scope_id, snap = await mod.latest_snapshot(principal, workload_id, subscription_id)
        trend = coverage_trends.trend(feature, tenant_id, scope_kind, scope_id)
        items.append((feature, snap, trend))
        scope_name = scope_name or snap.get("scope_name") or scope_id

    pdf = await run_in_threadpool(build_estate_pdf, scope_name or "Estate", items)
    fname = f"estate-coverage-{safe_filename(scope_name)}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )

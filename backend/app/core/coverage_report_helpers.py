"""Shared request helpers for the coverage report endpoints.

The three coverage routers (``amba`` / ``telemetry`` / ``backupdr``) and the combined estate
report router all turn a *latest cached snapshot* into either a branded PDF download or an
Evidence Locker capture. That flow lives here so it is written once.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response


def safe_filename(text: str, *, fallback: str = "coverage") -> str:
    """A filesystem/Content-Disposition-safe slug derived from a scope name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-")
    return slug[:80] or fallback


async def coverage_pdf_response(
    feature: str, snap: dict[str, Any], *, tenant_id: str, scope_kind: str, scope_id: str
) -> Response:
    """Render one feature's latest snapshot to a PDF download response."""
    from app.core import coverage_trends
    from app.core.coverage_pdf import build_coverage_pdf

    trend = coverage_trends.trend(feature, tenant_id, scope_kind, scope_id)
    pdf = await run_in_threadpool(build_coverage_pdf, feature, snap, trend)
    scope_name = snap.get("scope_name") or scope_id or "scope"
    fname = f"{feature}-coverage-{safe_filename(scope_name)}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def capture_coverage_evidence(
    feature: str, snap: dict[str, Any], *, tenant_id: str, actor: str
) -> dict[str, Any]:
    """Persist the latest snapshot as an immutable Evidence Locker snapshot; returns its meta."""
    from app.core.coverage_pdf import build_evidence_content
    from app.evidence import registry

    name, scope, included, tags, content = build_evidence_content(feature, snap)
    return registry.create_snapshot(
        tenant_id=tenant_id,
        name=name,
        scope=scope,
        included=included,
        retention_class="standard",
        tags=tags,
        content=content,
        created_by=actor or "system",
        demo=bool(snap.get("demo")),
    )

"""Lightweight application metadata + health summary endpoints.

* ``GET /api/meta`` — name, version, environment, and a few capability flags for any signed-in
  user (powers the in-app About dialog and Welcome screen).
* ``GET /api/meta/status`` — an admin-only health summary (DB reachable, AI configured, Azure
  connected, scheduler) for the in-app System Status panel.

Kept deliberately small and dependency-light; every probe is best-effort and never raises.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import Principal, get_principal, require_admin

router = APIRouter(prefix="/meta", tags=["meta"])
log = logging.getLogger("app.api.meta")

# Process start time, for a coarse uptime in the status panel.
_STARTED_AT = time.time()


def _version() -> str:
    return os.getenv("APP_VERSION") or "dev"


def _release() -> str:
    """Sequential release number (git commit count), baked via APP_RELEASE. Empty locally."""
    return os.getenv("APP_RELEASE") or ""


@router.get("")
async def meta(_: Principal = Depends(get_principal)) -> dict[str, Any]:
    """App identity shown in About / Welcome. Available to any authenticated user."""
    return {
        "name": "Azure Support Agent",
        "version": _version(),
        "release": _release(),
        "environment": get_settings().environment,
    }


def _ai_configured() -> bool:
    try:
        from app.core.llm_config import load_config

        cfg = load_config()
        for name, prov in (cfg.get("providers") or {}).items():
            if prov.get("disabled"):
                continue
            # A provider counts as configured if it carries a credential or is keyless-OK.
            if prov.get("has_key") or prov.get("api_key") or prov.get("base_url"):
                return True
        return bool(cfg.get("active_provider"))
    except Exception:  # noqa: BLE001 — best-effort probe, never raise
        return False


def _azure_connected() -> int:
    try:
        from app.core.azure_connections import list_connections

        return len([c for c in list_connections() if not c.get("disabled")])
    except Exception:  # noqa: BLE001
        return 0


@router.get("/status")
async def status(
    _: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Best-effort health summary for the System Status panel (admin only)."""
    db_ok = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False

    connections = _azure_connected()
    return {
        "name": "Azure Support Agent",
        "version": _version(),
        "environment": get_settings().environment,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "checks": {
            "database": {"ok": db_ok, "label": "Database"},
            "ai_provider": {"ok": _ai_configured(), "label": "AI provider configured"},
            "azure_connection": {"ok": connections > 0, "count": connections, "label": "Azure tenant connected"},
        },
    }

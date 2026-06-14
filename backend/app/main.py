"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin,
    admin_demo,
    amba,
    architectures,
    assessments,
    auth,
    automations,
    backupdr,
    charts,
    chats,
    connections,
    connectors,
    dnsdebug,
    evidence,
    identity,
    inventory,
    netcheck,
    notifications,
    playbooks,
    policy,
    perfprofile,
    radar,
    telemetry,
    teleintel,
    users,
    vms,
    workbooks,
    workloads,
)
from app.core.config import get_settings
from app.core.db import ensure_schema
from app.core.llm_config import get_active
from app.core.security import Principal, get_principal

settings = get_settings()

# Configure root logging once so the app's structured warnings (failed title/suggestion
# generation, discovery timeouts, turn errors) are actually surfaced.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Azure Support Agent", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    # Keep the local SQLite schema in sync (creates tables + late-added columns).
    await ensure_schema()
    # Bootstrap auth: seed system roles + the initial admin/admin account.
    from app.auth.service import seed_admin
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        await seed_admin(db)
    # Seed curated starter workbooks on first run.
    from app.workbooks.registry import seed_if_empty

    try:
        seeded = seed_if_empty()
        if seeded:
            logging.getLogger("app.main").info("Seeded %d starter workbooks", seeded)
    except Exception:  # noqa: BLE001
        logging.getLogger("app.main").warning("Starter workbook seed failed", exc_info=True)
    # Seed sample custom assessment controls (one per pillar) on first run.
    from app.assessments.custom_checks import seed_sample_checks

    try:
        added = seed_sample_checks()
        if added:
            logging.getLogger("app.main").info("Seeded %d sample custom controls", added)
    except Exception:  # noqa: BLE001
        logging.getLogger("app.main").warning("Sample custom control seed failed", exc_info=True)
    # Fail any assessment runs orphaned at 'queued'/'running' by a previous process — an
    # in-flight run can't survive a restart, so they must not appear perpetually in progress.
    from app.assessments.runner import reap_orphaned_runs

    try:
        reaped = await reap_orphaned_runs()
        if reaped:
            logging.getLogger("app.main").info("Reaped %d orphaned assessment run(s)", reaped)
    except Exception:  # noqa: BLE001
        logging.getLogger("app.main").warning("Assessment orphan reaper failed", exc_info=True)
    # Backfill sub-agent categories (idempotent) so existing agents are grouped.
    from app.automations.agents import seed_categories

    try:
        catn = seed_categories()
        if catn:
            logging.getLogger("app.main").info("Categorized %d sub agent(s)", catn)
    except Exception:  # noqa: BLE001
        logging.getLogger("app.main").warning("Sub agent categorization failed", exc_info=True)
    # Start the automations scheduler (recurring tasks).
    from app.automations.scheduler import scheduler

    scheduler.start()

    # Start the Monitor availability sampler (web/TCP ping history for dashboards).
    from app.monitor.sampler import sampler as monitor_sampler

    monitor_sampler.start()

    # Warm the Azure MCP tool catalog in the background so the FIRST chat message
    # doesn't pay the `npx @azure/mcp` cold-start (node spawn + package resolve),
    # which the orchestrator awaits before streaming any token. Non-blocking.
    import asyncio

    from app.mcp.client import warm_tool_catalog

    asyncio.create_task(warm_tool_catalog())


@app.on_event("shutdown")
async def _shutdown() -> None:
    from app.automations.scheduler import scheduler

    await scheduler.stop()
    from app.monitor.sampler import sampler as monitor_sampler

    await monitor_sampler.stop()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# All application endpoints live under /api so the SPA can own every other path
# (client-side routes like /inventory, /admin, /policy collide with API prefixes
# otherwise). The frontend talks to this via VITE_API_BASE=/api in production.
api = APIRouter(prefix="/api")

api.include_router(auth.router)
api.include_router(users.router)
api.include_router(chats.router)
api.include_router(charts.router)
api.include_router(admin.router)
api.include_router(admin_demo.router)
api.include_router(connections.router)
api.include_router(connectors.router)
api.include_router(automations.router)
api.include_router(workbooks.router)
api.include_router(playbooks.router)
api.include_router(notifications.router)
api.include_router(workloads.router)
api.include_router(assessments.router)
api.include_router(architectures.router)
api.include_router(policy.router)
api.include_router(inventory.router)
api.include_router(identity.router)
api.include_router(amba.router)
api.include_router(telemetry.router)
api.include_router(backupdr.router)
api.include_router(netcheck.router)
api.include_router(dnsdebug.router)
api.include_router(evidence.router)
api.include_router(radar.router)
api.include_router(teleintel.router)
api.include_router(perfprofile.router)
api.include_router(vms.router)


# Health/readiness probes stay at the root (no auth, no /api) for Container Apps.
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready"}


@api.get("/llm/active")
async def llm_active():
    """Currently active AI provider + model (non-sensitive) for display in the UI."""
    cfg = get_active()
    return {"provider": cfg.get("provider", ""), "model": cfg.get("model", "")}


@api.get("/me")
async def me(principal: Principal = Depends(get_principal)):
    return {
        "subject": principal.subject,
        "email": principal.email,
        "tenant_id": principal.tenant_id,
        "role": principal.role,
    }


@api.get("/azure/connections")
async def azure_connections(_: Principal = Depends(get_principal)):
    """Enabled Azure connections (tenants) for the composer's tenant selector.

    Available to any authenticated user; returns only non-sensitive fields needed to
    pick a tenant for a prompt. Secrets are never included."""
    from app.core.azure_connections import public_connections

    conns = [c for c in public_connections() if not c.get("disabled")]
    return {
        "connections": [
            {
                "id": c["id"],
                "display_name": c["display_name"],
                "tenant_id": c["tenant_id"],
                "is_default": c["is_default"],
                "status": c["status"],
                "read_only": c["read_only"],
            }
            for c in conns
        ]
    }


# Register the API under /api.
app.include_router(api)


# --------------------------------------------------------------------------- static SPA
# In the single-container build the React app is built into app/static. Serve its assets
# and fall back to index.html for any non-API path so client-side routing (deep links,
# refresh) works. When the bundle is absent (pure local dev with Vite on :5173), these
# routes are simply not registered.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    # Hashed build assets (JS/CSS) + any files under /assets.
    _assets_dir = _STATIC_DIR / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    _index_file = _STATIC_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    async def _spa_root() -> FileResponse:
        return FileResponse(str(_index_file))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str) -> FileResponse:
        # Serve a real static file when one exists (favicon, agent-icons, etc.);
        # otherwise hand back index.html so the SPA router renders the route.
        candidate = (_STATIC_DIR / full_path).resolve()
        if candidate.is_file() and str(candidate).startswith(str(_STATIC_DIR)):
            return FileResponse(str(candidate))
        return FileResponse(str(_index_file))


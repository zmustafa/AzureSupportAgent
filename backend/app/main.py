"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin,
    admin_demo,
    amba,
    architectures,
    assessments,
    auth,
    automations,
    backup,
    backupdr,
    changeexplorer,
    charts,
    chats,
    connections,
    connectors,
    coverage_reports,
    dnsdebug,
    evidence,
    graph,
    identity,
    inventory,
    meta,
    missions,
    netcheck,
    notifications,
    playbooks,
    policy,
    perfprofile,
    radar,
    rbac,
    reservations,
    tagintel,
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

import os  # noqa: E402

# --------------------------------------------------------------- Production safety
#
# In production (`environment != "local"`) we disable the interactive OpenAPI UI to avoid
# information disclosure (the full schema is free reconnaissance for an attacker). Local
# dev keeps `/docs` + `/redoc` for convenience. Override with OPENAPI_PUBLIC=1 for an
# internal-only deployment that genuinely wants the docs.
#
# FastAPI registers the `/docs` / `/redoc` / `/openapi.json` routes at CONSTRUCTION time,
# so the only reliable way to remove them is to pass the URLs as None to the constructor.
# That's why the app is instantiated exactly ONCE, here, after we've resolved whether docs
# should be enabled. A startup log line records the decision so operators can verify it in
# the running container (the env var must be set on the active revision).
_OPENAPI_PUBLIC = os.getenv("OPENAPI_PUBLIC", "").lower() in ("1", "true", "yes")
_DOCS_ENABLED = (settings.environment == "local") or _OPENAPI_PUBLIC
app = FastAPI(
    title="Azure Support Agent",
    version="0.1.0",
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)
logging.getLogger("app.main").info(
    "Startup: environment=%s, openapi_docs_enabled=%s", settings.environment, _DOCS_ENABLED
)


@app.exception_handler(Exception)
async def _global_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Catch-all so an unexpected error never leaks a stack trace, file path, or
    library version to the client. We log the full traceback server-side so
    operators still have it for debugging.
    """
    logging.getLogger("app.main").exception("Unhandled exception", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. The error has been logged."},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """Preserve intentional HTTPExceptions (auth, validation, 404, etc.) verbatim;
    don't dress them as generic 500s. Mirrors FastAPI's default but plays well with
    the ``Exception`` handler above (which would otherwise swallow them).
    """
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=dict(headers) if headers else None,
    )


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
    # Seed the curated starter sub-agents (a full Azure troubleshooting team) on first run,
    # then backfill categories. Both idempotent (seed only when the registry is empty).
    from app.automations.agents import seed_categories, seed_if_empty

    try:
        agn = seed_if_empty()
        if agn:
            logging.getLogger("app.main").info("Seeded %d starter sub agent(s)", agn)
    except Exception:  # noqa: BLE001
        logging.getLogger("app.main").warning("Starter sub agent seed failed", exc_info=True)

    try:
        catn = seed_categories()
        if catn:
            logging.getLogger("app.main").info("Categorized %d sub agent(s)", catn)
    except Exception:  # noqa: BLE001
        logging.getLogger("app.main").warning("Sub agent categorization failed", exc_info=True)

    # Prune orphaned architecture memories — memory records whose architecture was
    # hard-deleted (purged) before the cascade existed. These are unreachable from the UI
    # (every memory endpoint 404s once the architecture is gone), so clean them up once.
    # Trashed (restorable) architectures keep their memory.
    from app.architectures.memory import prune_orphans
    from app.architectures.registry import all_architecture_ids

    try:
        pruned = prune_orphans(all_architecture_ids())
        if pruned:
            logging.getLogger("app.main").info("Pruned %d orphaned architecture memor(ies)", pruned)
    except Exception:  # noqa: BLE001
        logging.getLogger("app.main").warning("Orphaned memory prune failed", exc_info=True)

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
    # Explicit method + header allowlist instead of wildcards. Combined with
    # `allow_credentials=True`, wildcards are dangerous (any header from the
    # configured origin including bespoke ones the backend doesn't expect can
    # land); spelling out the small set of methods/headers we actually use is
    # required for SOC2/PCI baselines.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Accept",
        "Accept-Encoding",
        "Authorization",
        "Content-Type",
        "Cache-Control",
        "X-Requested-With",
    ],
    max_age=3600,
)

# Compress large JSON payloads on the wire. SSE clients (EventSource API) always
# send `Accept: text/event-stream`; we bypass GZipMiddleware for those requests
# so per-event delivery latency is preserved (gzip's internal buffer would
# otherwise batch/delay events).
class _SafeGZip:
    def __init__(self, app, minimum_size: int = 1024) -> None:
        self._raw_app = app
        self._gz_app = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope.get("type") != "http":
            await self._raw_app(scope, receive, send)
            return
        is_sse = False
        for name, value in scope.get("headers", []):
            if name == b"accept" and b"text/event-stream" in value.lower():
                is_sse = True
                break
        if is_sse:
            await self._raw_app(scope, receive, send)
            return
        await self._gz_app(scope, receive, send)


app.add_middleware(_SafeGZip, minimum_size=1024)


# --------------------------------------------------------------- Security headers
#
# Defense-in-depth response headers required by SOC2/ISO27001/PCI baselines. Some
# headers (HSTS, COOP/COEP) are only meaningful when served over HTTPS — they're
# emitted only when `cookie_secure=true` so local HTTP development isn't broken by
# the browser refusing future plaintext connections.
class _SecurityHeaders:
    """ASGI middleware that adds CSP, X-Frame-Options, X-Content-Type-Options,
    Referrer-Policy, Permissions-Policy, and (in HTTPS deployments) HSTS to every
    response. SSE responses get the same treatment — none of these headers affect
    chunked-event delivery.
    """

    def __init__(self, app) -> None:
        self._app = app
        cfg = get_settings()
        # CSP: lock script/style/img/connect to same-origin + data: for inline
        # images. `'unsafe-inline'` is allowed for style only because Tailwind's
        # generated utility classes are emitted via <style> tags. The SPA uses no
        # inline scripts, and connect-src is same-origin since the API is mounted
        # under /api on the same domain.
        self._headers = [
            (b"X-Content-Type-Options", b"nosniff"),
            (b"X-Frame-Options", b"DENY"),
            (b"Referrer-Policy", b"strict-origin-when-cross-origin"),
            (b"Permissions-Policy", b"geolocation=(), microphone=(), camera=(), payment=(), usb=()"),
            (
                b"Content-Security-Policy",
                (
                    b"default-src 'self'; "
                    b"script-src 'self' 'wasm-unsafe-eval'; "
                    b"style-src 'self' 'unsafe-inline'; "
                    b"img-src 'self' data: blob: https:; "
                    b"font-src 'self' data:; "
                    b"connect-src 'self'; "
                    b"worker-src 'self' blob:; "
                    b"frame-ancestors 'none'; "
                    b"base-uri 'self'; "
                    b"form-action 'self'"
                ),
            ),
        ]
        if cfg.cookie_secure:
            # Browsers cache HSTS aggressively; only enable it when we know we're
            # always serving over HTTPS.
            self._headers.append(
                (b"Strict-Transport-Security", b"max-age=31536000; includeSubDomains")
            )

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        extra = self._headers

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name for name, _ in headers}
                for name, value in extra:
                    if name.lower() not in existing:
                        headers.append((name, value))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, _send)


app.add_middleware(_SecurityHeaders)


# --------------------------------------------------------------- CSRF / cross-origin guard
#
# Cookie auth means the browser attaches the session automatically, so a state-changing
# request forged by another site would otherwise carry the victim's credentials. SameSite
# helps, but collapses for deployments that must run `cookie_samesite=none` (cross-site).
# This middleware adds an explicit Origin / Sec-Fetch-Site check for unsafe methods,
# rejecting cross-origin writes regardless of the SameSite mode.
class _CsrfGuard:
    """Reject cross-origin state-changing requests (POST/PUT/PATCH/DELETE).

    A request is allowed when:
      * the method is safe (GET/HEAD/OPTIONS/TRACE), or
      * it targets the SAML ACS (an IdP-posted cross-site form, protected instead by the
        signed assertion + single-use InResponseTo cookie), or
      * its Origin is same-origin / on the configured allowlist, or
      * it carries no Origin and is not flagged cross-site (non-browser client such as
        curl or the test suite — these have no ambient cookies to abuse).
    """

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self._app = app
        cfg = get_settings()
        self._allow = {o for o in (cfg.frontend_origin, cfg.public_base_url) if o}

    def _origin_ok(self, origin: str, host: str) -> bool:
        if origin in self._allow:
            return True
        from urllib.parse import urlparse

        try:
            return urlparse(origin).netloc == host
        except ValueError:
            return False

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if method in ("GET", "HEAD", "OPTIONS", "TRACE") or (
            path.startswith("/api/auth/saml/") and path.endswith("/acs")
        ):
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        origin = headers.get("origin", "")
        host = headers.get("host", "")
        sec_fetch_site = headers.get("sec-fetch-site", "")
        blocked = False
        if origin:
            blocked = not self._origin_ok(origin, host)
        elif sec_fetch_site == "cross-site":
            blocked = True
        if blocked:
            resp = JSONResponse(
                status_code=403, content={"detail": "Cross-origin request blocked."}
            )
            await resp(scope, receive, send)
            return
        await self._app(scope, receive, send)


app.add_middleware(_CsrfGuard)

# All application endpoints live under /api so the SPA can own every other path
# (client-side routes like /inventory, /admin, /policy collide with API prefixes
# otherwise). The frontend talks to this via VITE_API_BASE=/api in production.
api = APIRouter(prefix="/api")

api.include_router(auth.router)
api.include_router(users.router)
api.include_router(meta.router)
api.include_router(chats.router)
api.include_router(charts.router)
api.include_router(admin.router)
api.include_router(admin_demo.router)
api.include_router(connections.router)
api.include_router(connectors.router)
api.include_router(automations.router)
api.include_router(backup.router)
api.include_router(workbooks.router)
api.include_router(playbooks.router)
api.include_router(notifications.router)
api.include_router(workloads.router)
api.include_router(assessments.router)
api.include_router(architectures.router)
api.include_router(policy.router)
api.include_router(inventory.router)
api.include_router(tagintel.router)
api.include_router(changeexplorer.router)
api.include_router(identity.router)
api.include_router(amba.router)
api.include_router(telemetry.router)
api.include_router(backupdr.router)
api.include_router(coverage_reports.router)
api.include_router(netcheck.router)
api.include_router(dnsdebug.router)
api.include_router(evidence.router)
api.include_router(graph.router)
api.include_router(radar.router)
api.include_router(teleintel.router)
api.include_router(perfprofile.router)
api.include_router(missions.router)
api.include_router(rbac.router)
api.include_router(reservations.router)
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


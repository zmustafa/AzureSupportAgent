"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin,
    admin_demo,
    alert_analysis,
    alerts_manager,
    amba,
    architectures,
    assessments,
    auth,
    automations,
    backup,
    backup_manager,
    backupdr,
    capability,
    cases,
    changeexplorer,
    charts,
    chats,
    connections,
    connectors,
    coverage_reports,
    dnsdebug,
    entra,
    evidence,
    firewall,
    fmea,
    graph,
    iam,
    identity,
    insights,
    inventory,
    meta,
    missions,
    netcheck,
    notifications,
    ownership,
    playbooks,
    policy,
    perfprofile,
    quota,
    radar,
    reservations,
    resiliency,
    tagintel,
    telemetry,
    teleintel,
    users,
    vms,
    workbooks,
    work_batches,
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


# Handle for the background MCP tool-catalog warmup started at startup, so shutdown can
# cancel it instead of waiting for it (see _startup / _shutdown).
_warm_task = None


async def _ensure_schema_resilient(attempts: int = 4) -> None:
    """`ensure_schema` with backoff, and a last resort that does not kill the container."""
    import asyncio

    log = logging.getLogger("app.main")
    for attempt in range(1, attempts + 1):
        try:
            await ensure_schema()
            return
        except Exception:  # noqa: BLE001
            if attempt == attempts:
                # Boot anyway. A replica that cannot migrate can still serve reads against a
                # schema another replica just finished, and an exiting container serves nothing
                # at all while it restart-loops.
                log.error("Schema sync failed after %d attempts; continuing without it",
                          attempts, exc_info=True)
                return
            delay = min(8.0, 0.5 * 2 ** (attempt - 1))
            log.warning("Schema sync attempt %d/%d failed; retrying in %.1fs",
                        attempt, attempts, delay, exc_info=True)
            await asyncio.sleep(delay)


@app.on_event("startup")
async def _startup() -> None:
    # Keep the schema in sync (creates tables + late-added columns). Retried rather than
    # allowed to kill the process: this is DDL every replica runs at boot, and losing a race
    # with another replica used to raise straight out of the lifespan — "Application startup
    # failed. Exiting." — which the platform answered by restarting into the same race. The
    # loser of a deadlock has usually had its work done for it by the winner.
    await _ensure_schema_resilient()
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
    # Same orphan problem for Mission Control: a mission left 'queued'/'running' by a previous
    # process can't resume, and the board's reconnect-on-mount would otherwise try to follow its
    # dead stream and surface a spurious "Mission not found." Reap them to a terminal state.
    from app.missions.orchestrator import reap_orphaned_missions

    try:
        reaped_missions = await reap_orphaned_missions()
        if reaped_missions:
            logging.getLogger("app.main").info("Reaped %d orphaned mission(s)", reaped_missions)
    except Exception:  # noqa: BLE001
        logging.getLogger("app.main").warning("Mission orphan reaper failed", exc_info=True)
    # One-off cleanup: drop coverage snapshots that recorded a hard scan failure. Coverage
    # GETs are cached-only, so a scan that died mid-flight (classically an ARG 429 during a
    # fleet launch) used to persist as a 0%-coverage snapshot and render as the workload's
    # real posture until someone manually rescanned. Failures are no longer cached; this
    # clears the ones already on disk. Idempotent, so it stays safe on every boot.
    from app.amba import cache as _amba_cache
    from app.backupdr import cache as _backupdr_cache
    from app.telemetry import cache as _telemetry_cache

    for _label, _cache_mod in (
        ("monitoring", _amba_cache),
        ("telemetry", _telemetry_cache),
        ("backup/DR", _backupdr_cache),
    ):
        try:
            _purged = _cache_mod.purge_errored()
            if _purged:
                logging.getLogger("app.main").info(
                    "Purged %d errored %s coverage snapshot(s)", _purged, _label
                )
        except Exception:  # noqa: BLE001
            logging.getLogger("app.main").warning(
                "Errored %s coverage snapshot purge failed", _label, exc_info=True
            )
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

    # Guard: an EMPTY architecture set must never drive a prune — it would wipe every
    # memory/Know-Me. An empty set means the registry isn't loaded (e.g. a test harness
    # pointed the architecture store at a temp/empty path while sharing the lifespan), not
    # that every architecture was genuinely deleted.
    valid_arch_ids = set(all_architecture_ids())
    if not valid_arch_ids:
        logging.getLogger("app.main").info("Skipping orphan prune — architecture registry is empty.")
    else:
        try:
            pruned = prune_orphans(valid_arch_ids)
            if pruned:
                logging.getLogger("app.main").info("Pruned %d orphaned architecture memor(ies)", pruned)
        except Exception:  # noqa: BLE001
            logging.getLogger("app.main").warning("Orphaned memory prune failed", exc_info=True)

        # Same cascade for Workload Know-Me documents (derived from architecture memory).
        try:
            from app.knowme.registry import prune_orphans as prune_know_me

            kpruned = prune_know_me(valid_arch_ids)
            if kpruned:
                logging.getLogger("app.main").info("Pruned %d orphaned Know-Me document(s)", kpruned)
        except Exception:  # noqa: BLE001
            logging.getLogger("app.main").warning("Orphaned Know-Me prune failed", exc_info=True)

    # Start the automations scheduler (recurring tasks).
    from app.automations.scheduler import scheduler

    scheduler.start()

    # Watch the event loop itself. One worker means one loop, so any synchronous call left on it
    # freezes the WHOLE product, and the symptom people report is "the database locked up" —
    # an awaited commit cannot resume while nothing is being scheduled. This says which it is.
    from app.core import loopwatch

    loopwatch.start()

    # CPU-bound work now runs on worker threads, and a thread holds the GIL for a whole switch
    # interval before yielding. Every hop an interactive request makes — loop -> aiosqlite thread
    # -> loop — can therefore wait one interval, and a request that makes several hops pays it
    # several times over. Shortening the interval trades a little throughput on the CPU job for
    # the latency of everything else.
    #
    # Measured at the HTTP boundary during a 107-second graph rebuild on a 5,514-row tenant, so
    # this number is not a guess. Tunable because the right trade differs on a box dedicated to
    # analysis.
    import os as _os
    import sys as _sys

    _sys.setswitchinterval(float(_os.getenv("PY_SWITCH_INTERVAL_S", "0.001")))

    # Start the Monitor availability sampler (web/TCP ping history for dashboards).
    from app.monitor.sampler import sampler as monitor_sampler

    monitor_sampler.start()

    # Start the Backup Manager long-running-operation poller. Azure Backup control-plane
    # writes are asynchronous (202 + tracking URL), so an applied managed change only reaches
    # a terminal state once this worker polls it. Deliberately independent of the automations
    # scheduler so the ledger converges even when scheduling is paused.
    from app.backup_manager.lro import poller as backup_lro_poller

    backup_lro_poller.start()

    # Durable Performance Profiler Fleet worker. Queued/running SQL rows are recovered before
    # its runner starts, so browser reloads and container restarts cannot drop the batch tail.
    from app.perfprofile.fleet import worker as perf_fleet_worker

    await perf_fleet_worker.start()

    # Shared durable worker for Assessment, Change Explorer, coverage, Backup Manager,
    # Architecture, Mission, Cost, Deep Review and nightly batches.
    from app.core.work_batches import worker as work_batch_worker

    await work_batch_worker.start()

    # Warm the Azure MCP tool catalog in the background so the FIRST chat message
    # doesn't pay the `npx @azure/mcp` cold-start (node spawn + package resolve),
    # which the orchestrator awaits before streaming any token. Non-blocking.
    import asyncio

    from app.mcp.client import warm_tool_catalog

    # Keep the handle so shutdown can CANCEL it. Left untracked, this fire-and-forget task
    # keeps the event loop alive until the warmup finishes (node spawn + tool listing per
    # tenant, ~8s), which delays every container shutdown/rollout and made a TestClient
    # context-manager exit take 8s in the test suite.
    global _warm_task
    _warm_task = asyncio.create_task(warm_tool_catalog())


@app.on_event("shutdown")
async def _shutdown() -> None:
    import asyncio
    import contextlib

    # Cancel the background MCP warmup first — it's a pure optimization for the first chat
    # message and must never hold up shutdown.
    global _warm_task
    if _warm_task is not None and not _warm_task.done():
        _warm_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _warm_task
    _warm_task = None

    from app.core import loopwatch

    await loopwatch.stop()

    # Flush any queued session heartbeats. They are deliberately not awaited on the request
    # path; dropping them at shutdown would expire active sessions early on the next start.
    from app.auth.service import drain_session_slides

    with contextlib.suppress(Exception):
        await drain_session_slides()

    from app.automations.scheduler import scheduler

    await scheduler.stop()
    from app.monitor.sampler import sampler as monitor_sampler

    await monitor_sampler.stop()
    from app.perfprofile.fleet import worker as perf_fleet_worker

    await perf_fleet_worker.stop()
    from app.core.work_batches import worker as work_batch_worker

    await work_batch_worker.stop()
    from app.backup_manager.lro import poller as backup_lro_poller

    await backup_lro_poller.stop()

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
    # Downloads fetched as a Blob (rather than a plain link) cannot read the server's
    # filename unless it is exposed, so every such caller has to invent its own — and two
    # names for one file is how a report gets filed under the wrong scope.
    expose_headers=["Content-Disposition"],
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


# ------------------------------------------------------- network access control (IP allowlist)
#
# Refuses requests from sources outside the configured allowlist BEFORE authentication runs, so
# an unknown caller never reaches the sign-in page and cannot attempt credentials at all. The
# existing per-IP lockout is reactive — it responds after failures; this stops the attempt.
#
# ORDERING (verified, not assumed): Starlette's `add_middleware` INSERTS AT THE FRONT of
# `user_middleware`, and the stack is built so the FIRST entry is OUTERMOST — i.e. the LAST
# middleware registered runs FIRST. This class is therefore registered BEFORE `_SecurityHeaders`
# so that it ends up INSIDE it, which is what lets a 403 from here still carry CSP/HSTS/nosniff.
# `test_netaccess.py` asserts this ordering, because a refactor that moved this outside the
# header middleware, or after authentication, would weaken it with no other visible symptom.
class _IpAllowlist:
    """Enforce the configured IP allowlist.

    Exempt paths are ONLY the platform probes and the build id. Everything else — including
    `/api/auth/login`, the SSO routes and the SPA shell — is subject to the allowlist, because
    keeping unknown sources away from the sign-in page is the entire purpose of the feature.
    """

    #: Never blocked. `/healthz` and `/readyz` are the Container Apps liveness/readiness probes;
    #: blocking them would kill the revision and take the app down far more effectively than any
    #: attacker. `/version` is a public build id the SPA polls.
    EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/version"})

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self._app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope.get("type") != "http" or scope.get("path", "") in self.EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        from app.core import netaccess

        mode = netaccess.effective_mode()
        if mode == "off":
            await self._app(scope, receive, send)
            return

        from app.core.clientip import client_ip
        from app.core.netaccess_events import record

        cfg = netaccess.load_config()
        # A lightweight Request wrapper: `client_ip` only needs `.client` and `.headers`, and
        # building a full Starlette Request on every call would be wasted work on the hot path.
        ip = client_ip(_ScopeRequest(scope))
        if netaccess.matches(ip, cfg.get("rules", [])):
            await self._app(scope, receive, send)
            return

        record(ip or "unknown", mode, scope.get("path", ""))
        if mode == "monitor":
            # Recorded, deliberately NOT blocked. This is what makes it safe to discover the
            # right rules before committing to them.
            await self._app(scope, receive, send)
            return

        # Bare 403: do not confirm what is here, do not name the feature, do not tell the caller
        # their address is not on a list. A blocked scanner should learn nothing.
        resp = PlainTextResponse("Forbidden", status_code=403)
        await resp(scope, receive, send)


class _ScopeRequest:
    """Minimal `.client` / `.headers` view over a raw ASGI scope."""

    __slots__ = ("client", "headers")

    def __init__(self, scope) -> None:  # type: ignore[no-untyped-def]
        peer = scope.get("client")
        self.client = SimpleNamespace(host=peer[0]) if peer else None
        self.headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }


app.add_middleware(_IpAllowlist)

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
        elif headers.get("cookie") and sec_fetch_site != "same-origin":
            # Browser cookie auth is ambient. A state-changing cookie-bearing request
            # without Origin is rejected unless Fetch Metadata proves same-origin. API
            # clients without ambient cookies remain supported.
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
#
# Sub-routers are registered DIRECTLY on `app` with the /api prefix rather than nested inside
# this router. `include_router` re-creates every route it copies, so nesting made all ~1,077
# sub-router routes pay a second full registration pass: measured 2,012 ms nested vs 999 ms
# flat, for a route set proven byte-identical (same sha256 over path+methods+name+model).
# `api` still exists for the handful of endpoints defined directly below.
api = APIRouter(prefix="/api")

for _sub in (
    auth.router,
    users.router,
    meta.router,
    chats.router,
    charts.router,
    admin.router,
    admin_demo.router,
    firewall.router,
    connections.router,
    connectors.router,
    automations.router,
    backup.router,
    workbooks.router,
    playbooks.router,
    notifications.router,
    workloads.router,
    work_batches.router,
    assessments.router,
    architectures.router,
    fmea.router,
    policy.router,
    inventory.router,
    tagintel.router,
    changeexplorer.router,
    identity.router,
    entra.router,
    alert_analysis.router,
    alerts_manager.router,
    amba.router,
    backup_manager.router,
    resiliency.router,
    telemetry.router,
    backupdr.router,
    coverage_reports.router,
    netcheck.router,
    dnsdebug.router,
    evidence.router,
    graph.router,
    radar.router,
    teleintel.router,
    perfprofile.router,
    missions.router,
):
    app.include_router(_sub, prefix="/api")

app.include_router(iam.router, prefix="/api/iam")
# Legacy alias: /rbac was renamed to /iam. Kept (hidden from the schema, Deprecation-tagged)
# so existing bookmarks, saved automations and API clients keep working. Remove one release
# after the frontend has moved.
app.include_router(
    iam.router,
    prefix="/api/rbac",
    include_in_schema=False,
    dependencies=[Depends(iam.deprecated_rbac_alias)],
)

for _sub in (
    reservations.router,
    quota.router,
    ownership.router,
    vms.router,
    capability.router,
    cases.router,
    insights.router,
):
    app.include_router(_sub, prefix="/api")


# Health/readiness probes stay at the root (no auth, no /api) for Container Apps.
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready"}


@app.get("/version")
async def version():
    """Public, unauthenticated build version — polled by the SPA so a long-open tab can detect
    a new deploy and prompt the user to reload (defense-in-depth alongside the service worker).
    Mirrors the baked frontend VITE_APP_VERSION (both come from the same APP_VERSION build arg).
    No-store so a proxy/browser cache can't mask a fresh deploy."""
    import os

    from fastapi.responses import JSONResponse

    return JSONResponse(
        {"version": os.getenv("APP_VERSION") or "dev"},
        headers={"Cache-Control": "no-store"},
    )


@api.get("/llm/active")
async def llm_active(principal: Principal = Depends(get_principal)):  # noqa: ARG001
    """Currently active AI provider + model, for display in the UI.

    REQUIRES A SESSION -- do not remove the dependency. The provider and model name are
    not credentials, but they describe the deployment's AI supply chain and there is no
    reason to expose that to unauthenticated callers on an internet-facing instance.

    tests/test_route_authz_matrix.py asserts that no /api route answers an
    unauthenticated caller, so a regression here fails the suite.
    """
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


def _is_api_fallback_path(path: str) -> bool:
    """True when an unmatched SPA-fallback path belongs to the API namespace."""
    normalized = path.lstrip("/")
    return normalized == "api" or normalized.startswith("api/")


if _STATIC_DIR.is_dir():
    # Hashed build assets (JS/CSS) + any files under /assets. Vite emits content-hashed
    # filenames (index-<hash>.js), so these are safe to cache for a year as immutable —
    # a new deploy changes the hash, so clients never serve stale code. This removes the
    # per-asset revalidation (304) round-trip for non-service-worker clients.
    class _ImmutableStatic(StaticFiles):
        def is_not_modified(self, response_headers, request_headers) -> bool:  # type: ignore[override]
            response_headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return super().is_not_modified(response_headers, request_headers)

        async def get_response(self, path, scope):  # type: ignore[override]
            resp = await super().get_response(path, scope)
            resp.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
            return resp

    _assets_dir = _STATIC_DIR / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", _ImmutableStatic(directory=str(_assets_dir)), name="assets")

    _index_file = _STATIC_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    async def _spa_root() -> FileResponse:
        # index.html must NOT be cached — it references the hashed bundles, so a deploy
        # must be picked up immediately (otherwise clients pin to an old asset graph).
        return FileResponse(str(_index_file), headers={"Cache-Control": "no-cache"})

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        # Serve a real static file when one exists (favicon, agent-icons, etc.);
        # otherwise hand back index.html so the SPA router renders the route.
        if _is_api_fallback_path(full_path):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        candidate = (_STATIC_DIR / full_path).resolve()
        # `is_relative_to`, NOT `str.startswith`. startswith is a PREFIX test, not a
        # containment test: with _STATIC_DIR = /app/static it also accepts
        # /app/static-backup/secrets.env, because that string shares the prefix. Only one
        # stray sibling directory in the image separates that from a real file read.
        if candidate.is_file() and candidate.is_relative_to(_STATIC_DIR):
            return FileResponse(str(candidate))
        return FileResponse(str(_index_file), headers={"Cache-Control": "no-cache"})


"""Per-application sign-in outcomes from the Microsoft Graph per-event sign-in log.

Split out of the App Registrations collector so the Entra applications inventory reads the
same data the same way. The aggregate ``/reports/servicePrincipalSignInActivities`` report
cannot answer "did it actually succeed?": it emits no success/failure split for service
principals, and on tenants without a premium licence it silently serves a stale build rather
than failing.

The reads are expensive and mostly empty — measured on one tenant, 72 of 74 in-scope
applications returned zero rows, and an empty response still costs ~7 s. So this module does
not read on the refresh path at all: the collector applies whatever is cached, and
``run_backfill`` refreshes the stalest slice afterwards, out of band.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.core import fanout
from app.entra import cache
from app.entra.graphclient import GraphClient

log = logging.getLogger("app.entra.signin_outcomes")

#: Rolling window the sign-in log is queried over.
SIGNIN_WINDOW_DAYS = 30
#: One Graph call per application, so the total is bounded.
SIGNIN_OUTCOME_MAX_APPS = 600
#: One page of newest-first events per app, folded locally into (last success, last failure).
#:
#: ``$select`` is rejected by this endpoint (400 for ``createdDateTime,status``, 500 for
#: ``createdDateTime`` alone), so the per-row weight is fixed at ~3.5 KB and a page is the only
#: way to get both outcomes in one request.
SIGNIN_OUTCOME_PAGE = 50

#: Where the per-application outcome cache lives, under the tenant's cache directory.
CACHE_NAME = "signin_outcomes"
#: Where backfill progress lives, so the UI can say "measuring" instead of "none".
JOB_STATE = "signin_outcome_job"

SCOPE_OFF = "off"
SCOPE_VISIBLE = "visible"
SCOPE_ALL = "all"
SCOPES = (SCOPE_OFF, SCOPE_VISIBLE, SCOPE_ALL)

ProgressFn = Callable[[str, str], Awaitable[None]]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _now() -> datetime:
    return datetime.now(timezone.utc)



async def read_signin_outcomes(
    client: GraphClient,
    app_ids: list[str],
    *,
    max_apps: int = SIGNIN_OUTCOME_MAX_APPS,
    window_days: int = SIGNIN_WINDOW_DAYS,
    concurrency: int | None = None,
    max_seconds: float = 0.0,
) -> dict[str, Any]:
    """Last successful and last failed sign-in per application id.

    ``/auditLogs/signIns`` carries ``status.errorCode``, with two constraints found by
    measurement rather than documentation:

    * ``source=sp`` is required. Without it the endpoint returns the USER sign-in stream and
      a service principal simply is not in it — the query succeeds and returns nothing.
    * A **tenant-wide** query is refused (403, "doesn't have premium license"), but a query
      **scoped to one appId is allowed**. Hence one call per application, bounded.

    Newest-first with a small page, folded locally: one page carries both outcomes, where
    asking for each separately would double the request count — and request count, not
    payload, is what draws throttling from this endpoint.

    Returns ``measured``/``reason``/``by_app`` — never a silently empty ``by_app``, because
    "no sign-ins" and "could not read sign-ins" render identically and mean the opposite.
    """
    empty: dict[str, dict[str, str]] = {}
    if not app_ids:
        return {"measured": False, "reason": "No applications to query.", "by_app": empty,
                "queried": 0, "unreadable": 0, "capped": False}

    since = ((datetime.now(timezone.utc) - timedelta(days=window_days))
             .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    wanted = app_ids[:max_apps]
    capped = len(app_ids) > len(wanted)
    loop = asyncio.get_running_loop()
    # Per-call latency varies by an order of magnitude between tenants, so a count alone is a
    # weak bound. Whichever limit is reached first wins; apps not reached stay stale and are
    # picked up on the next pass.
    deadline = (loop.time() + max_seconds) if max_seconds and max_seconds > 0 else 0.0
    skipped: set[str] = set()

    async def _one(app_id: str) -> dict[str, str]:
        if deadline and loop.time() >= deadline:
            skipped.add(app_id)
            return {"success": "", "failed": "", "skipped": "1"}
        data = await client.get(
            "/auditLogs/signIns",
            params={
                "$filter": f"createdDateTime ge {since} and appId eq '{app_id}'",
                "$orderby": "createdDateTime desc",
                "$top": SIGNIN_OUTCOME_PAGE,
                "source": "sp",
            },
            beta=True,
        )
        success, failed = "", ""
        for row in (_as_dict(data).get("value") or []):
            row = _as_dict(row)
            created = str(row.get("createdDateTime") or "")
            if not created:
                continue
            code = str(_as_dict(row.get("status")).get("errorCode") or 0)
            if code in ("0", ""):
                success = success or created
            else:
                failed = failed or created
            if success and failed:
                break
        return {"success": success, "failed": failed}

    ok, errors = await fanout.bounded_map(
        wanted, _one,
        # Asking for more than the client will grant just queues on its gate.
        limit=concurrency if concurrency is not None else getattr(client, "concurrency", 6),
    )
    # An app abandoned at the deadline was never queried, so it must not be recorded as
    # checked — doing so would freeze "no events" into the cache for a full TTL.
    for app_id in skipped:
        ok.pop(app_id, None)
    capped = capped or bool(skipped)

    # Every app failing means the tenant cannot serve this at all — report that once, rather
    # than as a per-application absence that reads like "nothing signed in".
    if errors and not ok:
        first = next(iter(errors.values()))
        status = getattr(first, "status", None)
        licence = (" This needs a Microsoft Entra ID P1 or P2 license."
                   if status in (400, 403, 404) else "")
        return {
            "measured": False,
            "reason": f"Per-application sign-in outcomes could not be read "
                      f"(HTTP {status or 'network'}).{licence}",
            "by_app": empty,
            "queried": len(wanted),
            "unreadable": len(errors),
            "capped": capped,
        }

    by_app = {k: v for k, v in ok.items() if v.get("success") or v.get("failed")}
    return {
        "measured": True,
        "reason": "",
        "by_app": by_app,
        # Every app the log answered for, including the quiet ones. `by_app` deliberately
        # omits those; the cache must not, or an app with no events is re-read forever.
        "checked": sorted(ok),
        "queried": len(wanted),
        "unreadable": len(errors),
        "capped": capped,
    }


# --------------------------------------------------------------------------- merge
def merge_outcomes(block: dict[str, Any], by_app: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Overlay per-event outcomes onto an aggregate sign-in block, in place.

    Only what the log actually observed is overwritten. The aggregate reaches back further
    than the event window, so a success the log did not see must not erase its stamp.
    """
    seen = dict(block.get("last_seen") or {})
    rejected = dict(block.get("last_failed") or {})
    for app_id, outcome in (by_app or {}).items():
        key = str(app_id).lower()
        success = str((outcome or {}).get("success") or "")
        failed = str((outcome or {}).get("failed") or "")
        if success:
            seen[key] = success
        elif failed:
            # Every event the log holds for this app was rejected, and the aggregate's stamp
            # is that same attempt wearing a success's clothes. Keeping it is what makes a
            # dead credential read as a live application.
            seen.pop(key, None)
        if failed:
            rejected[key] = failed
    block["last_seen"] = seen
    block["last_failed"] = rejected
    block["active_app_ids"] = sorted(seen)
    return block


# --------------------------------------------------------------------------- cache
def read_cache(tenant_id: str) -> dict[str, dict[str, str]]:
    """``{app_id: {"success", "failed", "checked_at"}}`` for this tenant."""
    raw = cache.read_state(tenant_id, CACHE_NAME, {}) or {}
    entries = raw.get("apps") if isinstance(raw, dict) else None
    return {str(k).lower(): _as_dict(v) for k, v in (entries or {}).items()} if entries else {}


def write_cache(tenant_id: str, entries: dict[str, dict[str, str]]) -> None:
    cache.write_state(tenant_id, CACHE_NAME, {"apps": entries, "updated_at": cache.now_iso()})


def cached_by_app(entries: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """The subset `merge_outcomes` cares about — apps with an actual outcome."""
    return {k: {"success": v.get("success", ""), "failed": v.get("failed", "")}
            for k, v in entries.items() if v.get("success") or v.get("failed")}


def record(entries: dict[str, dict[str, str]], checked: list[str],
           by_app: dict[str, dict[str, str]], *, at: str = "") -> dict[str, dict[str, str]]:
    """Fold one read's results into the cache. Quiet apps are recorded as checked."""
    stamp = at or cache.now_iso()
    merged = dict(entries)
    for app_id in checked:
        key = str(app_id).lower()
        outcome = _as_dict(by_app.get(key))
        merged[key] = {
            "success": str(outcome.get("success") or ""),
            "failed": str(outcome.get("failed") or ""),
            "checked_at": stamp,
        }
    return merged


def select_stale(app_ids: list[str], entries: dict[str, dict[str, str]], *,
                 ttl_s: int, cap: int) -> list[str]:
    """The apps worth reading now: never-checked first, then the stalest, capped.

    Returning fewer than ``app_ids`` is the normal case, not a degradation — freshness is
    traded for a bounded run so one refresh can never stall on a large tenant.
    """
    if cap <= 0:
        return []

    def age_key(app_id: str) -> tuple[int, str]:
        stamp = str(_as_dict(entries.get(app_id)).get("checked_at") or "")
        # Never-checked sorts first; among the checked, the oldest stamp first.
        return (1, stamp) if stamp else (0, "")

    stale = []
    for app_id in app_ids:
        stamp = str(_as_dict(entries.get(app_id)).get("checked_at") or "")
        age = cache.age_seconds(stamp) if stamp else None
        if age is None or age >= max(0, ttl_s):
            stale.append(app_id)
    stale.sort(key=age_key)
    return stale[:cap]


# --------------------------------------------------------------------------- scope
def settings() -> tuple[str, int, int, int]:
    """``(scope, ttl_s, max_per_run, max_seconds)`` from app settings, already clamped."""
    from app.core.app_settings import load_settings

    s = load_settings()
    scope = str(s.get("entra_signin_outcome_scope") or SCOPE_VISIBLE).lower()
    if scope not in SCOPES:
        scope = SCOPE_VISIBLE
    return (scope,
            int(s.get("entra_signin_outcome_ttl_s") or 86400),
            int(s.get("entra_signin_outcome_max_per_run") or 100),
            int(s.get("entra_signin_outcome_max_seconds") or 300))


def in_scope_app_ids(apps_payload: dict[str, Any], scope: str) -> list[str]:
    """Application ids the configured scope wants outcomes for.

    ``visible`` mirrors exactly what the inventory grid renders — local registrations plus
    third-party enterprise applications. First-party Microsoft service principals are filtered
    out of that grid, so reading their per-event log populates nothing anyone can see.
    """
    if scope == SCOPE_OFF:
        return []
    data = _as_dict(apps_payload)
    ids: set[str] = {str(a.get("app_id") or "").lower()
                     for a in (data.get("applications") or []) if _as_dict(a).get("app_id")}
    for sp in (data.get("service_principals") or []):
        sp = _as_dict(sp)
        app_id = str(sp.get("app_id") or "").lower()
        if not app_id:
            continue
        if scope == SCOPE_ALL or not sp.get("is_first_party"):
            ids.add(app_id)
    ids.discard("")
    return sorted(ids)


# --------------------------------------------------------------------------- backfill
def job_state(tenant_id: str) -> dict[str, Any]:
    return _as_dict(cache.read_state(tenant_id, JOB_STATE, {}))


def _set_job_state(tenant_id: str, **fields: Any) -> None:
    state = job_state(tenant_id)
    state.update(fields)
    state["updated_at"] = cache.now_iso()
    cache.write_state(tenant_id, JOB_STATE, state)


async def run_backfill(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Read the stalest slice of per-application outcomes and patch the stored snapshot.

    Runs after a refresh rather than inside it. The reads are slow and overwhelmingly empty,
    so blocking a refresh on them buys a fresher failure column at the cost of the entire
    screen appearing minutes late.
    """
    async def say(level: str, message: str) -> None:
        if progress is not None:
            try:
                await progress(level, message)
            except Exception:  # noqa: BLE001 - progress is cosmetic
                pass

    scope, ttl_s, cap, budget_s = settings()
    if scope == SCOPE_OFF:
        _set_job_state(tenant_id, status="off", pending=0, checked=0)
        return {"ran": False, "reason": "Per-application sign-in outcomes are turned off."}

    payload = cache.read_domain(tenant_id, "apps")
    if not payload:
        return {"ran": False, "reason": "No applications snapshot to enrich."}

    app_ids = in_scope_app_ids(_as_dict(payload.get("data")), scope)
    entries = read_cache(tenant_id)
    wanted = select_stale(app_ids, entries, ttl_s=ttl_s, cap=cap)
    if not wanted:
        _set_job_state(tenant_id, status="fresh", pending=0, checked=len(app_ids),
                       scope=scope, total=len(app_ids))
        return {"ran": False, "reason": "Every in-scope application is within the cache window.",
                "total": len(app_ids)}

    _set_job_state(tenant_id, status="running", pending=len(wanted), checked=0,
                   scope=scope, total=len(app_ids), started_at=cache.now_iso())
    await say("info", f"Sign-in outcomes: reading {len(wanted):,} of {len(app_ids):,} application(s)…")

    from app.core.app_settings import load_settings

    try:
        async with GraphClient(
            connection, beta=bool(load_settings().get("entra_enable_beta_endpoints", True)),
        ) as client:
            result = await read_signin_outcomes(client, wanted, max_apps=cap,
                                                max_seconds=budget_s)
    except Exception as exc:  # noqa: BLE001 - a failed backfill must not break the snapshot
        log.warning("sign-in outcome backfill failed: %s", exc)
        _set_job_state(tenant_id, status="error", pending=0, error=str(exc)[:300])
        await say("warn", f"Sign-in outcomes unavailable ({str(exc)[:120]})")
        return {"ran": False, "reason": str(exc)[:300]}

    if not result.get("measured"):
        _set_job_state(tenant_id, status="unmeasured", pending=0,
                       reason=str(result.get("reason") or ""))
        await say("warn", str(result.get("reason") or "Sign-in outcomes unavailable."))
        return {"ran": False, "reason": result.get("reason") or ""}

    entries = record(entries, list(result.get("checked") or []),
                     _as_dict(result.get("by_app")))
    write_cache(tenant_id, entries)
    await _patch_snapshot(tenant_id, entries, scope=scope, reason="")

    remaining = len(select_stale(app_ids, entries, ttl_s=ttl_s, cap=10 ** 6))
    _set_job_state(tenant_id, status="done", pending=remaining, checked=len(wanted),
                   scope=scope, total=len(app_ids), finished_at=cache.now_iso())
    await say("ok", f"Sign-in outcomes: {len(result.get('by_app') or {}):,} application(s) "
                    f"with events, {remaining:,} still to check")
    return {"ran": True, "checked": len(wanted), "remaining": remaining,
            "with_events": len(result.get("by_app") or {})}


async def _patch_snapshot(tenant_id: str, entries: dict[str, dict[str, str]], *,
                          scope: str, reason: str) -> None:
    """Fold the cache into the stored apps payload under the domain lock."""
    async with cache.get_lock(tenant_id, "apps"):
        payload = cache.read_domain(tenant_id, "apps")
        if not payload:
            return
        data = _as_dict(payload.get("data"))
        block = _as_dict(data.get("signin_activity"))
        if not block:
            return
        merge_outcomes(block, cached_by_app(entries))
        block["outcomes"] = {
            "measured": True, "reason": reason, "scope": scope,
            "cached": len(entries), "updated_at": cache.now_iso(),
        }
        block["measured"] = True
        block["source"] = "auditLogs/signIns + servicePrincipalSignInActivities"
        data["signin_activity"] = block
        payload["data"] = data
        cache.write_domain(tenant_id, "apps", payload)

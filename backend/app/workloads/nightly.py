"""Optional nightly fleet profile refresh.

When ``workload_nightly_refresh`` is enabled (off by default), the scheduler calls
:func:`refresh_all` once per day. It warms every workload's per-feature caches by reusing the
SAME proven ``_get_snapshot`` paths the interactive Refresh buttons use — so the next morning
the command-center profiles are fully populated without anyone pressing Analyze.

Strictly best-effort and defensive: every feature/workload is wrapped in its own try/except
so one failure (e.g. a connection without permissions for a given scan) never aborts the run
or the scheduler loop. Performance profiling is intentionally skipped here (it's an SSE
streaming flow); everything else is cheap-ish cached scans.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("app.workloads.nightly")


def _admin_principal(tenant_id: str):
    from app.core.security import Principal

    return Principal(
        subject="scheduler",
        email="scheduler@local",
        tenant_id=tenant_id or "default",
        role="admin",
        permissions=frozenset(),  # is_admin via role=admin → passes require_permission
    )


async def _refresh_one(principal, wid: str, cid: str) -> int:
    """Refresh every cached signal for one workload. Returns the count that succeeded."""
    ok = 0

    async def _try(coro_factory, name: str) -> None:
        nonlocal ok
        try:
            await coro_factory()
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("nightly %s failed for %s: %s", name, wid, exc)

    # Coverage trio + radar share the api-layer `_get_snapshot(principal, kind, id, force, connection_id)`.
    from app.api import amba as amba_api
    from app.api import telemetry as tel_api
    from app.api import backupdr as bdr_api
    from app.api import radar as radar_api

    await _try(lambda: amba_api._get_snapshot(principal, "workload", wid, force=True, connection_id=cid), "amba")
    await _try(lambda: tel_api._get_snapshot(principal, "workload", wid, force=True, connection_id=cid), "telemetry")
    await _try(lambda: bdr_api._get_snapshot(principal, "workload", wid, force=True, connection_id=cid), "backupdr")
    await _try(lambda: radar_api._get_snapshot(principal, "workload", wid, force=True, connection_id=cid), "radar")

    # Ownership coverage uses its own collector + cache write.
    async def _own() -> None:
        from app.core.azure_connections import connection_for_scope
        from app.ownership import cache as own_cache, coverage as own_cov
        from app.workloads.registry import get_workload

        wl = get_workload(wid)
        conn = connection_for_scope("workload", connection_id=cid, workload=wl)
        snap = await own_cov.collect_coverage(conn, scope_kind="workload", scope_id=wid, workload=wl, tenant_id=principal.tenant_id)
        own_cache.write_snapshot(principal.tenant_id or "default", "workload", wid, snap)

    await _try(_own, "ownership")
    return ok


async def refresh_all() -> dict[str, int]:
    """Queue durable nightly refresh batches grouped by tenant."""
    from app.core import work_batches
    from app.workloads.registry import list_workloads

    workloads = list_workloads()
    by_tenant: dict[str, list[dict[str, Any]]] = {}
    for wl in workloads:
        tenant = wl.get("tenant_id") or "default"
        by_tenant.setdefault(tenant, []).append({
            "item_key": str(wl["id"]),
            "workload_id": str(wl["id"]),
            "workload_name": str(wl.get("name") or wl["id"]),
            "connection_id": str(wl.get("connection_id") or ""),
        })
    queued = 0
    day = datetime.now(timezone.utc).date().isoformat()
    for tenant, items in by_tenant.items():
        await work_batches.create_batch(
            tenant_id=tenant,
            feature="nightly",
            actor="scheduler",
            idempotency_key=f"nightly:{day}",
            items=items,
            config={"day": day},
            trigger="schedule",
        )
        queued += len(items)
    logger.info("Nightly workload refresh queued: %d workload(s) across %d tenant(s)", queued, len(by_tenant))
    return {"workloads": len(workloads), "signals": 0, "queued": queued}

"""Admin: load / remove ALL demo data in one place (Settings → Demo Data).

Seeds the demo workload + every feature's dummy snapshot, or purges all of it. Purge only
ever touches DEMO entries — anchored to the fixed demo workload id ``demo-amba-coverage``, the
demo architecture-run ids, the ``source == "demo_dummy_data"`` tag, and the ``demo``/``DEMO``
markers — so real workloads, real cached scans, and all Settings config are never affected.

Loading first purges, so it's idempotent and always produces a fresh demo dataset.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog, Chat

router = APIRouter(prefix="/admin/demo", tags=["admin"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("demo.manage")
log = logging.getLogger("app.api.admin_demo")

DEMO_WORKLOAD_ID = "demo-amba-coverage"
DEMO_DNS_ARCH = "demo-dnsdebug"
DEMO_NETCHECK_ARCH = "demo-netcheck"


# --------------------------------------------------------------------------- purge
def _purge_features(tenant_id: str) -> dict[str, Any]:
    """Clear only the regenerable per-feature demo data (caches, runs, snapshots). Does NOT
    touch the demo workload, demo architectures, or chats. Safe to run before a re-seed."""
    removed: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def step(name: str, fn) -> None:
        try:
            removed[name] = fn()
        except Exception as exc:  # noqa: BLE001 - one area failing must not block the rest
            errors[name] = str(exc)[:200]
            log.info("demo purge %s failed: %s", name, exc)

    from app.amba import cache as amba_cache
    from app.backupdr import cache as bdr_cache
    from app.perfprofile import cache as perf_cache, runs as perf_runs
    from app.radar import cache as radar_cache
    from app.telemetry import cache as tel_cache
    from app.teleintel import cache as ti_cache
    from app.demo_catalog import all_demo_ids

    demo_ids = all_demo_ids()

    def _del_all(fn) -> bool:
        removed_any = False
        for wid in demo_ids:
            try:
                if fn(wid):
                    removed_any = True
            except Exception:  # noqa: BLE001 - best-effort per workload
                pass
        return removed_any

    step("monitoring_coverage", lambda: _del_all(lambda w: amba_cache.delete_snapshot(tenant_id, "workload", w)))
    step("telemetry_coverage", lambda: _del_all(lambda w: tel_cache.delete_snapshot(tenant_id, "workload", w)))
    step("backup_dr_coverage", lambda: _del_all(lambda w: bdr_cache.delete_snapshot(tenant_id, "workload", w)))
    step("performance_cache", lambda: _del_all(lambda w: perf_cache.delete_snapshot(tenant_id, "workload", w)))
    step("performance_runs", lambda: _del_all(lambda w: perf_runs.delete_scope_runs(tenant_id, "workload", w)))
    step("retirement_radar", lambda: _del_all(lambda w: radar_cache.delete_snapshot(tenant_id, "workload", w)))
    step("telemetry_intelligence", lambda: ti_cache.delete_scope(tenant_id, DEMO_WORKLOAD_ID))

    # Coverage/posture trend series (one shared store across all 4 dashboards).
    from app.core import coverage_trends

    step("coverage_trends", lambda: _del_all(
        lambda w: any(coverage_trends.delete_scope(f, tenant_id, "workload", w) for f in coverage_trends.FEATURES)
    ))

    # Coverage scan history (shared store: Monitoring / Telemetry / Backup-DR).
    from app.core import coverage_runs

    step("coverage_runs", lambda: _del_all(
        lambda w: any(coverage_runs.delete_scope(f, tenant_id, "workload", w) for f in coverage_runs.FEATURES)
    ))

    from app.identity import appregs_cache
    step("app_registrations", lambda: appregs_cache.delete_demo(tenant_id))

    from app.identity import pim_cache
    step("pim_lifecycle", lambda: pim_cache.delete_snapshot(tenant_id))

    from app.rbac import cache as rbac_cache
    step("rbac", lambda: rbac_cache.purge_demo(tenant_id))

    from app.reservations import cache as res_cache, demo as res_demo
    step("reservations", lambda: res_cache.delete_snapshot(tenant_id, res_demo.DEMO_SCOPE_ID))

    def _purge_evidence() -> int:
        from app.evidence import registry as ev_reg
        snaps = ev_reg.list_snapshots(tenant_id, tag="demo", include_deleted=True)
        n = 0
        for s in snaps:
            if ev_reg.purge(tenant_id, s["id"]):
                n += 1
        return n
    step("evidence_locker", _purge_evidence)

    from app.dnsdebug import store as dns_store
    from app.netcheck import store as nc_store
    step("dns_debug", lambda: dns_store.delete_by_architecture(tenant_id, DEMO_DNS_ARCH))
    step("network_reachability", lambda: nc_store.delete_by_architecture(tenant_id, DEMO_NETCHECK_ARCH))

    from app.connectors import demo as conn_demo
    step("connectors", lambda: conn_demo.purge_demo())

    return {"removed": removed, "errors": errors}


async def _purge_all(tenant_id: str, db: AsyncSession) -> dict[str, Any]:
    """Full purge for the 'Remove demo data' button: feature data PLUS the demo workload, the
    DEMO-named architectures, and demo-scoped chats. Only demo artefacts; never real data."""
    result = _purge_features(tenant_id)
    removed, errors = result["removed"], result["errors"]

    def step(name: str, fn) -> None:
        try:
            removed[name] = fn()
        except Exception as exc:  # noqa: BLE001
            errors[name] = str(exc)[:200]
            log.info("demo purge %s failed: %s", name, exc)

    # Demo architectures (name-prefixed DEMO) — global demo content, hard delete.
    def _purge_architectures() -> int:
        from app.architectures import registry as arch_reg
        n = 0
        for a in arch_reg.list_architectures(None, include_deleted=True):
            if str(a.get("name", "")).upper().startswith("DEMO"):
                if arch_reg.purge_architecture(a["id"]):
                    n += 1
        return n
    step("architectures", _purge_architectures)

    # Demo chats (scoped to the demo workload).
    async def _purge_chats() -> int:
        res = await db.execute(select(Chat).where(Chat.workload_id == DEMO_WORKLOAD_ID))
        chats = list(res.scalars().all())
        for c in chats:
            await db.delete(c)
        if chats:
            await db.commit()
        return len(chats)
    try:
        removed["chats"] = await _purge_chats()
    except Exception as exc:  # noqa: BLE001
        errors["chats"] = str(exc)[:200]
        log.info("demo purge chats failed: %s", exc)

    # The demo workload itself (hard delete → soft then purge).
    def _purge_workload() -> bool:
        from app.workloads.registry import delete_workload, get_workload, purge_workload
        if get_workload(DEMO_WORKLOAD_ID) is None and get_workload(DEMO_WORKLOAD_ID, include_deleted=True) is None:
            return False
        delete_workload(DEMO_WORKLOAD_ID)
        purge_workload(DEMO_WORKLOAD_ID)
        return True
    step("workload", _purge_workload)

    # The extra Zava demo workloads.
    def _purge_zava() -> int:
        from app.workloads import demo_workloads as zava_demo
        return zava_demo.purge_demo()
    step("zava_workloads", _purge_zava)

    # Demo ownership (owners + assignments).
    def _purge_ownership() -> int:
        from app.ownership import demo as own_demo
        return own_demo.purge_demo(tenant_id)
    step("ownership", _purge_ownership)

    return {"removed": removed, "errors": errors}


# --------------------------------------------------------------------------- seed
def _seed_all(tenant_id: str) -> dict[str, Any]:
    """Seed the demo workload + every feature's dummy data. Returns a per-area summary."""
    seeded: list[str] = []
    errors: dict[str, str] = {}

    def step(name: str, fn) -> None:
        try:
            fn()
            seeded.append(name)
        except Exception as exc:  # noqa: BLE001
            errors[name] = str(exc)[:200]
            log.info("demo seed %s failed: %s", name, exc)

    from app.amba import demo as amba_demo
    from app.backupdr import demo as bdr_demo
    from app.connectors import demo as conn_demo
    from app.dnsdebug import demo as dns_demo
    from app.evidence import demo as ev_demo
    from app.netcheck import demo as nc_demo
    from app.perfprofile import demo as perf_demo
    from app.radar import demo as radar_demo
    from app.rbac import demo as rbac_demo
    from app.reservations import cache as res_cache, demo as res_demo
    from app.teleintel import demo as ti_demo
    from app.telemetry import demo as tel_demo
    from app.identity import appregs
    from app.identity import pim as pim_demo
    from app.workloads import demo_workloads as zava_demo
    from app.demo_catalog import all_demo_ids

    demo_ids = all_demo_ids()

    def _seed_each(label: str, fn) -> None:
        """Seed a per-feature snapshot for every demo workload (Contoso + both Zava)."""
        def run() -> None:
            for wid in demo_ids:
                fn(wid)
        step(label, run)

    step("workload", lambda: amba_demo.ensure_demo_workload())
    step("zava_workloads", lambda: zava_demo.seed_demo())
    _seed_each("monitoring_coverage", lambda w: amba_demo.seed_demo(tenant_id=tenant_id, scope_id=w))
    _seed_each("telemetry_coverage", lambda w: tel_demo.seed_demo(tenant_id=tenant_id, scope_id=w))
    _seed_each("backup_dr_coverage", lambda w: bdr_demo.seed_demo(tenant_id=tenant_id, scope_id=w))
    _seed_each("performance_profiler", lambda w: perf_demo.seed_demo(tenant_id=tenant_id, scope_id=w))
    _seed_each("retirement_radar", lambda w: radar_demo.seed_demo(tenant_id=tenant_id, scope_id=w))
    step("telemetry_intelligence", lambda: ti_demo.ensure_demo())
    step("rbac", lambda: rbac_demo.seed_demo(tenant_id))
    step("reservations", lambda: res_cache.write_snapshot(tenant_id, res_demo.DEMO_SCOPE_ID, res_demo.seed_demo()))
    step("app_registrations", lambda: appregs.seed_demo(tenant_id))
    step("pim_lifecycle", lambda: pim_demo.seed_demo(tenant_id))
    step("evidence_locker", lambda: ev_demo.seed_demo(tenant_id=tenant_id))
    step("dns_debug", lambda: dns_demo.seed_demo(tenant_id=tenant_id))
    step("network_reachability", lambda: nc_demo.seed_demo(tenant_id=tenant_id))
    step("connectors", lambda: conn_demo.seed_demo())
    from app.ownership import demo as own_demo
    step("ownership", lambda: own_demo.seed_demo(tenant_id))
    return {"seeded": seeded, "errors": errors}


# --------------------------------------------------------------------------- status
def _status(tenant_id: str) -> dict[str, Any]:
    """Report whether demo data is currently loaded (per area + overall)."""
    from app.amba import cache as amba_cache
    from app.backupdr import cache as bdr_cache
    from app.perfprofile import cache as perf_cache
    from app.radar import cache as radar_cache
    from app.telemetry import cache as tel_cache
    from app.workloads.registry import get_workload

    from app.connectors import demo as conn_demo
    from app.connectors.registry import get_connector
    from app.identity import appregs_cache
    from app.rbac import cache as rbac_cache
    from app.reservations import cache as res_cache, demo as res_demo

    def _appregs_demo_present() -> bool:
        entry = appregs_cache.get(tenant_id, "")
        return bool(entry and (entry.get("payload") or {}).get("source") == "demo_dummy_data")

    def _reservations_demo_present() -> bool:
        snap = res_cache.read_snapshot(tenant_id, res_demo.DEMO_SCOPE_ID)
        return bool(snap and snap.get("demo"))

    present: dict[str, bool] = {
        "workload": get_workload(DEMO_WORKLOAD_ID) is not None,
        "zava_workloads": get_workload("demo-zava-shoes-website") is not None,
        "monitoring_coverage": amba_cache.read_snapshot(tenant_id, "workload", DEMO_WORKLOAD_ID) is not None,
        "telemetry_coverage": tel_cache.read_snapshot(tenant_id, "workload", DEMO_WORKLOAD_ID) is not None,
        "backup_dr_coverage": bdr_cache.read_snapshot(tenant_id, "workload", DEMO_WORKLOAD_ID) is not None,
        "performance_profiler": perf_cache.read_snapshot(tenant_id, "workload", DEMO_WORKLOAD_ID) is not None,
        "retirement_radar": radar_cache.read_snapshot(tenant_id, "workload", DEMO_WORKLOAD_ID) is not None,
        "rbac": rbac_cache.is_demo(tenant_id),
        "reservations": _reservations_demo_present(),
        "app_registrations": _appregs_demo_present(),
        "connectors": any(get_connector(cid) is not None for cid in conn_demo.DEMO_CONNECTOR_IDS),
    }
    return {"loaded": any(present.values()), "present": present}


# --------------------------------------------------------------------------- endpoints
@router.get("/status")
async def demo_status(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    return _status(principal.tenant_id or "default")


@router.post("/seed")
async def demo_seed(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Load a fresh demo dataset (clears existing per-feature demo data first, then seeds).
    Does not delete demo architectures — those are removed only via the Remove button."""
    tenant_id = principal.tenant_id or "default"
    _purge_features(tenant_id)  # fresh slate for the regenerable feature data
    result = _seed_all(tenant_id)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="admin.demo.seed",
            target="demo-data",
            metadata_json={"seeded": result["seeded"], "errors": list(result["errors"].keys())},
        )
    )
    await db.commit()
    return {"ok": True, **result, "status": _status(tenant_id)}


@router.post("/purge")
async def demo_purge(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Remove ALL demo data (only demo entries; real data is untouched)."""
    tenant_id = principal.tenant_id or "default"
    result = await _purge_all(tenant_id, db)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="admin.demo.purge",
            target="demo-data",
            metadata_json={"removed": result["removed"], "errors": list(result["errors"].keys())},
        )
    )
    await db.commit()
    return {"ok": True, **result, "status": _status(tenant_id)}

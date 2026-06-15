"""RBAC scan-run history + drift (RbacScanRun).

Each completed refresh records a compact summary so the dashboard can chart movement and answer
"what privileged access is NEW since the last scan?". The heavy rows stay in the file cache; here
we persist KPIs, per-scope summaries and the set of privileged-access keys used to diff runs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select

from app.core.db import SessionLocal
from app.models import RbacScanRun
from app.rbac import compose, schema

log = logging.getLogger("app.rbac.store")


def _privileged_keys(rows: list[dict[str, Any]]) -> list[str]:
    """Stable identity of each privileged grant — effective principal | role | scope."""
    keys = set()
    for r in rows:
        if not r.get("roleIsPrivileged"):
            continue
        who = r.get("effectivePrincipalId") or r.get("effectivePrincipalName") or r.get("principalId")
        keys.add(f"{who}|{r.get('roleName','')}|{r.get('scope','')}")
    return sorted(keys)


def _public(run: RbacScanRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "tenant_id": run.tenant_id,
        "scope": run.scope,
        "trigger": run.trigger,
        "status": run.status,
        "total_rows": run.total_rows,
        "privileged_count": run.privileged_count,
        "unique_principals": run.unique_principals,
        "kpis": run.kpis_json or {},
        "scopes": run.scopes_json or [],
        "diff": run.diff_json or None,
        "demo": run.demo,
        "triggered_by": run.triggered_by,
        "started_at": run.started_at.isoformat() if run.started_at else "",
        "ended_at": run.ended_at.isoformat() if run.ended_at else "",
        "duration_ms": run.duration_ms,
    }


async def save_run(
    tenant_id: str,
    *,
    connection_id: str | None = None,
    scope: str = "__all__",
    trigger: str = "manual",
    triggered_by: str = "",
    demo: bool = False,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Snapshot the current composed access for ``tenant_id`` into a history row + diff vs prev."""
    overview = compose.compute_overview(tenant_id)
    master = compose.build_master_rows(tenant_id)
    keys = _privileged_keys(master)

    async with SessionLocal() as db:
        prev = (
            await db.execute(
                select(RbacScanRun).where(RbacScanRun.tenant_id == tenant_id).order_by(desc(RbacScanRun.started_at)).limit(1)
            )
        ).scalar_one_or_none()
        prev_keys = set(prev.privileged_keys_json or []) if prev else set()
        cur_keys = set(keys)
        diff = None
        if prev:
            added = sorted(cur_keys - prev_keys)
            removed = sorted(prev_keys - cur_keys)
            diff = {
                "baseline_run_id": prev.id,
                "added_privileged": added,
                "removed_privileged": removed,
                "added_count": len(added),
                "removed_count": len(removed),
            }

        run = RbacScanRun(
            tenant_id=tenant_id,
            connection_id=connection_id,
            scope=scope,
            trigger=trigger,
            status="succeeded",
            total_rows=len(master),
            privileged_count=overview["kpis"].get("privileged", 0),
            unique_principals=overview["kpis"].get("unique_principals", 0),
            kpis_json=overview["kpis"],
            scopes_json=[
                {"scope": s["scope"], "displayName": s["displayName"], "row_count": s["row_count"], "status": s["status"]}
                for s in overview["scopes"]
            ],
            privileged_keys_json=keys,
            diff_json=diff,
            demo=demo,
            triggered_by=triggered_by,
            ended_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return _public(run)


async def list_runs(tenant_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(RbacScanRun).where(RbacScanRun.tenant_id == tenant_id).order_by(desc(RbacScanRun.started_at)).limit(limit)
            )
        ).scalars().all()
        return [_public(r) for r in rows]


async def get_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    async with SessionLocal() as db:
        run = (
            await db.execute(select(RbacScanRun).where(RbacScanRun.tenant_id == tenant_id, RbacScanRun.id == run_id))
        ).scalar_one_or_none()
        return _public(run) if run else None

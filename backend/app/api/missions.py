"""Workload Mission Control endpoints.

Launch a *mission* (one sweep that runs many per-workload analyses), stream its live
progress, read mission history, and render the current per-system board without running.
Also a fleet endpoint to launch missions for several workloads at once. Admin-gated.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.missions import orchestrator, systems
from app.models import AuditLog

router = APIRouter(prefix="/missions", tags=["missions"])

# Viewing missions requires missions.read; launching/cancelling/deleting requires
# missions.run. The `require_admin` alias maps to the read tier so existing GET call sites
# stay correct; mutating endpoints below opt into `_run`. Admins always pass either way.
require_admin = require_permission("missions.read")
_run = require_permission("missions.run")
log = logging.getLogger("app.api.missions")


class MissionRunRequest(BaseModel):
    workload_id: str
    systems: list[str] | None = None
    force: bool = False
    connection_id: str | None = None


class MissionFleetRequest(BaseModel):
    workload_ids: list[str] = Field(default_factory=list)
    systems: list[str] | None = None
    force: bool = False
    connection_id: str | None = None


async def _audit(db: AsyncSession, principal: Principal, action: str, target: str, **meta) -> None:
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action=action,
            target=target,
            metadata_json=meta or None,
        )
    )
    await db.commit()


def _resolve(workload_id: str, connection_id: str | None):
    """Resolve the workload + the connection id a mission should run under (the workload's
    own connection unless explicitly overridden)."""
    from app.core.azure_connections import connection_for_workload, resolve_connection
    from app.workloads.registry import get_workload

    wl = get_workload(workload_id)
    if wl is None:
        return None, ""
    if connection_id:
        conn = resolve_connection(connection_id)
    else:
        conn = connection_for_workload(wl)
    return wl, (connection_id or (conn or {}).get("id", ""))


@router.get("/systems")
async def list_systems(_: Principal = Depends(require_admin)):
    """The catalog of mission systems (key, label, icon) in canonical order."""
    return {
        "systems": [
            {"key": s.key, "label": s.label, "icon": s.icon, "informational": s.informational}
            for s in systems.SYSTEMS
        ]
    }


@router.post("/run")
async def run_mission(
    payload: MissionRunRequest,
    principal: Principal = Depends(_run),
    db: AsyncSession = Depends(get_db),
):
    """Launch a mission for one workload; returns the mission immediately (status running)."""
    wl, conn_id = _resolve(payload.workload_id, payload.connection_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="Workload not found.")
    mission = orchestrator.manager.create(
        tenant_id=principal.tenant_id,
        workload_id=payload.workload_id,
        workload_name=wl.get("name", "workload"),
        connection_id=conn_id,
        actor=principal.subject,
        force=payload.force,
        trigger="manual",
        system_keys=payload.systems or [],
    )
    await _audit(db, principal, "mission.run", mission["id"], workload_id=payload.workload_id, force=payload.force)
    return {"mission": mission}


@router.post("/fleet")
async def run_fleet(
    payload: MissionFleetRequest,
    principal: Principal = Depends(_run),
    db: AsyncSession = Depends(get_db),
):
    """Launch missions for several workloads at once (fleet sweep)."""
    if not payload.workload_ids:
        raise HTTPException(status_code=400, detail="Select at least one workload.")
    launched = []
    for wid in payload.workload_ids:
        wl, conn_id = _resolve(wid, payload.connection_id)
        if wl is None:
            continue
        mission = orchestrator.manager.create(
            tenant_id=principal.tenant_id,
            workload_id=wid,
            workload_name=wl.get("name", "workload"),
            connection_id=conn_id,
            actor=principal.subject,
            force=payload.force,
            trigger="fleet",
            system_keys=payload.systems or [],
        )
        launched.append(mission)
    if not launched:
        raise HTTPException(status_code=404, detail="None of the selected workloads were found.")
    await _audit(db, principal, "mission.fleet", ",".join(payload.workload_ids)[:128], count=len(launched), force=payload.force)
    return {"missions": launched, "launched": len(launched)}


@router.get("/state")
async def mission_state(
    workload_id: str = Query(...),
    principal: Principal = Depends(require_admin),
):
    """Current per-system board for a workload from cached last-runs — never scans Azure."""
    return await orchestrator.manager.state(
        tenant_id=principal.tenant_id, workload_id=workload_id, actor=principal.subject
    )


@router.get("")
async def list_missions(
    workload_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_admin),
):
    """Mission history (newest first), optionally filtered to one workload."""
    return {"missions": await orchestrator.list_missions(principal.tenant_id, workload_id, limit)}


@router.get("/{mission_id}")
async def get_mission(mission_id: str, principal: Principal = Depends(require_admin)):
    mission = await orchestrator.get_mission(mission_id, principal.tenant_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found.")
    return {"mission": mission}


@router.get("/{mission_id}/stream")
async def stream_mission(mission_id: str, principal: Principal = Depends(require_admin)):
    """SSE: snapshot → per-system + log deltas → done."""
    return EventSourceResponse(orchestrator.manager.stream(mission_id, principal.tenant_id))


@router.post("/{mission_id}/cancel")
async def cancel_mission(mission_id: str, principal: Principal = Depends(_run)):
    ok = orchestrator.manager.cancel(mission_id, principal.tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Mission not running.")
    return {"ok": True}


@router.delete("/{mission_id}")
async def delete_mission(
    mission_id: str,
    principal: Principal = Depends(_run),
    db: AsyncSession = Depends(get_db),
):
    ok = await orchestrator.delete_mission(mission_id, principal.tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Mission not found.")
    await _audit(db, principal, "mission.delete", mission_id)
    return {"ok": True}


@router.delete("/workload/{workload_id}")
async def delete_workload_missions(
    workload_id: str,
    principal: Principal = Depends(_run),
    db: AsyncSession = Depends(get_db),
):
    """Delete a workload's entire Mission Control (all of its mission runs). No trash —
    this is permanent. Returns the number of runs removed."""
    deleted = await orchestrator.delete_missions_for_workload(principal.tenant_id, workload_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="No mission control to delete for this workload.")
    await _audit(db, principal, "mission.delete_workload", workload_id, deleted=deleted)
    return {"ok": True, "deleted": deleted}


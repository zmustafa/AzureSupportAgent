"""Private Network Reachability Analyzer endpoints.

Runs live reachability probes from a sandbox VM (SSE for the canvas hop animation, plus a
captured variant for the report), persists runs for Re-run/diff, pins evidence to the
architecture activity feed / War Room, and exposes source-VM candidates. Admin-gated."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.security import Principal, require_permission
from app.netcheck import demo, store

router = APIRouter(prefix="/netcheck", tags=["netcheck"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("netdiag.run")
log = logging.getLogger("app.api.netcheck")


class RunRequest(BaseModel):
    architecture_id: str = ""
    source_vm_id: str = ""
    source_host: str = ""  # optional manual FQDN/IP override
    source_node_id: str = ""
    target_node_id: str = ""
    target_host: str = ""  # manual FQDN/IP for the target
    port: int = 443
    protocol: str = "tcp"
    payload: dict[str, Any] = Field(default_factory=dict)


def _load_arch(architecture_id: str) -> dict[str, Any]:
    from app.architectures.registry import get_architecture

    return get_architecture(architecture_id) or {"id": architecture_id, "nodes": [], "workload_id": ""}


@router.get("/sources")
async def sources(architecture_id: str = "", _: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Candidate sandbox VMs to probe FROM (the architecture's workload), for the picker.

    Primary list = VMs explicitly linked to the architecture's workload. When none match
    (e.g. the workload was re-created with a new id, leaving the VM's links stale), fall
    back to ALL enabled sandbox VMs flagged ``linked=false`` so the picker is never empty
    when a usable VM exists — the user can still pick it (or override the source host)."""
    from app.core.sandbox_vms import list_vms, resolve_for_workload

    arch = _load_arch(architecture_id)
    wl = arch.get("workload_id") or ""
    linked = resolve_for_workload(wl) if wl else []
    fallback = False
    if linked:
        vms = [{**v, "_linked": True} for v in linked]
    else:
        vms = [{**v, "_linked": False} for v in list_vms() if not v.get("disabled")]
        fallback = True
    return {
        "workload_id": wl,
        "fallback": fallback,
        "sources": [
            {"id": v["id"], "display_name": v.get("display_name", ""), "vnet_label": v.get("vnet_label", ""),
             "disabled": v.get("disabled", False), "linked": v.get("_linked", True)}
            for v in vms
        ],
    }


@router.post("/run/stream")
async def run_stream(payload: RunRequest, principal: Principal = Depends(require_admin)):
    """Stream a live reachability run over SSE (start → step* → evidence → done)."""
    from app.netcheck.analyzer import run_analysis

    arch = _load_arch(payload.architecture_id)

    async def _gen():
        try:
            async for ev in run_analysis(
                tenant_id=principal.tenant_id,
                actor=principal.subject,
                architecture=arch,
                source_vm_id=payload.source_vm_id,
                source_host_override=payload.source_host,
                source_node_id=payload.source_node_id,
                target_node_id=payload.target_node_id,
                target_host=payload.target_host,
                port=payload.port,
                protocol=payload.protocol,
                payload=payload.payload,
            ):
                yield {"event": ev["event"], "data": json.dumps(ev["data"])}
        except Exception as exc:  # noqa: BLE001
            log.exception("netcheck run failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


@router.get("/runs")
async def runs(
    architecture_id: str = "", principal: Principal = Depends(require_admin)
) -> dict[str, Any]:
    return {"runs": store.list_runs(principal.tenant_id, architecture_id=architecture_id or None)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    r = store.get_run(principal.tenant_id, run_id)
    if r is None:
        return {"ok": False, "detail": "Run not found."}
    return {"ok": True, "run": r}


class PinRequest(BaseModel):
    run_id: str
    to_war_room: bool = False


@router.post("/pin")
async def pin(payload: PinRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Pin a run to the architecture activity feed (and optionally a War Room handoff)."""
    from app.architectures import activity

    run = store.get_run(principal.tenant_id, payload.run_id)
    if run is None:
        return {"ok": False, "detail": "Run not found."}
    detail = f"Connectivity test {run['source']} → {run['target']}:{run['port']} — {run['verdict']}"
    activity.log(
        run.get("architecture_id", ""),
        "connectivity_test",
        detail,
        principal.subject,
        meta={
            "run_id": run["id"],
            "verdict": run["verdict"],
            "target": run["target"],
            "port": run["port"],
            "mismatch": bool(run.get("mismatch")),
        },
    )
    return {"ok": True, "pinned": True, "detail": detail}


@router.get("/report/{run_id}")
async def report(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """A self-contained markdown report for a run (for export)."""
    run = store.get_run(principal.tenant_id, run_id)
    if run is None:
        return {"ok": False, "detail": "Run not found."}
    lines = [
        f"# Connectivity report — {run['source']} → {run['target']}:{run['port']}",
        f"- Verdict: **{run['verdict']}**",
        f"- Protocol: {run['protocol']}",
        f"- When: {run.get('created_at', '')} by {run.get('created_by', '')}",
        "",
        "## Probe steps",
    ]
    for s in run.get("steps", []):
        lines.append(f"- **{s['step'].upper()}** — {s['status']}: {s['evidence']}")
    if run.get("evidence", {}).get("matched_deny"):
        d = run["evidence"]["matched_deny"]
        lines += ["", f"## Blocked by\n- NSG `{d.get('nsg')}` rule `{d.get('name')}` (priority {d.get('priority')}, {d.get('access')} {d.get('direction')} :{d.get('destinationPortRange')})"]
    if run.get("mismatch"):
        lines += ["", f"## Intent mismatch\n- {run['mismatch']['detail']}"]
    return {"ok": True, "markdown": "\n".join(lines), "run": run}


@router.post("/demo/seed")
async def seed_demo_endpoint(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    result = demo.seed_demo(tenant_id=principal.tenant_id)
    return {"ok": True, "run_id": result["run"]["id"], "verdict": result["run"]["verdict"], "diff": result["diff"]}

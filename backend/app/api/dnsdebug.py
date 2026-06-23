"""Private Endpoint Resolution Debugger endpoints.

Streams the live DNS resolution chain (SSE) from one or more sandbox VMs, classifies the
exact private-DNS misconfiguration, generates Bicep remediation, persists runs for
Re-run/diff, and pins evidence to the architecture activity feed / War Room. Admin-gated."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.security import Principal, require_permission
from app.dnsdebug import demo, store

router = APIRouter(prefix="/dnsdebug", tags=["dnsdebug"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("netdiag.run")
log = logging.getLogger("app.api.dnsdebug")


class RunRequest(BaseModel):
    architecture_id: str = ""
    source_vm_ids: list[str] = Field(default_factory=list)
    fqdn: str = ""
    source_vnet_id: str = ""


def _load_arch(architecture_id: str) -> dict[str, Any]:
    from app.architectures.registry import get_architecture

    return get_architecture(architecture_id) or {"id": architecture_id, "nodes": [], "workload_id": ""}


@router.get("/sources")
async def sources(architecture_id: str = "", _: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.core.sandbox_vms import list_vms, resolve_for_workload

    arch = _load_arch(architecture_id)
    wl = arch.get("workload_id") or ""
    linked = resolve_for_workload(wl) if wl else []
    fallback = False
    if linked:
        vms = [{**v, "_linked": True} for v in linked]
    else:
        # No VM linked to this workload (e.g. stale links after a workload re-create) —
        # fall back to all enabled VMs so the picker isn't empty when one exists.
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
    """Stream a live multi-source resolution diagnosis over SSE."""
    from app.dnsdebug.analyzer import run_analysis

    arch = _load_arch(payload.architecture_id)

    async def _gen():
        try:
            async for ev in run_analysis(
                tenant_id=principal.tenant_id,
                actor=principal.subject,
                architecture=arch,
                source_vm_ids=payload.source_vm_ids,
                fqdn=payload.fqdn,
                source_vnet_id=payload.source_vnet_id,
            ):
                yield {"event": ev["event"], "data": json.dumps(ev["data"])}
        except Exception as exc:  # noqa: BLE001
            log.exception("dnsdebug run failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


@router.get("/runs")
async def runs(architecture_id: str = "", principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    return {"runs": store.list_runs(principal.tenant_id, architecture_id=architecture_id or None)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    r = store.get_run(principal.tenant_id, run_id)
    if r is None:
        return {"ok": False, "detail": "Run not found."}
    return {"ok": True, "run": r}


class IacRequest(BaseModel):
    run_id: str


@router.post("/iac")
async def generate_iac_endpoint(payload: IacRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.dnsdebug.iac import generate_for_run

    run = store.get_run(principal.tenant_id, payload.run_id)
    if run is None:
        return {"ok": False, "detail": "Run not found."}
    return {"ok": True, "iac": generate_for_run(run), "format": "bicep"}


class PinRequest(BaseModel):
    run_id: str
    to_war_room: bool = False


@router.post("/pin")
async def pin(payload: PinRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    from app.architectures import activity

    run = store.get_run(principal.tenant_id, payload.run_id)
    if run is None:
        return {"ok": False, "detail": "Run not found."}
    detail = f"DNS resolution debug {run['fqdn']} — {run.get('overall_classification', '')}"
    activity.log(
        run.get("architecture_id", ""),
        "dns_resolution",
        detail,
        principal.subject,
        meta={"run_id": run["id"], "fqdn": run["fqdn"], "classification": run.get("overall_classification", ""),
              "misconfig": run.get("misconfig_kind", "")},
    )
    return {"ok": True, "pinned": True, "detail": detail}


@router.get("/report/{run_id}")
async def report(run_id: str, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    run = store.get_run(principal.tenant_id, run_id)
    if run is None:
        return {"ok": False, "detail": "Run not found."}
    lines = [
        f"# Private Endpoint resolution report — {run['fqdn']}",
        f"- Overall: **{run.get('overall_classification', '')}**",
        f"- Verdict: {run.get('verdict', '')}",
        f"- When: {run.get('created_at', '')} by {run.get('created_by', '')}",
        "",
        "## Per-source resolution",
    ]
    for s in run.get("sources", []):
        lines.append(f"### {s['source']} — {s['classification']} ({s.get('resolved_ip', '')})")
        lines.append(f"- {s.get('verdict', '')}")
        for st in s.get("steps", []):
            lines.append(f"  - {st['step']}: {st['status']} — {st['evidence']}")
    z = run.get("zone_facts", {})
    if z:
        lines += ["", "## Azure DNS facts", f"- Expected zone: {z.get('expected_zone')}",
                  f"- Zone exists: {z.get('zone_exists')}", f"- Linked to source VNet: {z.get('linked_to_source_vnet')}"]
    return {"ok": True, "markdown": "\n".join(lines), "run": run}


@router.post("/demo/seed")
async def seed_demo_endpoint(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    result = demo.seed_demo(tenant_id=principal.tenant_id)
    return {"ok": True, "run_id": result["run"]["id"], "classification": result["run"]["overall_classification"], "diff": result["diff"]}

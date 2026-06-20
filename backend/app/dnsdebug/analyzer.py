"""Orchestrate a Private Endpoint resolution diagnosis.

Runs the resolution chain from one or more sources (sandbox VMs / VNets) — in parallel for
the multi-source comparison — corroborates with the Azure-side zone facts, classifies the
exact misconfiguration in plain English, persists + diffs, and streams events for the live
UI chain animation."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.dnsdebug import store
from app.dnsdebug.resolver import run_resolution

log = logging.getLogger("app.dnsdebug.analyzer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_sources(architecture: dict[str, Any], *, vm_ids: list[str]) -> list[dict[str, Any]]:
    """The sandbox VMs to resolve FROM: explicit ids, else the first enabled box on the
    architecture's workload."""
    from app.core.sandbox_vms import get_vm, resolve_for_workload

    if vm_ids:
        return [v for v in (get_vm(i) for i in vm_ids) if v]
    wl = architecture.get("workload_id") if architecture else ""
    vms = resolve_for_workload(wl) if wl else []
    return vms[:1]


def classify(observed: dict[str, Any], zone_facts: dict[str, Any]) -> dict[str, Any]:
    """Return {classification, misconfig_kind, verdict} naming the exact problem in English."""
    ip = observed.get("resolved_ip", "")
    is_private = observed.get("is_private")
    custom_dns = observed.get("custom_dns") or []
    zone = zone_facts.get("expected_zone", "")
    zone_exists = zone_facts.get("zone_exists")
    linked = zone_facts.get("linked_to_source_vnet")

    if not ip:
        return {"classification": "nxdomain", "misconfig_kind": "no_resolution",
                "verdict": f"The FQDN did not resolve at all from this source — check the resolver and that the record exists in {zone or 'the privatelink zone'}."}

    if is_private:
        return {"classification": "private", "misconfig_kind": "",
                "verdict": f"Resolves to the private IP {ip} — Private Link DNS is working from this source."}

    # Public resolution — name the most likely exact cause, preferring Azure-truth signals.
    if zone_exists is False:
        return {"classification": "public", "misconfig_kind": "missing_zone",
                "verdict": f"Resolves to PUBLIC IP {ip} — Private DNS zone {zone} does not exist. Create it and link it to the source VNet."}
    if zone_exists and linked is False:
        return {"classification": "public", "misconfig_kind": "missing_link",
                "verdict": f"Resolves to PUBLIC IP {ip} — Private DNS zone {zone} exists but is NOT linked to the source VNet. Add a virtual-network link."}
    if custom_dns:
        return {"classification": "public", "misconfig_kind": "custom_dns_override",
                "verdict": f"Resolves to PUBLIC IP {ip} — the VNet uses custom DNS server(s) {', '.join(custom_dns)} that don't forward {zone or 'privatelink'} to Azure DNS (168.63.129.16)."}
    # Public with no clear control-plane signal (command-exec off): still actionable.
    return {"classification": "public", "misconfig_kind": "public_unknown",
            "verdict": f"Resolves to PUBLIC IP {ip} instead of a private one. Likely a missing/unlinked Private DNS zone ({zone or 'privatelink.*'}) or a custom-DNS override — enable Azure-side checks for the exact cause."}


async def _run_one_source(vm: dict[str, Any], *, fqdn: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the chain for one source; returns (summary, steps)."""
    steps: list[dict[str, Any]] = []
    state: dict[str, Any] = {}
    async for ev in run_resolution(vm, fqdn=fqdn):
        if ev.get("type") == "step":
            steps.append({k: ev[k] for k in ("step", "status", "evidence", "command", "raw", "duration_ms")})
        elif ev.get("type") == "state":
            state = {k: v for k, v in ev.items() if k != "type"}
    return state, steps


async def run_analysis(
    *,
    tenant_id: str,
    actor: str,
    architecture: dict[str, Any],
    source_vm_ids: list[str],
    fqdn: str,
    source_vnet_id: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """Stream SSE-ready events: start → (per source) source_start → step* → source_done →
    evidence → done(run+diff+verdict)."""
    architecture_id = architecture.get("id", "")
    fqdn = (fqdn or "").strip()
    vms = resolve_sources(architecture, vm_ids=source_vm_ids)

    if not vms:
        yield {"event": "error", "data": {"message": "No sandbox VM onboarded for this architecture's workload. Onboard one in Settings → Sandbox VMs."}}
        return
    if not fqdn:
        yield {"event": "error", "data": {"message": "No target FQDN provided."}}
        return

    yield {"event": "start", "data": {"architecture_id": architecture_id, "fqdn": fqdn, "source_count": len(vms)}}

    # Azure-side truth (once; shared across sources). Use the architecture's OWN connection
    # (its workload's connection_id) so private-DNS zone facts resolve even when the
    # subscription is reachable only via a non-default connection.
    from app.core.azure_connections import connection_for_workload, resolve_connection
    from app.dnsdebug.zones import gather_zone_facts

    conn_id = architecture.get("connection_id") or ""
    if conn_id:
        connection = resolve_connection(conn_id)
    elif architecture.get("workload_id"):
        from app.workloads.registry import get_workload

        connection = connection_for_workload(get_workload(str(architecture["workload_id"])))
    else:
        from app.core.azure_connections import get_default_connection

        connection = get_default_connection()
    zone_facts = await gather_zone_facts(connection, fqdn=fqdn, source_vnet_id=source_vnet_id)
    yield {"event": "evidence", "data": zone_facts}

    source_results: list[dict[str, Any]] = []
    for vm in vms:
        label = vm.get("display_name") or vm.get("vnet_label") or vm.get("id")
        yield {"event": "source_start", "data": {"source": label, "vm_id": vm.get("id")}}
        state, steps = await _run_one_source(vm, fqdn=fqdn)
        for s in steps:
            yield {"event": "step", "data": {"source": label, **s}}
        cls = classify(state, zone_facts)
        summary = {
            "source": label,
            "vm_id": vm.get("id"),
            "resolved_ip": state.get("resolved_ip", ""),
            "classification": cls["classification"],
            "misconfig_kind": cls["misconfig_kind"],
            "verdict": cls["verdict"],
            "custom_dns": state.get("custom_dns", []),
            "steps": steps,
        }
        source_results.append(summary)
        yield {"event": "source_done", "data": summary}

    # Overall verdict = the worst source (public/nxdomain beats private).
    rank = {"public": 0, "nxdomain": 1, "private": 2}
    worst = min(source_results, key=lambda s: rank.get(s["classification"], 3)) if source_results else {}

    key = store.run_key(architecture_id, ",".join(s["source"] for s in source_results), fqdn)
    prev = store.latest_for_key(tenant_id, key)
    run = {
        "key": key,
        "architecture_id": architecture_id,
        "fqdn": fqdn,
        "source_vnet_id": source_vnet_id,
        "sources": source_results,
        "zone_facts": zone_facts,
        "verdict": worst.get("verdict", ""),
        "misconfig_kind": worst.get("misconfig_kind", ""),
        "overall_classification": worst.get("classification", ""),
        "created_at": _now(),
        "created_by": actor,
    }
    diff = store.diff_runs(prev, run)
    run = store.save_run(tenant_id, run)
    yield {"event": "done", "data": {"run": run, "diff": diff, "previous_id": (prev or {}).get("id", "")}}

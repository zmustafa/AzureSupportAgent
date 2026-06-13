"""Orchestrate a reachability run: pick the source VM, run the live probe, gather Azure
evidence, compare observed reachability against the architecture Memory's expected_flow,
build the canvas path overlay, persist + diff. Streams events for the live UI animation."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.netcheck import store
from app.netcheck.probe import STATUS_FAIL, STATUS_OK, STATUS_WARN, run_probe

log = logging.getLogger("app.netcheck.analyzer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_source_vm(architecture: dict[str, Any], *, vm_id: str = "") -> dict[str, Any] | None:
    """Pick the sandbox VM to probe FROM: explicit vm_id, else the first enabled box on
    the architecture's workload."""
    from app.core.sandbox_vms import get_vm, resolve_for_workload

    if vm_id:
        return get_vm(vm_id)
    wl = architecture.get("workload_id") if architecture else ""
    vms = resolve_for_workload(wl) if wl else []
    return vms[0] if vms else None


def _verdict(steps: list[dict[str, Any]]) -> str:
    """Overall reachable/blocked/degraded verdict from the authoritative steps."""
    tcp = next((s for s in steps if s["step"] == "tcp"), None)
    http = next((s for s in steps if s["step"] == "http"), None)
    if tcp and tcp["status"] == STATUS_FAIL:
        return "blocked"
    if http and http["status"] == STATUS_FAIL:
        return "degraded"
    if any(s["status"] == STATUS_FAIL for s in steps if s["step"] in ("dns", "tcp")):
        return "blocked"
    if any(s["status"] == STATUS_WARN for s in steps):
        return "degraded"
    return "reachable"


def _node_arm_lookup(architecture: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n.get("id")): n for n in (architecture.get("nodes") or [])}


def _build_path(architecture: dict[str, Any], source_node_id: str, target_node_id: str, verdict: str) -> list[dict[str, Any]]:
    """A simple canvas path overlay: source → (boundary/NSG nodes if discoverable) → target.

    Without live topology we render source→target and mark the failing hop; the UI animates
    edges along this node sequence."""
    path = []
    if source_node_id:
        path.append({"node_id": source_node_id, "role": "source"})
    if target_node_id:
        path.append({"node_id": target_node_id, "role": "target", "status": "fail" if verdict == "blocked" else "ok"})
    return path


def _expected_flow_text(architecture_id: str) -> str:
    from app.architectures.memory import get_memory

    try:
        mem = get_memory(architecture_id)
    except Exception:  # noqa: BLE001
        return ""
    if not mem:
        return ""
    parts = []
    for sec in mem.get("sections", []) or []:
        if sec.get("key") in ("expected_flow", "network_topology") and sec.get("content"):
            parts.append(f"[{sec.get('key')}]\n{sec['content']}")
    return "\n\n".join(parts)


def _intent_mismatch(expected_text: str, *, target: str, port: int, verdict: str) -> dict[str, Any] | None:
    """Compare observed reachability against the Memory expected_flow text (heuristic).

    If the expected flow mentions this target/port as an allowed path but we observed it
    blocked (or vice-versa), surface the contradiction."""
    if not expected_text:
        return None
    text = expected_text.lower()
    mentions_target = bool(target and target.split(".")[0].lower() in text)
    mentions_port = str(port) in text
    if not (mentions_target or mentions_port):
        return None
    # Heuristic: expected_flow describing a path implies it SHOULD be reachable.
    if verdict == "blocked":
        return {
            "kind": "expected_reachable_but_blocked",
            "detail": (
                f"Memory's expected_flow references {target}{':' + str(port) if mentions_port else ''}, "
                "implying this path should work — but the live probe found it BLOCKED."
            ),
        }
    return None


async def run_analysis(
    *,
    tenant_id: str,
    actor: str,
    architecture: dict[str, Any],
    source_vm_id: str = "",
    source_host_override: str = "",
    source_node_id: str = "",
    target_node_id: str = "",
    target_host: str = "",
    port: int = 443,
    protocol: str = "tcp",
    payload: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream the run as SSE-ready events:
        {event: 'start', data:{...}}            run metadata + planned steps
        {event: 'step',  data:{...}}            one probe step result (animate this hop)
        {event: 'evidence', data:{...}}         Azure control-plane corroboration
        {event: 'done',  data:{run}}            final aggregated run + verdict + diff + mismatch
    """
    architecture_id = architecture.get("id", "")
    arm_nodes = _node_arm_lookup(architecture)

    # Resolve target host (explicit override wins, else the clicked node's fqdn/ip).
    if not target_host and target_node_id:
        node = arm_nodes.get(target_node_id, {})
        meta = node.get("meta") or {}
        target_host = meta.get("fqdn") or meta.get("private_ip") or node.get("name", "")
    target_host = (target_host or "").strip()

    vm = resolve_source_vm(architecture, vm_id=source_vm_id)
    source_label = source_host_override or (vm.get("display_name") if vm else "") or "sandbox VM"

    if not vm and not source_host_override:
        yield {"event": "error", "data": {"message": "No sandbox VM is onboarded for this architecture's workload. Onboard one in Settings → Sandbox VMs, or enter a source host."}}
        return
    if not target_host:
        yield {"event": "error", "data": {"message": "No target host/FQDN/IP resolved. Enter a target."}}
        return

    yield {
        "event": "start",
        "data": {
            "architecture_id": architecture_id,
            "source": source_label,
            "source_vm_id": vm.get("id") if vm else "",
            "target": target_host,
            "port": port,
            "protocol": protocol,
        },
    }

    steps: list[dict[str, Any]] = []
    if vm:
        async for ev in run_probe(vm, target=target_host, port=port, protocol=protocol, payload=payload):
            steps.append({k: ev[k] for k in ("step", "status", "evidence", "command", "raw", "duration_ms")})
            yield {"event": "step", "data": ev}
    else:
        # Source override without an onboarded VM: cannot run live probes from an arbitrary
        # host (no SSH creds). Report clearly instead of faking it.
        yield {"event": "error", "data": {"message": "A source host was entered but no onboarded sandbox VM backs it; live probes require an SSH-reachable sandbox. Pick an onboarded VM."}}
        return

    verdict = _verdict(steps)

    # Azure control-plane evidence (best-effort).
    from app.core.azure_connections import get_default_connection
    from app.netcheck.evidence import gather_evidence, matched_deny_rule

    connection = get_default_connection()
    target_node = arm_nodes.get(target_node_id, {})
    evidence = await gather_evidence(
        connection,
        source_nic_id=(vm or {}).get("nic_id", ""),
        target_ip=target_host,
        target_resource_id=target_node.get("arm_id", ""),
    )
    deny = matched_deny_rule(evidence.get("nsg_rules", []), port) if verdict == "blocked" else None
    if deny:
        evidence["matched_deny"] = deny
    yield {"event": "evidence", "data": evidence}

    # Intent vs observed (Memory).
    mismatch = _intent_mismatch(_expected_flow_text(architecture_id), target=target_host, port=port, verdict=verdict)

    path = _build_path(architecture, source_node_id, target_node_id, verdict)

    key = store.run_key(architecture_id, source_label, target_host, port)
    prev = store.latest_for_key(tenant_id, key)
    run = {
        "key": key,
        "architecture_id": architecture_id,
        "source": source_label,
        "source_vm_id": vm.get("id") if vm else "",
        "target": target_host,
        "port": port,
        "protocol": protocol,
        "payload": payload or {},
        "steps": steps,
        "verdict": verdict,
        "evidence": evidence,
        "mismatch": mismatch,
        "path": path,
        "created_at": _now(),
        "created_by": actor,
    }
    diff = store.diff_runs(prev, run)
    run = store.save_run(tenant_id, run)
    yield {"event": "done", "data": {"run": run, "diff": diff, "previous_id": (prev or {}).get("id", "")}}

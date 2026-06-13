"""Dummy reachability runs for review/demo without a live sandbox VM.

Produces a realistic *blocked* run (DNS ok, ICMP blocked, TCP timed out at an NSG) and a
*reachable* run, plus a stored "previous" run so the Re-run diff has something to compare.
Marked demo=True so it's clearly distinguishable. Bound to the shared demo architecture if
one exists, else a synthetic id."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.netcheck import store

DEMO_ARCH_ID = "demo-netcheck"
DEMO_SOURCE = "shop-jumpbox (sandbox)"
DEMO_TARGET = "orders-db.privatelink.postgres.database.azure.com"
DEMO_PORT = 5432


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blocked_steps() -> list[dict[str, Any]]:
    return [
        {"step": "dns", "status": "ok", "evidence": "Resolved to 10.20.3.14 (private zone)",
         "command": "dig +short orders-db…", "raw": "10.20.3.14", "duration_ms": 120},
        {"step": "icmp", "status": "warn", "evidence": "ICMP blocked (common; not conclusive)",
         "command": "ping -c 3 …", "raw": "3 packets transmitted, 0 received, 100% packet loss", "duration_ms": 3100},
        {"step": "tcp", "status": "fail", "evidence": "TCP connect timed out (likely NSG/firewall/route block)",
         "command": "nc -vz -w 5 orders-db… 5432", "raw": "nc: connect to orders-db… port 5432 (tcp) timed out: Operation now in progress", "duration_ms": 5050},
    ]


def _reachable_steps() -> list[dict[str, Any]]:
    return [
        {"step": "dns", "status": "ok", "evidence": "Resolved to 10.20.3.14 (private zone)",
         "command": "dig +short orders-db…", "raw": "10.20.3.14", "duration_ms": 110},
        {"step": "icmp", "status": "warn", "evidence": "ICMP blocked (common; not conclusive)",
         "command": "ping -c 3 …", "raw": "100% packet loss", "duration_ms": 3050},
        {"step": "tcp", "status": "ok", "evidence": "TCP connect succeeded",
         "command": "nc -vz -w 5 orders-db… 5432", "raw": "Connection to orders-db… 5432 port [tcp/postgresql] succeeded!", "duration_ms": 240},
    ]


def _demo_evidence(blocked: bool) -> dict[str, Any]:
    rules = [
        {"nsg": "nsg-spoke1-app", "name": "AllowVnetOutbound", "access": "Allow", "direction": "Outbound",
         "protocol": "*", "destinationPortRange": "*", "priority": 65000},
    ]
    if blocked:
        rules.insert(0, {"nsg": "nsg-spoke1-app", "name": "DenyDbOutbound", "access": "Deny", "direction": "Outbound",
                         "protocol": "Tcp", "destinationPortRange": "5432", "priority": 200})
    ev = {
        "available": True,
        "notes": "Effective routes + NSG rules read from the source NIC.",
        "effective_routes": [
            {"addressPrefix": ["10.20.0.0/16"], "nextHopType": "VnetLocal"},
            {"addressPrefix": ["0.0.0.0/0"], "nextHopType": "VirtualAppliance", "nextHopIpAddress": ["10.10.0.4"]},
        ],
        "nsg_rules": rules,
        "peerings": [{"name": "hub-to-spoke1", "peeringState": "Connected"}],
        "error": "",
    }
    if blocked:
        ev["matched_deny"] = rules[0]
    return ev


def build_demo_run(*, blocked: bool = True, tenant_id: str = "default") -> dict[str, Any]:
    steps = _blocked_steps() if blocked else _reachable_steps()
    verdict = "blocked" if blocked else "reachable"
    key = store.run_key(DEMO_ARCH_ID, DEMO_SOURCE, DEMO_TARGET, DEMO_PORT)
    run = {
        "key": key,
        "architecture_id": DEMO_ARCH_ID,
        "source": DEMO_SOURCE,
        "source_vm_id": "",
        "target": DEMO_TARGET,
        "port": DEMO_PORT,
        "protocol": "tcp",
        "payload": {},
        "steps": steps,
        "verdict": verdict,
        "evidence": _demo_evidence(blocked),
        "mismatch": (
            {
                "kind": "expected_reachable_but_blocked",
                "detail": "Memory's expected_flow says app → orders-db:5432 is an allowed path, but the live probe found it BLOCKED at nsg-spoke1-app rule DenyDbOutbound.",
            }
            if blocked else None
        ),
        "path": [
            {"node_id": "app", "role": "source"},
            {"node_id": "db", "role": "target", "status": "fail" if blocked else "ok"},
        ],
        "created_at": _now(),
        "created_by": "system-demo",
        "demo": True,
    }
    return run


def seed_demo(*, tenant_id: str = "default") -> dict[str, Any]:
    # Seed a prior reachable run, then a current blocked run, so the diff shows tcp ok→fail.
    prev = build_demo_run(blocked=False, tenant_id=tenant_id)
    store.save_run(tenant_id, prev)
    cur = build_demo_run(blocked=True, tenant_id=tenant_id)
    diff = store.diff_runs(prev, cur)
    cur = store.save_run(tenant_id, cur)
    return {"run": cur, "diff": diff, "previous_id": prev.get("id", "")}

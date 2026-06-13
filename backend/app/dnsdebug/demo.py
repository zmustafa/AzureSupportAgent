"""Dummy DNS-debug runs for review/demo without a live sandbox VM.

A multi-source diagnosis where spoke1 resolves PRIVATE (healthy) and spoke2 resolves PUBLIC
because the privatelink zone isn't linked to spoke2's VNet — the classic hub-and-spoke
asymmetry. Marked demo=True. Stores a prior run so the Re-run diff has a baseline."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.dnsdebug import store

DEMO_ARCH_ID = "demo-dnsdebug"
DEMO_FQDN = "shopassets.blob.core.windows.net"
DEMO_ZONE = "privatelink.blob.core.windows.net"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chain(*, private: bool, custom_dns: bool) -> list[dict[str, Any]]:
    resolvers = ["10.10.0.4"] if custom_dns else ["168.63.129.16"]
    ip = "10.20.3.18" if private else "20.150.34.4"
    return [
        {"step": "effective_dns", "status": "warn" if custom_dns else "ok",
         "evidence": ("Custom DNS server(s): " + ", ".join(resolvers)) if custom_dns else "DNS server(s): 168.63.129.16",
         "command": "resolvectl status", "raw": "DNS Servers: " + " ".join(resolvers), "duration_ms": 90},
        {"step": "resolve", "status": "ok", "evidence": f"Resolved to {ip}",
         "command": "dig +short …", "raw": ip, "duration_ms": 130},
        {"step": "cname", "status": "ok", "evidence": "CNAME chain reaches privatelink.* (expected for PE)",
         "command": "dig CNAME …", "raw": f"{DEMO_FQDN} CNAME shopassets.{DEMO_ZONE}.", "duration_ms": 120},
        {"step": "hosts", "status": "ok", "evidence": "No /etc/hosts shadow entry", "command": "getent hosts …", "raw": "no hosts entry", "duration_ms": 30},
        {"step": "classify", "status": "ok" if private else "fail",
         "evidence": f"{ip} is a {'PRIVATE' if private else 'PUBLIC'} IP", "command": "", "raw": "", "duration_ms": 0},
    ]


def _zone_facts(*, linked_spoke2: bool) -> dict[str, Any]:
    return {
        "available": True,
        "notes": "Private DNS zone landscape read from Azure.",
        "expected_zone": DEMO_ZONE,
        "zone_exists": True,
        "linked_to_source_vnet": linked_spoke2,  # reflects the failing spoke
        "a_record_ip": "10.20.3.18",
        "custom_dns_servers": [],
        "error": "",
    }


def build_demo_run(*, healthy: bool = False, tenant_id: str = "default") -> dict[str, Any]:
    # spoke1 = private (linked); spoke2 = public (not linked) unless healthy.
    spoke2_private = healthy
    sources = [
        {
            "source": "spoke1-jumpbox", "vm_id": "", "resolved_ip": "10.20.3.18",
            "classification": "private", "misconfig_kind": "",
            "verdict": "Resolves to the private IP 10.20.3.18 — Private Link DNS is working from this source.",
            "custom_dns": [], "steps": _chain(private=True, custom_dns=False),
        },
        {
            "source": "spoke2-jumpbox", "vm_id": "", "resolved_ip": ("10.20.3.18" if spoke2_private else "20.150.34.4"),
            "classification": "private" if spoke2_private else "public",
            "misconfig_kind": "" if spoke2_private else "missing_link",
            "verdict": ("Resolves to the private IP 10.20.3.18 — working." if spoke2_private
                        else f"Resolves to PUBLIC IP 20.150.34.4 — Private DNS zone {DEMO_ZONE} exists but is NOT linked to spoke2's VNet. Add a virtual-network link."),
            "custom_dns": [], "steps": _chain(private=spoke2_private, custom_dns=False),
        },
    ]
    worst = "private" if spoke2_private else "public"
    verdict = sources[1]["verdict"]
    key = store.run_key(DEMO_ARCH_ID, "spoke1-jumpbox,spoke2-jumpbox", DEMO_FQDN)
    return {
        "key": key,
        "architecture_id": DEMO_ARCH_ID,
        "fqdn": DEMO_FQDN,
        "source_vnet_id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet-spoke2",
        "sources": sources,
        "zone_facts": _zone_facts(linked_spoke2=spoke2_private),
        "verdict": verdict,
        "misconfig_kind": "" if spoke2_private else "missing_link",
        "overall_classification": worst,
        "pe_private_ip": "10.20.3.18",
        "created_at": _now(),
        "created_by": "system-demo",
        "demo": True,
    }


def seed_demo(*, tenant_id: str = "default") -> dict[str, Any]:
    prev = build_demo_run(healthy=True, tenant_id=tenant_id)
    store.save_run(tenant_id, prev)
    cur = build_demo_run(healthy=False, tenant_id=tenant_id)
    diff = store.diff_runs(prev, cur)
    cur = store.save_run(tenant_id, cur)
    return {"run": cur, "diff": diff, "previous_id": prev.get("id", "")}

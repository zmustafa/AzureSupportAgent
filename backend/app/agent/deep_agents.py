"""Specialist investigation agents for Deep Investigation's "war room".

A deep investigation can dispatch several specialist sub-agents, each focused on a
domain of the Azure estate (networking, identity, compute, …). Before a run starts the
UI shows this catalog with the relevant agents AI-pre-selected; the chosen agents become
the live roster shown in the war room and each formed hypothesis is attributed to one.

This module owns the catalog and a lightweight AI suggester (with a deterministic
keyword fallback so the picker always has sensible defaults even if the model is slow or
unavailable).
"""
from __future__ import annotations

from typing import Any

# Fixed catalog. ``keywords`` drives the deterministic fallback selection.
DEEP_AGENTS: list[dict[str, Any]] = [
    {
        "id": "networking",
        "name": "Networking",
        "icon": "🌐",
        "domain": "NSGs, routes, DNS, private endpoints, peering, connectivity",
        "keywords": ["network", "nsg", "subnet", "vnet", "dns", "connect", "rdp", "ssh",
                     "firewall", "peering", "vpn", "expressroute", "private endpoint",
                     "port", "ip", "gateway", "load balancer", "latency", "timeout",
                     "unreachable", "503", "502", "504"],
    },
    {
        "id": "identity",
        "name": "Identity & Access",
        "icon": "🔑",
        "domain": "RBAC, Entra ID, managed identities, Key Vault, secrets, app registrations",
        "keywords": ["rbac", "role", "permission", "identity", "managed identity", "entra",
                     "aad", "key vault", "keyvault", "secret", "service principal", "spn",
                     "app registration", "auth", "unauthorized", "403", "401", "access denied",
                     "consent", "token", "certificate"],
    },
    {
        "id": "compute",
        "name": "Compute & Apps",
        "icon": "⚡",
        "domain": "VMs, App Service, AKS, Container Apps, Functions, scale sets",
        "keywords": ["vm", "virtual machine", "app service", "webapp", "web app", "aks",
                     "kubernetes", "container", "function", "scale set", "vmss", "deployment",
                     "pod", "crash", "restart", "cpu", "memory", "boot", "startup", "app"],
    },
    {
        "id": "storage",
        "name": "Storage & Data",
        "icon": "💾",
        "domain": "Storage accounts, SQL, Cosmos DB, disks, backups, data services",
        "keywords": ["storage", "blob", "disk", "sql", "database", "cosmos", "backup",
                     "data", "table", "queue", "file share", "managed disk", "snapshot",
                     "replication", "throughput", "dtu", "capacity"],
    },
    {
        "id": "security",
        "name": "Security & Exposure",
        "icon": "🔐",
        "domain": "Public exposure, Defender for Cloud, open ports, NSG 0.0.0.0/0, posture",
        "keywords": ["security", "exposure", "public", "internet", "defender", "0.0.0.0",
                     "open port", "vulnerab", "cve", "exposed", "breach", "attack",
                     "compliance", "encryption", "tls", "https"],
    },
    {
        "id": "reliability",
        "name": "Reliability & Performance",
        "icon": "📈",
        "domain": "Resource health, availability, metrics, scaling, SLA, resilience",
        "keywords": ["health", "availability", "reliab", "performance", "slow", "degraded",
                     "outage", "down", "sla", "scaling", "autoscale", "resilien", "failover",
                     "zone", "region", "metric", "throttle", "429"],
    },
    {
        "id": "cost",
        "name": "Cost & Governance",
        "icon": "💰",
        "domain": "Spend anomalies, Azure Policy, quotas, tags, budgets, governance",
        "keywords": ["cost", "spend", "bill", "budget", "policy", "quota", "tag",
                     "governance", "compliance", "expensive", "savings", "reservation",
                     "waste", "idle", "unused"],
    },
    {
        "id": "monitoring",
        "name": "Monitoring & Logs",
        "icon": "📊",
        "domain": "Activity logs, alerts, App Insights, diagnostics, Log Analytics",
        "keywords": ["log", "alert", "monitor", "diagnostic", "app insights",
                     "application insights", "log analytics", "activity log", "trace",
                     "exception", "error log", "kql", "query log", "telemetry", "event"],
    },
]

_AGENTS_BY_ID = {a["id"]: a for a in DEEP_AGENTS}


def get_agents(ids: list[str]) -> list[dict[str, Any]]:
    """Resolve agent ids to their public catalog entries (icon/name/domain), order-preserving."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i in ids:
        a = _AGENTS_BY_ID.get(i)
        if a and i not in seen:
            seen.add(i)
            out.append({"id": a["id"], "name": a["name"], "icon": a["icon"], "domain": a["domain"]})
    return out


def public_catalog() -> list[dict[str, Any]]:
    """The catalog without internal keyword lists (safe for the API)."""
    return [{"id": a["id"], "name": a["name"], "icon": a["icon"], "domain": a["domain"]} for a in DEEP_AGENTS]


def _heuristic_pick(question: str) -> dict[str, str]:
    """Deterministic keyword scoring: returns {agent_id: reason} for relevant agents."""
    q = (question or "").lower()
    picks: dict[str, str] = {}
    for a in DEEP_AGENTS:
        hits = [k for k in a["keywords"] if k in q]
        if hits:
            picks[a["id"]] = f"Matched: {', '.join(hits[:3])}"
    return picks


async def suggest_agents(
    question: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Return the full catalog, each annotated with ``recommended`` (bool) + ``reason``.

    Uses the active LLM to choose which specialists are relevant, falling back to a
    deterministic keyword match (and finally to a sensible default trio) so the picker
    is always pre-populated.
    """
    catalog = public_catalog()
    heuristic = _heuristic_pick(question)

    chosen: dict[str, str] = {}
    try:
        from app.agent.factory import build_provider_for
        from app.core.utils import safe_json_parse

        listing = "\n".join(f"- {a['id']}: {a['name']} — {a['domain']}" for a in DEEP_AGENTS)
        sys = (
            "You triage an Azure troubleshooting question and pick which specialist "
            "investigation agents should be dispatched. Choose ONLY the agents whose "
            "domain is clearly relevant to the question (usually 2-4). Respond with ONLY "
            'a JSON array: [{"id": "<agent id>", "reason": "<=12 words why"}]. Valid ids:\n'
            f"{listing}"
        )
        prov = build_provider_for(provider, model)
        acc = ""
        async for ev in prov.stream(
            [{"role": "system", "content": sys}, {"role": "user", "content": question[:2000]}],
            None,
        ):
            if getattr(ev, "type", "") == "token":
                acc += getattr(ev, "text", "")
        parsed = safe_json_parse(acc, default=None)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get("id") in _AGENTS_BY_ID:
                    chosen[str(item["id"])] = str(item.get("reason", ""))[:120]
    except Exception:  # noqa: BLE001 - fall back to the heuristic below
        chosen = {}

    if not chosen:
        chosen = heuristic
    # Final safety net: never present an empty pre-selection.
    if not chosen:
        chosen = {
            "reliability": "General health & availability check",
            "monitoring": "Inspect logs and alerts for signals",
            "compute": "Examine the affected workloads",
        }

    for a in catalog:
        a["recommended"] = a["id"] in chosen
        a["reason"] = chosen.get(a["id"]) or heuristic.get(a["id"], "")
    return catalog

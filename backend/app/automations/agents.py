"""Custom Agents registry (admin-managed).

A *custom agent* is a specialized worker: a name, natural-language instructions, a
chosen provider/model, an Azure tenant connection, an explicit set of allowed tools
(connector + Azure MCP), and a run mode (autonomous | review). Scheduled tasks (and
ad-hoc runs) invoke a custom agent. Persisted as JSON under backend/.data, consistent
with the other registries. No secrets here, so no encryption needed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "custom_agents.json"

# Curated sub-agent categories (id -> label + icon). Agents carry a ``category`` id; the
# UI groups the list by these. "general" is the catch-all for uncategorized agents.
CATEGORIES: list[dict[str, str]] = [
    {"id": "networking", "label": "Networking", "icon": "🌐"},
    {"id": "compute", "label": "Compute", "icon": "⚙️"},
    {"id": "data", "label": "Data & Storage", "icon": "🗄️"},
    {"id": "security", "label": "Security & Identity", "icon": "🔐"},
    {"id": "operations", "label": "Operations & Monitoring", "icon": "📈"},
    {"id": "cost", "label": "Cost & Governance", "icon": "💰"},
    {"id": "general", "label": "General", "icon": "🧩"},
]
_CATEGORY_IDS = {c["id"] for c in CATEGORIES}

DEFAULTS: dict[str, Any] = {
    "name": "",
    "instructions": "",
    "category": "general",  # one of CATEGORIES ids
    "provider": "",
    "model": "",
    "connection_id": "",
    # Tool selection. allow_all_azure exposes every Azure MCP tool; allow_all_entra
    # exposes the EntraID (Microsoft Graph) MCP tools; connector_tools is a list of
    # connector tool names (e.g. ["email_send", "teams_post_message"]).
    "allow_all_azure": True,
    "allow_all_entra": False,
    # Optional scoped catalogs. Existing agents keep allow-all behavior; setting an
    # allow-all flag false with explicit names/bundles restricts what routing may use.
    "azure_tools": [],
    "azure_bundles": [],
    "entra_tools": [],
    "entra_bundles": [],
    "connector_tools": [],
    "run_mode": "review",  # review | autonomous
    # Whether the agent is enabled. Disabled agents are hidden from the chat sidebar's
    # quick-launch menu and the composer's agent picker, but are kept (not deleted).
    "enabled": True,
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"agents": {}})
    return data if isinstance(data, dict) else {"agents": {}}


def list_agents() -> list[dict[str, Any]]:
    data = _read()
    out: list[dict[str, Any]] = []
    for aid, agent in data.get("agents", {}).items():
        merged = dict(DEFAULTS)
        merged.update(agent)
        merged["id"] = aid
        # Normalize the category to a known id (fall back to "general").
        if merged.get("category") not in _CATEGORY_IDS:
            merged["category"] = "general"
        out.append(merged)
    out.sort(key=lambda a: a.get("name", "").lower())
    return out


def get_agent(agent_id: str) -> dict[str, Any] | None:
    if not agent_id:
        return None
    for a in list_agents():
        if a["id"] == agent_id:
            return a
    return None


def upsert_agent(agent: dict[str, Any]) -> dict[str, Any]:
    aid = agent.get("id") or str(uuid.uuid4())
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        agents = data.setdefault("agents", {})
        existing = agents.get(aid, {})
        is_update = bool(existing)
        merged = dict(DEFAULTS)
        merged.update(existing)
        # Defense-in-depth: never let a partial UPDATE blank out the meaningful identity
        # fields. A bulk model change sends only {name, provider, model}; an empty/missing
        # instructions or name must NOT overwrite the stored value (that bug wiped every
        # agent's instructions once). For a brand-new agent these may be empty.
        preserve_if_blank = {"instructions", "name"}
        for key in DEFAULTS:
            if key not in agent or agent[key] is None:
                continue
            if is_update and key in preserve_if_blank and not str(agent[key]).strip():
                continue  # keep the existing non-blank value
            merged[key] = agent[key]
        merged["created_at"] = existing.get("created_at") or _now()
        merged["updated_at"] = _now()
        # Normalize the category; auto-classify a brand-new agent left uncategorized.
        if merged.get("category") not in _CATEGORY_IDS:
            merged["category"] = (
                existing.get("category")
                if existing.get("category") in _CATEGORY_IDS
                else classify_category(merged.get("name", ""), merged.get("instructions", ""))
            )
        merged.pop("id", None)
        agents[aid] = merged
        result.update(merged)
        result["id"] = aid

    jsonstore.mutate_json(_PATH, {"agents": {}}, _mutate)
    return result


def delete_agent(agent_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        if agent_id in data.get("agents", {}):
            del data["agents"][agent_id]
            deleted = True

    jsonstore.mutate_json(_PATH, {"agents": {}}, _mutate)
    return deleted


# Keyword → category rules used to auto-classify existing/unseen agents by name. First
# matching rule wins; order matters (more specific first).
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("networking", ["network", "dns", "firewall", "waf", "vpn", "expressroute", "load balancer", "ingress", "traffic"]),
    ("security", ["security", "identity", "access", "entra", "rbac", "policy", "defender", "compliance", "key vault", "secret"]),
    ("data", ["sql", "database", "data platform", "storage", "cosmos", "postgres", "mysql", "redis", "blob", "data factory"]),
    ("compute", ["vm", "virtual machine", "aks", "kubernetes", "container app", "app service", "web app", "function", "compute", "scale set"]),
    ("cost", ["cost", "billing", "finops", "budget", "governance", "tag"]),
    ("operations", ["monitor", "alert", "log", "deployment", "devops", "backup", "recovery", "reliability", "resilien", "ops", "sre", "health"]),
]


def classify_category(name: str, instructions: str = "") -> str:
    """Best-effort category id from an agent's name (preferred) or instructions.

    The NAME is matched first because it's specific ("DNS Agent", "SQL Agent"); the
    instructions are only consulted as a fallback, since nearly every Azure agent's body
    mentions cross-cutting words like "network" which would otherwise mis-classify it."""
    name_l = name.lower()
    for cat, keywords in _CATEGORY_RULES:
        if any(k in name_l for k in keywords):
            return cat
    body_l = instructions.lower()
    for cat, keywords in _CATEGORY_RULES:
        if any(k in body_l for k in keywords):
            return cat
    return "general"


def seed_categories() -> int:
    """Assign a category to any agent that doesn't have one yet (idempotent).

    Returns the number of agents updated. Never overwrites an explicit category."""
    changed = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal changed
        for stored in data.get("agents", {}).values():
            if stored.get("category") in _CATEGORY_IDS:
                continue
            stored["category"] = classify_category(
                stored.get("name", ""), stored.get("instructions", "")
            )
            changed += 1

    jsonstore.mutate_json(_PATH, {"agents": {}}, _mutate)
    return changed


# Curated starter sub-agents shipped with the product (a full Azure troubleshooting team:
# Networking, Compute, Data, Security, Operations, Cost). Bundled IN the package (NOT under
# .data, which is gitignored / excluded from the image) so a fresh install / deployment comes
# up with them pre-loaded. Each carries an empty provider/model/connection_id so it inherits
# the deployment's active LLM provider and default Azure connection at run time.
_SEED_PATH = Path(__file__).resolve().parent / "builtin_agents.json"


def _read_builtin_seed() -> dict[str, Any]:
    data = jsonstore.read_json(_SEED_PATH, {})
    if isinstance(data, dict):
        return data.get("agents", {}) or {}
    return {}


def seed_if_empty() -> int:
    """Seed the curated starter sub-agents on first run, ONLY when the registry is empty.

    Returns the number of agents seeded. Idempotent: once any agent exists (including after
    the admin edits or deletes some), this never re-adds them, so a deleted starter stays
    deleted. Mirrors the workbook / sample-control starter-seed convention."""
    builtins = _read_builtin_seed()
    if not builtins:
        return 0
    seeded = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal seeded
        if data.get("agents"):
            return
        agents = data.setdefault("agents", {})
        for aid, seed in builtins.items():
            merged = dict(DEFAULTS)
            merged.update(seed)
            if merged.get("category") not in _CATEGORY_IDS:
                merged["category"] = classify_category(
                    merged.get("name", ""), merged.get("instructions", "")
                )
            merged["created_at"] = _now()
            merged["updated_at"] = _now()
            merged.pop("id", None)
            agents[aid] = merged
        seeded = len(agents)

    jsonstore.mutate_json(_PATH, {"agents": {}}, _mutate)
    return seeded


"""AI architecture designer.

Given a workload's resource inventory (with full Azure Resource Graph ``properties``),
the LLM infers the application architecture: nodes (resources), edges (relationships read
from the config), groups (containers/tiers) and a rationale. Mirrors the workbook/agent
designers: a plain JSON completion with NO tools, grounded entirely on real resources so
it can't invent infrastructure. Output is validated/normalized before it reaches the UI.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from app.agent.factory import build_provider
from app.architectures import catalog
from app.architectures.layout import layout_nodes, needs_layout
from app.core.ai_prompts import get_full_prompt
from app.core.utils import safe_json_parse

logger = logging.getLogger("app.architectures.designer")

_VALID_EDGE_KINDS = ("depends_on", "connects_to", "data_flow", "network", "identity", "monitors")
_VALID_GROUP_KINDS = ("subscription", "resource_group", "vnet", "tier", "custom")


GENERATE_PROMPT = """\
You are a principal Azure solutions architect. You are given the COMPLETE resource \
inventory of one application "workload" — every resource with its real Azure Resource \
Graph `properties` (the actual configuration). Your job is to REVERSE-ENGINEER the \
application architecture and return it as a diagram (nodes + edges + groups).

Infer relationships from the `properties`, not from guesses. Use signals such as:
- Network interface `ipConfigurations[].subnet.id` -> NIC sits in a Subnet/VNet.
- VM `networkProfile.networkInterfaces[].id` and `storageProfile.osDisk.managedDisk.id`.
- Private endpoint `privateLinkServiceConnections[].privateLinkServiceId` -> the target.
- App Service `serverFarmId` -> its App Service Plan; site config / connection strings -> \
  databases, storage, Key Vault references.
- Function app linked storage; AKS `agentPoolProfiles[].vnetSubnetID`, `addonProfiles` \
  (monitoring, ACR), `networkProfile`.
- SQL database parent server (ARM id segment); Cosmos/Redis firewall vnet rules.
- Application Gateway / Front Door / Load Balancer backend pools -> their backends.
- `identity` (managed identity) -> resources it accesses; diagnostic settings -> Log \
  Analytics / Storage.

Then organize the diagram:
- Assign every node a `category` (web, compute, containers, data, storage, integration, \
  networking, security, ai, monitoring, analytics, other) and a `layer` tier (edge, \
  presentation, application, integration, data, networking, security, monitoring, shared).
- Create `groups` for meaningful containers you observe: each resource group, each VNet, \
  or a logical tier. Put a node in a group via its `group_id`.
- Only reference real resources from the inventory by their exact ARM `id` as the node's \
  `arm_id`. Do NOT invent resources. You MAY add at most a couple of conceptual nodes \
  (e.g. "Users", "Internet") with an empty `arm_id` when they clarify the entry point.
- Keep node `name` short (the resource name). Put 2–5 key facts in `meta` (e.g. tier, \
  sku, capacity, runtime) drawn from the properties.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "name": "Short architecture title",
  "description": "1-2 sentence summary of the application architecture",
  "nodes": [
    {
      "id": "n1",
      "arm_id": "<exact ARM id from the inventory, or '' for a conceptual node>",
      "name": "resource name",
      "type": "microsoft.web/sites",
      "category": "web",
      "layer": "presentation",
      "group_id": "g1",
      "meta": {"tier": "P1v3", "runtime": "dotnet"}
    }
  ],
  "edges": [
    {"id": "e1", "source": "n1", "target": "n2", "label": "reads/writes", "kind": "data_flow", "dashed": false}
  ],
  "groups": [
    {"id": "g1", "name": "prod-rg", "kind": "resource_group"}
  ],
  "rationale": "2-4 sentences explaining the inferred architecture and the key dependencies",
  "confidence": 0.0
}
"""


ENHANCE_PROMPT = """\
You are a principal Azure solutions architect refining an EXISTING architecture diagram. \
You are given the current diagram (nodes, edges, groups), the real resource inventory it \
was built from, and the owner's instruction. Improve the diagram per the instruction — \
add missing relationships, regroup, relabel, add conceptual nodes, fix categories/tiers — \
while PRESERVING correct existing structure and only referencing real resources by their \
exact ARM `id`.

Respond with ONLY a JSON object of the SAME shape as the generator (name, description, \
nodes[], edges[], groups[], rationale, confidence). Return the COMPLETE updated diagram, \
not a delta.
"""


def _resources_block(resources: list[dict[str, Any]]) -> str:
    """Compact JSON inventory the model reasons over."""
    return json.dumps(resources, separators=(",", ":"))


async def _complete_json(system: str, user: str) -> Any:
    provider = build_provider()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    # Architecture/diagram JSON for a real estate can be large; the default response cap
    # (~4k tokens) truncates it mid-JSON, so the parse yields nothing. Request a generous
    # cap for these structured completions so the JSON object is returned whole. Reasoning
    # models (e.g. gpt-5.x via the Responses API) occasionally return an empty completion
    # when reasoning consumes the budget, so retry once on an empty/unparseable result.
    text = ""
    for attempt in range(2):
        text = ""
        async for ev in provider.stream(messages, None, max_tokens=16000):
            if ev.type == "token":
                text += ev.text
        if text.strip():
            break
        if attempt == 0:
            logger.warning("Architecture JSON completion returned empty; retrying once.")
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)
    parsed = safe_json_parse(t, default=None)
    if parsed is None:
        logger.warning(
            "Architecture JSON completion did not parse (raw len=%d): head=%r tail=%r",
            len(text),
            text[:200],
            text[-200:],
        )
    return parsed


def _normalize(parsed: dict[str, Any], resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate + normalize raw LLM output into a safe architecture (nodes/edges/groups)."""
    arm_by_id = {r.get("id", "").lower(): r for r in resources if r.get("id")}

    # --- Groups ---
    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for g in parsed.get("groups") or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or f"g{len(groups)+1}")[:60]
        if gid in seen_groups:
            continue
        seen_groups.add(gid)
        kind = str(g.get("kind", "custom")).lower()
        if kind not in _VALID_GROUP_KINDS:
            kind = "custom"
        groups.append({
            "id": gid,
            "name": str(g.get("name", ""))[:120],
            "kind": kind,
            "color": "",
            "x": 0, "y": 0, "w": 0, "h": 0,
        })
    valid_group_ids = {g["id"] for g in groups}

    # --- Nodes ---
    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for n in parsed.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or f"n{len(nodes)+1}")[:60]
        if nid in node_ids:
            continue
        arm_id = str(n.get("arm_id", "") or "")
        src = arm_by_id.get(arm_id.lower())
        arm_type = (src.get("type") if src else n.get("type")) or ""
        category = str(n.get("category") or catalog.categorize(arm_type))
        if category not in catalog.CATEGORY_META:
            category = catalog.categorize(arm_type)
        layer = str(n.get("layer") or catalog.layer_for(category))
        meta = n.get("meta") if isinstance(n.get("meta"), dict) else {}
        meta = {str(k)[:40]: str(v)[:80] for k, v in list(meta.items())[:6]}
        gid = n.get("group_id")
        node_ids.add(nid)
        nodes.append({
            "id": nid,
            "arm_id": arm_id,
            "name": str(n.get("name") or (src.get("name") if src else "") or "resource")[:120],
            "type": str(arm_type)[:120],
            "category": category,
            "layer": layer,
            "resource_group": (src.get("resourceGroup") if src else n.get("resource_group")) or "",
            "subscription_id": (src.get("subscriptionId") if src else n.get("subscription_id")) or "",
            "location": (src.get("location") if src else n.get("location")) or "",
            "sku": _sku_label(src.get("sku")) if src else "",
            "meta": meta,
            "group_id": gid if gid in valid_group_ids else "",
            "x": float(n["x"]) if isinstance(n.get("x"), (int, float)) else 0.0,
            "y": float(n["y"]) if isinstance(n.get("y"), (int, float)) else 0.0,
        })

    # --- Edges (drop any dangling endpoints) ---
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    for e in parsed.get("edges") or []:
        if not isinstance(e, dict):
            continue
        s, t = str(e.get("source", "")), str(e.get("target", ""))
        if s not in node_ids or t not in node_ids or s == t:
            continue
        if (s, t) in seen_edges:
            continue
        seen_edges.add((s, t))
        kind = str(e.get("kind", "depends_on")).lower()
        if kind not in _VALID_EDGE_KINDS:
            kind = "depends_on"
        edges.append({
            "id": str(e.get("id") or f"e{len(edges)+1}")[:60],
            "source": s,
            "target": t,
            "label": str(e.get("label", ""))[:80],
            "kind": kind,
            "dashed": bool(e.get("dashed", kind in ("identity", "monitors", "depends_on"))),
        })

    if needs_layout(nodes):
        nodes = layout_nodes(nodes)

    return {
        "name": str(parsed.get("name", "") or "Architecture")[:200],
        "description": str(parsed.get("description", ""))[:2000],
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "rationale": str(parsed.get("rationale", ""))[:2000],
        "confidence": float(parsed["confidence"]) if isinstance(parsed.get("confidence"), (int, float)) else None,
    }


def _sku_label(sku: Any) -> str:
    if isinstance(sku, dict):
        return str(sku.get("name") or sku.get("tier") or "")[:60]
    if isinstance(sku, str):
        return sku[:60]
    return ""


async def generate_architecture(
    workload_name: str, resources: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """One-shot reverse-engineer an architecture from a resource inventory."""
    if not resources:
        return None
    user = (
        f"Workload: {workload_name or '(unnamed)'}\n"
        f"Resource count: {len(resources)}\n\n"
        f"Resource inventory (id, name, type, location, sku, identity, tags, full properties):\n"
        + _resources_block(resources)
    )
    parsed = await _complete_json(get_full_prompt("architecture_generate"), user)
    if not isinstance(parsed, dict) or not parsed.get("nodes"):
        return None
    return _normalize(parsed, resources)


async def enhance_architecture(
    arch: dict[str, Any], resources: list[dict[str, Any]], goal: str
) -> dict[str, Any] | None:
    """Refine an existing diagram per an instruction, grounded on the inventory."""
    current = {
        "name": arch.get("name"),
        "nodes": [
            {"id": n.get("id"), "arm_id": n.get("arm_id"), "name": n.get("name"), "type": n.get("type"),
             "category": n.get("category"), "layer": n.get("layer"), "group_id": n.get("group_id")}
            for n in arch.get("nodes", [])
        ],
        "edges": arch.get("edges", []),
        "groups": [{"id": g.get("id"), "name": g.get("name"), "kind": g.get("kind")} for g in arch.get("groups", [])],
    }
    user = (
        f"Instruction: {goal.strip() or 'Improve the diagram.'}\n\n"
        f"CURRENT DIAGRAM:\n{json.dumps(current, separators=(',', ':'))}\n\n"
        f"RESOURCE INVENTORY:\n{_resources_block(resources)}"
    )
    parsed = await _complete_json(get_full_prompt("architecture_enhance"), user)
    if not isinstance(parsed, dict) or not parsed.get("nodes"):
        return None
    return _normalize(parsed, resources)


def new_id() -> str:
    return uuid.uuid4().hex[:10]


async def answer_question(arch: dict[str, Any], question: str) -> str:
    """Grounded Q&A about an architecture diagram. Summarizes the nodes/edges (+ any node
    `meta` config) and asks the LLM to answer using ONLY that context. Returns markdown."""
    nodes = arch.get("nodes", []) or []
    edges = arch.get("edges", []) or []
    name_by_id = {n.get("id"): (n.get("name") or n.get("type") or "resource") for n in nodes}

    node_lines = []
    for n in nodes[:120]:
        meta = n.get("meta") or {}
        meta_s = ", ".join(f"{k}={v}" for k, v in list(meta.items())[:6])
        node_lines.append(
            f"- {n.get('name', '?')} [{n.get('type', 'concept')}] tier={n.get('layer', '')}"
            + (f" sku={n.get('sku')}" if n.get("sku") else "")
            + (f" ({meta_s})" if meta_s else "")
        )
    edge_lines = []
    for e in edges[:160]:
        s = name_by_id.get(e.get("source"), "?")
        t = name_by_id.get(e.get("target"), "?")
        edge_lines.append(f"- {s} --{e.get('kind', 'connects_to')}{f' [{e.get('label')}]' if e.get('label') else ''}--> {t}")

    context = (
        f"ARCHITECTURE: {arch.get('name', 'Untitled')}\n"
        f"{arch.get('description', '')}\n\n"
        f"RESOURCES ({len(nodes)}):\n" + "\n".join(node_lines) + "\n\n"
        f"CONNECTIONS ({len(edges)}):\n" + ("\n".join(edge_lines) or "(none)")
    )
    system = (
        "You are a principal Azure solutions architect. Answer the user's question about "
        "the architecture below using ONLY the provided resources and connections. Be "
        "concise and specific; cite resource names. If the diagram lacks the info, say so "
        "and suggest what to add. Cover reliability, security, cost, and SPOFs when relevant. "
        "Reply in short markdown (no code fences)."
    )
    user = f"{context}\n\nQUESTION: {question.strip()[:1000]}"
    provider = build_provider()
    text = ""
    async for ev in provider.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], None
    ):
        if ev.type == "token":
            text += ev.text
    return text.strip()


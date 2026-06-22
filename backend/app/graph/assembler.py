"""Pure node/edge builders + graph composition for the ``/graph`` surface.

Everything here is a **pure function**: it takes already-loaded plain dicts (workloads,
architectures, inventory resources, assessment risk rollups) and returns a
``{nodes, edges, stats}`` graph. No Azure calls, no DB access, no file I/O — that lives in
``app.api.graph`` so this layer stays trivially unit-testable.

Node id scheme (decodable, URL-safe enough to round-trip as a query param):

    conn:<connection_id>
    sub:<subscription_id>
    rg:<subscription_id>|<resource_group_lower>
    res:<arm_id>
    wl:<workload_id>
    arch:<architecture_id>
    mem:<architecture_id>
    finding:<run_id>|<check_id>

``decode_node_id`` splits on the FIRST ``:`` so ARM ids (which contain ``:`` only in rare
SaaS ids but always ``/``) round-trip unharmed.
"""
from __future__ import annotations

from typing import Any

# ----------------------------------------------------------------- node / edge kinds
KIND_CONNECTION = "tenant_connection"
KIND_MANAGEMENT_GROUP = "management_group"
KIND_SUBSCRIPTION = "subscription"
KIND_RESOURCE_GROUP = "resource_group"
KIND_RESOURCE = "resource"
KIND_WORKLOAD = "workload"
KIND_ARCHITECTURE = "architecture"
KIND_MEMORY = "architecture_memory"
KIND_FINDING = "assessment_finding"
KIND_RBAC_PRINCIPAL = "rbac_principal"
KIND_COST_BUCKET = "cost_bucket"
KIND_RETIREMENT = "retirement_item"
KIND_CHANGE = "change_event"
KIND_COVERAGE_GAP = "coverage_gap"
KIND_IDENTITY_FINDING = "identity_finding"

EDGE_CONTAINS = "contains"          # connection→sub, sub→rg, rg→resource, sub→workload
EDGE_MEMBER_OF = "member_of"        # sub→management_group
EDGE_BELONGS_TO = "belongs_to"      # resource→workload (membership)
EDGE_MODELS = "models"              # workload→architecture
EDGE_DOCUMENTS = "documents"        # architecture→memory
EDGE_HAS_FINDING = "has_finding"    # workload→finding
EDGE_DEPENDS_ON = "depends_on"      # resource→resource (from architecture edges)
EDGE_CONNECTS_TO = "connects_to"
EDGE_DATA_FLOW = "data_flow"
EDGE_CAN_ACCESS = "can_access"      # rbac_principal→workload/resource
EDGE_COSTS = "costs"                # workload/sub→cost_bucket
EDGE_RETIRING_IN = "retiring_in"    # workload/resource→retirement_item
EDGE_CHANGED_IN = "changed_in"      # resource→change_event
EDGE_HAS_GAP = "has_gap"            # workload→coverage_gap

# Dependency edge kinds copied verbatim from architecture edges (reverse-engineered).
_DEP_EDGE_KINDS = {
    "depends_on", "connects_to", "data_flow", "identity_dependency", "monitors",
    "private_endpoint_to", "vnet_link", "subnet_link",
}

# How many failing findings to surface as discrete nodes when expanding a workload.
_FINDING_NODE_CAP = 18
# How many resources to attach when expanding a workload/RG before the graph gets noisy.
_RESOURCE_NODE_CAP = 400


# --------------------------------------------------------------------- id helpers
def _nid(prefix: str, value: str) -> str:
    return f"{prefix}:{value}"


def decode_node_id(node_id: str) -> tuple[str, str]:
    """Return ``(prefix, value)`` for a node id, splitting on the first ``:``."""
    if not node_id or ":" not in node_id:
        return ("", node_id or "")
    prefix, _, value = node_id.partition(":")
    return (prefix, value)


def conn_id(connection_id: str) -> str:
    return _nid("conn", connection_id or "default")


def sub_id(subscription_id: str) -> str:
    return _nid("sub", (subscription_id or "").lower())


def rg_id(subscription_id: str, resource_group: str) -> str:
    return _nid("rg", f"{(subscription_id or '').lower()}|{(resource_group or '').lower()}")


def res_id(arm_id: str) -> str:
    return _nid("res", arm_id or "")


def wl_id(workload_id: str) -> str:
    return _nid("wl", workload_id or "")


def arch_id(architecture_id: str) -> str:
    return _nid("arch", architecture_id or "")


def mem_id(architecture_id: str) -> str:
    return _nid("mem", architecture_id or "")


def finding_id(run_id: str, check_id: str) -> str:
    return _nid("finding", f"{run_id}|{check_id}")


def mg_id(management_group_id: str) -> str:
    return _nid("mg", (management_group_id or "").lower())


def rbac_id(principal_id: str) -> str:
    return _nid("rbac", (principal_id or "").lower())


def cost_id(scope_key: str) -> str:
    return _nid("cost", scope_key or "")


def retire_id(tracking_id: str) -> str:
    return _nid("retire", tracking_id or "")


def change_id(change_key: str) -> str:
    return _nid("change", change_key or "")


def gap_id(workload_id: str, feature: str) -> str:
    return _nid("gap", f"{workload_id}|{feature}")


def ident_id(finding_key: str) -> str:
    return _nid("ident", finding_key or "")


# --------------------------------------------------------------- primitives
def _node(
    node_id: str,
    kind: str,
    label: str,
    *,
    data: dict[str, Any] | None = None,
    badges: dict[str, Any] | None = None,
    expandable: bool = False,
    parent: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": node_id,
        "kind": kind,
        "label": label or node_id,
        "data": data or {},
        "badges": badges or {},
        "expandable": bool(expandable),
    }
    if parent:
        out["parent"] = parent
    return out


def _edge(source: str, target: str, kind: str, *, label: str = "") -> dict[str, Any]:
    return {
        "id": f"{source}__{kind}__{target}",
        "source": source,
        "target": target,
        "kind": kind,
        "label": label,
    }


def _short_type(arm_type: str) -> str:
    """``microsoft.web/sites`` → ``sites`` (last path segment) for compact resource badges."""
    t = (arm_type or "").strip().lower()
    return t.rsplit("/", 1)[-1] if "/" in t else t


def _risk_level(failed: int, severity: str) -> str:
    """Coarse risk band for lens colouring."""
    sev = (severity or "").lower()
    if failed <= 0:
        return "ok"
    if sev in ("critical", "high", "error"):
        return "high"
    if sev in ("medium", "warning"):
        return "medium"
    return "low"


# --------------------------------------------------------------- node builders
def connection_node(conn_public: dict[str, Any]) -> dict[str, Any]:
    cid = conn_public.get("id") or "default"
    label = conn_public.get("display_name") or conn_public.get("tenant_id") or "Azure tenant"
    return _node(
        conn_id(cid),
        KIND_CONNECTION,
        label,
        data={
            "connection_id": cid,
            "tenant_id": conn_public.get("tenant_id", ""),
            "auth_method": conn_public.get("auth_method", ""),
            "status": conn_public.get("status", "unknown"),
            "is_default": bool(conn_public.get("is_default")),
        },
        expandable=True,
    )


def subscription_node(
    subscription_id: str, name: str, *, connection_id: str, resource_count: int = 0
) -> dict[str, Any]:
    return _node(
        sub_id(subscription_id),
        KIND_SUBSCRIPTION,
        name or subscription_id,
        data={
            "subscription_id": subscription_id,
            "name": name or subscription_id,
            "connection_id": connection_id,
        },
        badges={"resources": resource_count} if resource_count else {},
        expandable=True,
    )


def resource_group_node(
    subscription_id: str, resource_group: str, *, resource_count: int = 0
) -> dict[str, Any]:
    return _node(
        rg_id(subscription_id, resource_group),
        KIND_RESOURCE_GROUP,
        resource_group or "(no resource group)",
        data={
            "subscription_id": subscription_id,
            "resource_group": resource_group,
        },
        badges={"resources": resource_count} if resource_count else {},
        expandable=resource_count > 0,
    )


def resource_node(resource: dict[str, Any]) -> dict[str, Any]:
    arm = resource.get("id", "")
    arm_type = resource.get("type", "")
    flags = resource.get("flags") or []
    return _node(
        res_id(arm),
        KIND_RESOURCE,
        resource.get("name", "") or _short_type(arm_type),
        data={
            "arm_id": arm,
            "name": resource.get("name", ""),
            "type": arm_type,
            "short_type": _short_type(arm_type),
            "location": resource.get("location", ""),
            "resource_group": resource.get("resource_group", ""),
            "subscription_id": resource.get("subscription_id", ""),
            "sku": resource.get("sku", ""),
            "tier": resource.get("tier", ""),
            "tags": resource.get("tags", {}) or {},
            "managed_by": resource.get("managed_by", ""),
            "flags": flags,
            "workloads": resource.get("workloads", []) or [],
        },
        badges={"flags": len(flags)} if flags else {},
    )


def workload_node(workload: dict[str, Any], *, risk: dict[str, Any] | None = None) -> dict[str, Any]:
    risk = risk or {}
    summary = workload.get("summary") or {}
    failed = int(risk.get("failed", 0) or 0)
    badges: dict[str, Any] = {}
    if summary.get("total_resources"):
        badges["resources"] = int(summary.get("total_resources") or 0)
    if risk:
        badges["score"] = risk.get("score")
        badges["failed"] = failed
    return _node(
        wl_id(workload.get("id", "")),
        KIND_WORKLOAD,
        workload.get("name", "") or "Untitled workload",
        data={
            "workload_id": workload.get("id", ""),
            "name": workload.get("name", ""),
            "description": workload.get("description", ""),
            "workload_type": workload.get("workload_type", ""),
            "environment": workload.get("environment", ""),
            "criticality": workload.get("criticality", ""),
            "data_classification": workload.get("data_classification", ""),
            "tags": workload.get("tags", []) or [],
            "connection_id": workload.get("connection_id", ""),
            "confidence": workload.get("confidence", 0.0),
            "total_resources": summary.get("total_resources", 0),
            "type_breakdown": summary.get("types", []),
            "last_refreshed": workload.get("last_refreshed", ""),
            "risk": {
                "run_id": risk.get("run_id", ""),
                "score": risk.get("score"),
                "failed": failed,
                "passed": int(risk.get("passed", 0) or 0),
                "severity": risk.get("severity", ""),
                "level": _risk_level(failed, risk.get("severity", "")),
                "completed_at": risk.get("completed_at", ""),
            },
        },
        badges=badges,
        expandable=True,
    )


def architecture_node(architecture: dict[str, Any]) -> dict[str, Any]:
    nodes = architecture.get("nodes") or []
    return _node(
        arch_id(architecture.get("id", "")),
        KIND_ARCHITECTURE,
        architecture.get("name", "") or "Untitled architecture",
        data={
            "architecture_id": architecture.get("id", ""),
            "name": architecture.get("name", ""),
            "workload_id": architecture.get("workload_id", ""),
            "workload_name": architecture.get("workload_name", ""),
            "state": architecture.get("state", ""),
            "source": architecture.get("source", ""),
            "node_count": len(nodes),
            "updated_at": architecture.get("updated_at", ""),
        },
        badges={"nodes": len(nodes)} if nodes else {},
    )


def memory_node(architecture_id: str, *, name: str = "", sections: int = 0, confidence: Any = None) -> dict[str, Any]:
    return _node(
        mem_id(architecture_id),
        KIND_MEMORY,
        name or "Architecture memory",
        data={
            "architecture_id": architecture_id,
            "sections": sections,
            "confidence": confidence,
        },
        badges={"sections": sections} if sections else {},
    )


def finding_node(run_id: str, finding: dict[str, Any]) -> dict[str, Any]:
    check = finding.get("check_id") or finding.get("id") or ""
    return _node(
        finding_id(run_id, check),
        KIND_FINDING,
        finding.get("title", "") or check or "Finding",
        data={
            "run_id": run_id,
            "check_id": check,
            "title": finding.get("title", ""),
            "pillar": finding.get("pillar", ""),
            "severity": finding.get("severity", ""),
            "status": finding.get("status", ""),
            "rationale": finding.get("ai_rationale", "") or finding.get("rationale", ""),
            "resource_count": len(finding.get("flagged_resources", []) or []),
        },
        badges={"severity": finding.get("severity", "")},
    )


def mg_node(management_group_id: str, name: str, *, sub_count: int = 0) -> dict[str, Any]:
    return _node(
        mg_id(management_group_id),
        KIND_MANAGEMENT_GROUP,
        name or management_group_id,
        data={"management_group_id": management_group_id, "name": name or management_group_id},
        badges={"subscriptions": sub_count} if sub_count else {},
        expandable=True,
    )


def rbac_principal_node(principal_id: str, *, name: str = "", ptype: str = "", privileged: bool = False, role_count: int = 0) -> dict[str, Any]:
    return _node(
        rbac_id(principal_id),
        KIND_RBAC_PRINCIPAL,
        name or principal_id,
        data={
            "principal_id": principal_id,
            "name": name or principal_id,
            "principal_type": ptype,
            "privileged": bool(privileged),
            "role_count": role_count,
        },
        badges={"privileged": "yes"} if privileged else {},
    )


def cost_node(scope_key: str, *, label: str, amount: float, currency: str = "USD", period: str = "") -> dict[str, Any]:
    return _node(
        cost_id(scope_key),
        KIND_COST_BUCKET,
        label or "Cost",
        data={"amount": round(float(amount or 0), 2), "currency": currency, "period": period},
        badges={"cost": round(float(amount or 0), 2)},
    )


def retirement_node(event: dict[str, Any]) -> dict[str, Any]:
    tid = event.get("tracking_id") or event.get("id") or ""
    return _node(
        retire_id(tid),
        KIND_RETIREMENT,
        event.get("title", "") or event.get("service", "") or "Retirement",
        data={
            "tracking_id": tid,
            "title": event.get("title", ""),
            "service": event.get("service", ""),
            "change_type": event.get("change_type", ""),
            "deadline": event.get("retirement_date", "") or event.get("planned_date", ""),
            "days_until": event.get("days_until"),
            "severity": event.get("severity", ""),
        },
        badges={"days": event.get("days_until")},
    )


def change_node(change_key: str, change: dict[str, Any]) -> dict[str, Any]:
    return _node(
        change_id(change_key),
        KIND_CHANGE,
        change.get("operation", "") or change.get("type", "") or "Change",
        data={
            "operation": change.get("operation", "") or change.get("type", ""),
            "resource_id": change.get("resource_id", "") or change.get("target", ""),
            "timestamp": change.get("timestamp", "") or change.get("at", ""),
            "caller": change.get("caller", ""),
            "kind": change.get("change_kind", "") or change.get("source", ""),
        },
        badges={},
    )


def coverage_gap_node(workload_id: str, feature: str, *, label: str, pct: Any = None, severity: str = "") -> dict[str, Any]:
    return _node(
        gap_id(workload_id, feature),
        KIND_COVERAGE_GAP,
        label or feature,
        data={"workload_id": workload_id, "feature": feature, "coverage_pct": pct, "severity": severity},
        badges={"pct": pct} if pct is not None else {},
    )


def identity_finding_node(finding_key: str, finding: dict[str, Any]) -> dict[str, Any]:
    return _node(
        ident_id(finding_key),
        KIND_IDENTITY_FINDING,
        finding.get("title", "") or finding.get("subject", "") or "Identity finding",
        data={
            "kind": finding.get("kind", ""),
            "title": finding.get("title", ""),
            "subject": finding.get("subject", ""),
            "severity": finding.get("severity", ""),
            "days_left": finding.get("days_left"),
        },
        badges={"severity": finding.get("severity", "")},
    )


# --------------------------------------------------------------- dependency edges (from architectures)
def architecture_dependency_edges(architecture: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate an architecture's own edges (reverse-engineered: depends_on / connects_to /
    data_flow / private_endpoint_to / vnet_link / …) into resource→resource graph edges, by
    mapping each architecture node id to its ARM id → ``res:<arm_id>``.

    This is how the graph reuses the relationship knowledge already inferred by the
    Architecture canvas instead of re-deriving it."""
    node_arm: dict[str, str] = {}
    for n in architecture.get("nodes", []) or []:
        if not isinstance(n, dict):
            continue
        arm = n.get("arm_id") or ""
        if arm and n.get("id"):
            node_arm[n["id"]] = arm
    out: list[dict[str, Any]] = []
    for e in architecture.get("edges", []) or []:
        if not isinstance(e, dict):
            continue
        kind = (e.get("kind") or "").lower()
        if kind not in _DEP_EDGE_KINDS:
            continue
        sa = node_arm.get(e.get("source", ""))
        ta = node_arm.get(e.get("target", ""))
        if not sa or not ta or sa == ta:
            continue
        out.append(_edge(res_id(sa), res_id(ta), kind, label=e.get("label", "")))
    return out


# --------------------------------------------------------------- graph composition
def _stats(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for n in nodes:
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
    return {"node_count": len(nodes), "edge_count": len(edges), "by_kind": by_kind}


def _dedupe(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seen_n: dict[str, dict[str, Any]] = {}
    for n in nodes:
        seen_n.setdefault(n["id"], n)
    seen_e: dict[str, dict[str, Any]] = {}
    for e in edges:
        seen_e.setdefault(e["id"], e)
    return (list(seen_n.values()), list(seen_e.values()))


def build_overview(
    *,
    connection: dict[str, Any],
    subscriptions: list[dict[str, Any]],
    workloads: list[dict[str, Any]],
    architectures: list[dict[str, Any]],
    risk_by_workload: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The landing graph: connection → subscriptions → workloads → architectures.

    ``subscriptions`` = ``[{id, name, resource_count}]``. Workloads attach under every
    subscription they span (via the workload's ``summary.scope_counts`` / node subs); a
    workload with no resolvable subscription attaches directly under the connection so it's
    never orphaned. Architectures attach to their workload (``models``)."""
    risk_by_workload = risk_by_workload or {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    cid = connection.get("id") or "default"
    cnode = connection_node(connection)
    nodes.append(cnode)

    sub_ids_present: set[str] = set()
    for sub in subscriptions:
        sid = (sub.get("id") or "").lower()
        if not sid:
            continue
        sub_ids_present.add(sid)
        nodes.append(
            subscription_node(
                sub.get("id", ""),
                sub.get("name", ""),
                connection_id=cid,
                resource_count=int(sub.get("resource_count", 0) or 0),
            )
        )
        edges.append(_edge(cnode["id"], sub_id(sub.get("id", "")), EDGE_CONTAINS))

    # Workloads — attach under each subscription they touch; fall back to the connection.
    for wl in workloads:
        wid = wl.get("id", "")
        if not wid:
            continue
        nodes.append(workload_node(wl, risk=risk_by_workload.get(wid)))
        wl_subs = _workload_subscription_ids(wl)
        linked = False
        for s in wl_subs:
            if s in sub_ids_present:
                edges.append(_edge(sub_id(s), wl_id(wid), EDGE_CONTAINS))
                linked = True
        if not linked:
            edges.append(_edge(cnode["id"], wl_id(wid), EDGE_CONTAINS))

    # Architectures — link to their workload (models); orphans hang off the connection.
    wl_present = {wl.get("id", "") for wl in workloads}
    for arch in architectures:
        aid = arch.get("id", "")
        if not aid:
            continue
        nodes.append(architecture_node(arch))
        awl = arch.get("workload_id", "")
        if awl and awl in wl_present:
            edges.append(_edge(wl_id(awl), arch_id(aid), EDGE_MODELS))
        else:
            edges.append(_edge(cnode["id"], arch_id(aid), EDGE_MODELS, label="unlinked"))

    nodes, edges = _dedupe(nodes, edges)
    return {"nodes": nodes, "edges": edges, "stats": _stats(nodes, edges)}


def _workload_subscription_ids(workload: dict[str, Any]) -> set[str]:
    """Lowercased subscription ids a workload spans, from its nodes + cached scope counts."""
    out: set[str] = set()
    for node in workload.get("nodes", []) or []:
        sid = node.get("subscription_id") or ""
        if node.get("kind") == "subscription":
            sid = sid or node.get("id", "")
        if sid:
            # node ids may be full ARM scope paths — extract the guid if present.
            out.add(_extract_sub(sid))
    scope_counts = (workload.get("summary") or {}).get("scope_counts") or {}
    for s in scope_counts.get("subscriptions", []) or []:
        if isinstance(s, str) and s:
            out.add(_extract_sub(s))
    return {s for s in out if s}


def _extract_sub(value: str) -> str:
    import re

    m = re.search(r"/subscriptions/([0-9a-fA-F-]{36})", value or "")
    if m:
        return m.group(1).lower()
    return (value or "").lower()


def expand_subscription(
    *,
    subscription_id: str,
    name: str,
    resources: list[dict[str, Any]],
    workloads: list[dict[str, Any]],
) -> dict[str, Any]:
    """Children of a subscription node: its resource groups (from inventory) + workloads."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    parent = sub_id(subscription_id)
    sid = (subscription_id or "").lower()

    rg_counts: dict[str, int] = {}
    for r in resources:
        if (r.get("subscription_id", "") or "").lower() != sid:
            continue
        rg = r.get("resource_group", "") or ""
        rg_counts[rg] = rg_counts.get(rg, 0) + 1
    for rg, count in sorted(rg_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        nodes.append(resource_group_node(subscription_id, rg, resource_count=count))
        edges.append(_edge(parent, rg_id(subscription_id, rg), EDGE_CONTAINS))

    for wl in workloads:
        if sid in _workload_subscription_ids(wl):
            nodes.append(workload_node(wl))
            edges.append(_edge(parent, wl_id(wl.get("id", "")), EDGE_CONTAINS))

    nodes, edges = _dedupe(nodes, edges)
    return {"nodes": nodes, "edges": edges, "stats": _stats(nodes, edges)}


def expand_resource_group(
    *, subscription_id: str, resource_group: str, resources: list[dict[str, Any]]
) -> dict[str, Any]:
    """Resource nodes inside a resource group."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    parent = rg_id(subscription_id, resource_group)
    sid = (subscription_id or "").lower()
    rgl = (resource_group or "").lower()
    count = 0
    for r in resources:
        if (r.get("subscription_id", "") or "").lower() != sid:
            continue
        if (r.get("resource_group", "") or "").lower() != rgl:
            continue
        nodes.append(resource_node(r))
        edges.append(_edge(parent, res_id(r.get("id", "")), EDGE_CONTAINS))
        count += 1
        if count >= _RESOURCE_NODE_CAP:
            break
    nodes, edges = _dedupe(nodes, edges)
    out = {"nodes": nodes, "edges": edges, "stats": _stats(nodes, edges)}
    if count >= _RESOURCE_NODE_CAP:
        out["truncated"] = True
    return out


def expand_workload(
    *,
    workload: dict[str, Any],
    resources: list[dict[str, Any]],
    architectures: list[dict[str, Any]],
    memory: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Everything one hop from a workload: member resources, its architecture(s),
    architecture memory, and the top failing assessment findings."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    wid = workload.get("id", "")
    parent = wl_id(wid)

    # Member resources (inventory attribution).
    count = 0
    for r in resources:
        member = any((w or {}).get("id") == wid for w in (r.get("workloads") or []))
        if not member:
            continue
        nodes.append(resource_node(r))
        edges.append(_edge(parent, res_id(r.get("id", "")), EDGE_BELONGS_TO))
        count += 1
        if count >= _RESOURCE_NODE_CAP:
            break

    member_node_ids = {n["id"] for n in nodes if n["kind"] == KIND_RESOURCE}

    # Architecture(s) that model this workload + their memory.
    for arch in architectures:
        if arch.get("workload_id", "") != wid:
            continue
        nodes.append(architecture_node(arch))
        edges.append(_edge(parent, arch_id(arch.get("id", "")), EDGE_MODELS))
        # Reuse the architecture's reverse-engineered relationships as resource→resource
        # dependency edges (only between resources already on the canvas).
        for dep in architecture_dependency_edges(arch):
            if dep["source"] in member_node_ids and dep["target"] in member_node_ids:
                edges.append(dep)
        if memory and memory.get("architecture_id") == arch.get("id"):
            nodes.append(
                memory_node(
                    arch.get("id", ""),
                    name=f"{arch.get('name', '')} — memory" if arch.get("name") else "",
                    sections=int(memory.get("sections", 0) or 0),
                    confidence=memory.get("confidence"),
                )
            )
            edges.append(_edge(arch_id(arch.get("id", "")), mem_id(arch.get("id", "")), EDGE_DOCUMENTS))

    # Top failing findings from the latest run.
    if risk and findings:
        run_id = risk.get("run_id", "")
        failing = [f for f in findings if (f.get("status") or "").lower() == "fail"]
        failing.sort(key=lambda f: _severity_rank(f.get("severity", "")))
        for f in failing[:_FINDING_NODE_CAP]:
            nodes.append(finding_node(run_id, f))
            edges.append(_edge(parent, finding_id(run_id, f.get("check_id") or f.get("id") or ""), EDGE_HAS_FINDING))

    nodes, edges = _dedupe(nodes, edges)
    out = {"nodes": nodes, "edges": edges, "stats": _stats(nodes, edges)}
    if count >= _RESOURCE_NODE_CAP:
        out["truncated"] = True
    return out


def _severity_rank(severity: str) -> int:
    order = {"critical": 0, "high": 1, "error": 1, "medium": 2, "warning": 2, "low": 3, "info": 4}
    return order.get((severity or "").lower(), 5)


def search(
    *,
    query: str,
    subscriptions: list[dict[str, Any]],
    workloads: list[dict[str, Any]],
    architectures: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    risk_by_workload: dict[str, dict[str, Any]] | None = None,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Flat list of nodes whose label/metadata matches ``query`` (case-insensitive).

    Workloads and architectures rank first (most navigationally useful), then
    subscriptions, then resources."""
    risk_by_workload = risk_by_workload or {}
    q = (query or "").strip().lower()
    if not q:
        return []
    out: list[dict[str, Any]] = []

    for wl in workloads:
        hay = " ".join(
            str(wl.get(k, "")) for k in ("name", "description", "workload_type", "environment", "criticality")
        ).lower()
        if q in hay:
            out.append(workload_node(wl, risk=risk_by_workload.get(wl.get("id", ""))))

    for arch in architectures:
        if q in f"{arch.get('name', '')} {arch.get('workload_name', '')}".lower():
            out.append(architecture_node(arch))

    for sub in subscriptions:
        if q in f"{sub.get('name', '')} {sub.get('id', '')}".lower():
            out.append(
                subscription_node(
                    sub.get("id", ""),
                    sub.get("name", ""),
                    connection_id="",
                    resource_count=int(sub.get("resource_count", 0) or 0),
                )
            )

    for r in resources:
        if len(out) >= limit:
            break
        hay = f"{r.get('name', '')} {r.get('type', '')} {r.get('resource_group', '')} {r.get('id', '')}".lower()
        if q in hay:
            out.append(resource_node(r))

    return out[:limit]

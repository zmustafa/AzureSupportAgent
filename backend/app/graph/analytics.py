"""Pure graph algorithms for the ``/graph`` analytics + investigation features.

Operates on the same ``{nodes, edges}`` shape the assembler produces. No external deps
(NetworkX etc.) — Brandes betweenness + BFS are fine for the few-hundred-to-few-thousand
node estate graphs we render, and staying pure keeps everything unit-testable.
"""
from __future__ import annotations

from collections import deque
from typing import Any


def _adjacency(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *, directed: bool) -> dict[str, list[str]]:
    ids = {n["id"] for n in nodes}
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s not in ids or t not in ids:
            continue
        adj[s].append(t)
        if not directed:
            adj[t].append(s)
    return adj


# --------------------------------------------------------------------- shortest path
def shortest_path(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], source: str, target: str, *, directed: bool = False
) -> dict[str, Any]:
    """BFS shortest path between two nodes. Returns ``{found, path:[ids], hops, edges:[edge ids]}``."""
    if source == target:
        return {"found": True, "path": [source], "hops": 0, "edges": []}
    adj = _adjacency(nodes, edges, directed=directed)
    if source not in adj or target not in adj:
        return {"found": False, "path": [], "hops": 0, "edges": []}
    prev: dict[str, str] = {}
    seen = {source}
    q = deque([source])
    while q:
        cur = q.popleft()
        for nxt in adj.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            prev[nxt] = cur
            if nxt == target:
                path = [target]
                while path[-1] != source:
                    path.append(prev[path[-1]])
                path.reverse()
                edge_ids = _edges_for_path(edges, path, directed=directed)
                return {"found": True, "path": path, "hops": len(path) - 1, "edges": edge_ids}
            q.append(nxt)
    return {"found": False, "path": [], "hops": 0, "edges": []}


def _edges_for_path(edges: list[dict[str, Any]], path: list[str], *, directed: bool) -> list[str]:
    pairs = set()
    for a, b in zip(path, path[1:]):
        pairs.add((a, b))
        if not directed:
            pairs.add((b, a))
    return [e["id"] for e in edges if (e.get("source"), e.get("target")) in pairs]


# --------------------------------------------------------------------- blast radius
def blast_radius(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], source: str, *, max_depth: int = 3, directed: bool = False
) -> dict[str, Any]:
    """All nodes reachable from ``source`` within ``max_depth`` hops, layered by distance.

    Returns ``{source, direct:[ids], indirect:[ids], by_depth:{d:[ids]}, impacted_workloads:[…],
    impacted_count}``. ``direct`` = depth 1, ``indirect`` = depth ≥ 2."""
    adj = _adjacency(nodes, edges, directed=directed)
    by_id = {n["id"]: n for n in nodes}
    if source not in adj:
        return {"source": source, "direct": [], "indirect": [], "by_depth": {}, "impacted_workloads": [], "impacted_count": 0}
    dist = {source: 0}
    q = deque([source])
    while q:
        cur = q.popleft()
        if dist[cur] >= max_depth:
            continue
        for nxt in adj.get(cur, []):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                q.append(nxt)
    by_depth: dict[int, list[str]] = {}
    for nid, d in dist.items():
        if d == 0:
            continue
        by_depth.setdefault(d, []).append(nid)
    direct = by_depth.get(1, [])
    indirect = [nid for d, ids in by_depth.items() if d >= 2 for nid in ids]
    impacted_workloads = [
        {"id": nid, "label": by_id[nid].get("label", "")}
        for nid in dist
        if nid != source and by_id.get(nid, {}).get("kind") == "workload"
    ]
    return {
        "source": source,
        "direct": direct,
        "indirect": indirect,
        "by_depth": {str(k): v for k, v in sorted(by_depth.items())},
        "impacted_workloads": impacted_workloads,
        "impacted_count": len(dist) - 1,
    }


# --------------------------------------------------------------------- centrality
def degree_centrality(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, int]:
    adj = _adjacency(nodes, edges, directed=False)
    return {nid: len(neigh) for nid, neigh in adj.items()}


def betweenness_centrality(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, float]:
    """Brandes' algorithm (unweighted, undirected). O(V·E) — fine for our graph sizes."""
    adj = _adjacency(nodes, edges, directed=False)
    bc: dict[str, float] = {v: 0.0 for v in adj}
    for s in adj:
        stack: list[str] = []
        pred: dict[str, list[str]] = {w: [] for w in adj}
        sigma = {w: 0.0 for w in adj}
        sigma[s] = 1.0
        dist = {w: -1 for w in adj}
        dist[s] = 0
        q = deque([s])
        while q:
            v = q.popleft()
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = {w: 0.0 for w in adj}
        while stack:
            w = stack.pop()
            for v in pred[w]:
                if sigma[w]:
                    delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    # Undirected → each pair counted twice; normalize.
    return {v: round(val / 2.0, 4) for v, val in bc.items()}


# --------------------------------------------------------------------- community detection
def connected_components(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[list[str]]:
    """Undirected connected components (a cheap, stable community proxy), largest first."""
    adj = _adjacency(nodes, edges, directed=False)
    seen: set[str] = set()
    comps: list[list[str]] = []
    for start in adj:
        if start in seen:
            continue
        comp: list[str] = []
        q = deque([start])
        seen.add(start)
        while q:
            cur = q.popleft()
            comp.append(cur)
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


# --------------------------------------------------------------------- orphan detection
def detect_orphans(
    *,
    resources: list[dict[str, Any]],
    workloads: list[dict[str, Any]],
    architectures: list[dict[str, Any]],
) -> dict[str, Any]:
    """Estate hygiene gaps: unowned resources, workloads without an architecture, and
    architectures without a workload."""
    unowned = [
        {"id": r.get("id", ""), "name": r.get("name", ""), "type": r.get("type", "")}
        for r in resources
        if not (r.get("workloads") or [])
    ]
    wl_with_arch = {a.get("workload_id", "") for a in architectures if a.get("workload_id")}
    wl_present = {w.get("id", "") for w in workloads}
    workloads_without_arch = [
        {"id": w.get("id", ""), "name": w.get("name", "")}
        for w in workloads
        if w.get("id") not in wl_with_arch
    ]
    architectures_without_workload = [
        {"id": a.get("id", ""), "name": a.get("name", "")}
        for a in architectures
        if not a.get("workload_id") or a.get("workload_id") not in wl_present
    ]
    return {
        "unowned_resources": unowned,
        "unowned_count": len(unowned),
        "workloads_without_architecture": workloads_without_arch,
        "architectures_without_workload": architectures_without_workload,
    }


# --------------------------------------------------------------------- candidate workloads
def candidate_workloads(
    *,
    resources: list[dict[str, Any]],
    dependency_edges: list[dict[str, Any]],
    min_size: int = 3,
) -> list[dict[str, Any]]:
    """Cluster currently-unowned resources into likely workloads.

    Two signals: (a) connected components over dependency edges, and (b) shared resource
    group. A cluster must have ≥ ``min_size`` unowned resources to be proposed."""
    unowned = {r.get("id", "").lower(): r for r in resources if not (r.get("workloads") or []) and r.get("id")}
    if not unowned:
        return []
    # Union-find over dependency edges restricted to unowned resources.
    parent: dict[str, str] = {rid: rid for rid in unowned}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def _arm_of(node_id: str) -> str:
        # dependency edges use res:<arm_id> ids.
        return node_id.split(":", 1)[1].lower() if ":" in node_id else node_id.lower()

    for e in dependency_edges:
        a = _arm_of(e.get("source", ""))
        b = _arm_of(e.get("target", ""))
        if a in unowned and b in unowned:
            union(a, b)
    # Fall back to grouping leftover singletons by resource group.
    clusters: dict[str, list[str]] = {}
    for rid in unowned:
        clusters.setdefault(find(rid), []).append(rid)
    rg_groups: dict[str, list[str]] = {}
    out: list[dict[str, Any]] = []
    for root, members in clusters.items():
        if len(members) >= min_size:
            out.append(_candidate(members, unowned, reason="dependency-linked"))
        else:
            for rid in members:
                rg = (unowned[rid].get("resource_group", "") or "").lower()
                rg_groups.setdefault(rg or "(none)", []).append(rid)
    for rg, members in rg_groups.items():
        if rg != "(none)" and len(members) >= min_size:
            out.append(_candidate(members, unowned, reason=f"shared resource group {rg}"))
    out.sort(key=lambda c: c["size"], reverse=True)
    return out


def _candidate(member_ids: list[str], unowned: dict[str, dict[str, Any]], *, reason: str) -> dict[str, Any]:
    members = [unowned[m] for m in member_ids]
    types: dict[str, int] = {}
    for m in members:
        t = (m.get("type", "") or "").lower()
        types[t] = types.get(t, 0) + 1
    return {
        "size": len(members),
        "reason": reason,
        "resource_ids": [m.get("id", "") for m in members],
        "types": [{"type": t, "count": c} for t, c in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))],
        "resource_group": members[0].get("resource_group", ""),
        "subscription_id": members[0].get("subscription_id", ""),
    }


# --------------------------------------------------------------------- concentration risk
def concentration_risk(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *, top: int = 10
) -> list[dict[str, Any]]:
    """Load-bearing nodes: rank by betweenness (then degree) — failure here fragments the
    estate. Resources/management-groups shared across many workloads bubble up."""
    bc = betweenness_centrality(nodes, edges)
    deg = degree_centrality(nodes, edges)
    by_id = {n["id"]: n for n in nodes}
    ranked = sorted(
        nodes,
        key=lambda n: (bc.get(n["id"], 0.0), deg.get(n["id"], 0)),
        reverse=True,
    )
    out = []
    for n in ranked[:top]:
        out.append({
            "id": n["id"],
            "label": n.get("label", ""),
            "kind": n.get("kind", ""),
            "betweenness": bc.get(n["id"], 0.0),
            "degree": deg.get(n["id"], 0),
        })
    return [o for o in out if o["degree"] > 0]

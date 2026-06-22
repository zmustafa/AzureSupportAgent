"""Optional graph overlays — enrich the base graph with cost, retirement, coverage, RBAC,
and change signals pulled from the EXISTING server-side caches/registries.

Every overlay is best-effort and fail-soft: a missing or differently-shaped cache yields an
empty overlay, never an exception. Each returns ``{nodes, edges, patches}`` where ``patches``
is ``{node_id: {overlay: {...}}}`` used to enrich existing node ``data`` (drives the lens
colourings and the 'ask the graph' predicates without adding visible nodes).
"""
from __future__ import annotations

import logging
from typing import Any

from app.graph import assembler as A

log = logging.getLogger("app.graph.overlays")

OVERLAY_NAMES = ("cost", "retirement", "coverage", "rbac", "change")

# How many privileged principals / retirement items / change events to surface as nodes.
_RBAC_CAP = 12
_RETIRE_CAP = 20
_CHANGE_CAP = 40


def _empty() -> dict[str, Any]:
    return {"nodes": [], "edges": [], "patches": {}}


# --------------------------------------------------------------------- cost
def cost_overlay(*, tenant_id: str, connection_id: str, subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
    out = _empty()
    try:
        from app.inventory.cost import peek_cost
    except Exception:  # noqa: BLE001
        return out
    try:
        payload = peek_cost(tenant_id, connection_id, "")
    except Exception:  # noqa: BLE001
        return out
    if not payload:
        return out
    by_sub = {}
    for row in payload.get("by_subscription", []) or payload.get("subscriptions", []) or []:
        sid = (row.get("subscription_id") or row.get("id") or "").lower()
        amt = row.get("cost") or row.get("amount") or row.get("total") or 0
        if sid:
            by_sub[sid] = float(amt or 0)
    currency = payload.get("currency", "USD")
    period = payload.get("period", "") or payload.get("period_label", "")
    for sub in subscriptions:
        sid = (sub.get("id") or "").lower()
        amt = by_sub.get(sid)
        if not amt:
            continue
        node = A.cost_node(f"{sid}", label=f"${round(amt):,}/mo", amount=amt, currency=currency, period=period)
        out["nodes"].append(node)
        out["edges"].append(A._edge(A.sub_id(sid), node["id"], A.EDGE_COSTS))
        out["patches"].setdefault(A.sub_id(sid), {}).setdefault("overlay", {})["cost"] = round(amt, 2)
    return out


# --------------------------------------------------------------------- retirement
def retirement_overlay(*, tenant_id: str, workloads: list[dict[str, Any]]) -> dict[str, Any]:
    out = _empty()
    try:
        from app.radar import cache as radar_cache
    except Exception:  # noqa: BLE001
        return out
    count = 0
    for wl in workloads:
        wid = wl.get("id", "")
        if not wid or count >= _RETIRE_CAP:
            continue
        try:
            snap = radar_cache.read_snapshot(tenant_id, "workload", wid)
        except Exception:  # noqa: BLE001
            snap = None
        if not snap:
            continue
        events = snap.get("events", []) or []
        if events:
            out["patches"].setdefault(A.wl_id(wid), {}).setdefault("overlay", {})["retiring"] = True
        for ev in events[:6]:
            node = A.retirement_node(ev)
            out["nodes"].append(node)
            out["edges"].append(A._edge(A.wl_id(wid), node["id"], A.EDGE_RETIRING_IN))
            count += 1
            if count >= _RETIRE_CAP:
                break
    return out


# --------------------------------------------------------------------- coverage
_COVERAGE_FEATURES = (
    ("amba", "no_monitoring", "Monitoring coverage"),
    ("telemetry", "no_telemetry", "Telemetry coverage"),
    ("backupdr", "no_backup", "Backup & DR coverage"),
)


def coverage_overlay(*, tenant_id: str, workloads: list[dict[str, Any]]) -> dict[str, Any]:
    out = _empty()
    cache_mods: dict[str, Any] = {}
    for feature, _flag, _label in _COVERAGE_FEATURES:
        try:
            cache_mods[feature] = __import__(f"app.{feature}.cache", fromlist=["read_snapshot"])
        except Exception:  # noqa: BLE001
            cache_mods[feature] = None
    for wl in workloads:
        wid = wl.get("id", "")
        if not wid:
            continue
        for feature, flag, label in _COVERAGE_FEATURES:
            mod = cache_mods.get(feature)
            if mod is None:
                continue
            try:
                snap = mod.read_snapshot(tenant_id, "workload", wid)
            except Exception:  # noqa: BLE001
                snap = None
            if not snap:
                continue
            pct = _coverage_pct(snap)
            if pct is None or pct >= 100:
                continue
            sev = "high" if pct < 50 else "medium"
            node = A.coverage_gap_node(wid, feature, label=f"{label} {pct}%", pct=pct, severity=sev)
            out["nodes"].append(node)
            out["edges"].append(A._edge(A.wl_id(wid), node["id"], A.EDGE_HAS_GAP))
            out["patches"].setdefault(A.wl_id(wid), {}).setdefault("overlay", {})[flag] = pct < 100
    return out


def _coverage_pct(snap: dict[str, Any]) -> int | None:
    if "coverage_pct" in snap:
        try:
            return int(snap.get("coverage_pct") or 0)
        except (TypeError, ValueError):
            return None
    sc = snap.get("scorecard") or {}
    for k in ("pct_protected", "coverage_pct", "pct"):
        if k in sc:
            try:
                return int(sc.get(k) or 0)
            except (TypeError, ValueError):
                return None
    return None


# --------------------------------------------------------------------- rbac
def rbac_overlay(*, tenant_id: str, connection_id: str) -> dict[str, Any]:
    out = _empty()
    try:
        from app.rbac.compose import build_master_rows
    except Exception:  # noqa: BLE001
        return out
    try:
        rows = build_master_rows(tenant_id)
    except Exception:  # noqa: BLE001
        return out
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        pid = (r.get("effectivePrincipalId") or r.get("principalId") or "").lower()
        if not pid:
            continue
        entry = agg.setdefault(pid, {
            "id": pid,
            "name": r.get("effectivePrincipalName") or r.get("principalDisplayName") or pid,
            "type": r.get("effectivePrincipalType") or r.get("principalType") or "",
            "privileged": False,
            "roles": 0,
            "subs": set(),
        })
        entry["roles"] += 1
        if r.get("roleIsPrivileged"):
            entry["privileged"] = True
        scope = (r.get("scope") or "").lower()
        import re as _re

        m = _re.search(r"/subscriptions/([0-9a-f-]{36})", scope)
        if m:
            entry["subs"].add(m.group(1))
    privileged = [e for e in agg.values() if e["privileged"]]
    privileged.sort(key=lambda e: e["roles"], reverse=True)
    for e in privileged[:_RBAC_CAP]:
        node = A.rbac_principal_node(e["id"], name=e["name"], ptype=e["type"], privileged=True, role_count=e["roles"])
        out["nodes"].append(node)
        linked = False
        for sid in e["subs"]:
            out["edges"].append(A._edge(node["id"], A.sub_id(sid), A.EDGE_CAN_ACCESS))
            linked = True
        if not linked and connection_id:
            out["edges"].append(A._edge(node["id"], A.conn_id(connection_id), A.EDGE_CAN_ACCESS))
    return out


# --------------------------------------------------------------------- change
def change_overlay(*, resources: list[dict[str, Any]], changes: list[dict[str, Any]]) -> dict[str, Any]:
    """``changes`` = recent change rows (already loaded by the caller; this stays pure)."""
    out = _empty()
    res_ids = {(r.get("id") or "").lower() for r in resources}
    for i, ch in enumerate(changes[:_CHANGE_CAP]):
        rid = (ch.get("resource_id") or ch.get("target") or ch.get("targetResourceId") or "").lower()
        key = f"{i}:{rid[-40:]}"
        node = A.change_node(key, ch)
        out["nodes"].append(node)
        if rid and rid in res_ids:
            out["edges"].append(A._edge(A.res_id(rid), node["id"], A.EDGE_CHANGED_IN))
            out["patches"].setdefault(A.res_id(rid), {}).setdefault("overlay", {})["changed_recently"] = True
    return out


# --------------------------------------------------------------------- apply
def apply_overlays(graph: dict[str, Any], overlays: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge overlay nodes/edges into ``graph`` and apply data patches. Returns the graph."""
    existing_node_ids = {n["id"] for n in graph["nodes"]}
    existing_edge_ids = {e["id"] for e in graph["edges"]}
    patches: dict[str, Any] = {}
    for ov in overlays:
        for n in ov.get("nodes", []):
            if n["id"] not in existing_node_ids:
                graph["nodes"].append(n)
                existing_node_ids.add(n["id"])
        for e in ov.get("edges", []):
            if e["id"] not in existing_edge_ids:
                graph["edges"].append(e)
                existing_edge_ids.add(e["id"])
        for nid, patch in ov.get("patches", {}).items():
            dst = patches.setdefault(nid, {})
            for k, v in patch.items():
                if isinstance(v, dict):
                    dst.setdefault(k, {}).update(v)
                else:
                    dst[k] = v
    if patches:
        by_id = {n["id"]: n for n in graph["nodes"]}
        for nid, patch in patches.items():
            node = by_id.get(nid)
            if not node:
                continue
            for k, v in patch.items():
                if isinstance(v, dict):
                    node["data"].setdefault(k, {}).update(v)
                else:
                    node["data"][k] = v
    graph["stats"] = A._stats(graph["nodes"], graph["edges"])
    return graph

"""Workload definition registry (JSON, no secrets → no encryption).

Persisted as backend/.data/workloads.json, consistent with the other registries. A
workload's membership is a list of *nodes* at mixed levels. A node may carry an
``excludes`` list (child ARM ids removed from an otherwise-whole parent) so that
"select a whole RG, minus one resource" stays precise."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "workloads.json"

NODE_KINDS = ("mg", "subscription", "resource_group", "resource")

DEFAULTS: dict[str, Any] = {
    "name": "",
    "description": "",
    "connection_id": "",
    "tenant_id": "",
    # nodes: [{kind, id (ARM id), name, subscription_id?, resource_group?,
    #          resource_type?, location?, excludes?: [arm_id,...]}]
    "nodes": [],
    "tags": [],
    # How this workload was created/scoped: {kind: mg|subscription, id, name} when built
    # by Autopilot (drives the Refresh button); empty for hand-built workloads.
    "origin": {},
    # Cached type breakdown + counts (recomputed on every save). Shape:
    # {types: [{label, count}], total_resources, scope_counts: {...}}
    "summary": {},
    # AI reasoning captured when Autopilot proposed this workload.
    "reasoning": "",
    "confidence": 0.0,
    # Autopilot classification + business context (AI-inferred, user-editable).
    "workload_type": "",      # web_app | data_pipeline | ai_ml | networking | storage | identity | integration | other
    "environment": "",        # production | staging | development | test | dr | shared | unknown
    "criticality": "",        # critical | high | medium | low (drives downstream severity/SLA weighting)
    "data_classification": "",  # confidential | internal | public | unknown
    # Evidence the grouper used to justify membership (network/RBAC/dependency/provenance
    # signals). Shape: [{kind, detail}] — surfaced in the UI for trust.
    "evidence": [],
    "last_refreshed": "",
    "created_by": "",
    "created_at": "",
    "updated_at": "",
    # Soft-delete: when set (ISO timestamp), the workload is in the Trash — hidden from
    # the active list and from all consumers (assessments, architectures, chat scope),
    # but restorable until purged.
    "deleted_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    from app.core import jsonstore

    data = jsonstore.read_json(_PATH, {"workloads": {}})
    return data if isinstance(data, dict) else {"workloads": {}}


def _write(data: dict[str, Any]) -> None:
    from app.core import jsonstore

    jsonstore.write_json(_PATH, data)


def list_workloads(include_deleted: bool = False) -> list[dict[str, Any]]:
    data = _read()
    out: list[dict[str, Any]] = []
    for wid, wl in data.get("workloads", {}).items():
        merged = json.loads(json.dumps(DEFAULTS))  # deep copy (nested dicts)
        merged.update(wl)
        merged["id"] = wid
        # Hide trashed workloads from the active list (and every consumer) unless asked.
        if not include_deleted and merged.get("deleted_at"):
            continue
        out.append(merged)
    out.sort(key=lambda w: w.get("name", "").lower())
    return out


def list_trashed_workloads() -> list[dict[str, Any]]:
    """Workloads currently in the Trash (soft-deleted), most-recently-deleted first."""
    out = [w for w in list_workloads(include_deleted=True) if w.get("deleted_at")]
    out.sort(key=lambda w: w.get("deleted_at", ""), reverse=True)
    return out


def get_workload(workload_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
    if not workload_id:
        return None
    for w in list_workloads(include_deleted=include_deleted):
        if w["id"] == workload_id:
            return w
    return None


def upsert_workload(wl: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    workloads = data.setdefault("workloads", {})
    wid = wl.get("id") or str(uuid.uuid4())
    existing = workloads.get(wid, {})
    merged = dict(existing)
    for key in DEFAULTS:
        if key in wl and wl[key] is not None:
            merged[key] = wl[key]
    # Recompute the cached summary from the node list (cheap, no Azure calls).
    from app.workloads.summarize import summarize_nodes

    merged["summary"] = summarize_nodes(merged.get("nodes", []))
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    workloads[wid] = merged
    _write(data)
    result = get_workload(wid)
    assert result is not None
    return result


def delete_workload(workload_id: str) -> bool:
    """Soft-delete: move the workload to the Trash (set ``deleted_at``). It's hidden
    everywhere but restorable until purged. Returns False if not found / already trashed."""
    data = _read()
    wl = data.get("workloads", {}).get(workload_id)
    if wl is None or wl.get("deleted_at"):
        return False
    wl["deleted_at"] = _now()
    wl["updated_at"] = _now()
    _write(data)
    return True


def restore_workload(workload_id: str) -> dict[str, Any] | None:
    """Restore a trashed workload back into the active list."""
    data = _read()
    wl = data.get("workloads", {}).get(workload_id)
    if wl is None or not wl.get("deleted_at"):
        return None
    wl["deleted_at"] = ""
    wl["updated_at"] = _now()
    _write(data)
    return get_workload(workload_id)


def purge_workload(workload_id: str) -> bool:
    """Permanently delete a single trashed workload (hard delete)."""
    data = _read()
    wl = data.get("workloads", {}).get(workload_id)
    if wl is None or not wl.get("deleted_at"):
        return False
    del data["workloads"][workload_id]
    _write(data)
    return True


def empty_trash() -> int:
    """Permanently delete every trashed workload. Returns the count removed."""
    data = _read()
    workloads = data.get("workloads", {})
    trashed = [wid for wid, wl in workloads.items() if wl.get("deleted_at")]
    for wid in trashed:
        del workloads[wid]
    if trashed:
        _write(data)
    return len(trashed)


def merge_workloads(workload_ids: list[str], new_name: str = "") -> dict[str, Any] | None:
    """Merge two or more active workloads into a single NEW workload.

    Combines their node membership (deduped by ARM id), tags and evidence; the highest
    criticality across the sources wins. The merged workload's name gets a trailing
    ``MERGED`` marker. The source workloads are moved to the Trash (soft-deleted), so the
    operation stays reversible. Returns the new workload, or ``None`` when fewer than two
    valid (active) sources are found. The result is a normal workload, so Refresh, Mission
    Control, assessments and architecture generation can all be run against it again."""
    data = _read()
    workloads = data.get("workloads", {})
    sources: list[dict[str, Any]] = []
    for wid in workload_ids:
        wl = workloads.get(wid)
        if wl is None or wl.get("deleted_at"):
            continue
        merged = json.loads(json.dumps(DEFAULTS))
        merged.update(wl)
        merged["id"] = wid
        sources.append(merged)
    if len(sources) < 2:
        return None

    # Combine nodes, deduped by ARM id (case-insensitive). When the same node appears in
    # multiple sources, intersect their excludes — a child is only excluded if EVERY source
    # excluded it (otherwise some source legitimately included it).
    merged_nodes: dict[str, dict[str, Any]] = {}
    for src in sources:
        for n in src.get("nodes", []):
            key = (n.get("id") or "").lower()
            if not key:
                continue
            if key not in merged_nodes:
                merged_nodes[key] = {**n, "excludes": list(n.get("excludes", []) or [])}
            else:
                prev = merged_nodes[key]
                prev_ex = {e.lower() for e in prev.get("excludes", [])}
                cur_ex = {e.lower() for e in (n.get("excludes", []) or [])}
                keep = prev_ex & cur_ex
                prev["excludes"] = [e for e in prev.get("excludes", []) if e.lower() in keep]

    # Combine tags / evidence (deduped, order-preserving).
    tags: list[str] = []
    for src in sources:
        for t in src.get("tags", []) or []:
            if t not in tags:
                tags.append(t)
    evidence: list[dict[str, Any]] = []
    seen_ev: set[str] = set()
    for src in sources:
        for ev in src.get("evidence", []) or []:
            sig = json.dumps(ev, sort_keys=True)
            if sig not in seen_ev:
                seen_ev.add(sig)
                evidence.append(ev)

    base = sources[0]
    name = (new_name.strip() or " + ".join(s.get("name", "") for s in sources)).strip()
    if not name.upper().endswith("MERGED"):
        name = f"{name} MERGED"

    crit_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "": 0}
    criticality = max(
        (s.get("criticality", "") for s in sources), key=lambda c: crit_rank.get(c, 0)
    )

    new_wl = {
        "name": name[:200],
        "description": base.get("description", ""),
        "connection_id": base.get("connection_id", ""),
        "tenant_id": base.get("tenant_id", ""),
        "nodes": list(merged_nodes.values()),
        "tags": tags,
        "origin": base.get("origin", {}),
        "reasoning": "Merged from: "
        + ", ".join(f"\u201c{s.get('name', '')}\u201d" for s in sources),
        "confidence": max((float(s.get("confidence", 0) or 0) for s in sources), default=0.0),
        "workload_type": base.get("workload_type", ""),
        "environment": base.get("environment", ""),
        "criticality": criticality,
        "data_classification": base.get("data_classification", ""),
        "evidence": evidence,
        "created_by": base.get("created_by", ""),
    }
    # upsert_workload does its own read/write, so create the merged workload first, then
    # soft-delete the sources via delete_workload (each manages its own persistence).
    saved = upsert_workload(new_wl)
    for src in sources:
        delete_workload(src["id"])
    return saved


# --------------------------------------------------------------------------- overlaps
def _overlap_workload_scope(wl: dict[str, Any], connection_id: str | None) -> bool:
    """True when a workload should be included given an optional connection filter."""
    if not connection_id:
        return True
    wl_conn = wl.get("connection_id") or ""
    # Include workloads with no connection (hand-built) so they aren't silently dropped.
    return (not wl_conn) or wl_conn == connection_id


def _index_explicit_memberships(
    workloads: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Map ARM id (lowercased) -> one membership per OWNING WORKLOAD for every explicit
    ``resource``-kind node. A resource listed twice in the same workload counts once."""
    index: dict[str, list[dict[str, Any]]] = {}
    for wl in workloads:
        wid = wl["id"]
        seen_in_wl: set[str] = set()
        for n in wl.get("nodes", []):
            if n.get("kind") != "resource":
                continue
            rid = str(n.get("id") or "")
            key = rid.lower()
            if not key or key in seen_in_wl:
                continue
            seen_in_wl.add(key)
            index.setdefault(key, []).append({
                "workload_id": wid,
                "workload_name": wl.get("name", ""),
                "via": "explicit",
                "id": rid,
                "name": n.get("name", ""),
                "resource_type": n.get("resource_type", ""),
                "resource_group": n.get("resource_group", ""),
                "subscription_id": n.get("subscription_id", ""),
                "location": n.get("location", ""),
            })
    return index


def _shape_overlaps(index: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Turn an id->memberships index into the API payload: only ids in >=2 DISTINCT
    workloads, plus summary + pairwise tallies."""
    from app.workloads.summarize import friendly_type

    overlaps: list[dict[str, Any]] = []
    pair_counts: dict[tuple[str, str], int] = {}
    pair_names: dict[str, str] = {}
    workloads_involved: set[str] = set()
    type_counts: dict[str, int] = {}

    for key, members in index.items():
        # Dedupe by workload (a resource pulled in both explicitly AND via scope = one chip,
        # preferring the explicit membership so the UI can offer a remove action).
        by_wl: dict[str, dict[str, Any]] = {}
        for m in members:
            prev = by_wl.get(m["workload_id"])
            if prev is None or (prev.get("via") != "explicit" and m.get("via") == "explicit"):
                by_wl[m["workload_id"]] = m
        if len(by_wl) < 2:
            continue
        chips = sorted(by_wl.values(), key=lambda m: (m["workload_name"] or "").lower())
        # Display metadata: prefer the first non-empty value across memberships.
        def _first(field: str) -> str:
            for m in chips:
                if m.get(field):
                    return str(m[field])
            return ""
        rtype = _first("resource_type")
        ftype = friendly_type(rtype) if rtype else ""
        overlaps.append({
            "id": chips[0]["id"],
            "name": _first("name"),
            "resource_type": rtype,
            "friendly_type": ftype,
            "resource_group": _first("resource_group"),
            "subscription_id": _first("subscription_id"),
            "location": _first("location"),
            "count": len(by_wl),
            "all_explicit": all(m.get("via") == "explicit" for m in chips),
            "workloads": [
                {"id": m["workload_id"], "name": m["workload_name"], "via": m.get("via", "explicit")}
                for m in chips
            ],
        })
        for m in chips:
            workloads_involved.add(m["workload_id"])
            pair_names[m["workload_id"]] = m["workload_name"]
        if ftype:
            type_counts[ftype] = type_counts.get(ftype, 0) + 1
        # Pairwise tally (every unordered pair of workloads sharing this resource).
        wids = sorted(by_wl.keys())
        for i in range(len(wids)):
            for j in range(i + 1, len(wids)):
                pair_counts[(wids[i], wids[j])] = pair_counts.get((wids[i], wids[j]), 0) + 1

    overlaps.sort(key=lambda o: (-o["count"], o["friendly_type"], (o["name"] or "").lower()))
    by_pair = [
        {
            "a": {"id": a, "name": pair_names.get(a, "")},
            "b": {"id": b, "name": pair_names.get(b, "")},
            "shared_count": n,
        }
        for (a, b), n in pair_counts.items()
    ]
    by_pair.sort(key=lambda p: -p["shared_count"])
    by_type = sorted(
        ({"friendly_type": k, "count": v} for k, v in type_counts.items()),
        key=lambda t: -t["count"],
    )
    total_extra = sum(o["count"] - 1 for o in overlaps)
    return {
        "overlaps": overlaps,
        "summary": {
            "duplicated_resources": len(overlaps),
            "workloads_involved": len(workloads_involved),
            "total_extra_memberships": total_extra,
            "by_type": by_type,
        },
        "by_pair": by_pair,
    }


def find_overlaps(connection_id: str | None = None) -> dict[str, Any]:
    """Tier 1 — resources EXPLICITLY listed (as ``resource`` nodes) in 2+ workloads.

    Pure, instant (no Azure calls). Optionally restricted to a connection. Returns
    ``{overlaps, summary, by_pair}`` (see ``_shape_overlaps``)."""
    workloads = [w for w in list_workloads() if _overlap_workload_scope(w, connection_id)]
    return _shape_overlaps(_index_explicit_memberships(workloads))


def find_overlaps_with_memberships(
    connection_id: str | None,
    scope_members: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Tier 2 — combine explicit resource memberships with caller-supplied SCOPE-implied
    memberships (a resource pulled in by a workload's whole RG/subscription/MG). The caller
    enumerates scopes (live, cached) and passes ``scope_members`` keyed by lowercased ARM id;
    each value is a list of ``{workload_id, workload_name, via, id, name, resource_type,
    resource_group, subscription_id, location}``. Excludes must already be applied."""
    workloads = [w for w in list_workloads() if _overlap_workload_scope(w, connection_id)]
    index = _index_explicit_memberships(workloads)
    for key, members in scope_members.items():
        index.setdefault(key, []).extend(members)
    return _shape_overlaps(index)

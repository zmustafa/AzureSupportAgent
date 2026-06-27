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

"""Field-level diff between two evidence snapshots in the same scope.

Computes adds / removes / changes for inventory (per-resource, per-field), and finding
status deltas. Filterable by resource type, tag, or finding check id."""
from __future__ import annotations

from typing import Any


def _index_resources(inv: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in (inv or {}).get("resources", []) or []:
        rid = str(r.get("id", "")).lower()
        if rid:
            out[rid] = r
    return out


def _resource_passes(r: dict[str, Any], *, type_filter: str, tag_filter: str) -> bool:
    if type_filter and type_filter.lower() not in str(r.get("type", "")).lower():
        return False
    if tag_filter:
        tags = r.get("tags") or {}
        if tag_filter not in tags and tag_filter not in (tags.values() if isinstance(tags, dict) else []):
            return False
    return True


def _field_changes(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Top-level field changes between two resource records (incl. tags + location + sku)."""
    changes: dict[str, Any] = {}
    keys = set(a.keys()) | set(b.keys())
    for k in keys:
        if k in ("id",):
            continue
        av, bv = a.get(k), b.get(k)
        if av != bv:
            # Keep diffs compact for big nested props.
            changes[k] = {"from": _trim(av), "to": _trim(bv)}
    return changes


def _trim(v: Any) -> Any:
    s = v
    if isinstance(v, (dict, list)):
        import json
        s = json.dumps(v, sort_keys=True)[:300]
    elif isinstance(v, str):
        s = v[:300]
    return s


def diff_inventory(a: dict[str, Any], b: dict[str, Any], *, type_filter: str = "", tag_filter: str = "") -> dict[str, Any]:
    ia, ib = _index_resources(a), _index_resources(b)
    a_ids, b_ids = set(ia), set(ib)
    added, removed, changed = [], [], []
    for rid in sorted(b_ids - a_ids):
        r = ib[rid]
        if _resource_passes(r, type_filter=type_filter, tag_filter=tag_filter):
            added.append({"id": r.get("id"), "name": r.get("name"), "type": r.get("type")})
    for rid in sorted(a_ids - b_ids):
        r = ia[rid]
        if _resource_passes(r, type_filter=type_filter, tag_filter=tag_filter):
            removed.append({"id": r.get("id"), "name": r.get("name"), "type": r.get("type")})
    for rid in sorted(a_ids & b_ids):
        if not _resource_passes(ib[rid], type_filter=type_filter, tag_filter=tag_filter):
            continue
        fc = _field_changes(ia[rid], ib[rid])
        if fc:
            changed.append({"id": ib[rid].get("id"), "name": ib[rid].get("name"), "type": ib[rid].get("type"), "fields": fc})
    return {"added": added, "removed": removed, "changed": changed,
            "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)}}


def _flatten_findings(section: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for run in (section or {}).get("runs", []) or []:
        for f in run.get("findings", []) or []:
            cid = f.get("check_id", "")
            if cid:
                out[cid] = {"check_id": cid, "title": f.get("title", ""), "status": f.get("status", ""),
                            "severity": f.get("severity", ""), "pillar": f.get("pillar", "")}
    return out


def diff_findings(a: dict[str, Any], b: dict[str, Any], *, finding_filter: str = "") -> dict[str, Any]:
    fa, fb = _flatten_findings(a), _flatten_findings(b)
    a_ids, b_ids = set(fa), set(fb)
    def _ok(cid: str) -> bool:
        return (not finding_filter) or finding_filter.lower() in cid.lower()
    added = [fb[c] for c in sorted(b_ids - a_ids) if _ok(c)]
    removed = [fa[c] for c in sorted(a_ids - b_ids) if _ok(c)]
    changed = []
    for c in sorted(a_ids & b_ids):
        if not _ok(c):
            continue
        if fa[c].get("status") != fb[c].get("status") or fa[c].get("severity") != fb[c].get("severity"):
            changed.append({"check_id": c, "title": fb[c]["title"],
                            "from": {"status": fa[c]["status"], "severity": fa[c]["severity"]},
                            "to": {"status": fb[c]["status"], "severity": fb[c]["severity"]}})
    return {"added": added, "removed": removed, "changed": changed,
            "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)}}


def diff_snapshots(
    content_a: dict[str, Any],
    content_b: dict[str, Any],
    *,
    type_filter: str = "",
    tag_filter: str = "",
    finding_filter: str = "",
) -> dict[str, Any]:
    return {
        "inventory": diff_inventory(content_a.get("inventory", {}), content_b.get("inventory", {}),
                                    type_filter=type_filter, tag_filter=tag_filter),
        "findings": diff_findings(content_a.get("findings", {}), content_b.get("findings", {}),
                                  finding_filter=finding_filter),
    }

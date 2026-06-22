"""Required-tag coverage and the 'missing only one tag' queue (F6).

Pure functions over normalized resource dicts. ``required`` is the list of required tag keys
(from the canonical catalog); ``exempt_types`` is a list of lower-cased resource-type
substrings that are exempt from required-tag enforcement (shared/platform services).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.tagintel.analysis import norm_key


def _resource_keys(r: dict[str, Any]) -> set[str]:
    return {norm_key(k) for k, v in (r.get("tags") or {}).items() if str(v or "").strip()}


def _is_exempt(rtype: str, exempt_types: list[str]) -> bool:
    rt = (rtype or "").lower()
    return any(sub and sub in rt for sub in exempt_types)


def coverage(resources: list[dict[str, Any]], required: list[str],
             exempt_types: list[str] | None = None, sub_names: dict[str, str] | None = None) -> dict[str, Any]:
    """Evaluate every in-scope resource against the required-tag set."""
    exempt_types = [e.lower() for e in (exempt_types or [])]
    sub_names = sub_names or {}
    req_norm = [(k, norm_key(k)) for k in required]
    total = len(resources)

    compliant = 0
    exempt = 0
    per_key_missing: dict[str, int] = defaultdict(int)      # required key -> # resources missing it
    missing_one: dict[str, list[dict[str, Any]]] = defaultdict(list)  # the single missing key -> resources
    # Matrix: resource group -> {required key -> missing count} (+ totals).
    rg_rows: dict[str, dict[str, Any]] = {}

    for r in resources:
        rtype = r.get("type", "")
        if req_norm and _is_exempt(rtype, exempt_types):
            exempt += 1
            continue
        have = _resource_keys(r)
        missing = [orig for orig, nk in req_norm if nk not in have]
        rg = f"{r.get('subscription_id','')}/{r.get('resource_group','')}"
        row = rg_rows.setdefault(rg, {"key": rg, "subscription_id": r.get("subscription_id", ""),
                                      "resource_group": r.get("resource_group", ""), "total": 0,
                                      "missing": defaultdict(int)})
        row["total"] += 1
        if not missing:
            compliant += 1
        else:
            for m in missing:
                per_key_missing[m] += 1
                row["missing"][m] += 1
            if len(missing) == 1:
                missing_one[missing[0]].append({
                    "id": r.get("id", ""), "name": r.get("name", ""), "type": rtype,
                    "resource_group": r.get("resource_group", ""), "subscription_id": r.get("subscription_id", ""),
                })

    evaluated = total - exempt
    per_key = [{
        "key": orig,
        "missing": per_key_missing.get(orig, 0),
        "present": evaluated - per_key_missing.get(orig, 0),
        "coverage_pct": round((evaluated - per_key_missing.get(orig, 0)) / evaluated * 100, 1) if evaluated else 100.0,
    } for orig, _nk in req_norm]

    # "Missing only one tag" — the highest-ROI fixes, grouped by the single absent key.
    missing_one_groups = [{
        "key": k,
        "count": len(v),
        "resources": v[:500],
    } for k, v in sorted(missing_one.items(), key=lambda kv: -len(kv[1]))]

    matrix = []
    cells_per_row = max(1, len(req_norm))
    for row in sorted(rg_rows.values(), key=lambda x: (sum(x["missing"].values()), -x["total"]), reverse=True)[:200]:
        miss_total = sum(row["missing"].values())
        filled = round((1 - miss_total / (row["total"] * cells_per_row)) * 100, 1) if row["total"] else 100.0
        matrix.append({
            "key": row["key"],
            "subscription": sub_names.get(row["subscription_id"], row["subscription_id"]),
            "resource_group": row["resource_group"],
            "total": row["total"],
            "missing": dict(row["missing"]),
            "compliant_pct": filled,
        })

    return {
        "required": required,
        "total_resources": total,
        "evaluated": evaluated,
        "exempt": exempt,
        "compliant": compliant,
        "non_compliant": evaluated - compliant,
        "coverage_pct": round(compliant / evaluated * 100, 1) if evaluated else 100.0,
        "per_key": per_key,
        "missing_one": missing_one_groups,
        "missing_one_total": sum(len(v) for v in missing_one.values()),
        "matrix": matrix,
    }

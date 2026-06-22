"""FinOps tag intelligence: billing-code -> workload -> owner mapping (F4) and cost
allocation / showback (F5). Pure functions.

``cost_by_resource`` is a ``{resource_id: cost}`` map (the trailing-30-day actuals from
``app.inventory.cost``); ids are matched case-insensitively.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.tagintel.analysis import norm_value

DEFAULT_BILLING_KEYS = ["BillingCode", "CostCenter", "CostCentre", "Billing"]
DEFAULT_OWNER_KEYS = ["Owner", "SupportTeam", "Team", "ManagedBy"]


def _cost_of(rid: str, cost_by_resource: dict[str, float]) -> float:
    return float(cost_by_resource.get(rid, cost_by_resource.get((rid or "").lower(), 0.0)) or 0.0)


def _first_tag(tags: dict[str, Any], keys: list[str]) -> str:
    low = {k.lower(): v for k, v in tags.items()}
    for k in keys:
        v = low.get(k.lower())
        if v:
            return str(v)
    return ""


def billing_map(resources: list[dict[str, Any]], cost_by_resource: dict[str, float],
                billing_keys: list[str] | None = None, owner_keys: list[str] | None = None) -> dict[str, Any]:
    """Join billing-code tag -> workloads + owners + 30-day cost. Resources without any
    billing tag roll up under a synthetic ``<unallocated>`` bucket so the gap is explicit."""
    billing_keys = billing_keys or DEFAULT_BILLING_KEYS
    owner_keys = owner_keys or DEFAULT_OWNER_KEYS
    buckets: dict[str, dict[str, Any]] = {}

    for r in resources:
        tags = r.get("tags") or {}
        code = _first_tag(tags, billing_keys) or "<unallocated>"
        owner = _first_tag(tags, owner_keys)
        b = buckets.setdefault(code, {
            "billing_code": code, "cost": 0.0, "resource_count": 0,
            "workloads": defaultdict(int), "owners": defaultdict(int), "subscriptions": set(),
            "untagged_owner": 0,
        })
        b["cost"] += _cost_of(r.get("id", ""), cost_by_resource)
        b["resource_count"] += 1
        if r.get("subscription_id"):
            b["subscriptions"].add(r["subscription_id"])
        for w in (r.get("workloads") or []):
            b["workloads"][w["name"]] += 1
        if owner:
            b["owners"][owner] += 1
        else:
            b["untagged_owner"] += 1

    rows = []
    for b in buckets.values():
        rows.append({
            "billing_code": b["billing_code"],
            "cost": round(b["cost"], 2),
            "resource_count": b["resource_count"],
            "subscription_count": len(b["subscriptions"]),
            "workloads": [{"name": n, "count": c} for n, c in sorted(b["workloads"].items(), key=lambda kv: -kv[1])[:10]],
            "owners": [{"name": n, "count": c} for n, c in sorted(b["owners"].items(), key=lambda kv: -kv[1])[:10]],
            "owner_coverage_pct": round((b["resource_count"] - b["untagged_owner"]) / b["resource_count"] * 100, 1) if b["resource_count"] else 0,
            "unallocated": b["billing_code"] == "<unallocated>",
        })
    rows.sort(key=lambda x: (x["unallocated"], -x["cost"]))
    return {"rows": rows, "total_codes": sum(1 for r in rows if not r["unallocated"])}


def cost_allocation(resources: list[dict[str, Any]], cost_by_resource: dict[str, float],
                    dimension: str = "workload", billing_keys: list[str] | None = None) -> dict[str, Any]:
    """Allocate trailing-30-day cost by a chosen dimension and quantify the unallocatable
    spend (resources missing a billing tag). ``dimension`` is one of
    ``workload|owner|environment|subscription|<tagkey>``."""
    billing_keys = billing_keys or DEFAULT_BILLING_KEYS
    total_cost = 0.0
    allocatable = 0.0
    tagged_cost = 0.0
    untagged_cost = 0.0
    by_dim: dict[str, float] = defaultdict(float)
    unalloc_resources: list[dict[str, Any]] = []
    shared_candidates: list[dict[str, Any]] = []

    for r in resources:
        c = _cost_of(r.get("id", ""), cost_by_resource)
        total_cost += c
        tags = r.get("tags") or {}
        if tags:
            tagged_cost += c
        else:
            untagged_cost += c
        # Allocatable = has a billing tag.
        if _first_tag(tags, billing_keys):
            allocatable += c
        elif c > 0:
            unalloc_resources.append({"id": r.get("id", ""), "name": r.get("name", ""),
                                      "type": r.get("type", ""), "cost": round(c, 2),
                                      "resource_group": r.get("resource_group", "")})
        # Dimension bucket.
        if dimension == "workload":
            wls = r.get("workloads") or []
            label = wls[0]["name"] if wls else "<unassigned>"
            if len(wls) > 1 and c > 0:
                shared_candidates.append({"id": r.get("id", ""), "name": r.get("name", ""),
                                          "cost": round(c, 2), "workloads": [w["name"] for w in wls]})
        elif dimension == "subscription":
            label = r.get("subscription_id", "") or "<none>"
        else:
            low = {k.lower(): v for k, v in tags.items()}
            label = str(low.get(dimension.lower(), "") or f"<no {dimension}>")
        by_dim[label] += c

    breakdown = [{"label": k, "cost": round(v, 2)} for k, v in sorted(by_dim.items(), key=lambda kv: -kv[1])]
    unalloc_resources.sort(key=lambda x: -x["cost"])
    shared_candidates.sort(key=lambda x: -x["cost"])
    return {
        "dimension": dimension,
        "currency": "",
        "total_cost": round(total_cost, 2),
        "allocatable_cost": round(allocatable, 2),
        "unallocatable_cost": round(total_cost - allocatable, 2),
        "allocatable_pct": round(allocatable / total_cost * 100, 1) if total_cost else 0,
        "tagged_cost": round(tagged_cost, 2),
        "untagged_cost": round(untagged_cost, 2),
        "breakdown": breakdown[:50],
        "unallocatable_resources": unalloc_resources[:200],
        "shared_candidates": shared_candidates[:100],
    }


def reconcile_cmdb(discovered_codes: list[str], cmdb_codes: list[str]) -> dict[str, Any]:
    """Diff billing codes discovered in Azure against an imported CMDB list."""
    disc = {norm_value(c): c for c in discovered_codes if c and c != "<unallocated>"}
    cmdb = {norm_value(c): c for c in cmdb_codes if c}
    in_both = sorted(disc[k] for k in disc.keys() & cmdb.keys())
    only_azure = sorted(disc[k] for k in disc.keys() - cmdb.keys())
    only_cmdb = sorted(cmdb[k] for k in cmdb.keys() - disc.keys())
    return {"in_both": in_both, "only_in_azure": only_azure, "only_in_cmdb": only_cmdb,
            "match_pct": round(len(in_both) / len(disc) * 100, 1) if disc else 0}

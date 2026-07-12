"""Cost optimization analysis over the cached inventory.

Surfaces orphaned / idle resources that typically cost money for no benefit
(unattached disks, idle public IPs, orphaned NICs) by reusing the hygiene `flags`
already computed per resource during collection, joined with trailing-30-day actual
cost (≈ monthly) so each finding carries an estimated monthly saving.

Pure functions — no Azure / DB access — so they're cheap to unit test and safe to
call from the request path against already-cached data.
"""
from __future__ import annotations

from typing import Any

# Each cleanup flag maps to a user-facing category with remediation guidance. Ordered
# by how confidently the resource is wasteful.
_CATEGORY_META: dict[str, dict[str, str]] = {
    "unattached_disk": {
        "label": "Unattached managed disks",
        "reason": "Managed disk is not attached to any VM but still bills for provisioned capacity.",
        "remediation": "Snapshot if needed, then delete the disk to stop capacity charges.",
        "severity": "warning",
    },
    "idle_public_ip": {
        "label": "Idle public IP addresses",
        "reason": "Standard public IP is not associated with any resource but still incurs an hourly charge.",
        "remediation": "Delete the public IP, or associate it with a load balancer / NIC if intended.",
        "severity": "warning",
    },
    "orphaned_nic": {
        "label": "Orphaned network interfaces",
        "reason": "Network interface is not attached to a VM or private endpoint.",
        "remediation": "Delete the NIC if the parent resource is gone; usually free but signals leftover proliferation.",
        "severity": "info",
    },
}

# Flags that represent real cost-cleanup opportunities (vs. pure governance like untagged).
CLEANUP_FLAGS = list(_CATEGORY_META.keys())


def _primary_category(flags: list[str]) -> str | None:
    """Pick the most actionable cleanup flag for a resource (first by our ordering)."""
    for flag in CLEANUP_FLAGS:
        if flag in flags:
            return flag
    return None


def analyze_resources(
    resources: list[dict[str, Any]],
    cost_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a cost-optimization report from cached inventory + cost.

    Returns categories (with counts + estimated monthly cost), a flat item list, and
    tenant-wide totals. Costs are best-effort: resources with no cost data report 0.
    """
    by_resource: dict[str, float] = {}
    currency = "USD"
    cost_available = False
    cost_period = ""
    if cost_payload and cost_payload.get("available"):
        raw = cost_payload.get("by_resource")
        # Defensive: cost data is best-effort upstream — tolerate a non-dict shape or a
        # non-numeric value (skip it) rather than crashing the request path.
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    by_resource[str(k).lower()] = float(v or 0)
                except (TypeError, ValueError):
                    continue
        currency = cost_payload.get("currency") or "USD"
        cost_available = True
        cost_period = cost_payload.get("period") or ""

    items: list[dict[str, Any]] = []
    cat_acc: dict[str, dict[str, Any]] = {}

    for r in resources:
        flags = r.get("flags") or []
        category = _primary_category(flags)
        if not category:
            continue
        rid = str(r.get("id") or "")
        monthly = round(by_resource.get(rid.lower(), 0.0), 2)
        meta = _CATEGORY_META[category]
        item = {
            "id": rid,
            "name": r.get("name"),
            "type": r.get("type"),
            "location": r.get("location"),
            "resource_group": r.get("resource_group"),
            "subscription_id": r.get("subscription_id"),
            "flags": flags,
            "category": category,
            "category_label": meta["label"],
            "reason": meta["reason"],
            "remediation": meta["remediation"],
            "severity": meta["severity"],
            "monthly_cost": monthly,
            "workloads": r.get("workloads") or [],
        }
        items.append(item)
        acc = cat_acc.setdefault(
            category,
            {
                "flag": category,
                "label": meta["label"],
                "reason": meta["reason"],
                "remediation": meta["remediation"],
                "severity": meta["severity"],
                "count": 0,
                "monthly_cost": 0.0,
            },
        )
        acc["count"] += 1
        acc["monthly_cost"] = round(acc["monthly_cost"] + monthly, 2)

    # Sort items by cost (desc) so the biggest wins float to the top.
    items.sort(key=lambda i: i["monthly_cost"], reverse=True)
    categories = sorted(
        cat_acc.values(), key=lambda c: (c["monthly_cost"], c["count"]), reverse=True
    )

    return {
        "categories": categories,
        "items": items,
        "total_count": len(items),
        "total_monthly_cost": round(sum(i["monthly_cost"] for i in items), 2),
        "currency": currency,
        "cost_available": cost_available,
        "cost_period": cost_period,
    }

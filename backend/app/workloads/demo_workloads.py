"""Extra demo workloads (Zava Shoes) for review without a live Azure tenant.

Two hand-built workloads (Zava Shoes Website + Zava Shoes CRM) whose resources come from
the shared per-workload catalog (``app.demo_catalog``) — each with its own realistic,
*distinct* resource set — so the workload picker, inventory, architectures and coverage
scopes look populated. Seeded/removed alongside the rest of the demo dataset
(Settings → Demo Data).
"""
from __future__ import annotations

from typing import Any

from app.demo_catalog import ZAVA_CRM_ID, ZAVA_WEB_ID, nodes_for, workload_meta

# Fixed ids so seed is idempotent and purge can target exactly these.
DEMO_WORKLOAD_IDS = [ZAVA_WEB_ID, ZAVA_CRM_ID]


def seed_demo() -> list[dict[str, Any]]:
    """Create/refresh the two Zava demo workloads from the catalog. Idempotent."""
    from app.workloads.registry import get_workload, upsert_workload

    out: list[dict[str, Any]] = []
    for wid in DEMO_WORKLOAD_IDS:
        meta = workload_meta(wid)
        payload = {
            "id": wid,
            "name": meta["name"],
            "description": meta["description"],
            "nodes": nodes_for(wid),
            "tags": list(meta.get("tags", [])),
            "created_by": "system-demo",
        }
        existing = get_workload(wid)
        if existing:
            payload = {**existing, **payload}
        out.append(upsert_workload(payload))
    return out


def purge_demo() -> int:
    """Hard-delete the Zava demo workloads. Returns how many were removed."""
    from app.workloads.registry import delete_workload, get_workload, purge_workload

    removed = 0
    for wid in DEMO_WORKLOAD_IDS:
        if get_workload(wid) is not None or get_workload(wid, include_deleted=True) is not None:
            delete_workload(wid)
            purge_workload(wid)
            removed += 1
    return removed

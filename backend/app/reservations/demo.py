"""Synthetic Reservations data for review/demo without live reservations.

The original Logic App's audience has real reservations; this local tenant may have none,
so the demo scope fabricates a representative spread — expiring soon, recently expired,
healthy, a non-renewing one and an under-utilized one — to exercise the panel and the
digest. Marked ``demo: True``; the API serves this instead of calling Azure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.reservations.collector import compute_reservations

DEMO_SCOPE_ID = "demo"


def _date_in(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def _record(
    rid: str,
    name: str,
    *,
    expiry_days: int,
    term: str = "P1Y",
    renew: bool | None = True,
    util: float | None = 78.0,
    sku: str = "Standard_D4s_v5",
    qty: int = 3,
    created_days: int = -360,
    rtype: str = "VirtualMachines",
    scope: str = "Shared",
    state: str = "Succeeded",
) -> dict[str, Any]:
    return {
        "id": rid,
        "order_id": f"/providers/Microsoft.Capacity/reservationOrders/{rid}",
        "display_name": name,
        "term": term,
        "billing_plan": "Upfront",
        "created_date": _date_in(created_days),
        "expiry_date": _date_in(expiry_days),
        "provisioning_state": state,
        "renew": renew,
        "utilization_pct": util,
        "sku": sku,
        "reserved_resource_type": rtype,
        "applied_scope_type": scope,
        "quantity": qty,
        "reservation_count": 1,
    }


def demo_records() -> list[dict[str, Any]]:
    return [
        _record("d1a2b3c4-0001-0000-0000-000000000001", "prod-vm-d4s-v5 (eastus)", expiry_days=18, util=91.0),
        _record(
            "d1a2b3c4-0002-0000-0000-000000000002",
            "sql-db-gp-gen5 (eastus2)",
            expiry_days=44,
            term="P3Y",
            renew=False,
            util=63.0,
            sku="SQLDB_GP_Gen5",
            rtype="SqlDatabases",
            qty=2,
        ),
        _record(
            "d1a2b3c4-0003-0000-0000-000000000003",
            "appgw-storage-reserved (westus)",
            expiry_days=-12,
            renew=False,
            util=18.0,
            sku="Standard_LRS",
            rtype="Storage",
            state="Expired",
            qty=1,
        ),
        _record(
            "d1a2b3c4-0004-0000-0000-000000000004",
            "core-vm-e8s-v5 (northeurope)",
            expiry_days=410,
            term="P3Y",
            util=84.0,
            sku="Standard_E8s_v5",
            qty=5,
        ),
        _record(
            "d1a2b3c4-0005-0000-0000-000000000005",
            "analytics-cosmos-ru (centralus)",
            expiry_days=120,
            renew=True,
            util=22.0,
            sku="Cosmos_RU",
            rtype="CosmosDb",
            qty=1,
        ),
    ]


def seed_demo(*, window_days: int = 60) -> dict[str, Any]:
    snap = compute_reservations(demo_records(), window_days=window_days)
    snap.update(
        {
            "source": "demo",
            "demo": True,
            "connection_configured": True,
            "error": "",
            "never_loaded": False,
        }
    )
    return snap


def is_demo_scope(scope_id: str) -> bool:
    return (scope_id or "") == DEMO_SCOPE_ID

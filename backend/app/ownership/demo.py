"""Demo ownership data — seeds a few owners/teams and assigns them to the demo workloads
so the /ownership screens have something to show without a live Azure scan.

Idempotent: fixed owner + assignment ids, upserted. ``purge_demo`` removes exactly what was
seeded. Wired into the admin Demo Data Load / Remove buttons (app/api/admin_demo.py)."""
from __future__ import annotations

from typing import Any

from app.ownership import registry

# Fixed ids so seed/purge are idempotent and don't duplicate on repeated Load.
_OWNERS = [
    {"id": "own-demo-platform", "kind": "team", "display_name": "Platform Team",
     "email": "platform-team@contoso.com", "source": "manual",
     "notes": "Owns shared platform + Contoso Hotels."},
    {"id": "own-demo-john", "kind": "person", "display_name": "John Doe",
     "email": "john.doe@contoso.com", "source": "manual",
     "notes": "Demo person owner."},
    {"id": "own-demo-web", "kind": "team", "display_name": "Storefront Team",
     "email": "storefront-team@zava.com", "source": "manual",
     "notes": "Owns the Zava storefront."},
    {"id": "own-demo-crm", "kind": "team", "display_name": "Sales Ops",
     "email": "sales-ops@zava.com", "source": "manual",
     "notes": "Owns the Zava CRM."},
]

# (assignment_id, owner_id, subject_kind, subject_id, subject_name, role, primary)
_ASSIGNMENTS = [
    ("asn-demo-contoso", "own-demo-platform", "workload", "demo-amba-coverage", "Contoso Hotels", "technical", True),
    ("asn-demo-contoso-biz", "own-demo-john", "workload", "demo-amba-coverage", "Contoso Hotels", "business", False),
    ("asn-demo-zava-web", "own-demo-web", "workload", "demo-zava-shoes-website", "Zava Shoes Website", "technical", True),
    ("asn-demo-zava-crm", "own-demo-crm", "workload", "demo-zava-shoes-crm", "Zava Shoes CRM", "technical", True),
]

_DEMO_OWNER_IDS = {o["id"] for o in _OWNERS}
_DEMO_ASSIGNMENT_IDS = {a[0] for a in _ASSIGNMENTS}


def seed_demo(tenant_id: str) -> dict[str, Any]:
    """Upsert the demo owners + assignments under ``tenant_id`` (idempotent)."""
    for o in _OWNERS:
        registry.upsert_owner(tenant_id, {**o, "created_by": "demo"})
    for aid, owner_id, kind, sid, sname, role, primary in _ASSIGNMENTS:
        registry.upsert_assignment(tenant_id, {
            "id": aid, "owner_id": owner_id, "subject_kind": kind, "subject_id": sid,
            "subject_name": sname, "role": role, "primary": primary, "source": "manual",
            "created_by": "demo",
        })
    return {"owners": len(_OWNERS), "assignments": len(_ASSIGNMENTS)}


def purge_demo(tenant_id: str) -> int:
    """Hard-delete exactly the seeded demo owners + assignments. Returns count removed."""
    removed = 0
    for aid in _DEMO_ASSIGNMENT_IDS:
        # purge requires the record to be trashed first; soft-delete then purge.
        registry.delete_assignment(tenant_id, aid)
        if registry.purge_assignment(tenant_id, aid):
            removed += 1
    for oid in _DEMO_OWNER_IDS:
        registry.delete_owner(tenant_id, oid)
        if registry.purge_owner(tenant_id, oid):
            removed += 1
    return removed


def is_seeded(tenant_id: str) -> bool:
    return any(o["id"] in _DEMO_OWNER_IDS for o in registry.list_owners(tenant_id, include_deleted=True))

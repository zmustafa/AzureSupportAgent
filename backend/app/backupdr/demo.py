"""Synthetic Backup & DR Coverage data for review/demo without a live Azure tenant.

Drives the demo from the shared per-workload catalog (``app.demo_catalog``) so each demo
workload gets its own resources and a realistic backup/DR posture derived from each
resource's health tier:
    green → protected, geo-redundant, offsite, DR pair, recent job, restore-tested
    amber → protected but local-only / same-region / no DR / stale job / never tested
    red   → not protected at all (Key Vault: soft-delete off)
"""
from __future__ import annotations

from typing import Any

from app.backupdr.collector import compute_coverage
from app.demo_catalog import CONTOSO_ID, resources_for, workload_meta

DEMO_WORKLOAD_ID = CONTOSO_ID  # default demo scope used by the API when none is supplied

# Region pairing for "offsite" backup/DR destinations.
_REGION_PAIR = {
    "eastus": "westus",
    "eastus2": "centralus",
    "centralus": "eastus2",
    "westeurope": "northeurope",
    "westus2": "eastus2",
    "global": "westus",
}


def _pair(region: str) -> str:
    return _REGION_PAIR.get((region or "").lower(), "westus")


def demo_state_by_resource(scope_id: str = CONTOSO_ID) -> dict[str, dict[str, Any]]:
    """Backup-state facts keyed by lowercased resource id, exercising red/amber/green."""
    out: dict[str, dict[str, Any]] = {}
    for res in resources_for(scope_id):
        rid = res["id"].lower()
        tier = res["tier"]
        region = res["location"]
        if tier == "green":
            out[rid] = {
                "backup_enabled": True, "vault_name": "rsv-prod", "policy": "DailyPolicy",
                "retention_days": 90, "last_job_status": "succeeded", "last_job_age_hours": 6,
                "geo_redundant": True, "backup_region": _pair(region), "dr_pair": True,
                "dr_target_region": _pair(region), "encryption": "cmk", "last_restore_test_age_days": 40,
                "soft_delete": True, "purge_protection": True, "pitr": True, "persistence": True,
                "persistence_mode": "AOF", "geo_dr_pair": True,
            }
        elif tier == "amber":
            out[rid] = {
                "backup_enabled": True, "vault_name": "rsv-regional", "policy": "DailyPolicy",
                "retention_days": 14, "last_job_status": "succeeded", "last_job_age_hours": 40,
                "geo_redundant": False, "backup_region": region, "dr_pair": False, "encryption": "pmk",
                "last_restore_test_age_days": 220, "soft_delete": True, "purge_protection": False,
                "pitr": True, "persistence": False, "geo_dr_pair": False,
            }
        else:  # red
            out[rid] = {
                "backup_enabled": False, "encryption": "pmk", "soft_delete": False,
                "purge_protection": False, "pitr": False, "persistence": False,
            }
    return out


def demo_dr_pairs(scope_id: str = CONTOSO_ID) -> list[dict[str, Any]]:
    res = resources_for(scope_id)
    region = workload_meta(scope_id)["primary_region"]
    pairs: list[dict[str, Any]] = []
    greens = [r for r in res if r["tier"] == "green"
              and r["type"] in ("microsoft.compute/virtualmachines", "microsoft.web/sites")]
    if greens:
        pairs.append({
            "name": f"{greens[0]['name']} ASR", "primary_region": region, "secondary_region": _pair(region),
            "replication_health": "Healthy", "last_failover_test_age_days": 45, "protected_items": 3,
        })
    bad = [r for r in res if r["tier"] in ("red", "amber")]
    if bad:
        pairs.append({
            "name": f"{bad[0]['name']} ASR", "primary_region": region, "secondary_region": _pair(region),
            "replication_health": "Critical", "last_failover_test_age_days": 240, "protected_items": 1,
        })
    return pairs


def build_demo_snapshot(*, sla_hours: int = 24, stale_drill_days: int = 180,
                        scope_id: str = CONTOSO_ID, scope_name: str | None = None) -> dict[str, Any]:
    from app.amba.demo import demo_scope_name

    snap = compute_coverage(
        resources_for(scope_id), demo_state_by_resource(scope_id), demo_dr_pairs(scope_id),
        sla_hours=sla_hours, stale_drill_days=stale_drill_days,
    )
    snap.update(
        {
            "scope_kind": "workload",
            "scope_id": scope_id,
            "scope_name": scope_name or demo_scope_name(scope_id),
            "connection_configured": False,
            "source": "demo_dummy_data",
            "demo": True,
            "error": "",
        }
    )
    return snap


def seed_demo(*, sla_hours: int = 24, stale_drill_days: int = 180, tenant_id: str = "default",
              scope_id: str = CONTOSO_ID, scope_name: str | None = None) -> dict[str, Any]:
    # Cache the demo snapshot only — do NOT auto-register the demo workload (explicit Demo Data
    # load handles that), so viewing demo backup/DR coverage never creates a phantom workload.
    from app.backupdr import cache

    snap = build_demo_snapshot(sla_hours=sla_hours, stale_drill_days=stale_drill_days,
                               scope_id=scope_id, scope_name=scope_name)
    cache.write_snapshot(tenant_id, "workload", scope_id, snap)
    return snap


def is_demo_scope(scope_kind: str, scope_id: str) -> bool:
    from app.amba.demo import is_demo_scope as _is

    return _is(scope_kind, scope_id)

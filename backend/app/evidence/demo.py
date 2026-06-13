"""Dummy evidence snapshots for review without live Azure.

Creates two snapshots of the shared demo workload a few 'moments' apart with a deliberate
inventory + findings delta, so the locker list, detail tabs, SHA verification, and the
field-level diff are all reviewable. Marked demo=True."""
from __future__ import annotations

from typing import Any

from app.evidence import registry

DEMO_SCOPE = {"kind": "workload", "id": "demo-amba-coverage", "resource_ids": []}


def _content(*, variant: str) -> dict[str, Any]:
    base_resources = [
        {"id": "/subs/x/rg-demo-shop/providers/microsoft.web/sites/shop-web-prod", "name": "shop-web-prod",
         "type": "microsoft.web/sites", "resourceGroup": "rg-demo-shop", "location": "eastus", "tags": {"env": "prod"}},
        {"id": "/subs/x/rg-demo-shop/providers/microsoft.keyvault/vaults/shop-kv-prod", "name": "shop-kv-prod",
         "type": "microsoft.keyvault/vaults", "resourceGroup": "rg-demo-shop", "location": "eastus", "tags": {"env": "prod"}},
        {"id": "/subs/x/rg-demo-shop/providers/microsoft.storage/storageaccounts/shopassetsprod", "name": "shopassetsprod",
         "type": "microsoft.storage/storageaccounts", "resourceGroup": "rg-demo-shop", "location": "eastus",
         "tags": {"env": "prod", "sku": "Standard_LRS" if variant == "before" else "Standard_RAGRS"}},
    ]
    if variant == "after":
        # A new resource appeared + storage sku changed (the diff target).
        base_resources.append({
            "id": "/subs/x/rg-demo-shop/providers/microsoft.cache/redis/shop-redis-prod", "name": "shop-redis-prod",
            "type": "microsoft.cache/redis", "resourceGroup": "rg-demo-shop", "location": "eastus", "tags": {"env": "prod"}})

    findings_before = [
        {"check_id": "amba_microsoft.keyvault_vaults_kv_availability", "title": "KV availability alert missing",
         "status": "fail", "severity": "error", "pillar": "operations"},
        {"check_id": "backupdr_microsoft.cache_redis_backup_enabled", "title": "Redis not backed up",
         "status": "fail", "severity": "warning", "pillar": "reliability"},
    ]
    findings_after = [
        # KV alert fixed; redis backup still failing; a new MFA finding appeared.
        {"check_id": "amba_microsoft.keyvault_vaults_kv_availability", "title": "KV availability alert missing",
         "status": "pass", "severity": "error", "pillar": "operations"},
        {"check_id": "backupdr_microsoft.cache_redis_backup_enabled", "title": "Redis not backed up",
         "status": "fail", "severity": "warning", "pillar": "reliability"},
        {"check_id": "telemetry_microsoft.web_sites_no_diagnostics", "title": "No diagnostics on shop-web-prod",
         "status": "fail", "severity": "error", "pillar": "operations"},
    ]
    return {
        "_meta": {"scope": DEMO_SCOPE, "connection_configured": False, "demo": True},
        "inventory": {"resources": base_resources, "captured_at": ""},
        "findings": {"runs": [{"id": "demo", "trigger": "demo", "pillars": ["operations", "reliability"],
                               "findings": findings_before if variant == "before" else findings_after}], "waivers": []},
        "changes": {"changes": [], "window_days": 14},
        "architecture": {"architectures": []},
        "memory": {"memories": []},
    }


def seed_demo(*, tenant_id: str = "default") -> dict[str, Any]:
    before = registry.create_snapshot(
        tenant_id=tenant_id, name="DEMO — Shop workload (baseline)", scope=DEMO_SCOPE,
        included=["inventory", "properties", "findings", "changes"], retention_class="audit",
        tags=["demo", "baseline"], content=_content(variant="before"), created_by="system-demo",
        finding_links=["amba_microsoft.keyvault_vaults_kv_availability"], demo=True,
    )
    after = registry.create_snapshot(
        tenant_id=tenant_id, name="DEMO — Shop workload (after change)", scope=DEMO_SCOPE,
        included=["inventory", "properties", "findings", "changes"], retention_class="standard",
        tags=["demo", "after"], content=_content(variant="after"), created_by="system-demo", demo=True,
    )
    return {"before_id": before["id"], "after_id": after["id"]}

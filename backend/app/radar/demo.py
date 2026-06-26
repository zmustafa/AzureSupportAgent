"""Synthetic Retirement & Breaking-Change Radar data for review/demo without a live tenant.

Drives the demo from the shared per-workload catalog (``app.demo_catalog``) so each demo
workload gets its own, distinct radar feed (retirements, breaking changes, owned/unowned
items and Azure OpenAI model deployments) referencing that workload's own resources.
Marked ``demo: True`` everywhere; the API serves this instead of querying Azure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.demo_catalog import CONTOSO_ID, DEMO_SUB, ZAVA_CRM_ID, ZAVA_WEB_ID, rg_for, workload_meta
from app.radar.collector import build_model_items, compute_radar, merge_events

DEMO_WORKLOAD_ID = CONTOSO_ID  # default demo scope used by the API when none is supplied


def _date_in(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def _rid(rg: str, provider_type: str, name: str) -> str:
    return f"/subscriptions/{DEMO_SUB}/resourceGroups/{rg}/providers/{provider_type}/{name}"


def _impacted(rg: str, name: str, ptype: str, owner: str | None = "platform-team", region: str = "eastus") -> dict[str, Any]:
    tags = {"environment": "prod"}
    if owner:
        tags["owner"] = owner
    return {
        "id": _rid(rg, ptype, name), "name": name, "type": ptype,
        "resourceGroup": rg, "subscriptionId": DEMO_SUB, "location": region, "tags": tags,
    }


def _contoso_events() -> list[dict[str, Any]]:
    rg = rg_for(CONTOSO_ID)
    region = workload_meta(CONTOSO_ID)["primary_region"]
    outbound_impacted = [
        _impacted(rg, f"contoso-vm-{i:02d}", "microsoft.compute/virtualmachines", region=region)
        for i in range(1, 12)
    ] + [_impacted(rg, "contoso-pms-vm", "microsoft.compute/virtualmachines", owner=None, region="westeurope")]
    return [
        {
            "source": "service_health", "tracking_id": "DOA-2026-0331",
            "title": "Default outbound access for VMs will be retired",
            "summary": "On 31 Mar 2026 Azure retires default outbound internet access for new and existing VMs without an explicit outbound method.",
            "retirement_date": _date_in(47), "change_type": "retirement",
            "impacted_resources": outbound_impacted,
        },
        {
            "source": "advisor", "tracking_id": "TLS-CONTOSO-001",
            "title": "Event Grid topic still accepts TLS 1.0/1.1",
            "summary": "Minimum TLS version will be enforced to 1.2; clients negotiating 1.0/1.1 will fail.",
            "retirement_date": _date_in(75),
            "recommended_replacement": "Set minimumTlsVersionAllowed = 1.2 on the topic and update clients.",
            "migration_url": "https://learn.microsoft.com/azure/event-grid/transport-layer-security",
            "impacted_resources": [_impacted(rg, "contoso-events", "microsoft.eventgrid/topics", owner="messaging-team")],
        },
        {
            "source": "advisor", "tracking_id": "BASICIP-RETIRE",
            "title": "Basic SKU Public IP addresses will be retired",
            "summary": "Basic SKU public IPs retire on 30 Sep 2025; upgrade to Standard SKU.",
            "retirement_date": _date_in(140),
            "impacted_resources": [_impacted(rg, "contoso-pip-legacy", "microsoft.network/publicipaddresses", owner=None)],
        },
        {
            "source": "service_health", "tracking_id": "CLASSIC-STG-2026",
            "title": "Classic storage accounts (ASM) retirement",
            "summary": "Classic (ASM) storage accounts are retiring; migrate to ARM.",
            "retirement_date": _date_in(220),
            "impacted_resources": [_impacted(rg, "contosolegacyclassic", "microsoft.classicstorage/storageaccounts", owner="storage-team")],
        },
    ]


def _zava_web_events() -> list[dict[str, Any]]:
    rg = rg_for(ZAVA_WEB_ID)
    region = workload_meta(ZAVA_WEB_ID)["primary_region"]
    return [
        {
            "source": "service_health", "tracking_id": "DOTNET6-EOS-WEB",
            "title": ".NET 6 runtime end of support on App Service",
            "summary": ".NET 6 reaches end of support; App Service apps on this stack must move to .NET 8.",
            "retirement_date": _date_in(58), "change_type": "retirement",
            "recommended_replacement": "Re-target the storefront and checkout function to .NET 8 LTS.",
            "impacted_resources": [
                _impacted(rg, "zava-web-storefront", "microsoft.web/sites", owner="web-team", region=region),
                _impacted(rg, "zava-web-checkout-func", "microsoft.web/sites", owner=None, region=region),
            ],
        },
        {
            "source": "advisor", "tracking_id": "TLS-ZAVAWEB-APPGW",
            "title": "Application Gateway listener still allows TLS 1.0/1.1",
            "summary": "TLS 1.0/1.1 will be disallowed; configure a TLS 1.2+ SSL policy on the gateway.",
            "retirement_date": _date_in(92),
            "migration_url": "https://learn.microsoft.com/azure/application-gateway/ssl-overview",
            "impacted_resources": [_impacted(rg, "zava-web-appgw", "microsoft.network/applicationgateways", owner="web-team", region=region)],
        },
        {
            "source": "advisor", "tracking_id": "BASICIP-ZAVAWEB",
            "title": "Basic SKU Public IP addresses will be retired",
            "summary": "Basic SKU public IPs retire; upgrade to Standard SKU.",
            "retirement_date": _date_in(150),
            "impacted_resources": [_impacted(rg, "zava-web-pip", "microsoft.network/publicipaddresses", owner=None, region=region)],
        },
    ]


def _zava_crm_events() -> list[dict[str, Any]]:
    rg = rg_for(ZAVA_CRM_ID)
    region = workload_meta(ZAVA_CRM_ID)["primary_region"]
    return [
        {
            "source": "service_health", "tracking_id": "DOA-2026-0331",
            "title": "Default outbound access for VMs will be retired",
            "summary": "Azure retires default outbound internet access for VMs without an explicit outbound method.",
            "retirement_date": _date_in(31), "change_type": "retirement",
            "impacted_resources": [
                _impacted(rg, "zava-crm-vm01", "microsoft.compute/virtualmachines", owner="crm-team", region=region),
                _impacted(rg, "zava-crm-vm02", "microsoft.compute/virtualmachines", owner=None, region=region),
            ],
        },
        {
            "source": "advisor", "tracking_id": "PG11-EOS-CRM",
            "title": "PostgreSQL 11 community version is retiring",
            "summary": "Azure Database for PostgreSQL flexible server v11 reaches end of life; upgrade to v16.",
            "retirement_date": _date_in(70), "change_type": "retirement",
            "recommended_replacement": "Major-version upgrade the analytics server to PostgreSQL 16.",
            "impacted_resources": [_impacted(rg, "zava-crm-pg", "microsoft.dbforpostgresql/flexibleservers", owner="data-team", region=region)],
        },
        {
            "source": "advisor", "tracking_id": "TLS-ZAVACRM-APPGW",
            "title": "Application Gateway listener still allows TLS 1.0/1.1",
            "summary": "TLS 1.0/1.1 will be disallowed; configure a TLS 1.2+ SSL policy on the gateway.",
            "retirement_date": _date_in(105),
            "impacted_resources": [_impacted(rg, "zava-crm-appgw", "microsoft.network/applicationgateways", owner="crm-team", region=region)],
        },
    ]


_RAW_EVENTS = {
    CONTOSO_ID: _contoso_events,
    ZAVA_WEB_ID: _zava_web_events,
    ZAVA_CRM_ID: _zava_crm_events,
}


def demo_raw_events(scope_id: str = CONTOSO_ID) -> list[dict[str, Any]]:
    return _RAW_EVENTS.get(scope_id, _contoso_events)()


def _aoai(rg: str, account: str, deployment: str, model: str, version: str, region: str) -> dict[str, Any]:
    return {
        "id": _rid(rg, "microsoft.cognitiveservices/accounts/deployments", f"{account}/{deployment}"),
        "account": account, "deployment": deployment, "model": model,
        "model_version": version, "region": region, "resource_group": rg, "subscription_id": DEMO_SUB,
    }


def demo_aoai_deployments(scope_id: str = CONTOSO_ID) -> list[dict[str, Any]]:
    if scope_id == ZAVA_WEB_ID:
        rg = rg_for(ZAVA_WEB_ID)
        return [
            _aoai(rg, "zava-web-aoai", "recommend", "gpt-4o", "2024-11-20", "eastus2"),
            _aoai(rg, "zava-web-aoai", "search-embed", "text-embedding-ada-002", "2", "eastus2"),
        ]
    if scope_id == ZAVA_CRM_ID:
        rg = rg_for(ZAVA_CRM_ID)
        return [
            _aoai(rg, "zava-crm-aoai", "summarize", "gpt-35-turbo", "0613", "centralus"),
        ]
    rg = rg_for(CONTOSO_ID)
    return [
        _aoai(rg, "contoso-aoai", "gpt35", "gpt-35-turbo", "0613", "eastus"),
        _aoai(rg, "contoso-aoai", "ada", "text-embedding-ada-002", "2", "eastus"),
        _aoai(rg, "contoso-aoai", "gpt4o", "gpt-4o", "2024-11-20", "eastus"),
    ]


def build_demo_snapshot(tenant_id: str = "default", *, scope_id: str = CONTOSO_ID, scope_name: str | None = None) -> dict[str, Any]:
    from app.amba.demo import demo_scope_name
    from app.radar.collector import _workload_index
    from app.radar.state import apply_states

    wl_index = _workload_index()
    events = merge_events(demo_raw_events(scope_id), wl_index=wl_index)
    events = apply_states(tenant_id, events)
    model_items = build_model_items(demo_aoai_deployments(scope_id))
    snap = compute_radar(events, model_items)
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


def seed_demo(tenant_id: str = "default", *, scope_id: str = CONTOSO_ID, scope_name: str | None = None) -> dict[str, Any]:
    # Cache the demo snapshot only — do NOT auto-register the demo workload (explicit Demo Data
    # load handles that), so viewing the demo radar never creates a phantom workload.
    from app.radar import cache

    snap = build_demo_snapshot(tenant_id=tenant_id, scope_id=scope_id, scope_name=scope_name)
    cache.write_snapshot(tenant_id, "workload", scope_id, snap)
    return snap


def is_demo_scope(scope_kind: str, scope_id: str) -> bool:
    from app.amba.demo import is_demo_scope as _is

    return _is(scope_kind, scope_id)

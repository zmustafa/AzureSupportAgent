"""Architecture retail-pricing regressions: authoritative rates, never guessed totals."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app.architectures import catalog, designer, pricing
from app.api import architectures as architecture_api
from app.core import retail_prices
from app.core.security import Principal


def _row(
    *,
    service: str = "Virtual Machines",
    product: str = "Virtual Machines D Series Linux",
    sku: str = "Standard D2s v5",
    arm_sku: str = "Standard_D2s_v5",
    sku_id: str = "sku-linux",
    meter: str = "D2s v5",
    unit: str = "1 Hour",
    price: float = 0.1,
    meter_id: str = "meter-1",
    row_type: str = "Consumption",
    region: str = "eastus",
) -> dict[str, Any]:
    return {
        "currencyCode": "USD",
        "tierMinimumUnits": 0.0,
        "retailPrice": price,
        "unitPrice": price,
        "armRegionName": region,
        "location": "US East",
        "effectiveStartDate": "2026-01-01T00:00:00Z",
        "meterId": meter_id,
        "meterName": meter,
        "productId": "product-1",
        "skuId": sku_id,
        "productName": product,
        "skuName": sku,
        "serviceName": service,
        "serviceId": "service-1",
        "serviceFamily": "Compute",
        "unitOfMeasure": unit,
        "type": row_type,
        "isPrimaryMeterRegion": True,
        "armSkuName": arm_sku,
    }


def _node(
    node_id: str = "n1",
    arm_type: str = "microsoft.compute/virtualmachines",
    *,
    sku: str = "Standard_D2s_v5",
    hint: dict[str, Any] | None = None,
    arm_id: str = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "arm_id": arm_id,
        "name": node_id,
        "type": arm_type,
        "location": "eastus",
        "sku": sku,
        "pricing_hint": hint or {},
        "meta": {},
    }


def test_odata_filter_escapes_values_and_never_accepts_a_raw_expression() -> None:
    built = retail_prices.build_filter("Owner's Service", regions=("eastus", ""), sku_names=("P1'v3",))
    assert "serviceName eq 'Owner''s Service'" in built
    assert "armRegionName eq 'eastus'" in built and "armRegionName eq ''" in built
    assert "P1''v3" in built
    assert "type eq 'Consumption'" in built


@pytest.mark.asyncio
async def test_retail_client_pages_and_normalizes_the_observed_api_shape() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={
                "BillingCurrency": "USD",
                "Items": [
                    {**_row(), "futureField": {"ignored": True}},
                    _row(meter_id="reservation", row_type="Reservation", price=100_000),
                    {"type": "Consumption", "meterName": "broken", "retailPrice": "NaN"},
                ],
                "NextPageLink": "https://prices.azure.com/api/retail/prices?$skip=1000",
                "Count": 3,
            })
        return httpx.Response(200, json={
            "Items": [_row(meter_id="meter-2", meter="D4s v5", price=0.2)],
            "NextPageLink": "",
            "Count": 1,
        })

    result = await retail_prices.fetch_retail_prices(
        "Virtual Machines",
        currency="USD",
        regions=("eastus",),
        transport=httpx.MockTransport(handler),
    )
    assert result.error == ""
    assert result.pages == 2
    assert len(result.items) == 2
    assert result.invalid_rows == 2
    assert "futureField" not in result.items[0]
    assert result.items[0]["retailPrice"] == pytest.approx(0.1)
    assert "$filter" in calls[0].url.params


@pytest.mark.asyncio
async def test_retail_client_refuses_a_foreign_next_page_host() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "Items": [_row()],
            "NextPageLink": "https://example.invalid/collect",
        })

    result = await retail_prices.fetch_retail_prices(
        "Virtual Machines", currency="USD", regions=("eastus",),
        transport=httpx.MockTransport(handler),
    )
    assert result.error == "next_page_refused"
    assert result.pages == 1
    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_retail_client_retries_transient_throttling() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"Items": [_row()], "NextPageLink": ""})

    result = await retail_prices.fetch_retail_prices(
        "Virtual Machines", currency="USD", regions=("eastus",),
        transport=httpx.MockTransport(handler),
    )
    assert result.error == ""
    assert calls == 2
    assert len(result.items) == 1


async def _catalogs_with(items: list[dict[str, Any]], *, stale: bool = False, truncated: bool = False):
    async def load(queries: dict[str, pricing.CatalogQuery], *, force: bool):
        del force
        return {
            key: {
                "items": items,
                "fetched_at": "2026-08-11T00:00:00Z",
                "truncated": truncated,
                "stale": stale,
                "error": "Retail Prices refresh failed; cached rates are shown." if stale else "",
            }
            for key in queries
        }
    return load


@pytest.mark.asyncio
async def test_exact_vm_hourly_meter_becomes_a_730_hour_monthly_baseline(monkeypatch) -> None:
    monkeypatch.setattr(pricing, "_load_catalogs", await _catalogs_with([_row()]))
    result = await pricing.price_architecture({"id": "a1", "nodes": [_node(hint={"os_type": "Linux"})]})
    node = result["nodes"][0]
    assert node["status"] == "priced_monthly"
    assert node["monthly_estimate"] == pytest.approx(73.0)
    assert result["summary"]["known_fixed_monthly"] == pytest.approx(73.0)
    assert result["monthly_hours"] == 730.0


@pytest.mark.asyncio
async def test_vm_license_variants_are_ambiguous_without_os_fact(monkeypatch) -> None:
    windows = _row(
        product="Virtual Machines D Series Windows", sku_id="sku-windows",
        meter_id="meter-windows", price=0.2,
    )
    linux = _row(product="Virtual Machines D Series Linux", sku_id="sku-linux", meter_id="meter-linux")
    monkeypatch.setattr(pricing, "_load_catalogs", await _catalogs_with([windows, linux]))
    result = await pricing.price_architecture({"id": "a1", "nodes": [_node()]})
    node = result["nodes"][0]
    assert node["status"] == "ambiguous"
    assert len(node["candidates"]) == 2
    assert node["monthly_estimate"] is None


@pytest.mark.asyncio
async def test_mixed_nat_meter_only_totals_the_single_fixed_baseline(monkeypatch) -> None:
    gateway = _row(
        service="NAT Gateway", product="NAT Gateway", sku="Standard", arm_sku="",
        sku_id="nat-standard", meter="Standard Gateway", unit="1 Hour", price=0.045,
        meter_id="nat-hour", region="Global",
    )
    data = _row(
        service="NAT Gateway", product="NAT Gateway", sku="Standard", arm_sku="",
        sku_id="nat-standard", meter="Standard Data Processed", unit="1 GB", price=0.045,
        meter_id="nat-gb", region="Global",
    )
    monkeypatch.setattr(pricing, "_load_catalogs", await _catalogs_with([gateway, data]))
    node = _node("nat", "microsoft.network/natgateways", sku="Standard")
    result = await pricing.price_architecture({"id": "a1", "nodes": [node]})
    priced = result["nodes"][0]
    assert priced["status"] == "priced_monthly"
    assert priced["monthly_estimate"] == pytest.approx(32.85)
    assert len(priced["components"]) == 2
    assert "usage-dependent" in priced["reason"]


@pytest.mark.asyncio
async def test_usage_services_do_not_invent_monthly_quantities(monkeypatch) -> None:
    async def should_not_fetch(*_args, **_kwargs):
        raise AssertionError("usage-only service without meter facts must not query a broad catalog")

    monkeypatch.setattr(pricing, "_load_catalogs", should_not_fetch)
    storage = _node("storage", "microsoft.storage/storageaccounts", sku="")
    result = await pricing.price_architecture({"id": "a1", "nodes": [storage]})
    node = result["nodes"][0]
    assert node["status"] == "rate_only"
    assert node["monthly_estimate"] is None
    assert node["components"] == []
    assert "required" in node["reason"].lower()


@pytest.mark.asyncio
async def test_every_nonpriced_case_is_explicit_and_never_zero(monkeypatch) -> None:
    async def no_queries(queries: dict[str, pricing.CatalogQuery], *, force: bool):
        assert not queries
        return {}

    monkeypatch.setattr(pricing, "_load_catalogs", no_queries)
    nodes = [
        _node("concept", "microsoft.compute/virtualmachines", arm_id=""),
        _node("logical-sql", "microsoft.sql/servers", sku=""),
        _node("future", "microsoft.future/widgets", sku="Future_1"),
    ]
    result = await pricing.price_architecture({"id": "a1", "nodes": nodes})
    assert [node["status"] for node in result["nodes"]] == [
        "not_applicable", "not_applicable", "unmatched",
    ]
    assert all(node["monthly_estimate"] is None for node in result["nodes"])


@pytest.mark.asyncio
async def test_stale_catalog_is_labeled_not_silently_presented_as_fresh(monkeypatch) -> None:
    monkeypatch.setattr(pricing, "_load_catalogs", await _catalogs_with([_row()], stale=True))
    result = await pricing.price_architecture({"id": "a1", "nodes": [_node(hint={"os_type": "Linux"})]})
    assert result["stale"] is True
    assert result["nodes"][0]["stale"] is True


@pytest.mark.asyncio
async def test_all_palette_and_arbitrary_arm_types_end_in_a_valid_state(monkeypatch) -> None:
    async def empty_catalogs(queries: dict[str, pricing.CatalogQuery], *, force: bool):
        del force
        return {
            key: {"items": [], "fetched_at": "2026-08-11T00:00:00Z", "truncated": False, "stale": False, "error": ""}
            for key in queries
        }

    monkeypatch.setattr(pricing, "_load_catalogs", empty_catalogs)
    nodes = [
        _node(f"palette-{index}", item["type"], sku="Standard", hint={"capacity": 2})
        for index, item in enumerate(catalog.PALETTE)
    ]
    nodes.extend(
        _node(f"unknown-{index}", f"microsoft.provider{index}/widgets/type", sku="S1")
        for index in range(5_000)
    )
    result = await pricing.price_architecture({"id": "catalog-audit", "nodes": nodes})
    valid = {"priced_monthly", "rate_only", "free", "ambiguous", "unmatched", "not_applicable", "unavailable"}
    assert len(result["nodes"]) == len(nodes)
    assert all(node["status"] in valid for node in result["nodes"])
    assert all(node["monthly_estimate"] is None for node in result["nodes"])


def test_designer_preserves_pricing_facts_from_arg_inventory() -> None:
    arm_id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachineScaleSets/vmss"
    parsed = {"nodes": [{"id": "n1", "arm_id": arm_id, "name": "vmss"}], "edges": [], "groups": []}
    resources = [{
        "id": arm_id,
        "name": "vmss",
        "type": "microsoft.compute/virtualmachinescalesets",
        "location": "eastus",
        "resourceGroup": "rg",
        "subscriptionId": "s",
        "sku": {"name": "Standard_D2s_v5", "tier": "Standard", "capacity": 3},
        "properties": {"virtualMachineProfile": {"storageProfile": {"osDisk": {"osType": "Linux"}}}},
    }]
    normalized = designer._normalize(parsed, resources)
    node = normalized["nodes"][0]
    assert node["sku"] == "Standard_D2s_v5"
    assert node["pricing_hint"] == {
        "sku": "Standard_D2s_v5", "tier": "Standard", "capacity": 3, "os_type": "Linux",
    }


@pytest.mark.asyncio
async def test_stale_disk_cache_fallback_survives_live_failure(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "architecture_prices.json"
    monkeypatch.setattr(pricing, "_CACHE_PATH", cache_path)
    query = pricing.CatalogQuery("Virtual Machines", "USD", ("eastus",), ("Standard_D2s_v5",))
    cache_path.write_text(json.dumps({"entries": {query.key: {
        "cached_at": 1.0,
        "fetched_at": "2026-01-01T00:00:00Z",
        "items": [_row()],
        "truncated": False,
        "invalid_rows": 0,
    }}}), encoding="utf-8")

    async def failed_fetch(*_args, **_kwargs):
        return retail_prices.RetailFetchResult([], "", 0, error="request_failed")

    monkeypatch.setattr(pricing, "fetch_retail_prices", failed_fetch)
    catalogs = await pricing._load_catalogs({query.key: query}, force=True)
    assert catalogs[query.key]["stale"] is True
    assert catalogs[query.key]["items"][0]["meterId"] == "meter-1"
    assert "request_failed" not in catalogs[query.key]["error"]


@pytest.mark.asyncio
async def test_pricing_endpoint_forwards_explicit_currency_after_tenant_guard(monkeypatch) -> None:
    arch = {"id": "a1", "tenant_id": "tenant-a", "nodes": []}
    principal = Principal("subject", "user@example.test", "tenant-a", "user", frozenset({"architectures.read"}))
    monkeypatch.setattr(architecture_api.arch_registry, "get_architecture", lambda *_args, **_kwargs: arch)

    async def resolved(found: dict[str, Any], currency: str, *, force: bool):
        return {"id": found["id"], "currency": currency, "force": force}

    monkeypatch.setattr(pricing, "price_architecture", resolved)
    result = await architecture_api.get_architecture_pricing_endpoint("a1", "eur", True, principal)
    assert result == {"id": "a1", "currency": "EUR", "force": True}


@pytest.mark.asyncio
async def test_pricing_endpoint_hides_cross_tenant_architecture(monkeypatch) -> None:
    arch = {"id": "a1", "tenant_id": "tenant-b", "nodes": []}
    principal = Principal("subject", "user@example.test", "tenant-a", "user", frozenset({"architectures.read"}))
    monkeypatch.setattr(architecture_api.arch_registry, "get_architecture", lambda *_args, **_kwargs: arch)
    with pytest.raises(HTTPException) as caught:
        await architecture_api.get_architecture_pricing_endpoint("a1", "USD", False, principal)
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_pricing_endpoint_does_not_reflect_validation_exception(monkeypatch) -> None:
    arch = {"id": "a1", "tenant_id": "tenant-a", "nodes": []}
    principal = Principal("subject", "user@example.test", "tenant-a", "user", frozenset({"architectures.read"}))
    monkeypatch.setattr(architecture_api.arch_registry, "get_architecture", lambda *_args, **_kwargs: arch)

    async def failed(*_args, **_kwargs):
        raise ValueError("SECRET C:/internal/path")

    monkeypatch.setattr(pricing, "price_architecture", failed)
    with pytest.raises(HTTPException) as caught:
        await architecture_api.get_architecture_pricing_endpoint("a1", "USD", False, principal)
    assert caught.value.status_code == 400
    assert caught.value.detail == "Currency must be a three-letter code such as USD or EUR."
    assert "SECRET" not in caught.value.detail and "internal" not in caught.value.detail
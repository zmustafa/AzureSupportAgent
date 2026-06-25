"""Subscription / region / resource-provider discovery for the quota scanner.

- ``list_subscriptions`` — subscriptions visible to a connection's identity (for the picker).
- ``list_regions`` — physical regions the subscription reports (so the operator can pick a subset
  or scan all). Cached in-memory per (token-hash, subscription) for a few minutes to bound ARM
  calls (requirement: avoid excessive ARM calls; cache region/provider metadata).
- ``provider_registration`` — registration state of the resource providers the collectors need,
  with remediation guidance for any that aren't registered."""
from __future__ import annotations

import time
from typing import Any

import httpx

_ARM = "https://management.azure.com"

# Resource providers the collectors depend on (surfaced in the registration check).
REQUIRED_PROVIDERS = [
    "Microsoft.Quota",
    "Microsoft.Compute",
    "Microsoft.Network",
    "Microsoft.Storage",
    "Microsoft.Web",
    "Microsoft.Sql",
    "Microsoft.Insights",
    "Microsoft.OperationalInsights",
    "Microsoft.KeyVault",
    "Microsoft.MachineLearningServices",
    "Microsoft.CognitiveServices",
]

# In-memory region cache: {(token_tail, sub): (expires_epoch, regions)}.
_REGION_TTL_S = 600
_region_cache: dict[tuple[str, str], tuple[float, list[dict[str, str]]]] = {}


async def _get(token: str, path: str, params: dict[str, str]) -> tuple[Any, str | None, int]:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=45, base_url=_ARM) as client:
            resp = await client.get(path, headers=headers, params=params)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            return None, f"ARM {resp.status_code}: {str(detail)[:300]}", resp.status_code
        return resp.json(), None, 200
    except httpx.HTTPError as exc:  # noqa: BLE001
        return None, f"ARM request error: {exc}", 0


async def list_regions(token: str, subscription_id: str, *, use_cache: bool = True) -> tuple[list[dict[str, str]], str | None]:
    """Physical regions the subscription supports, with the same metadata the Azure regions-list
    doc shows (geography, geography group, physical location, paired region, availability-zone
    support, category). Sourced live from the ARM ``/locations`` API — Microsoft recommends this
    REST API over a hardcoded list, so the set stays current as regions are added.

    Returns ``([{name, display_name, regional_display_name, geography, geography_group,
    physical_location, category, has_availability_zones, paired_region}], error)``."""
    key = (token[-12:], subscription_id)
    now = time.time()
    if use_cache:
        hit = _region_cache.get(key)
        if hit and hit[0] > now:
            return list(hit[1]), None
    # api-version 2022-12-01 returns availabilityZoneMappings + the full metadata block.
    data, err, _status = await _get(
        token, f"/subscriptions/{subscription_id}/locations", {"api-version": "2022-12-01"}
    )
    if err:
        return [], err
    regions: list[dict[str, str]] = []
    for loc in (data or {}).get("value", []) or []:
        meta = loc.get("metadata", {}) or {}
        # Only physical regions (skip logical/edge zones) so usage APIs accept them.
        if meta.get("regionType") not in (None, "Physical"):
            continue
        name = loc.get("name", "")
        if not name:
            continue
        paired = meta.get("pairedRegion") or []
        regions.append({
            "name": name,
            "display_name": loc.get("displayName", name),
            "regional_display_name": loc.get("regionalDisplayName", loc.get("displayName", name)),
            "geography": meta.get("geography", ""),
            "geography_group": meta.get("geographyGroup", "Other"),
            "physical_location": meta.get("physicalLocation", ""),
            "category": meta.get("regionCategory", ""),  # Recommended | Other
            "has_availability_zones": bool(loc.get("availabilityZoneMappings")),
            "paired_region": (paired[0].get("name", "") if paired else ""),
        })
    # Group by geography (US, Europe, Asia Pacific, …) then display name — mirrors the doc table.
    regions.sort(key=lambda r: (r.get("geography_group") or "zz", r["display_name"]))
    _region_cache[key] = (now + _REGION_TTL_S, regions)
    return regions, None


async def provider_registration(token: str, subscription_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """Registration state of the required providers. Returns [{namespace, state, registered}]."""
    data, err, _status = await _get(
        token, f"/subscriptions/{subscription_id}/providers",
        {"api-version": "2022-12-01", "$select": "namespace,registrationState"},
    )
    if err:
        return [], err
    state_by_ns = {
        str(p.get("namespace", "")).lower(): p.get("registrationState", "Unknown")
        for p in (data or {}).get("value", []) or []
    }
    out: list[dict[str, Any]] = []
    for ns in REQUIRED_PROVIDERS:
        state = state_by_ns.get(ns.lower(), "NotPresent")
        out.append({
            "namespace": ns,
            "state": state,
            "registered": str(state).lower() == "registered",
            "remediation": (
                "" if str(state).lower() == "registered"
                else f"az provider register --namespace {ns} --subscription {subscription_id}"
            ),
        })
    return out, None

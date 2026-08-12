"""Azure Retail Prices — live, authoritative list prices for Backup and Site Recovery.

The public Retail Prices API (`prices.azure.com`, no authentication) replaces the
hand-seeded rate table that would otherwise drift and be denominated in the wrong currency.

Two facts drive the shape of this module, both established by probing the live API rather
than by reading marketing pages:

* **Protected-instance charges are a flat monthly rate per datasource type**, not a tier on
  source size. `Azure VM Protected Instance`, `Azure Files Protected Instance`,
  `SQL Server in Azure VM Protected Instance` and friends each carry their own price. The
  legacy "under 50 GB / 50-500 GB / per additional 500 GB" model does not describe current
  Azure Backup pricing at all.
* **Reservation rows must be excluded.** The API returns `type: "Reservation"` rows whose
  `retailPrice` is the whole 100 TB / 1 PB term (hundreds of thousands), alongside the real
  `type: "Consumption"` per-GB rate. Mixing them inflates an estimate by ~6 orders of
  magnitude.

Prices are cached on disk per (region, currency) because they change rarely and a rate lookup
must never sit on the critical path of a dashboard render.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from app.core.retail_prices import fetch_retail_prices

log = logging.getLogger("app.backup_manager.pricing")

BACKUP_SERVICE = "Backup"
ASR_SERVICE = "Azure Site Recovery"

_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "backup_manager_prices.json"
# List prices move on the order of months; a week keeps them fresh without hammering the API.
CACHE_TTL_SECONDS = 7 * 24 * 3600
MAX_PAGES = 8

# Our normalised datasource types -> the retail meter that prices one protected instance.
# Keys are lowercase and matched loosely (see `instance_meter_key`).
DATASOURCE_TO_METER: dict[str, str] = {
    "vm": "azure vm",
    "azureiaasvm": "azure vm",
    "microsoft.compute/virtualmachines": "azure vm",
    "azurefileshare": "azure files",
    "microsoft.storage/storageaccounts/fileservices": "azure files",
    "microsoft.storage/storageaccounts/blobservices": "azure blob",
    "azureblob": "azure blob",
    "microsoft.containerservice/managedclusters": "azure kubernetes",
    "azurekubernetesservice": "azure kubernetes",
    "microsoft.dbforpostgresql/flexibleservers": "postgresql",
    "microsoft.dbforpostgresql/servers": "postgresql",
    "sqldatabase": "sql server in azure vm",
    "saphanadatabase": "sap hana on azure vm",
    "sapasedatabase": "sap ase on azure vm",
    "microsoft.documentdb/databaseaccounts": "cosmos db",
    "mabcontainer": "on premises server",
    "windows": "on premises server",
}

# Azure Disk Backup is snapshot-based: there is no protected-instance meter for it, and the
# snapshot storage is billed on the managed-disk snapshot meters in the snapshot resource
# group — outside the Backup service entirely. Priced as zero here, and flagged so the UI can
# say so rather than silently under-reporting.
NO_INSTANCE_METER = {"microsoft.compute/disks", "azuredisk"}

_REDUNDANCY_ALIASES = {
    "lrs": ("standard lrs data stored",),
    "zrs": ("standard zrs data stored",),
    "grs": ("standard grs data stored",),
    "ra_grs": ("standard ra-grs data stored",),
    "archive_lrs": ("archive lrs data stored",),
    "archive_grs": ("archive grs data stored",),
}
_FILES_REDUNDANCY_ALIASES = {
    "lrs": ("azure files vaulted lrs data stored",),
    "zrs": ("azure files vaulted zrs data stored",),
    "grs": ("azure files vaulted grs data stored",),
    "ra_grs": ("azure files vaulted ra-grs data stored",),
}


def instance_meter_key(meter_name: str) -> str:
    """Normalise `"Azure VM Protected Instance"` -> `"azure vm"`."""
    text = str(meter_name or "").strip().lower()
    for suffix in (" protected instance", " snapshot instance"):
        if text.endswith(suffix):
            return text[: -len(suffix)].strip()
    return text


# --------------------------------------------------------------------------- fetching
async def _fetch(service_name: str, region: str, currency: str) -> tuple[list[dict[str, Any]], str]:
    """Page through the retail API for one service/region. Returns ``(items, error)``."""
    result = await fetch_retail_prices(
        service_name,
        currency=currency,
        regions=(region,),
        max_pages=MAX_PAGES,
    )
    error = f"Retail Prices request failed ({result.error})." if result.error else ""
    return result.items, error


def build_rate_card(
    backup_items: list[dict[str, Any]],
    asr_items: list[dict[str, Any]],
    *,
    region: str,
    currency: str,
) -> dict[str, Any]:
    """Project raw retail meters onto the rate card the cost model consumes."""
    instance_meters: dict[str, float] = {}
    by_name: dict[str, float] = {}
    for item in backup_items:
        if str(item.get("type") or "") != "Consumption":
            continue
        name = str(item.get("meterName") or "").strip().lower()
        try:
            price = float(item.get("retailPrice"))
        except (TypeError, ValueError):
            continue
        by_name[name] = price
        if name.endswith("protected instance") or name.endswith("snapshot instance"):
            key = instance_meter_key(name)
            # A datasource can have both a protected-instance and a snapshot-instance meter
            # (SQL, SAP HANA). Keep the protected-instance price as the representative one.
            if key not in instance_meters or name.endswith("protected instance"):
                instance_meters[key] = price

    def pick(aliases: tuple[str, ...]) -> float | None:
        for alias in aliases:
            if alias in by_name:
                return by_name[alias]
        return None

    storage_rates = {key: pick(aliases) for key, aliases in _REDUNDANCY_ALIASES.items()}
    files_rates = {key: pick(aliases) for key, aliases in _FILES_REDUNDANCY_ALIASES.items()}

    asr_rates: dict[str, float] = {}
    for item in asr_items:
        if str(item.get("type") or "") != "Consumption":
            continue
        name = str(item.get("meterName") or "").strip().lower()
        try:
            price = float(item.get("retailPrice"))
        except (TypeError, ValueError):
            continue
        if "replicated to azure" in name:
            asr_rates["azure"] = price
        elif "replicated to" in name:
            asr_rates["system_center"] = price

    return {
        "source": "azure_retail_prices",
        "currency": currency,
        "region": region,
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "instance_meters": instance_meters,
        "storage_gb_month": {k: v for k, v in storage_rates.items() if v is not None},
        "files_storage_gb_month": {k: v for k, v in files_rates.items() if v is not None},
        "site_recovery_instance_month": asr_rates.get("azure"),
        "meter_count": len(backup_items) + len(asr_items),
        "error": "",
    }


# --------------------------------------------------------------------------- cache
def _read_cache() -> dict[str, Any]:
    if _CACHE_PATH.exists():
        try:
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_cache(data: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - a cache write failure must not break pricing
        log.debug("backup_manager: could not persist price cache: %s", exc)


def _cache_key(region: str, currency: str) -> str:
    return f"{region.lower()}|{currency.upper()}"


async def get_rate_card(region: str, currency: str, *, force: bool = False) -> dict[str, Any]:
    """Cached rate card for a region/currency. Never raises; degrades with ``error`` set."""
    region = (region or "eastus").strip().lower()
    currency = (currency or "USD").strip().upper()
    key = _cache_key(region, currency)
    cache = _read_cache()
    entry = cache.get(key) if isinstance(cache.get(key), dict) else None
    if entry and not force:
        age = time.time() - float(entry.get("cached_at") or 0)
        if age < CACHE_TTL_SECONDS and entry.get("card"):
            card = dict(entry["card"])
            card["cache_age_seconds"] = int(age)
            return card

    backup_items, backup_error = await _fetch(BACKUP_SERVICE, region, currency)
    asr_items, asr_error = await _fetch(ASR_SERVICE, region, currency)
    if not backup_items:
        # Keep serving a stale card rather than falling back to a wrong-currency guess.
        if entry and entry.get("card"):
            stale = dict(entry["card"])
            stale["error"] = backup_error or "No retail meters returned."
            stale["stale"] = True
            return stale
        return {
            "source": "unavailable", "currency": currency, "region": region, "as_of": "",
            "instance_meters": {}, "storage_gb_month": {}, "files_storage_gb_month": {},
            "site_recovery_instance_month": None, "meter_count": 0,
            "error": backup_error or asr_error or "No retail meters returned.",
        }

    card = build_rate_card(backup_items, asr_items, region=region, currency=currency)
    cache[key] = {"cached_at": time.time(), "card": card}
    _write_cache(cache)
    card["cache_age_seconds"] = 0
    return card


def instance_rate(card: dict[str, Any], datasource_type: str, backup_management_type: str = "") -> tuple[float | None, str]:
    """Monthly protected-instance rate for a datasource. Returns ``(rate, meter_key)``.

    ``None`` means "this datasource has no protected-instance meter" (Azure Disk Backup) or
    "the rate card does not price it", which the caller must surface rather than treat as 0.
    """
    meters = card.get("instance_meters") or {}
    for candidate in (datasource_type, backup_management_type):
        token = str(candidate or "").strip().lower()
        if not token:
            continue
        if token in NO_INSTANCE_METER:
            return None, "no_instance_meter"
        key = DATASOURCE_TO_METER.get(token)
        if key and key in meters:
            return float(meters[key]), key
        if token in meters:
            return float(meters[token]), token
    return None, ""


def storage_rate(card: dict[str, Any], redundancy: str, *, files: bool = False) -> float | None:
    """Per-GB/month vault storage rate for a redundancy setting."""
    token = str(redundancy or "").strip().lower().replace("-", "").replace(" ", "").replace("_", "")
    if token.startswith("geo"):
        key = "grs"
    elif token.startswith("zone"):
        key = "zrs"
    elif token.startswith("local"):
        key = "lrs"
    elif token in ("ragrs", "readaccessgeoredundant"):
        key = "ra_grs"
    else:
        key = "lrs"
    table = (card.get("files_storage_gb_month") if files else None) or card.get("storage_gb_month") or {}
    value = table.get(key)
    return float(value) if value is not None else None

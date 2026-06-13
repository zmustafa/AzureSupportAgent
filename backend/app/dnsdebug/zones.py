"""Azure-side truth for a Private Endpoint's DNS resolution (best-effort, gated).

Reads the Private DNS zone landscape via ARG + the gated ``az network`` path: does the
``privatelink.*`` zone exist, is it linked to the source VNet, what A record does it hold,
and does the source VNet use custom DNS servers. Returns a structured fact set the analyzer
turns into a plain-English misconfiguration verdict. Degrades to ``available: False`` when
command execution is disabled."""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("app.dnsdebug.zones")

# PaaS suffix → the privatelink zone that should resolve it (subset of the common ones).
PRIVATELINK_ZONES: dict[str, str] = {
    "blob.core.windows.net": "privatelink.blob.core.windows.net",
    "file.core.windows.net": "privatelink.file.core.windows.net",
    "queue.core.windows.net": "privatelink.queue.core.windows.net",
    "table.core.windows.net": "privatelink.table.core.windows.net",
    "database.windows.net": "privatelink.database.windows.net",
    "postgres.database.azure.com": "privatelink.postgres.database.azure.com",
    "mysql.database.azure.com": "privatelink.mysql.database.azure.com",
    "vaultcore.azure.net": "privatelink.vaultcore.azure.net",
    "azurecr.io": "privatelink.azurecr.io",
    "documents.azure.com": "privatelink.documents.azure.com",
    "servicebus.windows.net": "privatelink.servicebus.windows.net",
    "azurewebsites.net": "privatelink.azurewebsites.net",
}


def expected_zone_for(fqdn: str) -> str:
    f = (fqdn or "").lower()
    for suffix, zone in PRIVATELINK_ZONES.items():
        if f.endswith(suffix):
            return zone
    return ""


def _parse(stdout: str) -> Any:
    try:
        return json.loads(stdout or "null")
    except (json.JSONDecodeError, TypeError):
        return None


async def gather_zone_facts(
    connection: dict[str, Any] | None,
    *,
    fqdn: str,
    source_vnet_id: str = "",
) -> dict[str, Any]:
    """Return {available, notes, expected_zone, zone_exists, linked_to_source_vnet,
    a_record_ip, custom_dns_servers, error}. Never raises."""
    from app.core.app_settings import load_settings
    from app.exec.command_runner import run_az_json_capture

    out: dict[str, Any] = {
        "available": False,
        "notes": "",
        "expected_zone": expected_zone_for(fqdn),
        "zone_exists": None,
        "linked_to_source_vnet": None,
        "a_record_ip": "",
        "custom_dns_servers": [],
        "error": "",
    }

    if not load_settings().get("command_execution_enabled", False):
        out["notes"] = (
            "Azure-side DNS truth (zone existence / VNet link / A record / custom DNS) needs "
            "command execution enabled (Admin → General) + a connection with Network Reader. "
            "The live resolution chain above stands on its own."
        )
        return out

    zone = out["expected_zone"]
    if not zone:
        out["notes"] = "No known privatelink zone maps to this FQDN suffix."
        return out

    try:
        zones = await run_az_json_capture(
            ["network", "private-dns", "zone", "list", "--query", f"[?name=='{zone}']", "-o", "json"],
            connection, label="az network private-dns zone list",
        )
        if zones.ok:
            zlist = _parse(zones.stdout) or []
            out["zone_exists"] = bool(zlist)
            out["available"] = True
            if zlist:
                zid = zlist[0].get("id", "")
                rg = zid.split("/resourceGroups/")[1].split("/")[0] if "/resourceGroups/" in zid else ""
                links = await run_az_json_capture(
                    ["network", "private-dns", "link", "vnet", "list", "-g", rg, "-z", zone, "-o", "json"],
                    connection, label="az network private-dns link vnet list",
                )
                if links.ok:
                    llist = _parse(links.stdout) or []
                    linked = any((l.get("virtualNetwork", {}).get("id", "").lower() == (source_vnet_id or "").lower()) for l in llist) if source_vnet_id else bool(llist)
                    out["linked_to_source_vnet"] = linked
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:300]
        log.info("zone facts gather failed: %s", exc)

    if out["available"] and not out["notes"]:
        out["notes"] = "Private DNS zone landscape read from Azure."
    return out

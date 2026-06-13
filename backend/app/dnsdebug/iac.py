"""Bicep remediation for a detected private-DNS misconfiguration.

Emits a parameterized ``Microsoft.Network/privateDnsZones/virtualNetworkLinks`` (and an A
record when the misconfig is a stale/missing record), to link the missing zone to the
source VNet. Read-only download artifact; never applied by the app."""
from __future__ import annotations

from typing import Any


def generate_iac(*, zone: str, vnet_id: str = "", record_name: str = "", record_ip: str = "", misconfig: str = "") -> str:
    zone = zone or "privatelink.example.core.windows.net"
    lines = [
        "// Bicep generated from a Private Endpoint resolution diagnosis — review params, then deploy.",
        "// Read-only artifact; this app does not apply changes.",
        "",
        "@description('The privatelink Private DNS zone name.')",
        f"param zoneName string = '{zone}'",
        "@description('Resource id of the VNet to link the zone to.')",
        f"param vnetId string = '{vnet_id or '<source-vnet-resource-id>'}'",
        "",
        "resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {",
        "  name: zoneName",
        "}",
        "",
        "resource vnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {",
        "  parent: zone",
        "  name: '${zoneName}-link'",
        "  location: 'global'",
        "  properties: {",
        "    registrationEnabled: false",
        "    virtualNetwork: { id: vnetId }",
        "  }",
        "}",
    ]
    if (misconfig or "").startswith("stale_record") or (record_ip and record_name):
        lines += [
            "",
            "@description('Private IP of the private endpoint NIC.')",
            f"param recordIp string = '{record_ip or '<pe-nic-private-ip>'}'",
            "resource aRecord 'Microsoft.Network/privateDnsZones/A@2024-06-01' = {",
            "  parent: zone",
            f"  name: '{record_name or '<record-name>'}'",
            "  properties: {",
            "    ttl: 3600",
            "    aRecords: [ { ipv4Address: recordIp } ]",
            "  }",
            "}",
        ]
    return "\n".join(lines)


def generate_for_run(run: dict[str, Any]) -> str:
    z = run.get("zone_facts", {}) or {}
    primary = next((s for s in run.get("sources", []) if s.get("classification") == "public"), None) or (run.get("sources") or [{}])[0]
    return generate_iac(
        zone=z.get("expected_zone", ""),
        vnet_id=run.get("source_vnet_id", ""),
        record_name=(run.get("fqdn", "").split(".")[0]),
        record_ip=run.get("pe_private_ip", ""),
        misconfig=run.get("misconfig_kind", ""),
    )

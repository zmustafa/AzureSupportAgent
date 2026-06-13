"""Azure control-plane corroboration for a reachability probe (best-effort).

Adds the *why* behind a verdict: effective routes, effective NSG rules (which rule matched),
VNet peering state, and private-DNS / firewall presence. Uses the gated ``az network`` path
(run_az_json_capture) when command execution is enabled; otherwise returns a graceful
"control-plane evidence unavailable" note so the probe result still stands on its own."""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("app.netcheck.evidence")


def _parse(stdout: str) -> Any:
    try:
        return json.loads(stdout or "null")
    except (json.JSONDecodeError, TypeError):
        return None


async def gather_evidence(
    connection: dict[str, Any] | None,
    *,
    source_nic_id: str = "",
    target_ip: str = "",
    target_resource_id: str = "",
) -> dict[str, Any]:
    """Return {available, notes, effective_routes, nsg_rules, peerings, error}.

    Never raises; degrades to ``available: False`` with an explanatory note when the
    control-plane reads can't run (command execution disabled, no NIC id, az missing)."""
    from app.core.app_settings import load_settings
    from app.exec.command_runner import run_az_json_capture

    out: dict[str, Any] = {
        "available": False,
        "notes": "",
        "effective_routes": [],
        "nsg_rules": [],
        "peerings": [],
        "error": "",
    }

    if not load_settings().get("command_execution_enabled", False):
        out["notes"] = (
            "Azure control-plane evidence (effective routes / NSG rules / peering) needs "
            "command execution enabled (Admin → General) and a connection with Network "
            "Reader. The live probe result above stands on its own."
        )
        return out

    if not source_nic_id:
        out["notes"] = "No source NIC id resolved — skipping effective-route/NSG evidence."
        return out

    try:
        routes = await run_az_json_capture(
            ["network", "nic", "show-effective-route-table", "--ids", source_nic_id, "-o", "json"],
            connection, label="az network nic show-effective-route-table",
        )
        if routes.ok:
            data = _parse(routes.stdout)
            rows = (data or {}).get("value", data) if isinstance(data, (dict, list)) else []
            out["effective_routes"] = rows if isinstance(rows, list) else []
            out["available"] = True

        nsg = await run_az_json_capture(
            ["network", "nic", "list-effective-nsg", "--ids", source_nic_id, "-o", "json"],
            connection, label="az network nic list-effective-nsg",
        )
        if nsg.ok:
            data = _parse(nsg.stdout)
            rules: list[dict[str, Any]] = []
            groups = (data or {}).get("value", []) if isinstance(data, dict) else []
            for g in groups if isinstance(groups, list) else []:
                assoc = g.get("networkSecurityGroup", {}) or {}
                nsg_name = assoc.get("id", "").split("/")[-1] if assoc.get("id") else ""
                for r in (g.get("effectiveSecurityRules") or []):
                    rules.append(
                        {
                            "nsg": nsg_name,
                            "name": r.get("name", ""),
                            "access": r.get("access", ""),
                            "direction": r.get("direction", ""),
                            "protocol": r.get("protocol", ""),
                            "destinationPortRange": r.get("destinationPortRange", ""),
                            "priority": r.get("priority"),
                        }
                    )
            out["nsg_rules"] = rules
            out["available"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:300]
        log.info("evidence gather failed: %s", exc)

    if out["available"] and not out["notes"]:
        out["notes"] = "Effective routes + NSG rules read from the source NIC."
    return out


def matched_deny_rule(nsg_rules: list[dict[str, Any]], port: int) -> dict[str, Any] | None:
    """Return the highest-priority outbound Deny rule that could block the given port."""
    candidates = [
        r for r in nsg_rules
        if (r.get("access", "").lower() == "deny" and r.get("direction", "").lower() == "outbound")
    ]
    def _matches_port(r: dict[str, Any]) -> bool:
        rng = str(r.get("destinationPortRange", ""))
        if rng in ("*", ""):
            return True
        if "-" in rng:
            try:
                lo, hi = (int(x) for x in rng.split("-", 1))
                return lo <= int(port) <= hi
            except ValueError:
                return False
        try:
            return int(rng) == int(port)
        except ValueError:
            return False
    candidates = [r for r in candidates if _matches_port(r)]
    candidates.sort(key=lambda r: r.get("priority") or 99999)
    return candidates[0] if candidates else None

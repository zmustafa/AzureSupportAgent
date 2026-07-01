"""Per-connection capability & blind-spot matrix.

Translates each Azure connection's auth method + stored credential/token state into a
matrix of what the platform can ACTUALLY do with it, and — more importantly — what it
silently *cannot*. The headline blind spot: a pasted ARM token (``az_cli_token``) reads
Azure Resource Manager fine but cannot reach Microsoft Graph, Log Analytics, or the Key
Vault data plane unless extra tokens are pasted. An investigation that quietly ran on a
half-blind connection (no directory names, no logs, no secret-expiry) looks complete but
isn't — this surfaces that before the user trusts the answer.

Two modes:
  * static (default)  — pure inference from ``auth_method`` + token presence/expiry +
                        configured Log Analytics workspace + the read-only flag. No Azure
                        calls, instant, safe to render on every page visit.
  * live (opt-in)     — additionally verifies ARM and Microsoft Graph token acquisition
                        using the existing credential helpers and counts visible
                        subscriptions, so "full" is proven rather than assumed.

The output never includes secrets — only non-sensitive connection metadata and the
derived capability cells.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.azure.credentials import _token_expired, get_arm_token, get_graph_token
from app.core.azure_connections import list_connections

# Capability cell states (worst → best). The frontend colours these.
FULL = "full"          # works (subject to the identity's RBAC)
DEGRADED = "degraded"  # works but limited / unverifiable / short-lived
BLIND = "blind"        # cannot do this at all with the current credentials
DISABLED = "disabled"  # connection (or this capability) is turned off

# Column metadata — the capabilities we score every connection against.
CAPABILITIES: list[dict[str, str]] = [
    {"key": "arm_read", "label": "ARM control plane",
     "desc": "List and read Azure Resource Manager resources, subscriptions and management groups."},
    {"key": "resource_graph", "label": "Resource Graph",
     "desc": "Run Azure Resource Graph (ARG) queries for inventory and change history."},
    {"key": "graph_directory", "label": "Microsoft Graph token",
     "desc": "Acquire a raw Microsoft Graph access token (the app's own Graph client and the "
             "Entra-policy assessment controls). A managed identity with Directory.Read.All can do this."},
    {"key": "entra_directory", "label": "Entra directory (PIM / app regs)",
     "desc": "Run the EntraID directory features — PIM/JIT review, app registrations and conditional "
             "access — via the bundled Microsoft Graph MCP server, which needs a service-principal "
             "client secret or certificate (managed identity / pasted token cannot drive it)."},
    {"key": "log_analytics", "label": "Log Analytics",
     "desc": "Query Log Analytics / App Insights logs (KQL) on the data plane."},
    {"key": "key_vault_data", "label": "Key Vault data",
     "desc": "Read Key Vault secret / certificate metadata on the data plane (expiry checks)."},
    {"key": "writes", "label": "Gated writes",
     "desc": "Execute approved mutating operations (remediation, tagging, deployments)."},
]

_CAP_KEYS = [c["key"] for c in CAPABILITIES]
_WEIGHT = {FULL: 1.0, DEGRADED: 0.5, BLIND: 0.0, DISABLED: 0.0}

# Auth methods that hold (or can mint) a full identity — both ARM and Microsoft Graph
# tokens, plus data-plane tokens — as opposed to the pasted-token method.
_FULL_IDENTITY = ("service_principal", "service_principal_cert", "default_chain")


def _cell(status: str, reason: str, remediation: str = "") -> dict[str, str]:
    return {"status": status, "reason": reason, "remediation": remediation}


def _entra_directory_cell(conn: dict[str, Any]) -> dict[str, str]:
    """Capability for the EntraID directory features — PIM/JIT, app registrations, conditional
    access — which all run through the bundled Microsoft Graph MCP server.

    Driven by the EXACT gate those features enforce (``entra_graph_config_error``) so the matrix
    and the Identity / App-Registrations pages can never disagree. That server authenticates only
    with an explicit service-principal secret or certificate; a managed identity, a pasted ARM
    token and a secret-less service principal are all blind here — even when they can still mint a
    raw Graph token (the separate ``graph_directory`` column)."""
    from app.mcp.client import entra_graph_config_error  # local import: avoid an import cycle

    err = entra_graph_config_error(conn)
    if not err:
        return _cell(FULL, "A service-principal secret / certificate can drive the EntraID MCP "
                           "server (PIM, app registrations, conditional access).")
    return _cell(BLIND, err,
                 "Add a service-principal connection (client id + secret or certificate) granted "
                 "Directory.Read.All / Application.Read.All.")


def _static_caps(conn: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Infer the capability cells for one connection without any Azure calls."""
    method = conn.get("auth_method", "")
    read_only = bool(conn.get("read_only", True))
    has_la = bool(conn.get("log_analytics_workspace_id"))
    caps: dict[str, dict[str, str]] = {}

    if conn.get("disabled"):
        why = "Connection is disabled."
        return {k: _cell(DISABLED, why, "Enable the connection to use it.") for k in _CAP_KEYS}

    if method in _FULL_IDENTITY:
        mi_note = (
            " Depends on the host managed identity's role assignments."
            if method == "default_chain" else ""
        )
        caps["arm_read"] = _cell(FULL, f"{_method_label(method)} can mint an ARM token.{mi_note}")
        caps["resource_graph"] = _cell(FULL, "Resource Graph uses the ARM token.")
        caps["graph_directory"] = _cell(
            FULL,
            f"{_method_label(method)} can mint a Microsoft Graph token."
            + (" Requires Directory.Read.All on the identity." if method == "default_chain" else ""),
        )
        caps["entra_directory"] = _entra_directory_cell(conn)
        caps["log_analytics"] = (
            _cell(FULL, "Can mint a Log Analytics token (needs Log Analytics Reader on the workspace).")
            if has_la
            else _cell(
                DEGRADED,
                "No Log Analytics workspace is configured on this connection.",
                "Set the connection's Log Analytics workspace id to enable KQL log queries.",
            )
        )
        caps["key_vault_data"] = _cell(
            FULL, "Can mint a Key Vault data-plane token (subject to each vault's access policy / RBAC)."
        )
        caps["writes"] = (
            _cell(DISABLED, "Connection is marked read-only.",
                  "Turn off read-only on the connection to allow gated writes.")
            if read_only
            else _cell(FULL, "Gated by approval unless auto-execute is enabled.")
        )
        return caps

    if method == "az_cli_token":
        arm_ok = bool(conn.get("access_token")) and not _token_expired(conn.get("token_expires_on", ""))
        if not conn.get("access_token"):
            arm_cell = _cell(BLIND, "No pasted ARM token stored.",
                             "Paste a token: az account get-access-token.")
        elif not arm_ok:
            arm_cell = _cell(BLIND, "The pasted ARM token has expired.",
                             "Paste a fresh token: az account get-access-token.")
        else:
            arm_cell = _cell(FULL, "Pasted ARM token is present and unexpired.")
        caps["arm_read"] = arm_cell
        caps["resource_graph"] = dict(arm_cell) if arm_ok else _cell(
            arm_cell["status"], arm_cell["reason"], arm_cell["remediation"])

        gtok = conn.get("graph_access_token", "")
        if gtok and not _token_expired(conn.get("graph_token_expires_on", "")):
            caps["graph_directory"] = _cell(FULL, "A separate Microsoft Graph token was pasted and is valid.")
        elif gtok:
            caps["graph_directory"] = _cell(
                BLIND, "The pasted Microsoft Graph token has expired.",
                "Paste a fresh Graph token: az account get-access-token --resource-type ms-graph.")
        else:
            caps["graph_directory"] = _cell(
                BLIND,
                "A pasted ARM token cannot call Microsoft Graph, so directory names, app "
                "registrations, PIM and conditional access are invisible.",
                "Paste a Graph token (az account get-access-token --resource-type ms-graph) "
                "or use a service-principal / managed-identity connection.")

        caps["entra_directory"] = _entra_directory_cell(conn)
        caps["log_analytics"] = _cell(
            BLIND,
            "A pasted ARM token cannot query the Log Analytics data plane — no log token is available.",
            "Use a service-principal or managed-identity connection for KQL log queries.")
        caps["key_vault_data"] = _cell(
            BLIND,
            "A pasted ARM token cannot read the Key Vault data plane, so secret / certificate "
            "expiry can't be checked.",
            "Use a service-principal or managed-identity connection for Key Vault data-plane reads.")
        caps["writes"] = (
            _cell(DISABLED, "Connection is marked read-only.",
                  "Turn off read-only on the connection to allow gated writes.")
            if read_only
            else _cell(
                DEGRADED,
                "A pasted token can write only within its granted scope and expires within ~1 hour.",
                "Use a service-principal connection for durable, gated writes.")
        )
        return caps

    # Unknown / future auth method — be honest rather than guess.
    return {k: _cell(DEGRADED, f"Unrecognized auth method '{method}'.") for k in _CAP_KEYS}


def _method_label(method: str) -> str:
    return {
        "service_principal": "A service principal",
        "service_principal_cert": "A service-principal certificate",
        "default_chain": "The host identity",
        "az_cli_token": "A pasted token",
    }.get(method, method or "This connection")


async def _live_overlay(conn: dict[str, Any], caps: dict[str, dict[str, str]]) -> None:
    """Verify ARM + Microsoft Graph reachability for real and overwrite those cells.

    Best-effort and bounded: a probe failure downgrades only its own cell. Log Analytics
    and Key Vault data-plane probes are intentionally NOT performed live (heavier, vault-
    by-vault) — their static inference already captures the structural blind spot.
    """
    try:
        arm_tok, arm_err = await asyncio.wait_for(get_arm_token(conn), timeout=20)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        arm_tok, arm_err = None, "ARM token request timed out."
    if arm_tok:
        sub_note = ""
        try:
            from app.azure.arm import list_subscriptions

            subs, sub_err = await asyncio.wait_for(list_subscriptions(arm_tok), timeout=20)
            if not sub_err:
                sub_note = f" — {len(subs)} subscription(s) visible"
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            sub_note = ""
        caps["arm_read"] = _cell(FULL, f"Verified live{sub_note}.")
        if caps.get("resource_graph", {}).get("status") != DISABLED:
            caps["resource_graph"] = _cell(FULL, "Verified live (ARM token acquired).")
    else:
        msg = (arm_err or "Could not acquire an ARM token.")[:300]
        caps["arm_read"] = _cell(BLIND, msg, caps.get("arm_read", {}).get("remediation", ""))
        caps["resource_graph"] = _cell(BLIND, msg)

    try:
        graph_tok, graph_err = await asyncio.wait_for(get_graph_token(conn), timeout=20)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        graph_tok, graph_err = None, "Microsoft Graph token request timed out."
    if graph_tok:
        caps["graph_directory"] = _cell(FULL, "Verified live (Microsoft Graph token acquired).")
    else:
        caps["graph_directory"] = _cell(
            BLIND, (graph_err or "Could not acquire a Microsoft Graph token.")[:300],
            caps.get("graph_directory", {}).get("remediation", ""))


def _score(caps: dict[str, dict[str, str]]) -> int:
    if not caps:
        return 0
    total = sum(_WEIGHT.get(c.get("status", BLIND), 0.0) for c in caps.values())
    return round(100 * total / len(caps))


def _public_conn(conn: dict[str, Any]) -> dict[str, Any]:
    """Non-secret connection metadata safe to return to the client."""
    return {
        "id": conn.get("id", ""),
        "display_name": conn.get("display_name") or conn.get("id", ""),
        "auth_method": conn.get("auth_method", ""),
        "tenant_id": conn.get("tenant_id", ""),
        "default_subscription": conn.get("default_subscription", ""),
        "is_default": bool(conn.get("is_default")),
        "disabled": bool(conn.get("disabled")),
        "read_only": bool(conn.get("read_only", True)),
        "status": conn.get("status", "unknown"),
        "status_detail": conn.get("status_detail", ""),
        "last_tested": conn.get("last_tested", ""),
        "log_analytics_workspace_id": conn.get("log_analytics_workspace_id", ""),
        "has_graph_token": bool(conn.get("graph_access_token")),
    }


async def build_matrix(*, live: bool = False) -> dict[str, Any]:
    """Build the capability matrix across every configured connection."""
    conns = list_connections()
    rows: list[dict[str, Any]] = []
    for conn in conns:
        caps = _static_caps(conn)
        if live and not conn.get("disabled"):
            await _live_overlay(conn, caps)
        blind = [k for k, c in caps.items() if c.get("status") == BLIND]
        rows.append({
            **_public_conn(conn),
            "caps": caps,
            "blind_spots": blind,
            "score": _score(caps),
        })

    with_blind = sum(1 for r in rows if r["blind_spots"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live": bool(live),
        "capabilities": CAPABILITIES,
        "connections": rows,
        "summary": {
            "connections": len(rows),
            "with_blind_spots": with_blind,
            "fully_capable": sum(1 for r in rows if r["score"] == 100),
        },
    }

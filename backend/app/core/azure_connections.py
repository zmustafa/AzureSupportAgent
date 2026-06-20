"""Runtime registry of Azure connections (multi-tenant, admin-managed).

Each connection describes how to authenticate to ONE Azure tenant and how the agent
may act within it. Persisted to a small JSON file under backend/.data (consistent
with llm_config / app_settings) so admins can add/edit connections from the dashboard
WITHOUT a restart. Secrets are encrypted at rest via app.core.crypto.

Auth methods
------------
- ``service_principal``      : Entra app registration with a client SECRET. Works
                               cross-tenant and drives the Azure MCP server fully
                               (read + gated write). RECOMMENDED for enterprise.
- ``service_principal_cert`` : Same, but a PEM CERTIFICATE instead of a secret.
- ``azure_cli``              : Use the host's ``az login`` session for a specific
                               tenant. Sign in ONCE on the server host; the Azure CLI
                               keeps the session refreshed (~90 days, rolling) so this
                               is effectively always-on with no token pasting.
- ``default_chain``          : Use the host DefaultAzureCredential (managed identity in
                               cloud, or the machine's az login). Single-tenant.
- ``az_cli_token``           : Paste the JSON from ``az account get-access-token`` run
                               on your own machine. Short-lived (~1h, no refresh token);
                               best for quick/headless checks. Prefer ``azure_cli``.

The shape mirrors the LLM provider registry: a flat dict keyed by connection id.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.crypto import decrypt, encrypt

_PATH = Path(__file__).resolve().parents[2] / ".data" / "azure_connections.json"

AUTH_METHODS = (
    "service_principal",
    "service_principal_cert",
    "default_chain",
    "az_cli_token",
)

# Fields that hold secrets and must be encrypted at rest.
_SECRET_FIELDS = ("client_secret", "certificate_pem", "access_token", "refresh_token", "graph_access_token")

_DEFAULTS: dict[str, Any] = {
    "display_name": "",
    "tenant_id": "",
    "auth_method": "service_principal",
    "default_subscription": "",
    # Log Analytics workspace (GUID) for `az monitor log-analytics query` widgets.
    "log_analytics_workspace_id": "",
    # Governance, per connection (overrides the global app settings for this tenant).
    "read_only": True,
    "auto_execute_writes": False,
    "disabled": False,
    "is_default": False,
    # Credentials (encrypted when persisted).
    "client_id": "",
    "client_secret": "",
    "certificate_pem": "",
    "access_token": "",
    "refresh_token": "",
    "token_expires_on": "",
    # Optional Microsoft Graph token for the pasted-token (az_cli_token) method. An ARM token
    # can't be used against Graph, so principal/group/Entra name resolution needs a separate
    # Graph token (az account get-access-token --resource-type ms-graph).
    "graph_access_token": "",
    "graph_token_expires_on": "",
    # Health.
    "status": "unknown",  # unknown | ok | error
    "status_detail": "",
    "last_tested": "",
    "created_at": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"connections": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge_defaults(conn: dict[str, Any]) -> dict[str, Any]:
    merged = dict(_DEFAULTS)
    merged.update(conn or {})
    return merged


def list_connections() -> list[dict[str, Any]]:
    """All connections with secrets DECRYPTED (for internal/server use only)."""
    data = _read()
    out: list[dict[str, Any]] = []
    for cid, conn in data.get("connections", {}).items():
        merged = _merge_defaults(conn)
        merged["id"] = cid
        for f in _SECRET_FIELDS:
            merged[f] = decrypt(merged.get(f, ""))
        out.append(merged)
    out.sort(key=lambda c: (not c.get("is_default"), c.get("display_name", "").lower()))
    return out


def get_connection(connection_id: str) -> dict[str, Any] | None:
    if not connection_id:
        return None
    for conn in list_connections():
        if conn["id"] == connection_id:
            return conn
    return None


def get_default_connection() -> dict[str, Any] | None:
    conns = [c for c in list_connections() if not c.get("disabled")]
    if not conns:
        return None
    for c in conns:
        if c.get("is_default"):
            return c
    return conns[0]


def resolve_connection(connection_id: str | None) -> dict[str, Any] | None:
    """Pick the connection for a turn: explicit id, else the default."""
    if connection_id:
        conn = get_connection(connection_id)
        if conn and not conn.get("disabled"):
            return conn
    return get_default_connection()


def connection_for_workload(workload: dict[str, Any] | None) -> dict[str, Any] | None:
    """The Azure connection to use when operating on a WORKLOAD scope.

    A workload is scanned with ITS OWN connection (``connection_id``), falling back to the
    default only when it has none. Using the default for a workload whose subscription is
    reachable only via a non-default connection silently returns zero resources — the bug
    this helper prevents across every workload-scoped feature (coverage, radar, teleintel,
    evidence, performance profiler, …)."""
    return resolve_connection((workload or {}).get("connection_id") or None)


def upsert_connection(conn: dict[str, Any]) -> dict[str, Any]:
    """Create or update a connection. Secrets are encrypted before write. An empty
    secret field on update means 'keep the existing value' (so the UI never has to
    round-trip the plaintext)."""
    data = _read()
    connections = data.setdefault("connections", {})
    cid = conn.get("id") or str(uuid.uuid4())
    existing = connections.get(cid, {})

    merged = _merge_defaults(existing)
    # Apply only known fields from the incoming payload.
    for key in _DEFAULTS:
        if key in conn and conn[key] is not None:
            merged[key] = conn[key]

    # Encrypt secrets; blank on update keeps the stored (encrypted) value.
    for f in _SECRET_FIELDS:
        incoming = conn.get(f)
        if incoming:
            merged[f] = encrypt(incoming)
        else:
            merged[f] = existing.get(f, "")  # keep prior encrypted value (or empty)

    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    connections[cid] = merged

    # Enforce a single default.
    if merged.get("is_default"):
        for other_id, other in connections.items():
            if other_id != cid:
                other["is_default"] = False

    _write(data)
    result = get_connection(cid)
    assert result is not None
    return result


def delete_connection(connection_id: str) -> bool:
    data = _read()
    connections = data.get("connections", {})
    if connection_id in connections:
        del connections[connection_id]
        _write(data)
        return True
    return False


def set_default(connection_id: str) -> bool:
    data = _read()
    connections = data.get("connections", {})
    if connection_id not in connections:
        return False
    for cid, conn in connections.items():
        conn["is_default"] = cid == connection_id
    _write(data)
    return True


def update_status(
    connection_id: str, status: str, detail: str = "", *, tested: bool = True
) -> None:
    data = _read()
    connections = data.get("connections", {})
    if connection_id not in connections:
        return
    connections[connection_id]["status"] = status
    connections[connection_id]["status_detail"] = detail
    if tested:
        connections[connection_id]["last_tested"] = _now()
    _write(data)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "••••"
    return value[:4] + "…" + value[-4:]


def public_connection(conn: dict[str, Any]) -> dict[str, Any]:
    """A single connection safe for the UI: secrets masked, never raw."""
    return {
        "id": conn["id"],
        "display_name": conn.get("display_name", ""),
        "tenant_id": conn.get("tenant_id", ""),
        "auth_method": conn.get("auth_method", ""),
        "default_subscription": conn.get("default_subscription", ""),
        "log_analytics_workspace_id": conn.get("log_analytics_workspace_id", ""),
        "read_only": bool(conn.get("read_only", True)),
        "auto_execute_writes": bool(conn.get("auto_execute_writes", False)),
        "disabled": bool(conn.get("disabled", False)),
        "is_default": bool(conn.get("is_default", False)),
        "status": conn.get("status", "unknown"),
        "status_detail": conn.get("status_detail", ""),
        "last_tested": conn.get("last_tested", ""),
        "token_expires_on": conn.get("token_expires_on", ""),
        "client_id": conn.get("client_id", ""),
        "has_client_secret": bool(conn.get("client_secret")),
        "has_certificate": bool(conn.get("certificate_pem")),
        "has_access_token": bool(conn.get("access_token")),
        "client_secret_hint": _mask(conn.get("client_secret", "")),
        "access_token_hint": _mask(conn.get("access_token", "")),
        "has_graph_access_token": bool(conn.get("graph_access_token")),
        "graph_access_token_hint": _mask(conn.get("graph_access_token", "")),
        "graph_token_expires_on": conn.get("graph_token_expires_on", ""),
        "created_at": conn.get("created_at", ""),
        "updated_at": conn.get("updated_at", ""),
    }


def public_connections() -> list[dict[str, Any]]:
    return [public_connection(c) for c in list_connections()]

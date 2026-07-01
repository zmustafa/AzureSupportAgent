"""Unit tests for the connection capability & blind-spot matrix (static inference)."""
from __future__ import annotations

from app.capability import probe


def _matrix(connections, monkeypatch):
    monkeypatch.setattr(probe, "list_connections", lambda: connections)
    import asyncio

    return asyncio.run(probe.build_matrix(live=False))


def test_service_principal_is_fully_capable(monkeypatch):
    conns = [{
        "id": "sp1", "display_name": "Prod SP", "auth_method": "service_principal",
        "tenant_id": "t1", "client_id": "c", "client_secret": "s",
        "log_analytics_workspace_id": "ws-guid", "read_only": False,
    }]
    m = _matrix(conns, monkeypatch)
    row = m["connections"][0]
    assert row["score"] == 100
    assert row["blind_spots"] == []
    assert row["caps"]["graph_directory"]["status"] == probe.FULL
    assert row["caps"]["entra_directory"]["status"] == probe.FULL  # SP has a secret → EntraID MCP can run
    assert row["caps"]["log_analytics"]["status"] == probe.FULL
    assert row["caps"]["writes"]["status"] == probe.FULL
    # Secrets must never leak into the response.
    assert "client_secret" not in row and "access_token" not in row


def test_pasted_token_is_blind_on_data_planes(monkeypatch):
    conns = [{
        "id": "cli1", "display_name": "Pasted", "auth_method": "az_cli_token",
        "tenant_id": "t1", "access_token": "tok", "token_expires_on": "9999999999",
        "read_only": True,
    }]
    m = _matrix(conns, monkeypatch)
    row = m["connections"][0]
    caps = row["caps"]
    assert caps["arm_read"]["status"] == probe.FULL
    assert caps["resource_graph"]["status"] == probe.FULL
    # The headline blind spots: Graph, Log Analytics, Key Vault data plane.
    assert caps["graph_directory"]["status"] == probe.BLIND
    assert caps["entra_directory"]["status"] == probe.BLIND  # pasted token can't drive the EntraID MCP
    assert caps["log_analytics"]["status"] == probe.BLIND
    assert caps["key_vault_data"]["status"] == probe.BLIND
    assert caps["writes"]["status"] == probe.DISABLED  # read-only
    assert set(row["blind_spots"]) == {"graph_directory", "entra_directory", "log_analytics", "key_vault_data"}
    assert m["summary"]["with_blind_spots"] == 1


def test_pasted_token_with_graph_token_resolves_graph(monkeypatch):
    conns = [{
        "id": "cli2", "auth_method": "az_cli_token", "tenant_id": "t1",
        "access_token": "tok", "token_expires_on": "9999999999",
        "graph_access_token": "gtok", "graph_token_expires_on": "9999999999",
    }]
    m = _matrix(conns, monkeypatch)
    caps = m["connections"][0]["caps"]
    assert caps["graph_directory"]["status"] == probe.FULL
    # A pasted Graph token resolves raw Graph, but still can't drive the EntraID MCP server.
    assert caps["entra_directory"]["status"] == probe.BLIND
    # Data-plane blind spots remain.
    assert caps["log_analytics"]["status"] == probe.BLIND


def test_expired_pasted_token_is_blind_on_arm(monkeypatch):
    conns = [{
        "id": "cli3", "auth_method": "az_cli_token", "tenant_id": "t1",
        "access_token": "tok", "token_expires_on": "1",  # epoch 1 = long expired
    }]
    m = _matrix(conns, monkeypatch)
    caps = m["connections"][0]["caps"]
    assert caps["arm_read"]["status"] == probe.BLIND
    assert "expired" in caps["arm_read"]["reason"].lower()


def test_disabled_connection_is_all_disabled(monkeypatch):
    conns = [{"id": "d1", "auth_method": "service_principal", "disabled": True}]
    m = _matrix(conns, monkeypatch)
    row = m["connections"][0]
    assert all(c["status"] == probe.DISABLED for c in row["caps"].values())
    assert row["score"] == 0


def test_managed_identity_can_get_graph_token_but_is_blind_on_entra_mcp(monkeypatch):
    """A host / managed-identity connection can mint a raw Graph token (graph_directory full) but
    cannot drive the EntraID MCP server (PIM / app regs / conditional access) — that needs an
    explicit service-principal secret or certificate. This is exactly the blind spot the PIM and
    App-Registrations pages report, so it must surface in the matrix too."""
    from app.mcp.client import entra_graph_config_error

    conns = [{
        "id": "mi1", "display_name": "LU", "auth_method": "default_chain",
        "tenant_id": "t1", "log_analytics_workspace_id": "ws", "read_only": True,
    }]
    m = _matrix(conns, monkeypatch)
    row = m["connections"][0]
    caps = row["caps"]
    assert caps["graph_directory"]["status"] == probe.FULL   # raw Graph token path works for MI
    assert caps["entra_directory"]["status"] == probe.BLIND  # EntraID MCP path does not
    assert "entra_directory" in row["blind_spots"]
    assert row["score"] < 100
    # The matrix cell must say EXACTLY what the PIM / App-Registrations pages say (single gate).
    assert caps["entra_directory"]["reason"] == entra_graph_config_error(conns[0])


def test_service_principal_without_secret_is_blind_on_entra_mcp(monkeypatch):
    """A service principal with a client id but NO stored secret/certificate can't drive the
    EntraID MCP server either — the matrix must flag it even though the auth method looks
    privileged (and a raw Graph token still isn't available without the secret)."""
    conns = [{
        "id": "sp-nosecret", "display_name": "SP no secret", "auth_method": "service_principal",
        "tenant_id": "t1", "client_id": "c",  # no client_secret / certificate_pem
        "read_only": True,
    }]
    m = _matrix(conns, monkeypatch)
    row = m["connections"][0]
    assert row["caps"]["entra_directory"]["status"] == probe.BLIND
    assert "entra_directory" in row["blind_spots"]

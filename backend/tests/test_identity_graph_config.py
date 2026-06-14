"""Tests for the EntraID (Microsoft Graph) MCP precondition check + exception-group
unwrapping that make the Identity features fail with a clear, actionable message instead of
the opaque "unhandled errors in a TaskGroup (1 sub-exception)" when the selected Azure
connection can't authenticate to Microsoft Graph (managed identity / pasted token / host
az-login — no service-principal client secret or certificate).
"""
from __future__ import annotations

import asyncio

import pytest

from app.identity import collector
from app.mcp import client as mcp_client


# --------------------------------------------------------------------- unwrap_exc_message
def test_unwrap_flattens_taskgroup_to_real_cause():
    inner = RuntimeError("Missing required credentials: client_id, client_secret")
    eg = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    assert mcp_client.unwrap_exc_message(eg) == (
        "Missing required credentials: client_id, client_secret"
    )


def test_unwrap_recurses_nested_groups_and_dedupes():
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("boom")])])
    assert mcp_client.unwrap_exc_message(nested) == "boom"
    dupes = ExceptionGroup("g", [RuntimeError("same"), RuntimeError("same")])
    assert mcp_client.unwrap_exc_message(dupes) == "same"


def test_unwrap_plain_and_empty_exception():
    assert mcp_client.unwrap_exc_message(RuntimeError("plain")) == "plain"
    assert mcp_client.unwrap_exc_message(RuntimeError("")) == "RuntimeError"


# ------------------------------------------------------------------ entra_graph_config_error
@pytest.mark.parametrize(
    "conn,ok",
    [
        ({"auth_method": "service_principal", "client_id": "c", "client_secret": "s"}, True),
        ({"auth_method": "service_principal_cert", "client_id": "c", "certificate_pem": "PEM"}, True),
        ({"auth_method": "service_principal", "client_id": "c"}, False),  # SP, no secret/cert
        ({"auth_method": "default_chain", "tenant_id": "t", "display_name": "Host"}, False),
        ({"auth_method": "az_cli_token", "tenant_id": "t"}, False),
        (None, False),
    ],
)
def test_config_error_matrix(conn, ok):
    msg = mcp_client.entra_graph_config_error(conn)
    assert (msg == "") is ok
    if not ok:
        assert "service-principal" in msg


def test_config_error_names_the_connection():
    msg = mcp_client.entra_graph_config_error(
        {"auth_method": "default_chain", "tenant_id": "t", "display_name": "My Host Conn"}
    )
    assert "My Host Conn" in msg


# ------------------------------------------------------------------------- collect_identity
def test_collect_identity_short_circuits_without_service_principal(monkeypatch):
    """A non-SP connection must NOT spawn the EntraID MCP server (it would crash-loop); every
    Graph-backed group gets the clear precondition message, while Key Vault (Resource Graph)
    still runs."""
    def _boom(*_a, **_k):
        raise AssertionError("build_entra_mcp_client must not be called for a non-SP connection")

    monkeypatch.setattr(mcp_client, "build_entra_mcp_client", _boom)

    async def _fake_kv(_connection, _index):
        return [], ""

    monkeypatch.setattr(collector, "_collect_keyvault_expiry", _fake_kv)

    conn = {"auth_method": "default_chain", "tenant_id": "t", "display_name": "Host identity"}
    snap = asyncio.run(
        collector.collect_identity(conn, days=90, mfa_cap=50, include_keyvault=True, tenant_id="t")
    )

    for g in ("expiring_credentials", "ownerless_apps", "ca_gaps", "users_without_mfa"):
        assert "service-principal" in snap["errors"][g], g
    # Key Vault ran via Resource Graph — no error, empty findings.
    assert "keyvault_expiry" not in snap["errors"]
    assert snap["groups"]["keyvault_expiry"] == []


def test_collect_identity_spawns_with_valid_service_principal(monkeypatch):
    """A valid SP connection DOES build the client and reports no config error."""
    calls = {"built": 0}

    class _FakeClient:
        def close(self):
            pass

    def _build(_settings, connection=None):
        calls["built"] += 1
        return _FakeClient()

    monkeypatch.setattr(mcp_client, "build_entra_mcp_client", _build)

    async def _empty(*_a, **_k):
        return []

    async def _mfa(_client, _cap):
        return [], 0, 0

    async def _kv(_connection, _index):
        return [], ""

    monkeypatch.setattr(collector, "_collect_expiring_credentials", _empty)
    monkeypatch.setattr(collector, "_collect_ownerless_apps", _empty)
    monkeypatch.setattr(collector, "_collect_ca_gaps", _empty)
    monkeypatch.setattr(collector, "_collect_users_without_mfa", _mfa)
    monkeypatch.setattr(collector, "_collect_keyvault_expiry", _kv)

    conn = {"auth_method": "service_principal", "client_id": "c", "client_secret": "s", "tenant_id": "t"}
    snap = asyncio.run(
        collector.collect_identity(conn, days=90, mfa_cap=50, include_keyvault=True, tenant_id="t")
    )
    assert calls["built"] == 1
    assert snap["errors"] == {}

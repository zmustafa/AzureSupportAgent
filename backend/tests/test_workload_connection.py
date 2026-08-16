"""Regression tests for the workload-vs-default connection bug.

Every workload-scoped feature must scan with the WORKLOAD's OWN connection (``connection_id``),
falling back to the default only when it has none. Using the default for a workload whose
subscription is reachable only via a non-default connection silently returns zero resources
(the bug originally seen on the Performance Profiler for 'ZVM Compute Environment').
"""
from __future__ import annotations

import pytest

import app.core.azure_connections as conns


def _setup(monkeypatch):
    """Two connections: a default, and a workload-specific one."""
    default = {"id": "conn-default", "is_default": True}
    wl_conn = {"id": "conn-workload"}
    monkeypatch.setattr(conns, "get_default_connection", lambda: default)
    monkeypatch.setattr(conns, "get_connection", lambda cid: {"conn-workload": wl_conn, "conn-default": default}.get(cid))
    return default, wl_conn


def test_connection_for_workload_uses_own_connection(monkeypatch):
    default, wl_conn = _setup(monkeypatch)
    assert conns.connection_for_workload({"connection_id": "conn-workload"}) == wl_conn


def test_connection_for_workload_falls_back_to_default(monkeypatch):
    default, _ = _setup(monkeypatch)
    assert conns.connection_for_workload({"connection_id": ""}) == default
    assert conns.connection_for_workload({}) == default
    assert conns.connection_for_workload(None) == default


def test_connection_for_workload_rejects_disabled_link(monkeypatch):
    monkeypatch.setattr(conns, "get_connection", lambda cid: {"id": cid, "disabled": True})
    assert conns.connection_for_workload({"connection_id": "conn-x"}) is None


def test_connection_for_workload_rejects_missing_link(monkeypatch):
    monkeypatch.setattr(conns, "get_connection", lambda _cid: None)
    assert conns.connection_for_workload({"connection_id": "conn-missing"}) is None


def test_teleintel_conn_for_workload_scope(monkeypatch):
    """Telemetry Intelligence resolves a workload's own connection for workload scopes,
    and the default for subscription scopes."""
    from app.api import teleintel
    import app.workloads.registry as reg

    _setup(monkeypatch)
    monkeypatch.setattr(reg, "get_workload", lambda sid, **kw: {"id": sid, "connection_id": "conn-workload"})
    assert teleintel._conn_for("workload", "wl-x")["id"] == "conn-workload"
    assert teleintel._conn_for("subscription", "sub-x")["id"] == "conn-default"


def test_perfprofile_conn_and_workload(monkeypatch):
    from app.api import perfprofile
    import app.workloads.registry as reg

    _setup(monkeypatch)
    wl = {"id": "wl-x", "connection_id": "conn-workload", "nodes": []}
    monkeypatch.setattr(reg, "get_workload", lambda sid, **kw: wl if sid == "wl-x" else None)
    conn, workload = perfprofile._conn_and_workload("workload", "wl-x")
    assert conn["id"] == "conn-workload"
    assert workload is wl
    # Subscription scope: no workload, default connection.
    conn2, wl2 = perfprofile._conn_and_workload("subscription", "sub-x")
    assert conn2["id"] == "conn-default"
    assert wl2 is None


# --------------------------------------------------------------------------- connection_for_scope
def test_connection_for_scope_workload_ownership_precedes_explicit_picker(monkeypatch):
    """A workload's canonical connection wins, while subscription overrides remain explicit."""
    default, wl_conn = _setup(monkeypatch)
    other = {"id": "conn-other"}
    monkeypatch.setattr(conns, "get_connection", lambda cid: {
        "conn-workload": wl_conn, "conn-default": default, "conn-other": other}.get(cid))
    wl = {"connection_id": "conn-workload"}
    # Workload scope cannot be redirected to another tenant by stale picker state.
    assert conns.connection_for_scope("workload", connection_id="conn-other", workload=wl) == wl_conn
    # Subscription scope, explicit override beats the default.
    assert conns.connection_for_scope("subscription", connection_id="conn-other") == other


def test_connection_for_scope_workload_uses_own(monkeypatch):
    _setup(monkeypatch)
    wl = {"connection_id": "conn-workload"}
    # No override → workload scope uses the workload's own connection.
    assert conns.connection_for_scope("workload", workload=wl)["id"] == "conn-workload"


def test_connection_for_scope_subscription_defaults(monkeypatch):
    default, _ = _setup(monkeypatch)
    # No override, subscription/tenant scope → default connection.
    assert conns.connection_for_scope("subscription") == default
    assert conns.connection_for_scope("tenant") == default
    # Workload scope with no workload object also defaults.
    assert conns.connection_for_scope("workload") == default


def test_teleintel_conn_for_preserves_workload_ownership(monkeypatch):
    from app.api import teleintel
    import app.workloads.registry as reg

    default, wl_conn = _setup(monkeypatch)
    other = {"id": "conn-other"}
    monkeypatch.setattr(conns, "get_connection", lambda cid: {
        "conn-workload": wl_conn, "conn-default": default, "conn-other": other}.get(cid))
    monkeypatch.setattr(reg, "get_workload", lambda sid, **kw: {"id": sid, "connection_id": "conn-workload"})
    # Subscription scope + override → the picked connection (not the default).
    assert teleintel._conn_for("subscription", "sub-x", "conn-other")["id"] == "conn-other"
    # Workload scope + stale override → the workload's own connection.
    assert teleintel._conn_for("workload", "wl-x", "conn-other")["id"] == "conn-workload"


def test_perfprofile_conn_and_workload_preserves_workload_ownership(monkeypatch):
    from app.api import perfprofile
    import app.workloads.registry as reg

    default, wl_conn = _setup(monkeypatch)
    other = {"id": "conn-other"}
    monkeypatch.setattr(conns, "get_connection", lambda cid: {
        "conn-workload": wl_conn, "conn-default": default, "conn-other": other}.get(cid))
    wl = {"id": "wl-x", "connection_id": "conn-workload", "nodes": []}
    monkeypatch.setattr(reg, "get_workload", lambda sid, **kw: wl if sid == "wl-x" else None)
    conn, _wl = perfprofile._conn_and_workload("workload", "wl-x", "conn-other")
    assert conn["id"] == "conn-workload"


def test_chat_workload_connection_overrides_stale_tenant_picker(monkeypatch):
    from app.api import chats

    _setup(monkeypatch)
    connection = chats._resolve_workload_bound_connection(
        "conn-default", {"id": "wl-x", "connection_id": "conn-workload"}
    )
    assert connection and connection["id"] == "conn-workload"


def test_chat_connectionless_workload_honors_picker(monkeypatch):
    from app.api import chats

    default, _ = _setup(monkeypatch)
    assert chats._resolve_workload_bound_connection("conn-default", {"id": "wl-x"}) == default


def test_chat_missing_workload_connection_fails_closed(monkeypatch):
    from app.api import chats

    monkeypatch.setattr(conns, "get_connection", lambda _cid: None)
    with pytest.raises(ValueError, match="missing or disabled"):
        chats._resolve_workload_bound_connection(
            "conn-default", {"id": "wl-x", "connection_id": "conn-missing"}
        )


def test_changeexplorer_workload_ignores_stale_connection_picker(monkeypatch):
    from app.api import changeexplorer
    import app.workloads.registry as registry

    _setup(monkeypatch)
    workload = {"id": "wl-x", "connection_id": "conn-workload", "nodes": []}
    monkeypatch.setattr(registry, "get_workload", lambda _id: workload)
    resolved_workload, connection = changeexplorer._resolve("wl-x", "conn-default")
    assert resolved_workload is workload
    assert connection and connection["id"] == "conn-workload"


def test_tagintel_workload_write_ignores_stale_connection_picker(monkeypatch):
    from app.api import tagintel
    import app.workloads.registry as registry

    _setup(monkeypatch)
    workload = {"id": "wl-x", "connection_id": "conn-workload", "nodes": []}
    monkeypatch.setattr(registry, "get_workload", lambda _id: workload)
    connection = tagintel._resolve_write_connection("conn-default", "wl-x")
    assert connection and connection["id"] == "conn-workload"


def test_iam_workload_filter_uses_canonical_tenant(monkeypatch):
    from types import SimpleNamespace

    from app.api import iam
    import app.workloads.registry as registry

    default = {"id": "conn-default", "tenant_id": "tenant-default", "is_default": True}
    workload_connection = {"id": "conn-workload", "tenant_id": "tenant-workload"}
    monkeypatch.setattr(conns, "get_default_connection", lambda: default)
    monkeypatch.setattr(
        conns,
        "get_connection",
        lambda connection_id: {
            "conn-default": default,
            "conn-workload": workload_connection,
        }.get(connection_id),
    )
    monkeypatch.setattr(
        registry,
        "get_workload",
        lambda workload_id: {
            "id": workload_id,
            "connection_id": "conn-workload",
        },
    )

    connection, tenant_id, connection_id = iam._target(
        SimpleNamespace(tenant_id="principal-tenant"),
        "conn-default",
        "wl-x",
    )

    assert connection is workload_connection
    assert tenant_id == "tenant-workload"
    assert connection_id == "conn-workload"


def test_iam_unknown_workload_filter_fails_closed(monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException
    from app.api import iam
    import app.workloads.registry as registry

    monkeypatch.setattr(registry, "get_workload", lambda _workload_id: None)
    with pytest.raises(HTTPException) as exc:
        iam._target(SimpleNamespace(tenant_id="principal-tenant"), "conn-default", "missing")

    assert exc.value.status_code == 404


def test_policy_inventory_uses_workload_connection_for_cache_and_scan(monkeypatch):
    from app.api import policy
    import app.workloads.registry as registry

    default = {"id": "conn-default", "tenant_id": "tenant-default", "is_default": True}
    workload_connection = {"id": "conn-workload", "tenant_id": "tenant-workload"}
    monkeypatch.setattr(conns, "get_default_connection", lambda: default)
    monkeypatch.setattr(
        conns,
        "get_connection",
        lambda connection_id: {
            "conn-default": default,
            "conn-workload": workload_connection,
        }.get(connection_id),
    )
    workload = {"id": "wl-x", "connection_id": "conn-workload"}
    monkeypatch.setattr(registry, "get_workload", lambda _workload_id: workload)
    monkeypatch.setattr(policy, "get_workload", lambda _workload_id: workload)

    connection, resolved_workload, connection_id = policy._inventory_scope(
        "conn-default", "wl-x"
    )

    assert connection is workload_connection
    assert resolved_workload is workload
    assert connection_id == "conn-workload"


def test_alerts_manager_unknown_workload_fails_closed(monkeypatch):
    from app.alerts_manager import service
    import app.workloads.registry as registry

    monkeypatch.setattr(registry, "get_workload", lambda _workload_id: None)
    with pytest.raises(ValueError, match="not found"):
        service.resolve_selected_connection("conn-default", "missing")


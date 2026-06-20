"""Regression tests for the workload-vs-default connection bug.

Every workload-scoped feature must scan with the WORKLOAD's OWN connection (``connection_id``),
falling back to the default only when it has none. Using the default for a workload whose
subscription is reachable only via a non-default connection silently returns zero resources
(the bug originally seen on the Performance Profiler for 'ZVM Compute Environment').
"""
from __future__ import annotations

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


def test_connection_for_workload_ignores_disabled(monkeypatch):
    default = {"id": "conn-default", "is_default": True}
    monkeypatch.setattr(conns, "get_default_connection", lambda: default)
    monkeypatch.setattr(conns, "get_connection", lambda cid: {"id": cid, "disabled": True})
    # A disabled workload connection falls back to the default.
    assert conns.connection_for_workload({"connection_id": "conn-x"}) == default


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

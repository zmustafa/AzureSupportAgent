"""Connection-scoping contract tests for the Estate Graph (/graph).

Covers the cross-tenant leak fix: a selected Azure connection must only ever surface its
own workloads + architectures (plus connection-less demo/unassigned ones), never another
connection's. Mirrors test-plan section A (T1-T18). Registries are isolated to tmp_path so
these never touch real .data.
"""
from __future__ import annotations

import pytest

from app.architectures import registry as arch_registry
from app.workloads import registry as wl_registry

CONN_A = "conn-aaaa"
CONN_B = "conn-bbbb"
CONN_C = "conn-cccc"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(wl_registry, "_PATH", tmp_path / "workloads.json")
    monkeypatch.setattr(arch_registry, "_PATH", tmp_path / "architectures.json")
    # Seed a controlled estate: 2 workloads on A, 1 on B, 1 connection-less (demo).
    wl_registry.upsert_workload({"name": "A-Web", "connection_id": CONN_A, "tenant_id": "ta"})
    wl_registry.upsert_workload({"name": "A-Data", "connection_id": CONN_A, "tenant_id": "ta"})
    wl_registry.upsert_workload({"name": "B-CRM", "connection_id": CONN_B, "tenant_id": "tb"})
    wl_registry.upsert_workload({"name": "Demo", "connection_id": "", "tenant_id": ""})
    # Architectures: 2 on A, 1 on B, 1 connection-less. All principal-tenant "default".
    arch_registry.upsert_architecture({"name": "A-Arch1", "connection_id": CONN_A, "tenant_id": "default"})
    arch_registry.upsert_architecture({"name": "A-Arch2", "connection_id": CONN_A, "tenant_id": "default"})
    arch_registry.upsert_architecture({"name": "B-Arch", "connection_id": CONN_B, "tenant_id": "default"})
    arch_registry.upsert_architecture({"name": "Demo-Arch", "connection_id": "", "tenant_id": "default"})
    yield


# --------------------------------------------------------------- workloads scoping
def test_scoped_workloads_only_own_plus_demo():
    from app.api.graph import _scoped_workloads

    a = {w["name"] for w in _scoped_workloads(CONN_A)}
    assert a == {"A-Web", "A-Data", "Demo"}  # T1: own + demo, no B
    b = {w["name"] for w in _scoped_workloads(CONN_B)}
    assert b == {"B-CRM", "Demo"}  # T2/T3: own + demo, no A
    assert "B-CRM" not in a and "A-Web" not in b  # T1/T2: no cross-leak


def test_scoped_workloads_empty_cid_returns_all():
    from app.api.graph import _scoped_workloads

    names = {w["name"] for w in _scoped_workloads("")}
    assert names == {"A-Web", "A-Data", "B-CRM", "Demo"}  # T14: unscoped fallback


def test_scoped_workloads_unknown_cid_returns_only_demo():
    from app.api.graph import _scoped_workloads

    names = {w["name"] for w in _scoped_workloads(CONN_C)}
    assert names == {"Demo"}  # unknown connection → only connection-less


def test_switching_connections_changes_set():
    from app.api.graph import _scoped_workloads

    seq = [len(_scoped_workloads(c)) for c in (CONN_A, CONN_B, CONN_A, CONN_C)]
    assert seq == [3, 2, 3, 1]  # T4: counts change each switch, no carry-over


# --------------------------------------------------------------- architecture scoping
def test_scoped_architectures_follow_connection():
    from app.api.graph import _scoped_architectures

    a = {x["name"] for x in _scoped_architectures("default", CONN_A)}
    assert a == {"A-Arch1", "A-Arch2", "Demo-Arch"}  # T5: re-scope, no B
    b = {x["name"] for x in _scoped_architectures("default", CONN_B)}
    assert b == {"B-Arch", "Demo-Arch"}
    assert "B-Arch" not in a and "A-Arch1" not in b


def test_scoped_architectures_empty_cid_returns_all():
    from app.api.graph import _scoped_architectures

    names = {x["name"] for x in _scoped_architectures("default", "")}
    assert names == {"A-Arch1", "A-Arch2", "B-Arch", "Demo-Arch"}


# --------------------------------------------------------------- single-workload guard
def test_workload_in_scope_guard():
    from app.api.graph import _workload_in_scope

    a_wl = {"connection_id": CONN_A}
    demo_wl = {"connection_id": ""}
    assert _workload_in_scope(a_wl, CONN_A) is True       # T7: own connection
    assert _workload_in_scope(a_wl, CONN_B) is False      # T7/T8/T9: cross-connection blocked
    assert _workload_in_scope(demo_wl, CONN_B) is True    # T6: connection-less shown everywhere
    assert _workload_in_scope(a_wl, "") is True           # T14: empty cid = allow
    assert _workload_in_scope(None, CONN_A) is False      # missing workload


def test_demo_workload_visible_under_every_connection():
    from app.api.graph import _scoped_workloads

    for cid in (CONN_A, CONN_B, CONN_C):
        assert any(w["name"] == "Demo" for w in _scoped_workloads(cid))  # T6


def test_same_tenant_two_connections_stay_separate():
    """T13: two connections in the SAME tenant must not merge by tenant_id."""
    from app.api.graph import _scoped_workloads

    # A and a hypothetical sibling share tenant 'ta' but differ by connection_id.
    wl_registry.upsert_workload({"name": "A-Sibling", "connection_id": "conn-sibling", "tenant_id": "ta"})
    a = {w["name"] for w in _scoped_workloads(CONN_A)}
    assert "A-Sibling" not in a  # same tenant, different connection → not shown
    sib = {w["name"] for w in _scoped_workloads("conn-sibling")}
    assert sib == {"A-Sibling", "Demo"}


# --------------------------------------------------------------- multi-workload focus filtering
def test_multi_workload_request_filters_to_in_scope():
    """The /workloads endpoint must drop ids that belong to another connection (the dedupe +
    in-scope filtering is the same _workload_in_scope guard, exercised here over a list)."""
    from app.api.graph import _workload_in_scope
    from app.workloads.registry import get_workload, list_workloads

    by_name = {w["name"]: w for w in list_workloads()}
    a_id = by_name["A-Web"]["id"]
    b_id = by_name["B-CRM"]["id"]
    demo_id = by_name["Demo"]["id"]
    requested = [a_id, b_id, demo_id, a_id]  # includes a cross-connection (B) + a duplicate (A)

    seen: set[str] = set()
    kept = []
    for wid in requested:
        if wid in seen:
            continue
        seen.add(wid)
        wl = get_workload(wid)
        if _workload_in_scope(wl, CONN_A):
            kept.append(wl["name"])
    assert kept == ["A-Web", "Demo"]  # B dropped (other connection), duplicate A collapsed


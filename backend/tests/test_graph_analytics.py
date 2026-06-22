"""Unit tests for graph analytics, drift, overlays, narrative filters, and saved views."""
from __future__ import annotations

from app.graph import analytics as AN
from app.graph import assembler as A
from app.graph import drift as DR
from app.graph import narrative as NAR
from app.graph import overlays as OV


# --------------------------------------------------------------------- fixtures
def _chain():
    nodes = [{"id": f"n{i}", "kind": "resource", "label": f"n{i}", "data": {}} for i in range(5)]
    edges = [{"id": f"e{i}", "source": f"n{i}", "target": f"n{i+1}", "kind": "depends_on"} for i in range(4)]
    return nodes, edges


# --------------------------------------------------------------------- path
def test_shortest_path_chain():
    nodes, edges = _chain()
    r = AN.shortest_path(nodes, edges, "n0", "n4")
    assert r["found"] and r["hops"] == 4
    assert r["path"] == ["n0", "n1", "n2", "n3", "n4"]
    assert len(r["edges"]) == 4


def test_shortest_path_same_node_and_missing():
    nodes, edges = _chain()
    assert AN.shortest_path(nodes, edges, "n2", "n2") == {"found": True, "path": ["n2"], "hops": 0, "edges": []}
    assert AN.shortest_path(nodes, edges, "n0", "ghost")["found"] is False


def test_directed_path_respects_direction():
    nodes, edges = _chain()
    assert AN.shortest_path(nodes, edges, "n0", "n4", directed=True)["found"] is True
    # backwards is unreachable when directed
    assert AN.shortest_path(nodes, edges, "n4", "n0", directed=True)["found"] is False
    # undirected reaches it
    assert AN.shortest_path(nodes, edges, "n4", "n0", directed=False)["found"] is True


# --------------------------------------------------------------------- blast radius
def test_blast_radius_layers():
    nodes, edges = _chain()
    r = AN.blast_radius(nodes, edges, "n0", max_depth=2)
    assert r["direct"] == ["n1"]
    assert "n2" in r["indirect"]
    assert "n3" not in r["by_depth"].get("3", [])  # capped at depth 2
    assert r["impacted_count"] == 2


def test_blast_radius_counts_workloads():
    nodes = [{"id": "r1", "kind": "resource", "label": "r1"}, {"id": "wl:a", "kind": "workload", "label": "App"}]
    edges = [{"id": "e", "source": "r1", "target": "wl:a", "kind": "belongs_to"}]
    r = AN.blast_radius(nodes, edges, "r1", max_depth=3)
    assert r["impacted_workloads"] == [{"id": "wl:a", "label": "App"}]


# --------------------------------------------------------------------- centrality
def test_betweenness_identifies_broker():
    nodes, edges = _chain()
    bc = AN.betweenness_centrality(nodes, edges)
    # the middle node n2 sits on the most shortest paths.
    assert bc["n2"] >= bc["n1"] >= bc["n0"]
    assert bc["n0"] == 0.0


def test_degree_centrality():
    nodes, edges = _chain()
    deg = AN.degree_centrality(nodes, edges)
    assert deg["n0"] == 1 and deg["n2"] == 2


def test_concentration_risk_ranks_loadbearing():
    nodes, edges = _chain()
    ranked = AN.concentration_risk(nodes, edges, top=3)
    assert ranked and ranked[0]["id"] == "n2"


# --------------------------------------------------------------------- communities
def test_connected_components_splits_disjoint():
    nodes = [{"id": x, "kind": "resource"} for x in ["a", "b", "c", "d"]]
    edges = [{"id": "e1", "source": "a", "target": "b", "kind": "depends_on"}]
    comps = AN.connected_components(nodes, edges)
    assert [len(c) for c in comps] == [2, 1, 1]


# --------------------------------------------------------------------- orphans + candidates
def test_detect_orphans():
    resources = [
        {"id": "r1", "name": "a", "type": "t", "workloads": [{"id": "w1"}]},
        {"id": "r2", "name": "b", "type": "t", "workloads": []},
    ]
    workloads = [{"id": "w1", "name": "Owned"}, {"id": "w2", "name": "NoArch"}]
    architectures = [{"id": "a1", "name": "Arch", "workload_id": "w1"}, {"id": "a2", "name": "Stray", "workload_id": ""}]
    o = AN.detect_orphans(resources=resources, workloads=workloads, architectures=architectures)
    assert o["unowned_count"] == 1
    assert {w["id"] for w in o["workloads_without_architecture"]} == {"w2"}
    assert {a["id"] for a in o["architectures_without_workload"]} == {"a2"}


def test_candidate_workloads_clusters_unowned():
    resources = [
        {"id": "/x", "name": "x", "type": "t", "resource_group": "rg", "subscription_id": "s", "workloads": []},
        {"id": "/y", "name": "y", "type": "t", "resource_group": "rg", "subscription_id": "s", "workloads": []},
        {"id": "/z", "name": "z", "type": "t", "resource_group": "rg", "subscription_id": "s", "workloads": []},
    ]
    dep = [
        {"id": "e1", "source": A.res_id("/x"), "target": A.res_id("/y"), "kind": "depends_on"},
        {"id": "e2", "source": A.res_id("/y"), "target": A.res_id("/z"), "kind": "connects_to"},
    ]
    cands = AN.candidate_workloads(resources=resources, dependency_edges=dep, min_size=3)
    assert cands and cands[0]["size"] == 3
    assert cands[0]["reason"] == "dependency-linked"


# --------------------------------------------------------------------- drift
def test_drift_classifies_three_states():
    arch = {"nodes": [{"arm_id": "/A", "name": "A"}, {"arm_id": "/B", "name": "B"}]}
    members = [{"id": "/A", "name": "A"}, {"id": "/C", "name": "C"}]
    d = DR.compute_drift(architecture=arch, member_resources=members)
    assert d["counts"] == {"ok": 1, "documented_missing": 1, "live_uncontrolled": 1}
    assert d["drift_score"] == 33
    classes = DR.drift_classification(d)
    assert classes["/a"] == "ok" and classes["/b"] == "documented_missing" and classes["/c"] == "live_uncontrolled"


def test_drift_no_architecture():
    d = DR.compute_drift(architecture=None, member_resources=[{"id": "/A"}])
    assert d["has_architecture"] is False
    assert d["drift_score"] is None
    assert len(d["live_uncontrolled"]) == 1


def test_drift_perfect_alignment():
    arch = {"nodes": [{"arm_id": "/A"}]}
    d = DR.compute_drift(architecture=arch, member_resources=[{"id": "/A"}])
    assert d["drift_score"] == 100 and "matches" in d["summary"]


# --------------------------------------------------------------------- architecture dependency edges
def test_architecture_dependency_edges_translation():
    arch = {
        "nodes": [{"id": "n1", "arm_id": "/A"}, {"id": "n2", "arm_id": "/B"}, {"id": "n3", "arm_id": ""}],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "kind": "depends_on"},
            {"id": "e2", "source": "n1", "target": "n3", "kind": "connects_to"},  # n3 has no arm id → dropped
            {"id": "e3", "source": "n1", "target": "n2", "kind": "models"},        # not a dep kind → dropped
        ],
    }
    deps = A.architecture_dependency_edges(arch)
    assert len(deps) == 1
    assert deps[0]["source"] == A.res_id("/A") and deps[0]["target"] == A.res_id("/B")
    assert deps[0]["kind"] == "depends_on"


# --------------------------------------------------------------------- ask filter (deterministic)
def test_ask_keyword_filter_predicates():
    flt = NAR._keyword_filter("show internet-facing workloads without backup")
    assert "internet_facing" in flt["predicates"]
    assert "no_backup" in flt["predicates"]
    assert "workload" in flt["kinds"]


def test_ask_matches_predicates():
    nodes = [
        {"id": "wl:a", "kind": "workload", "label": "Public App", "data": {"flags": [], "overlay": {"no_backup": True, "internet_facing": True}}},
        {"id": "wl:b", "kind": "workload", "label": "Safe App", "data": {"overlay": {"no_backup": False}}},
    ]
    flt = {"kinds": ["workload"], "predicates": ["no_backup", "internet_facing"], "text": ""}
    matched = [n["id"] for n in nodes if NAR._matches(n, flt)]
    assert matched == ["wl:a"]


def test_ask_shared_service_predicate():
    node = {"id": "res:/kv", "kind": "resource", "label": "kv", "data": {"workloads": [{"id": "w1"}, {"id": "w2"}]}}
    assert NAR._matches(node, {"predicates": ["shared_service"]}) is True


# --------------------------------------------------------------------- overlays.apply
def test_apply_overlays_merges_nodes_and_patches():
    graph = {
        "nodes": [{"id": "wl:a", "kind": "workload", "label": "App", "data": {}}],
        "edges": [],
        "stats": {},
    }
    overlay = {
        "nodes": [{"id": "gap:wl:a|backupdr", "kind": "coverage_gap", "label": "Backup 40%", "data": {}, "badges": {}, "expandable": False}],
        "edges": [{"id": "x", "source": "wl:a", "target": "gap:wl:a|backupdr", "kind": "has_gap"}],
        "patches": {"wl:a": {"overlay": {"no_backup": True}}},
    }
    OV.apply_overlays(graph, [overlay])
    assert any(n["id"].startswith("gap:") for n in graph["nodes"])
    wl = next(n for n in graph["nodes"] if n["id"] == "wl:a")
    assert wl["data"]["overlay"]["no_backup"] is True
    assert graph["stats"]["node_count"] == 2

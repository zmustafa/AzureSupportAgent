"""Unit tests for the central knowledge-graph assembler (pure builders)."""
from __future__ import annotations

from app.graph import assembler as A


# --------------------------------------------------------------------- id helpers
def test_node_id_round_trip():
    assert A.decode_node_id(A.conn_id("c1")) == ("conn", "c1")
    assert A.decode_node_id(A.sub_id("SUB-1")) == ("sub", "sub-1")
    assert A.decode_node_id(A.wl_id("w1")) == ("wl", "w1")
    assert A.decode_node_id(A.arch_id("a1")) == ("arch", "a1")
    # ARM ids contain slashes — they must survive decoding (split on first ':' only).
    arm = "/subscriptions/abc/resourceGroups/rg/providers/microsoft.web/sites/app"
    assert A.decode_node_id(A.res_id(arm)) == ("res", arm)
    assert A.decode_node_id("") == ("", "")
    assert A.decode_node_id("bare") == ("", "bare")


def test_rg_and_finding_ids_compose():
    assert A.decode_node_id(A.rg_id("SUB", "MyRG")) == ("rg", "sub|myrg")
    assert A.decode_node_id(A.finding_id("run1", "cis_1_1")) == ("finding", "run1|cis_1_1")


# --------------------------------------------------------------------- overview
def _overview():
    return A.build_overview(
        connection={"id": "c1", "display_name": "khspn", "tenant_id": "t", "status": "ok", "is_default": True},
        subscriptions=[
            {"id": "sub-1", "name": "Sub One", "resource_count": 12},
            {"id": "sub-2", "name": "Sub Two", "resource_count": 0},
        ],
        workloads=[
            {"id": "w1", "name": "Web", "nodes": [{"kind": "subscription", "id": "sub-1"}], "summary": {"total_resources": 6}},
            {"id": "w2", "name": "Orphan", "nodes": [], "summary": {}},
        ],
        architectures=[
            {"id": "a1", "name": "Web Arch", "workload_id": "w1", "nodes": [1, 2, 3]},
            {"id": "a2", "name": "Stray Arch", "workload_id": "", "nodes": []},
        ],
        risk_by_workload={"w1": {"run_id": "r1", "score": 70, "failed": 3, "severity": "high"}},
    )


def test_overview_structure():
    g = _overview()
    kinds = sorted(n["kind"] for n in g["nodes"])
    # 1 connection + 2 subs + 2 workloads + 2 architectures.
    assert kinds.count("tenant_connection") == 1
    assert kinds.count("subscription") == 2
    assert kinds.count("workload") == 2
    assert kinds.count("architecture") == 2
    assert g["stats"]["node_count"] == 7


def test_overview_edges_topology():
    g = _overview()
    edges = {(e["source"], e["kind"], e["target"]) for e in g["edges"]}
    assert ("conn:c1", "contains", "sub:sub-1") in edges
    assert ("sub:sub-1", "contains", "wl:w1") in edges          # workload under its subscription
    assert ("conn:c1", "contains", "wl:w2") in edges            # orphan workload under connection
    assert ("wl:w1", "models", "arch:a1") in edges              # architecture models its workload
    # Stray architecture (no/unknown workload) hangs off the connection, marked unlinked.
    stray = next(e for e in g["edges"] if e["target"] == "arch:a2")
    assert stray["source"] == "conn:c1" and stray["label"] == "unlinked"


def test_overview_workload_risk_badge():
    g = _overview()
    w1 = next(n for n in g["nodes"] if n["id"] == "wl:w1")
    assert w1["data"]["risk"]["level"] == "high"
    assert w1["data"]["risk"]["failed"] == 3
    assert w1["badges"]["failed"] == 3
    w2 = next(n for n in g["nodes"] if n["id"] == "wl:w2")
    assert w2["data"]["risk"]["level"] == "ok"


# --------------------------------------------------------------------- resource node
def test_resource_node_shape():
    r = {
        "id": "/subscriptions/s/resourceGroups/rg/providers/microsoft.web/sites/app",
        "name": "app",
        "type": "microsoft.web/sites",
        "location": "eastus",
        "resource_group": "rg",
        "subscription_id": "s",
        "sku": "P1v2",
        "tags": {"env": "prod"},
        "flags": ["public_ip"],
        "workloads": [{"id": "w1", "name": "Web"}],
    }
    node = A.resource_node(r)
    assert node["kind"] == "resource"
    assert node["data"]["short_type"] == "sites"
    assert node["data"]["arm_id"] == r["id"]
    assert node["badges"]["flags"] == 1


# --------------------------------------------------------------------- expand
def test_expand_subscription_groups_and_workloads():
    resources = [
        {"id": "r1", "subscription_id": "sub-1", "resource_group": "rg-a"},
        {"id": "r2", "subscription_id": "sub-1", "resource_group": "rg-a"},
        {"id": "r3", "subscription_id": "sub-1", "resource_group": "rg-b"},
        {"id": "r4", "subscription_id": "other", "resource_group": "rg-z"},
    ]
    workloads = [{"id": "w1", "name": "Web", "nodes": [{"kind": "subscription", "id": "sub-1"}]}]
    g = A.expand_subscription(subscription_id="sub-1", name="Sub One", resources=resources, workloads=workloads)
    rgs = [n for n in g["nodes"] if n["kind"] == "resource_group"]
    assert {n["data"]["resource_group"] for n in rgs} == {"rg-a", "rg-b"}
    rg_a = next(n for n in rgs if n["data"]["resource_group"] == "rg-a")
    assert rg_a["badges"]["resources"] == 2
    assert any(n["kind"] == "workload" for n in g["nodes"])


def test_expand_resource_group_filters_and_caps():
    resources = [
        {"id": "r1", "name": "a", "type": "t", "subscription_id": "sub-1", "resource_group": "rg-a"},
        {"id": "r2", "name": "b", "type": "t", "subscription_id": "sub-1", "resource_group": "rg-a"},
        {"id": "r3", "name": "c", "type": "t", "subscription_id": "sub-1", "resource_group": "rg-b"},
    ]
    g = A.expand_resource_group(subscription_id="sub-1", resource_group="rg-a", resources=resources)
    res = [n for n in g["nodes"] if n["kind"] == "resource"]
    assert {n["data"]["arm_id"] for n in res} == {"r1", "r2"}
    assert all(e["kind"] == "contains" for e in g["edges"])


def test_expand_workload_members_arch_memory_findings():
    workload = {"id": "w1", "name": "Web"}
    resources = [
        {"id": "r1", "name": "a", "type": "microsoft.web/sites", "subscription_id": "s", "resource_group": "rg", "workloads": [{"id": "w1"}]},
        {"id": "r2", "name": "b", "type": "microsoft.sql/servers", "subscription_id": "s", "resource_group": "rg", "workloads": [{"id": "other"}]},
    ]
    architectures = [{"id": "a1", "name": "Web Arch", "workload_id": "w1", "nodes": [1, 2]}]
    memory = {"architecture_id": "a1", "sections": 12, "confidence": 0.8}
    risk = {"run_id": "run1", "severity": "high"}
    findings = [
        {"check_id": "c_fail", "title": "Bad", "status": "fail", "severity": "critical"},
        {"check_id": "c_pass", "title": "Good", "status": "pass", "severity": "info"},
    ]
    g = A.expand_workload(workload=workload, resources=resources, architectures=architectures, memory=memory, risk=risk, findings=findings)
    kinds = [n["kind"] for n in g["nodes"]]
    # Only the member resource (r1) is attached, plus the architecture, memory and the one failing finding.
    assert kinds.count("resource") == 1
    assert "architecture" in kinds and "architecture_memory" in kinds
    finding_nodes = [n for n in g["nodes"] if n["kind"] == "assessment_finding"]
    assert len(finding_nodes) == 1 and finding_nodes[0]["data"]["check_id"] == "c_fail"
    edges = {(e["kind"]) for e in g["edges"]}
    assert {"belongs_to", "models", "documents", "has_finding"} <= edges


# --------------------------------------------------------------------- search
def test_search_matches_and_orders():
    subs = [{"id": "sub-1", "name": "Production", "resource_count": 3}]
    workloads = [{"id": "w1", "name": "Production Web", "description": "", "workload_type": "web_app"}]
    architectures = [{"id": "a1", "name": "Prod Arch", "workload_name": "Production Web"}]
    resources = [{"id": "r1", "name": "prod-app", "type": "microsoft.web/sites", "resource_group": "rg"}]
    out = A.search(query="prod", subscriptions=subs, workloads=workloads, architectures=architectures, resources=resources)
    kinds = [n["kind"] for n in out]
    # Workloads + architectures rank ahead of subscriptions + resources.
    assert kinds[0] == "workload"
    assert "architecture" in kinds and "subscription" in kinds and "resource" in kinds
    assert A.search(query="", subscriptions=subs, workloads=workloads, architectures=architectures, resources=resources) == []


def test_search_respects_limit():
    resources = [{"id": f"r{i}", "name": f"match{i}", "type": "t", "resource_group": "rg"} for i in range(100)]
    out = A.search(query="match", subscriptions=[], workloads=[], architectures=[], resources=resources, limit=10)
    assert len(out) == 10


# --------------------------------------------------------------------- dedupe
def test_dedupe_collapses_repeats():
    workloads = [{"id": "w1", "name": "Web", "nodes": [{"kind": "subscription", "id": "sub-1"}], "summary": {}}]
    # Two subs with the same id must collapse to one node.
    g = A.build_overview(
        connection={"id": "c1", "display_name": "x", "tenant_id": "t"},
        subscriptions=[{"id": "sub-1", "name": "A", "resource_count": 1}, {"id": "sub-1", "name": "A", "resource_count": 1}],
        workloads=workloads,
        architectures=[],
    )
    sub_nodes = [n for n in g["nodes"] if n["kind"] == "subscription"]
    assert len(sub_nodes) == 1

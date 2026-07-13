---
layout: default
title: Explore the Estate Graph
parent: Design and assessment operations
grand_parent: How-to guides
nav_order: 4
description: Search and focus graph nodes, use lenses and overlays, trace paths, estimate blast radius, and export the visible graph.
permalink: /how-to/design-assessment/estate-graph/
---

# Explore the Estate Graph

![Estate Graph visualization]({{ site.baseurl }}/assets/estate-graph.png)

## Prerequisites

- `graph.read`; deployments may restrict Graph to administrators.
- Workloads, architectures, and cached inventory; optional assessment, cost, coverage, retirement, RBAC, and change data for overlays.

## Route

Open `/graph` or `/graph/{focusId}`.

## How to find, expand, and inspect nodes

1. Select the correct Azure connection and load the overview.
2. Search by workload, architecture, subscription, resource group, or resource name.
3. Select a result to highlight/inspect; double-select or use **Expand** to bloom children.
4. Use layer toggles to show/hide node kinds and choose Organic, Hierarchy, or Concentric layout.
5. Right-click for Inspect, Expand, Isolate/Focus, Hide, Blast radius, or export actions offered for that node.
6. Use **Open in** to navigate to the owning workload, architecture, inventory, or assessment view.

**Expected result:** The graph is narrowed to the relevant topology and the node inspector shows assembled context.

**Verification:** Compare node IDs and relationships with workload membership, architecture, and inventory; presence does not prove live traffic.

## How to use lenses, overlays, and drift

1. Focus one workload; overlays and drift require a focused workload.
2. Apply one lens/overlay at a time: risk, cost, coverage, retirements, access/RBAC, changes, or other labels exposed by the current menu.
3. Enable **Drift** to compare intended architecture with current inventory.
4. Inspect highlighted node details and source timestamps.
5. Refresh the source feature when data is absent/stale, then reload/focus the graph.
6. Clear/reset before switching to an unrelated question.

**Expected result:** Nodes are recolored/annotated with available cached source evidence and drift differences.

**Verification:** Open the source module and match scope, timestamp, value, and resource ID.

## How to find a path

1. Select **Path** mode.
2. Pick the source node, then the target node.
3. Review the highlighted path and hop count.
4. Expand hidden dependencies or reveal node kinds and retry when an expected path is absent.

**Expected result:** A shortest path through currently assembled nodes/edges is highlighted, or no path is reported.

**Verification:** Validate every edge against architecture/inventory; no graph path does not prove no real dependency.

## How to estimate blast radius

1. Select **Blast** mode and click the source node, or right-click and choose **Blast radius from here**.
2. Review direct and indirect highlights and impacted workload count.
3. Inspect the highest-impact nodes and open the affected workloads.
4. Correlate with redundancy, routing, failover, and application behavior outside the graph.

**Expected result:** Downstream reachable nodes are highlighted to the supported depth.

**Verification:** Confirm critical dependencies with reviewed architecture and operational owners; this is topology analysis, not failure simulation.

## How to export the graph

1. Focus/filter/layer the exact canvas to share.
2. Fit the required nodes and remove accidental highlights.
3. Open the View/context menu and choose **Export PNG** for a high-resolution visual or **Export JSON** for current nodes/edges.
4. Store the artifact as sensitive infrastructure metadata.

**Expected result:** The visible graph state is downloaded as PNG or JSON.

**Verification:** Open the export and confirm required nodes, filters, scope, and absence of unintended topology.

## Safety and rollback

- Graph is cache-backed/read-oriented; saved presentation preferences do not change Azure.
- Hidden nodes and filters change path/blast results.
- AI narrative/Ask features, where enabled, are advisory and cannot add evidence.
- Reset view or change saved layout to roll back presentation changes.
- Exports reveal topology and identifiers.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Overview is empty | Check permission, connection, active workloads, architectures, and inventory cache. |
| Overlay is empty | Focus a workload; refresh its source module and verify permission/freshness. |
| Search misses a resource | Confirm it is in selected-connection cached inventory. |
| Path is absent | Expand nodes, unhide kinds, and confirm the relationship is modeled. |
| Graph is crowded/slow | Focus fewer workloads, collapse findings, hide kinds, or change layout. |
| Export is incomplete | Export uses current visible state; reveal and fit required nodes first. |

## Related docs

- [Estate Graph reference]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/)
- [Architectures recipes]({{ site.baseurl }}/how-to/design-assessment/architectures-know-me/)

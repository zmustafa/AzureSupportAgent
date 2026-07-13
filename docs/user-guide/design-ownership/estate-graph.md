---
layout: default
title: Estate Graph
parent: Design & Ownership
grand_parent: User guide
nav_order: 5
description: Explore estate relationships, paths, blast radius, overlays, drift, and saved views.
permalink: /user-guide/design-ownership/estate-graph/
---

# Estate Graph

## Purpose

Estate Graph combines workloads, architectures, cached inventory, findings, and operational overlays into an interactive relationship map. Use it to navigate dependencies, inspect a node, compare paths, and estimate blast radius without starting a live estate-wide Azure scan.

**Application routes:** `/graph` and `/graph/:focusId`.

![Estate Graph showing connected workload and resource nodes]({{ site.baseurl }}/assets/estate-graph.png)

## Common use cases

- Find a workload, resource, subscription, or architecture and inspect its context.
- Visualize multiple workloads and shared dependencies.
- Find a shortest relationship path between two nodes.
- Estimate direct and indirect blast radius from a selected node.
- Overlay cost, coverage, retirement, access, and change signals.
- Save a useful filtered view for repeated investigations.

## Prerequisites, permissions, and data

- `graph.read` is required; deployments may restrict this permission to administrators.
- Workload definitions, architecture records, and cached inventory populate core nodes and edges.
- Findings and overlays depend on available assessment, cost, monitoring coverage, Retirement Radar, RBAC, and Change Explorer data.
- Select the correct Azure connection before interpreting tenant-specific scope.
- Because graph assembly is cache-oriented, refresh the source feature rather than expecting the graph to perform a live scan.

## Controls and actions

### Explore mode

Search for nodes, select one to inspect, double-select to expand where supported, or use the context menu to focus and show related data. The left controls select workloads and node kinds. Pan, zoom, fit, hide/show node kinds, and switch layout or visual lens as needed.

### Path mode

Choose a source and target. The graph highlights the computed path through currently supplied nodes and edges. No path means no relationship is present in the assembled graph; it does not prove that no real-world dependency exists.

### Blast mode

Choose a source node to highlight direct and indirect impacted nodes up to the configured depth. The result reports impacted nodes and workloads. Treat this as topology analysis, not a failure simulation.

### Focus, overlays, and drift

Focus a workload before applying overlays or drift. Available overlays are:

- **Cost** — cached cost context;
- **Coverage** — monitoring or related coverage gaps;
- **Retirements** — relevant retirement records;
- **Access** — RBAC/access context;
- **Changes** — recent cached Change Explorer records.

Drift and compare views depend on available snapshots. An empty overlay may mean no findings, no supported data, stale cache, or insufficient permission.

### Additional tools

Use node details, analytics, AI narrative or ask features where enabled, undo/redo for structural view changes, and saved views for a reusable combination of focus, lens, layout, hidden kinds, and overlays. Keyboard shortcuts shown in the UI include fit, blast, search, and clear.

## Workflow

1. Select the correct Azure connection and load the overview.
2. Search for a workload and focus it.
3. Expand relevant dependencies and inspect node details.
4. Enable one overlay at a time and note its source freshness.
5. Use **Path** to test a dependency question or **Blast** to estimate reach.
6. Compare the graph with the reviewed architecture and source-module records.
7. Save the view or export a snapshot for discussion.

## Interpret results

Node and edge presence means that the relationship was assembled from application records or cached evidence. It does not establish live traffic, current health, or causality. Centrality and blast-radius counts indicate graph structure, not business criticality. AI narrative summarizes the supplied graph and may miss hidden or uncached dependencies.

## Exports, history, and integrations

- Export the current visible graph as high-resolution PNG.
- Export current nodes and edges as JSON for analysis or evidence handling.
- Save named views and delete obsolete ones.
- Local undo/redo covers visual structural changes during the session; source history remains in the contributing modules.
- The graph integrates with [Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/), [Ownership]({{ site.baseurl }}/user-guide/design-ownership/ownership/), [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/), cached inventory, coverage, retirement, RBAC, cost, and changes.

## Safety and limitations

- Estate Graph is primarily cache-backed and can be stale or incomplete.
- Hidden nodes and workload filters change path, centrality, and blast results.
- The blast-radius tool does not model redundancy, failover, traffic routing, or application behavior.
- AI narrative is advisory and should be checked against node details and source modules.
- Exported images and JSON can reveal topology and resource identifiers; store them securely.
- Saved views preserve presentation choices, not a frozen evidence snapshot.

## Troubleshooting

| Symptom | Checks |
|---|---|
| Overview is empty | Confirm `graph.read`, connection, workload registry, and inventory cache. |
| Overlay has no nodes | Refresh its source module, verify permission and focus scope, and check source freshness. |
| Search misses a resource | Confirm it is inside cached inventory and the selected connection; try workload or resource name. |
| No path is found | Expand relevant nodes, unhide node kinds, and verify the relationship exists in architecture/inventory. |
| Graph is crowded | Focus fewer workloads, hide irrelevant kinds, collapse findings, or change layout. |
| Export differs from expectation | Export uses the current visible canvas/filter state; fit and reveal required nodes first. |

## Related docs

- [Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/)
- [Ownership]({{ site.baseurl }}/user-guide/design-ownership/ownership/)
- [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/)
- [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/)

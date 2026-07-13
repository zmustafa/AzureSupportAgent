---
layout: default
title: Architectures
parent: Design & Ownership
grand_parent: User guide
nav_order: 2
description: Draw, reverse-engineer, review, and maintain workload architecture diagrams.
permalink: /user-guide/design-ownership/architectures/
---

# Architectures

## Purpose

Architectures provide a visual model of workload components and relationships. Build a diagram manually or reverse-engineer a starting point from a workload's Azure resources, then refine it into reviewed design context for investigations, Know-Me documentation, FMEA, and Estate Graph.

**Application routes:** `/architectures`, `/architectures/:id`, `/architectures/:id/memory`, and `/architectures/memory`.

![Architecture designer showing a workload diagram and editing controls]({{ site.baseurl }}/assets/architecture-designer.png)

## Common use cases

- Document an existing workload for design review or support handoff.
- Reverse-engineer a resource inventory into a first-pass diagram.
- Compare intended relationships with current inventory and drift indicators.
- Maintain separate diagrams in collections for teams, environments, or domains.
- Provide grounding for Know-Me, FMEA, investigations, and Estate Graph.

## Prerequisites, permissions, and data

- `architectures.read` is required to browse diagrams, generation jobs, collections, memory, and revisions.
- `architectures.write` is required to create, edit, generate, organize, delete, restore, or purge content.
- Reverse-engineering requires a workload with accessible resource inventory and an Azure connection able to query its scope.
- AI-assisted generation requires a configured AI provider.
- Resource relationships are inferred from inventory and available metadata; some application-level or external dependencies cannot be discovered automatically.

## Registry, collections, and jobs

The architecture registry lists diagrams and supports search, collection organization, generation jobs, and Trash. Use collections to group diagrams without changing their workload scope. Generation jobs continue independently of the page; review job progress, cancel a running job when appropriate, or dismiss completed job records.

Deleting a diagram moves it to Trash. Restore it if it was removed accidentally; purge and **Empty Trash** are permanent.

## Canvas tabs and actions

The canvas is a visual editor for nodes, edges, groups, labels, and layout. Available actions include:

- add Azure or generic components from the catalog;
- drag, select, multi-select, connect, duplicate, and delete elements;
- edit component metadata and relationship kinds;
- use automatic layout and fit/zoom controls;
- undo or redo local canvas changes;
- run design lint checks and inspect drift where available;
- open resource-specific network or DNS diagnostic actions where supported;
- save the diagram and open its **Memory**.

Treat generated nodes and edges as proposals. Verify identity, direction, and dependency meaning against deployment definitions and service owners before publishing the diagram.

## Workflows

### Create manually

1. Open `/architectures` and create a diagram.
2. Name it, link the correct workload, and optionally choose a collection.
3. Add components and connect them with meaningful relationship types.
4. Group or arrange components by tier, region, environment, or trust boundary.
5. Run lint checks, resolve obvious gaps, and save.
6. Ask a workload owner to review the result.

### Reverse-engineer from Azure

1. Confirm the workload inventory and connection scope.
2. Start an architecture generation job for the workload.
3. Monitor progress; generation may continue if you navigate away.
4. Open the generated diagram and compare every resource with inventory.
5. Remove noise, add undiscoverable external systems, correct edges, and annotate intent.
6. Save the reviewed diagram, then create or refresh its [Know-Me document]({{ site.baseurl }}/user-guide/design-ownership/know-me/).

## Interpret the result

A node represents a modeled component, not necessarily a one-to-one Azure resource. An edge represents a documented or inferred relationship, not proof of live traffic. Lint findings are design prompts, while drift indicators compare available snapshots and may lag current Azure state. A diagram is support-ready only after scope, freshness, external dependencies, and ownership have been reviewed.

## Exports, history, and integrations

Use the canvas export menu for the formats exposed by the current view, including image or structured diagram exports where available. Exported diagrams are snapshots; they do not update when the source diagram changes.

Architectures integrate with:

- [Know-Me]({{ site.baseurl }}/user-guide/design-ownership/know-me/) and architecture memory;
- [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/) generation;
- [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/) nodes and relationships;
- workload detail and deep-investigation handoffs;
- collections, generation-job history, soft-delete Trash, and memory revision history.

## Safety and limitations

- Reverse-engineering is read-only, but it can be incomplete because Azure metadata does not expose every runtime or business dependency.
- AI-generated structure may be incorrect. Never use it as the sole basis for migration, outage, security, or network decisions.
- Local undo/redo is not a substitute for saved history; save deliberate milestones.
- Purging a diagram is irreversible and can remove context expected by downstream documents.
- Exported diagrams may reveal resource names, topology, or security boundaries; classify and distribute them appropriately.

## Troubleshooting

| Symptom | Checks |
|---|---|
| Workload has no resources | Refresh inventory, verify workload scope, and confirm connection permissions. |
| Generation is slow | Check the generation job rather than restarting repeatedly; large workloads take longer. |
| Missing dependencies | Add application, SaaS, on-premises, or manually configured edges that Azure inventory cannot infer. |
| Save conflict or stale canvas | Reload the latest diagram before reapplying edits; avoid editing the same diagram in multiple tabs. |
| Diagram differs from Estate Graph | Compare source freshness and scope; the graph also combines cached inventory and other records. |
| Memory is stale | Open Memory and regenerate after reviewing the updated diagram. |

## Related docs

- [Know-Me]({{ site.baseurl }}/user-guide/design-ownership/know-me/)
- [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/)
- [Ownership]({{ site.baseurl }}/user-guide/design-ownership/ownership/)
- [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/)

---
layout: default
title: Inventory
parent: Estate Intelligence
grand_parent: User guide
nav_order: 1
description: Search and export Azure resources, understand distribution and cost, identify optimization candidates, and capture inventory snapshots.
permalink: /user-guide/estate-intelligence/inventory/
feature_ids: [PROACTIVE_NAV:inventory, INVENTORY_NAV:changes, INVENTORY_NAV:cost, INVENTORY_NAV:grid, INVENTORY_NAV:location, INVENTORY_NAV:optimization, INVENTORY_NAV:overview]
---

# Inventory

**Product permission:** `inventory.read` (the current API is admin-gated).

## Purpose

**App routes:** `/inventory` and `/inventory/:tab`
Inventory creates a normalized Resource Graph view across the selected tenant, management group, subscription, or workload context. It enriches resources with workload attribution, tags, hygiene flags, optional cost, and point-in-time snapshots.

## Prerequisites and data sources

### Prerequisites

- An ARM/Resource Graph-capable connection with Reader access across the intended scope.
- Product access to Inventory.
- Workload definitions when using workload attribution/filtering.
- Cost Management Reader at the relevant billing/subscription scope for cost data; Inventory still works without cost.

## Tabs and actions

### Tabs

- **Grid**: searchable resource table with cascading facets, density and column controls, grouping, natural-language search, row details, and CSV export.
- **Overview**: estate KPIs, resource/type/workload summaries, and snapshot controls.
- **Location**: interactive geographic distribution; select a region to focus the estate.
- **Cost**: trailing-period per-resource cost and rollups when Cost Management data is available.
- **Optimization**: cached analysis of unattached disks, orphaned NICs, idle public IPs, and associated cost opportunities.
- **Changes**: differences between Inventory snapshots. This is snapshot drift, not the Azure Activity Log; use Change Explorer for actor/time forensics.

## Freshness and scope behavior

### Refresh and data freshness

The main inventory cache is persistent per tenant, connection, and scope and has no automatic TTL. A normal page visit is cache-only; an empty state indicates that the scope has never been loaded. **Refresh** forces Resource Graph collection and replaces the saved estate for that key.

The frontend can retain query results while navigating, and cost is cached separately. Refreshing inventory does not necessarily refresh cost. Optimization reads the current cached inventory rather than launching a new Azure scan. Large estates are protected by response and capture limits; heed truncation indicators.

## Workflow overview

### Find and export resources

1. Open `/inventory` and select the correct connection/scope.
2. Refresh if the snapshot predates the decision or is absent.
3. On **Grid**, combine text search with workload, type, location, subscription, resource group, tag, SKU, and hygiene facets.
4. Optionally use natural language. Review the generated structured filter/query and its matched set; AI interpretation can be imperfect.
5. Group or choose columns for analysis.
6. Open a row to inspect governance, findings, cost, and workload attribution available for that resource.
7. Export CSV. Export contains the current visible/filtered rows, so record filters and timestamp with the evidence.

### Other workflows

### Capture and compare snapshots

1. Refresh the intended scope.
2. In **Overview**, capture a snapshot.
3. After a change window, refresh and capture another snapshot.
4. Use **Changes** to inspect additions, removals, and changed normalized fields.

Snapshots are local application records and can be deleted. They are not immutable audit evidence and do not identify the actor.

### Review optimization candidates

1. Open **Optimization** after a recent inventory refresh.
2. Validate the hygiene signal and dependency/ownership context.
3. Check activity, locks, backups, and actual cost in Azure.
4. Route the candidate through the normal change process. Inventory does not delete or resize resources.

## Interpretation of results

### Interpret results

- **Untagged** means no tags were observed.
- **Unattached disk**, **orphaned NIC**, and **idle public IP** are heuristic cleanup flags, not deletion authorization.
- Missing cost usually means unavailable permissions, unmapped charge data, or a cache that has not been loaded—not zero spend.
- Workload attribution follows local workload definitions and can overlap or be absent.
- Snapshot changes show differences in observed normalized state, not every intermediate Azure event.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

- Inventory is read-only; it does not apply tags or perform cleanup.
- The full estate is processed server-side and much filtering is client-side, so very large results can be slow or truncated.
- The grid is paged in chunks rather than fully virtualized; narrow facets before browsing huge estates.
- Resource Graph is eventually consistent and does not expose every data-plane object/property.
- Natural-language search is assistive. Verify scope and filters before exporting conclusions.
- Cost values are trailing/processed billing data and may lag current usage.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| Never loaded | Select an ARM-capable connection and use Refresh. |
| Resource is missing | Verify scope, Reader assignment, provider/resource visibility in Resource Graph, filters, and truncation. |
| Cost is blank | Load cost data and verify Cost Management Reader and billing availability. |
| Optimization result is stale | Refresh Inventory first; Optimization uses cached inventory. |
| Workload filter is wrong | Review workload scope definitions and overlaps. |
| Grid is slow | Narrow by subscription/type first and reduce displayed groups/columns. |

## Related pages

- [Tag Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/)
- [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

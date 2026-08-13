---
layout: default
title: Inventory
parent: Estate Intelligence
grand_parent: User guide
nav_order: 1
description: Search and export Azure resources, understand distribution and cost, identify optimization candidates, and capture inventory snapshots.
permalink: /user-guide/estate-intelligence/inventory/
feature_ids: [PROACTIVE_NAV:inventory, ROUTE:inventory, INVENTORY_NAV:changes, INVENTORY_NAV:cost, INVENTORY_NAV:grid, INVENTORY_NAV:location, INVENTORY_NAV:optimization, INVENTORY_NAV:overview]
---

# Inventory

**App routes:** `/inventory` and `/inventory/:tab`

**Product permission:** `inventory.read` (administrators also pass the permission guard).

## Purpose

Inventory creates a normalized Resource Graph view for the selected Azure tenant connection. It enriches resources with workload attribution, tags, hygiene flags, optional cost, and point-in-time snapshots. The backend contracts support narrower scope keys, but the current Inventory connection picker selects a tenant connection and sends the default whole-connection scope.

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
- **Cost**: trailing-30-day actual cost by resource, workload, type, region, subscription, and resource group, with a detached refresh job and per-subscription progress.
- **Optimization**: cached analysis of unattached disks, orphaned NICs, idle public IPs, and associated cost opportunities.
- **Changes**: differences between Inventory snapshots. This is snapshot drift, not the Azure Activity Log; use Change Explorer for actor/time forensics.

## Freshness and scope behavior

### Refresh and data freshness

The main inventory cache is persistent per tenant, connection, and scope and has no automatic TTL. A normal page visit is cache-only; an empty state indicates that the scope has never been loaded. **Refresh** forces Resource Graph collection and replaces the saved estate for that key.

The frontend can retain query results while navigating, and cost is cached separately. Refreshing inventory does not refresh cost. Optimization reads the current cached inventory rather than launching a new Azure scan. Inventory responses are capped at 100,000 resource rows; per-subscription Resource Graph collection can also report truncation at 1,000. Facets and summary can therefore describe more resources than the returned grid contains.

### Cost refresh lifecycle

Selecting **Load cost** or **Refresh cost** sends `POST /inventory/cost/refresh`. The API returns `202 Accepted` with a server-owned job; repeated starts for the same tenant, connection, and normalized scope reattach to the active job instead of starting a duplicate. The Cost tab calls `GET /inventory/cost/refresh/status` every second while the job is `queued` or `running`, and also refetches on window focus.

After the start request has been accepted, switching tabs or routes, unmounting Inventory, closing the page, or making a hard navigation does not cancel the server task. While it is active, the progress card explicitly says **Safe to navigate away — this refresh is owned by the server and continues in the background.** Unmounting stops that component's polling. A hard navigation before the POST has reached the server and returned acceptance is not guaranteed to start or confirm a job. Returning to the Cost tab asks for the newest retained job for the same tenant, connection, and scope and resumes polling if it is still active.

The progress card shows completed/total subscriptions, succeeded/failed totals, percentage, elapsed time, active subscriptions, current attempt and retry delay, and subscriptions omitted by the 25-subscription safety cap. Recent per-subscription completion, retry, and error outcomes include returned row counts and duration when the event provides them. Queries run with concurrency four. Azure Cost Management throttling is retried up to four total attempts, with 2-, 6-, and 10-second waits before attempts two, three, and four. An omitted subscription makes the selected estate incomplete even when every queried subscription succeeds. Terminal cards distinguish complete, partial, and failed results.

Terminal job snapshots are in process memory for one hour and are pruned when the manager is accessed. They do not survive an application restart. Shutdown is the only owner-driven cancellation path: it marks the in-memory job interrupted/failed, but there is no restart recovery because job state is not persisted.

The terminal `InventoryCost` result for a succeeded or partial job is placed in the frontend's shared `inventoryCost` query cache, so Grid, Cost, Optimization, and the resource drawer can use the same result during that browser session. The server's file cache is separate: complete, available results with no subscription errors are saved indefinitely in `backend/.data/inventory_cost_cache.json`, keyed by tenant, connection, and normalized scope. Partial or unavailable refresh results are returned to the UI but are not allowed to replace that permanent cache. A normal cached-only page load never launches Cost Management queries.

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

- Grid exports the current filtered/sorted rows or selected rows as browser-generated CSV.
- Changes stores local inventory snapshots and allows local snapshot deletion; it is not Azure Activity Log history.
- Cost refresh history is limited to the newest in-memory job snapshot retained for one hour; it is not a durable run ledger.
- Inventory has no scheduling control. Cost and inventory refreshes are explicit user actions.

## Safety and limitations

- Resource collection, cost collection, natural-language filtering, exports, optimization analysis, and snapshots do not mutate Azure.
- The resource drawer can write an owner tag to Azure when ownership write-back is enabled. It presents a confirmation and calls the gated ownership write-back API; verify the resolved owner and prepare the prior tag value before confirming.
- Optimization never deletes a resource. It can copy an `az resource delete` command, which becomes destructive only if a user runs it outside the app.
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
| Cost refresh disappears after returning to Inventory | Select the same tenant connection and Cost tab. The status endpoint reattaches only to the newest retained job for the same tenant, connection, and scope; terminal progress expires after one hour and all job telemetry is lost on restart. |
| Cost progress shows subscriptions omitted | The refresh queried only the first 25 visible subscriptions. Treat totals as incomplete and use a narrower backend scope or separate connection; the current Inventory UI does not expose a narrower subscription/management-group scope picker. |
| Cost ends partial | Open recent subscription outcomes. Failed/throttled subscriptions remain in `errors`; partial data is shown for inspection but does not overwrite the permanent cost file cache. Refresh later after access or throttling is resolved. |
| Cost is blank | Load cost data and verify Cost Management Reader and billing availability. An unavailable result is not zero spend. |
| Optimization result is stale | Refresh Inventory first; Optimization uses cached inventory. |
| Workload filter is wrong | Review workload scope definitions and overlaps. |
| Grid is slow | Narrow by subscription/type first and reduce displayed groups/columns. |

## Related pages

- [Tag Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/)
- [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

---
layout: default
title: Operate Inventory
parent: Estate intelligence operations
grand_parent: How-to guides
nav_order: 11
description: Refresh, search, filter, export, map, cost, optimize, and compare Azure inventory.
permalink: /how-to/estate-intelligence/inventory/
---

# Operate Inventory

## Prerequisites

- Product permission `inventory.read`.
- ARM and Azure Resource Graph Reader access across the intended scope.
- Workload definitions for workload attribution.
- Cost Management Reader for the optional Cost tab.

## Route

Open `/inventory` or a tab route: `/inventory/grid`, `/inventory/overview`, `/inventory/location`, `/inventory/cost`, `/inventory/optimization`, or `/inventory/changes`.

## How to refresh inventory and recognize stale or partial data

1. Select the connection and intended scope.
2. Check **Updated**. A red stale marker appears when the saved inventory is more than six hours old.
3. Select **Refresh**. First load never scans automatically.
4. Wait for Resource Graph collection; the cache key is tenant, connection, and scope and remains until refreshed again.
5. Review truncation and inaccessible-subscription warnings before interpreting totals.

**Expected result:** Resources, subscriptions, types, locations, resource groups, tags, SKUs, hygiene flags, and workload attribution populate.

**Verification:** Match the timestamp, selected connection/scope, total count, and partial warnings with Azure Resource Graph visibility.

## How to search, filter, group, inspect, and export the Grid tab

1. Open **Grid**.
2. Combine text search with workload, type, location, subscription, resource group, tag, SKU, and hygiene facets.
3. Use natural language when useful, but review the generated structured filter/query and matched set.
4. Choose density, columns, sorting, or grouping.
5. Open a resource to inspect available governance, assessment findings, cost, and workload attribution.
6. Select individual rows for **Export selection**, or select **Export** for the current filtered/sorted view.
7. Preserve the CSV with the displayed filters and collection timestamp.

**Expected result:** The CSV contains only the current visible/selected resource set.

**Verification:** The export toast reports the row count; compare it with the selected or filtered count and inspect a sample of rows.

## How to use the Overview tab

1. Open **Overview** after a current refresh.
2. Review resource, type, subscription, and workload summaries.
3. Use the summary to identify unexpected concentration or unattributed resources.
4. Move to Grid with matching facets for resource-level inspection.

**Expected result:** Estate KPIs summarize the same cached resource set as Grid.

**Verification:** Cross-check total resources and selected breakdowns against Grid filters.

## How to use the Location tab

1. Open **Location**.
2. Hover or select a region to inspect resource distribution.
3. Compare region concentration with workload and resource-type context.
4. Return to Grid and apply a location facet for the exact rows.

**Expected result:** The map highlights observed Azure locations and their resource counts.

**Verification:** Compare a selected region's count with the same Grid location filter. Nonregional/global resources may not map geographically.

## How to load and analyze the Cost tab

1. Open `/inventory/cost` for the intended tenant connection. Cost has a separate cache and does not refresh with inventory.
2. Select **Load cost** when no cost has been collected, or **Refresh cost** to submit `POST /inventory/cost/refresh`. Wait until the start is accepted before a hard navigation if confirmation matters.
3. While the job is queued or running, inspect the card that the page refreshes from `GET /inventory/cost/refresh/status` every second. Review completed/total subscriptions, succeeded/failed totals, percentage, elapsed time, active subscriptions, current attempt/retry delay, and recent success/error/retry outcomes with row counts and duration where available.
4. Check **omitted**. One refresh queries at most 25 subscriptions with concurrency four. Omitted subscriptions make totals incomplete even if the job says succeeded.
5. When the active card says **Safe to navigate away — this refresh is owned by the server and continues in the background**, navigate elsewhere if needed. The accepted job continues; leaving Inventory only stops this component's polling. Return with the same tenant connection to reattach to the newest retained tenant+connection+scope job.
6. Review total and rollups by subscription, type, location, resource group, or workload. A succeeded or partial terminal result is copied into the shared frontend `inventoryCost` cache.
7. Check the terminal complete, partial, or failed state, unallocated values, subscription errors, row counts, and durations. A partial result is displayed but is not written over the permanent server cache; only a complete available result is persisted without a TTL.
8. Refresh cost when it predates the inventory or decision. Remember that terminal progress is retained in memory for one hour, is lost on application restart, and an interrupted shutdown job is not recovered.

**Expected result:** Available trailing-30-day cost is mapped to resources and rollups, with queried, failed, and omitted subscription outcomes visible rather than represented as zero.

**Verification:** Check currency, period, fetched time, 25-subscription cap/omitted count, succeeded/failed count, and a sample against Cost Management. Reopen the same route to confirm terminal data appears from the shared cache or newest retained job.

## How to review Optimization candidates safely

1. Refresh Inventory, then open **Optimization**; this tab is cache-only.
2. Review unattached disks, orphaned NICs, idle public IPs, and estimated savings.
3. Open each resource and validate owner, workload, dependencies, activity, locks, backups, and actual cost in Azure.
4. Route a reviewed candidate through the normal change process.

**Expected result:** Heuristic candidates are prioritized without deleting or resizing anything.

**Verification:** Confirm the condition in Azure and obtain owner approval. A heuristic flag is not deletion authorization.

## How to capture and compare snapshots on Changes

1. Refresh the intended scope.
2. Open **Changes** and select **Take snapshot**.
3. After the change window, refresh and take another snapshot.
4. Review added, removed, and changed normalized fields against the baseline.
5. Delete obsolete snapshots only when they are not needed for comparison.

**Expected result:** Snapshot drift shows differences between two observed states.

**Verification:** Validate a sample in Azure or Change Explorer. Inventory snapshots do not identify actors or intermediate events.

## Safety and rollback

- Inventory and cost collection are read-only. The owner drawer is the exception: when ownership write-back is enabled, confirming **Write owner tag to Azure** mutates the resource tag. Record the previous value for rollback.
- Resource Graph is eventually consistent and does not expose every data-plane object.
- Cost data lags usage and may be partial.
- Snapshot deletion removes local comparison data; preserve required evidence elsewhere.
- Use Tag Intelligence for controlled tag writes and Change Explorer for actor/time evidence.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Never loaded | Select an ARM-capable connection and select **Refresh inventory**. |
| Resource missing | Check scope, Reader role, Resource Graph visibility, filters, and truncation. |
| Cost refresh no longer appears | Return with the same tenant connection. Terminal job telemetry expires after one hour and is lost on application restart; the permanent complete cost payload can still load independently. |
| Cost reports omitted subscriptions | The 25-subscription cap was reached. Treat totals as partial and use a narrower backend scope or separate connection; the current Inventory page selects only the tenant connection. |
| Cost refresh is partial | Inspect failed subscription outcomes, correct Cost Management Reader access or wait for throttling to clear, then refresh. Partial results do not replace the permanent file cache. |
| Cost blank | Load/refresh cost and verify Cost Management Reader and billing availability. Do not interpret unavailable cost as zero. |
| Optimization stale | Refresh Inventory first. |
| Workload attribution wrong | Review workload definitions and overlaps. |
| Grid slow | Narrow by subscription/type and reduce grouping/columns. |

## Related docs

- [Inventory reference]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/)
- [Tag Intelligence recipes]({{ site.baseurl }}/how-to/estate-intelligence/tag-intelligence/)
- [Change Explorer recipes]({{ site.baseurl }}/how-to/estate-intelligence/change-explorer/)

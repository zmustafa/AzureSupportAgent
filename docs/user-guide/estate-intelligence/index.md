---
layout: default
title: Estate Intelligence
description: Explore Azure resources, govern tags, and investigate change history across workloads and subscriptions.
parent: User guide
nav_order: 5
permalink: /user-guide/estate-intelligence/
has_children: true
---

# Estate Intelligence

Estate Intelligence turns Azure inventory and change evidence into searchable operational views.

{% include screenshot.html file="estate-inventory-grid.png" title="Estate orientation — inventory rows with workload and location context" caption="The synthetic tenant inventory separates production, development, and unassigned resources, with type, resource-group, location, subscription, and workload columns. Use Inventory to establish the resource set, then Tag Intelligence or Change Explorer for their distinct evidence. Grid filters are not proof that the underlying collection scope was narrowed; no live collection is shown." %}

| Guide | Use it to |
| --- | --- |
| [Inventory]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/) | Search/export resources, inspect access/location/cost, find optimization candidates, and compare current cached inventory with a saved baseline. |
| [Tag Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/) | Audit tag census, hygiene, required-tag coverage, cost allocation, drift, policy, and governed remediation. |
| [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/) | Analyze activity over a time window by operation, actor, risk, resource, technical diff, and dependency impact. |

## Recommended sequence

1. Refresh [Inventory]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/) for the intended connection and scope.
2. Use [Tag Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/) to quantify and safely correct metadata gaps.
3. Use [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/) for a bounded forensic window when investigating drift or incidents.

Always verify generated time, selected scope, truncation indicators, and optional data-source permissions before treating the result as complete.

## Choose evidence and permissions deliberately

| Area | Application access and important boundary |
| --- | --- |
| Inventory | `inventory.read` includes cost and snapshot creation/deletion. Grid facets do not narrow the collection request. The Access drawer additionally needs `iam.read`. |
| Tag Intelligence | `tagintel.read` includes preview, scripts, policy and AI proposals; `tagintel.write` controls saved metadata changes and Azure apply/revert. Review the documented target-list and recovery limitations before approval. |
| Change Explorer | `changeexplorer.read` includes analysis, annotations, exports and destructive local-history cleanup. It is not admin-only or read-only access to saved records. |

Azure Reader/billing/directory/tag-write rights are separate from these application capabilities. Administrator access to the app does not grant the connection Azure permissions.

Inventory baselines compare only a small resource fingerprint; Tag Drift compares captured tag state; Change Explorer compares retained event evidence. None is an immutable, unlimited audit archive. Inventory snapshot storage is server-side, whereas display preferences and saved Change Explorer filter views are browser-local. Cost and Change Explorer Fleet jobs are database-backed durable batches; a Tag refresh or a single Explorer stream does not offer the same restart/reload guarantees.

## Write and AI boundaries

- Inventory's owner-tag action is an Azure write through `ownership.write`, despite the otherwise read-only inventory workflow. Its UI confirmation is not a server read-only-setting check or a saved recovery revision.
- Tag apply requires explicit approval and checks connection read-only/command execution; revert follows a different ARM path that does not enforce those same controls. Do not assume the chat execution mode governs either endpoint.
- Change Explorer does not execute rollback hints, but opening an unanalyzed event can invoke AI even when the initial analysis checkbox was off. Its exports use the saved run, not visible filters, and can contain sensitive evidence.

Use the [estate operation recipes]({{ site.baseurl }}/how-to/estate-intelligence/) for verification and recovery steps before acting on these views.

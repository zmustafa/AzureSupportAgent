---
layout: default
title: Estate intelligence operations
parent: How-to guides
nav_order: 2
description: Task recipes for resource inventory, tag governance, and change forensics.
permalink: /how-to/estate-intelligence/
has_children: true
---

# Estate intelligence operations

Use these recipes to build a current estate view, govern tags with preview and recovery, and investigate Azure changes with preserved evidence.

## Prerequisites

- An enabled Azure connection with Reader access to the intended scope.
- The product permission named in the selected guide.
- A current Inventory snapshot before dependent tag analysis.

## Route

Open the feature route listed in the selected guide.

## How to choose the right estate workflow

1. Use [Inventory]({{ site.baseurl }}/how-to/estate-intelligence/inventory/) to search, filter, export, map, cost, optimize, and compare resource snapshots.
2. Use [Tag Intelligence]({{ site.baseurl }}/how-to/estate-intelligence/tag-intelligence/) to audit every tag tab and perform explicit, recoverable apply/revert workflows.
3. Use [Change Explorer]({{ site.baseurl }}/how-to/estate-intelligence/change-explorer/) for actor/time forensics, technical diffs, dependency impact, comparison, evidence, and reports.

**Expected result:** Snapshot drift is handled in Inventory, tag-state drift in Tag Intelligence, and event/actor forensics in Change Explorer.

**Verification:** Confirm route, connection, scope, and timestamp before combining evidence across features.

## Safety and rollback

Inventory and Change Explorer do not mutate Azure. Tag Intelligence apply and revert require explicit approval and a writable authorized connection. Preserve current state before bulk tagging and avoid purging forensic runs required by retention policy.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Dependent feature is stale | Refresh Inventory first, then refresh the dependent feature for the same connection and scope. |
| Different views disagree | Compare timestamps, scopes, cache keys, source APIs, truncation, and eventual consistency. |
| A write is unavailable | Check product permission, read-only state, Azure RBAC, locks, and policy. |

## Related docs

- [Estate Intelligence feature reference]({{ site.baseurl }}/user-guide/estate-intelligence/)
- [Connection Capability]({{ site.baseurl }}/how-to/coverage/connection-capability/)

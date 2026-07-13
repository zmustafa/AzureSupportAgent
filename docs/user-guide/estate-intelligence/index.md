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

| Guide | Use it to |
| --- | --- |
| [Inventory]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/) | Search resources, inspect location and cost, find optimization candidates, and compare snapshots. |
| [Tag Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/) | Audit tag census, hygiene, required-tag coverage, cost allocation, drift, policy, and governed remediation. |
| [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/) | Analyze activity over a time window by operation, actor, risk, resource, technical diff, and dependency impact. |

## Recommended sequence

1. Refresh [Inventory]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/) for the intended connection and scope.
2. Use [Tag Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/) to quantify and safely correct metadata gaps.
3. Use [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/) for a bounded forensic window when investigating drift or incidents.

Always verify generated time, selected scope, truncation indicators, and optional data-source permissions before treating the result as complete.

---
layout: default
title: Use Proactive Support, Monitor, and Stats
parent: Core and workload operations
grand_parent: How-to guides
nav_order: 6
description: Navigate proactive tools and verify application health and summary statistics without triggering Azure writes.
permalink: /how-to/core-workloads/proactive-monitor-stats/
---

# Use Proactive Support, Monitor, and Stats

## Prerequisites

- An authenticated administrator for `/proactive` and `/stats`.
- `monitor.view` for `/monitor`.
- The destination feature's exact product permission before opening a Proactive Support card.

## Route

Use `/proactive`, `/monitor`, or `/stats`.

## How to choose a proactive workflow

1. Open `/proactive`.
2. Select the relevant group: daily intelligence, design and ownership, assessment and performance, coverage, estate intelligence, governance and identity, or lifecycle and investigation.
3. Read the card description and open the owning feature.
4. On the destination page, confirm connection, scope, freshness, and partial-result indicators before running or changing anything.

**Expected result:** The selected feature opens without a scan being started by the landing page.

**Verification:** Confirm the browser route and the destination title, then check its permission and freshness statement.

## How to inspect operational health

1. Open `/monitor`.
2. Review available runtime and activity indicators.
3. Note loading, failed, unavailable, or stale states rather than treating them as zero.
4. Follow a feature link or inspect the Audit Log when a durable explanation is required.

**Expected result:** Current application telemetry available to the signed-in role is displayed.

**Verification:** Compare the displayed update time or activity with the owning feature's latest run or history record.

## How to review read-only statistics

1. Open `/stats` as an administrator.
2. Review the at-a-glance counters.
3. Open the owning feature for scope, collection time, and evidence before drawing an operational conclusion.

**Expected result:** A read-only summary appears; no Azure operation is submitted.

**Verification:** Confirm no approval, apply, or mutation control is present on Stats and verify the source in the owning feature.

## Safety and rollback

These pages do not themselves mutate Azure. No rollback is needed for viewing or navigation. Destination features can create local records, artifacts, deliveries, or approval-gated Azure changes; follow their preview, approval, apply, verification, retry, and rollback instructions exactly.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Proactive Support or Stats is hidden | Sign in as an administrator. |
| Monitor returns forbidden | Assign a role containing `monitor.view`. |
| A destination is forbidden | Assign its exact product permission; landing-page visibility is not authorization. |
| A summary is blank | Run or refresh the owning feature if authorized, then check errors and partial-result warnings. |

## Related docs

- [Proactive Support, Monitor, and Stats reference]({{ site.baseurl }}/user-guide/core/proactive-monitor-stats/)
- [Dashboard]({{ site.baseurl }}/user-guide/core/dashboard/)
- [Permissions]({{ site.baseurl }}/reference/permissions/)

---
layout: default
title: Review reservations and renewal risk
parent: Lifecycle and investigation
grand_parent: How-to guides
nav_order: 2
description: Refresh reservation expiry and utilization, filter risks, export evidence, and preview digests.
permalink: /how-to/lifecycle-investigation/reservations-monitor/
---

# Review reservations and renewal risk

**Exact route:** `/reservations`.

![Reservations Monitor expiry and utilization view]({{ site.baseurl }}/usecase-assets/reservations.png)

## Prerequisites

- Product permission `reservations.read`.
- A default Azure connection whose identity can read tenant reservation orders.
- Approved connector and digest settings only when routing notifications.

## Route

**Exact route:** `/reservations`.

## How to review expiry, renewal, and utilization

1. Open `/reservations` and choose **Live** or synthetic **Demo**.

2. Select the intended connection and check snapshot age.
3. Select **Refresh** for current Azure state.
4. Use KPI tiles or filters for status, renewal mode, utilization, expiry window, and search.
5. Sort by days remaining, utilization, SKU, or region.
6. Inspect term, billing plan, quantity, scope, creation/expiry, auto-renew, and utilization.
7. Confirm financial ownership and current order state in Cost Management before action.

**Expected result:** A tenant-scoped list of expiring, recently expired, active, non-renewing, and low-utilization reservation candidates.

**Verification:** Spot-check order, expiry, renew state, quantity, and utilization in the authoritative billing view. Unavailable utilization is unknown, not zero.

## How to export a bounded reservation review

1. Apply intended filters and sorting.

2. Export the available CSV, JSON, HTML, or Markdown representation.
3. Open the output and confirm row count, filters, generated time, and tenant context.
4. Store the artifact as sensitive financial/operational data and remove it when no longer needed.

**Expected result:** A point-in-time report containing the currently selected reservation data.

**Verification:** Reconcile totals and one representative row with the UI and Azure.

## How to preview and route a digest

1. Open digest preview and review the HTML/text content.

2. Remove stale or duplicate items and verify expiry windows.
3. Configure daily/weekly schedule, time zone, recipients, or connector IDs in approved settings.
4. Send a test to a controlled destination before broad routing.

**Expected result:** A validated digest routed to intended recipients; preview alone does not purchase, renew, or cancel anything.

**Verification:** Confirm delivery and links in the destination, then compare the next scheduled digest with refreshed data.

## Safety and rollback

The monitor is Azure-read-only and cannot buy, exchange, renew, or cancel reservations. Exports and digests can disclose financial data. Correct routing by disabling/editing digest settings; Azure commercial decisions require a separate approved process and rollback feasibility depends on Microsoft's reservation terms.

### Freshness and partial results

The snapshot is cached and may lag exchanges, renewals, utilization, or billing changes until refresh. The time window is bounded. Reservation access is tenant/billing scoped, so subscription Reader alone may not be enough.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Live list is empty | Verify default connection and reservation-order permissions, then refresh. |
| Utilization is absent | Treat as unknown and verify product support/source data. |
| Values differ from billing portal | Align tenant, order, time window, and generated time. |
| Export misses rows | Clear unintended filters and confirm bounded window. |
| Digest does not arrive | Verify enabled state, schedule/time zone, recipients, and connector health. |

## Related docs

- [Reservations Monitor reference]({{ site.baseurl }}/user-guide/lifecycle-investigation/reservations-monitor/)
- [Notifications]({{ site.baseurl }}/user-guide/automations/notifications/)
- [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/)

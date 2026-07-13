---
layout: default
title: Reservations Monitor
parent: Lifecycle & Investigation
grand_parent: User guide
nav_order: 2
description: Monitor Azure reservation expiry, renewal posture, utilization, and digest routing.
permalink: /user-guide/lifecycle-investigation/reservations-monitor/
---

# Reservations Monitor

**Permission:** `reservations.read`

## Purpose

**App route:** `/reservations`
Reservations Monitor reads tenant-scoped Azure reservation data into a cache and highlights expiring, recently expired, non-renewing, and low-utilization orders. It does not buy, exchange, renew, or cancel reservations.

## Prerequisites and data sources

### Prerequisites

Use a default Azure connection whose identity can read reservation orders. Demo mode is synthetic and makes no Azure calls. Notification delivery additionally requires an enabled compatible connector and configured digest settings.

## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Workflow

1. Select **Live** or **Demo** and the intended connection.
2. Check snapshot age; use **Refresh** for current reservation state.
3. Filter status, renewal mode, utilization, or search by display name/SKU/scope.
4. Sort by days remaining, SKU, region, or utilization.
5. Review term, billing plan, quantity, creation/expiry dates, auto-renew state, utilization, and countdown.
6. Open the digest preview before enabling its schedule and recipients under General settings.

The main lanes are expiring soon, recently expired, active, and expired. Low utilization is a planning signal derived from available utilization data, not a recommendation to cancel.

## Interpretation of results



## Exports, history, scheduling, and integrations

### Exports

The UI can export the visible data to CSV and preview digest HTML/text. A digest can route to configured email recipients or connector IDs on a daily or weekly schedule. Keep recipients and destinations tenant-appropriate.

## Safety and limitations

### Safety

The feature is read-only. Validate financial decisions against Cost Management and reservation-owner records. A cached row may lag an exchange, renewal, or billing change until refresh.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Empty live list | Confirm the default connection can read reservations, then refresh. |
| Stale values | Refresh and compare generated time with the configured cache TTL. |
| Utilization unavailable | Azure may not return utilization for that product/order; do not infer zero. |
| Digest does not arrive | Verify digest enabled, schedule/time zone, recipients or connector IDs, and connector status. |

## Related pages

### Related docs

- [Notifications]({{ site.baseurl }}/user-guide/automations/notifications/)
- [General settings]({{ site.baseurl }}/admin/general-settings/)

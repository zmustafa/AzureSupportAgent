---
layout: default
title: Quota Monitor
parent: Lifecycle & Investigation
grand_parent: User guide
nav_order: 3
description: Scan subscription and regional quota usage, headroom, provider readiness, and throttling risk.
permalink: /user-guide/lifecycle-investigation/quota-monitor/
---

# Quota Monitor

**Permissions:** `quota.read`; `quota.run` to scan

## Purpose

**App route:** `/quota`
Quota Monitor uses modular collectors for compute, network, storage, App Service, SQL, Key Vault, Monitor, AI, governance, and throttling signals. Results are subscription-scoped and cached; scans are manual rather than scheduled.

## Prerequisites and data sources

### Prerequisites

- Select an Azure connection and subscription readable by the relevant quota, usage, Resource Graph, and Monitor APIs.
- Register required resource providers for the categories being scanned.
- Obtain `quota.run` for a fresh scan; read permission alone can view saved results.

## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Workflow

1. Choose a subscription.
2. Review saved risk distribution, generated time, and provider registration states.
3. Select bounded regions and categories, then start **Scan**.
4. Follow streamed collector progress; partial failures should be interpreted separately.
5. Filter by region, category, provider, risk, adjustability, or source.
6. Prioritize rows by usage percentage and remaining headroom, then follow the displayed recommendation.
7. Export CSV or JSON for capacity planning.

Default bands are configurable: Watch at 70%, Warning at 85%, and Critical at 95%. Administrators can change them, so use the metadata displayed for the run rather than assuming these values.

## Interpretation of results

### Interpret

A row shows service/quota, SKU family, region, usage, limit, remaining headroom, adjustability, source type, risk, and last checked. `Unknown` is not healthy. `Fixed`, `Manual`, or `NotSupported` describes the adjustment path, not business impact. The throttling lane reports observed API pressure separately from capacity quotas.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety

Scans are read-only, but can issue many Azure API calls. Keep region/category scope narrow and allow backoff when throttling appears. The monitor does not submit quota-increase requests.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Provider is not registered | Register the named namespace through an approved Azure process, then rescan. |
| Throttling observed | Stop repeated scans, allow recovery, and retry with fewer regions/categories. |
| Category has no rows | Check collector support, permissions, provider state, and source/remediation hint. |
| Scan button disabled | Another scan may be active or `quota.run` is missing. |
| Values differ from Portal | Confirm subscription/region/SKU and refresh both sources; APIs can expose different quota families. |

## Related pages

### Related docs

- [Permissions reference]({{ site.baseurl }}/reference/permissions/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

---
layout: default
title: Scan and investigate quota risk
parent: Lifecycle and investigation
grand_parent: How-to guides
nav_order: 3
description: Run bounded quota scans, interpret capacity and throttling results, export findings, and verify remediation.
permalink: /how-to/lifecycle-investigation/quota-monitor/
---

# Scan and investigate quota risk

**Exact route:** `/quota`.

## Prerequisites

- Product permissions `quota.read` and `quota.run` for a new scan.
- An Azure connection and subscription with access to relevant quota, usage, Resource Graph, and Monitor APIs.
- Required provider namespaces registered through an approved Azure process.

## Route

**Exact route:** `/quota`.

## How to run a bounded quota scan

1. Open `/quota`, select the connection and subscription, and inspect saved result age.

2. Choose only required regions and collector categories.
3. Select **Scan** and follow streamed region/provider/collector progress.
4. Do not start overlapping scans; the job can continue across navigation.
5. When complete, review collector errors, provider registration, throttling events, and generated time.

**Expected result:** A cached subscription snapshot with successful rows and explicit partial failures.

**Verification:** Confirm selected regions/categories ran and compare representative quota usage/limit with Azure. A failed collector does not invalidate successful collectors but does create a blind spot.

## How to prioritize capacity risk

1. Filter by region, provider, category, risk, adjustability, source, family, or usage range.

2. Sort by percent used and remaining headroom.
3. Open a row and inspect raw response, quota family, region, usage, limit, risk, adjustability, and recommendation.
4. Treat `Unknown` as unresolved and `ThrottlingObserved` as API-pressure evidence, not a capacity percentage.
5. Confirm business growth and deployment demand before requesting an increase.

**Expected result:** A source-checked list of capacity or throttling actions.

**Verification:** Recalculate percentage from usage/limit when both exist and verify the exact regional quota family in Azure.

## How to export and verify quota work

1. Apply the intended filters.

2. Export CSV or JSON and record scan time, subscription alias, regions, categories, and thresholds.
3. Create an approved capacity ticket without secrets or unnecessary subscription identifiers.
4. After an externally approved quota increase or workload change, run a bounded scan again.
5. Confirm new limit, headroom, and risk state.

**Expected result:** A reproducible planning artifact and verified follow-up.

**Verification:** The refreshed source row reflects the expected limit/usage; provider and collector errors are resolved or documented.

## Safety and rollback

Scans are read-only but can issue many Azure calls and trigger throttling. Narrow region/category scope and allow backoff. The app does not register providers or submit quota increases. Those writes occur externally; provider registration can have governance effects and quota increases may affect spend capacity, so use approvals. No scan rollback is required.

### Freshness and partial results

Results are cached. Risk thresholds are administrator-configurable, so read run metadata rather than assuming defaults. APIs expose different quota families and update times. Partial success is normal; unknown, absent, or failed rows must not be interpreted as healthy.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Scan button is disabled | Check `quota.run` and whether another scan is active. |
| Provider not registered | Use approved Azure registration, wait for propagation, and rescan. |
| Throttling appears | Stop repeated scans, allow recovery, and narrow scope. |
| Category has no rows | Check collector support, provider state, permission, region, and errors. |
| Portal differs | Match subscription, region, SKU/quota family, and refresh times. |

## Related docs

- [Quota Monitor reference]({{ site.baseurl }}/user-guide/lifecycle-investigation/quota-monitor/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
- [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/)

---
layout: default
title: Scan and investigate quota risk
parent: Lifecycle and investigation
grand_parent: How-to guides
nav_order: 3
description: Run bounded quota scans, interpret capacity and throttling results, export findings, and verify remediation.
permalink: /how-to/lifecycle-investigation/quota-monitor/
feature_ids: [PROACTIVE_NAV:quota, ROUTE:quota]
---

# Scan and investigate quota risk

## Route

Open `/quota`.

> **Screenshot context:** The native application example uses built-in **Demo data**, not a live subscription scan or Azure capacity evidence. Demo Azure writes are disabled. Portal escalation and command-copy actions were not invoked for the capture; displayed quota headroom is not guaranteed regional capacity.

## Prerequisites

- Product permissions `quota.read` and `quota.run` for a new scan.
- `workloads.read` for the UI subscription tree, which uses workload discovery rather than Quota's subscription-list endpoint.
- An Azure connection and subscription with access to relevant quota, usage, Resource Graph, and Monitor APIs.
- Required provider namespaces registered through an approved Azure process.

## How to run a bounded quota scan

1. Open `/quota`, select the connection and subscription, then select **Load** to inspect the saved quota snapshot and its age. For a safe offline quota walkthrough, enable **Demo data** instead; synthetic quota collection does not call Azure.

2. Choose only required regions and collector categories.
3. Set **Show unused** if zero-usage quota families matter, then select **Run scan** and follow streamed region/provider/collector progress.
4. Do not start overlapping scans. **Minimise** keeps collection running across in-app navigation, but a browser reload is not a resumable job handoff.
5. When complete, review collector errors, provider registration, throttling events, and generated time.

**Expected result:** A cached subscription snapshot with successful rows and explicit partial failures.

**Verification:** Confirm selected regions/categories ran and compare representative quota usage/limit with Azure. Check snapshot **Status** and row **Collection**, not only the completion message. A failed collector does not invalidate successful collectors but does create a blind spot.

## How to prioritize capacity risk

1. Filter by region, provider, category, risk, limit kind, VM families, text, or usage range. Adjustability and source are row/detail fields, not separate filter selectors.

2. Select the **Usage** or **Headroom** column heading to sort; select it again to reverse direction.
3. Open a row and inspect raw response, quota family, region, usage, limit, risk, adjustability, and recommendation.
4. Treat `Unknown` as unresolved unless the recommendation explains a by-design singleton/remaining-allowance counter. Treat `ThrottlingObserved` as API-pressure evidence, not a capacity percentage.
5. Confirm business growth and deployment demand before requesting an increase.

**Expected result:** A source-checked list of capacity or throttling actions.

**Verification:** Recalculate percentage from usage/limit when both exist and verify the exact regional quota family in Azure. The displayed percentage is capped at 100%; headroom is never shown below zero. An approved quota still does not guarantee available regional/SKU capacity.

{% include screenshot.html file="ops-quota-limit-detail.png" title="Verify a quota limit's source and collection status" caption="Inspect units, usage, limit, remaining capacity, collection status and recommendation before planning an increase. Unknown is not zero; portal and copy controls are handoffs, not an executed quota change." %}

## How to export and verify quota work

1. Apply the intended filters.

2. Choose **Export view** for filtered/sorted CSV, or **CSV**/**JSON** for the full cached snapshot. Record scan time, subscription alias, regions, categories, and the JSON snapshot's thresholds; the full export ignores table filters.
3. Create an approved capacity ticket in the appropriate external system without secrets or unnecessary subscription identifiers. The row drawer's **Request increase (Portal)** only opens the Quotas blade; copied commands/scopes are not executed by this app.
4. After an externally approved quota increase or workload change, run a bounded scan again.
5. Confirm new limit, headroom, and risk state.

**Expected result:** A reproducible planning artifact and verified follow-up.

**Verification:** The refreshed source row reflects the expected limit/usage; provider and collector errors are resolved or documented.

## How to recover after a partial scan or lost stream

1. Open the affected row's **Collection**, error, and recommendation. Resolve unauthorized, unregistered, or unsupported-provider conditions through the relevant Azure administrator; allow backoff after throttling.
2. If the progress stream disconnects or you selected **Cancel**, reopen the page and **Load**. Compare generated time and selected subscription before deciding that no result was saved.
3. Run a new bounded scan only after correcting the cause. There is no retry-failed-collectors button or durable resume control.
4. Review the latest 20 compact history points in **At-risk trend** when available. Compare equal scan scopes before interpreting “recovered”; changing categories/regions can remove an at-risk key without changing Azure.
5. Export important full snapshots before overwriting them. The cache is shared per application tenant/subscription across connections and scan subsets, and may be replaced by a completed error result. There is no restore/purge control for quota history on this page.

**Expected result:** Successful rows remain usable with explicit gaps, and a follow-up scan is initiated deliberately.

**Verification:** Confirm the new persisted time, source statuses, and representative raw values. A stopped browser stream or a green completion banner is not proof of complete collection or server-side cancellation.

## Safety and rollback

Scans are read-only but can issue many Azure calls and trigger throttling. Narrow region/category scope and allow backoff. The app does not register providers or submit quota increases. Those writes occur externally; provider registration can have governance effects and quota increases may affect spend capacity, so use approvals. No scan rollback is required.

### Freshness and partial results

Results are cached, with a default six-hour freshness interval rather than automatic expiry. Read snapshot threshold metadata rather than assuming defaults; quota tuning keys are not currently accepted by the General settings update schema. APIs expose different quota families and update times. Unknown, absent, or failed rows must not be interpreted as healthy.

## Troubleshooting

| Symptom | Cause and resolution |
| --- | --- |
| Run scan is disabled or forbidden | Select a subscription and check active progress. A forbidden API response requires `quota.run`; read/view/export access separately requires `quota.read`. |
| Provider not registered | Use approved Azure registration, wait for propagation, and rescan. |
| Throttling appears | Stop repeated scans, allow recovery, and narrow scope. |
| Category has no rows | Check collector support, provider state, permission, region, and errors. |
| Portal differs | Match subscription, region, SKU/quota family, and refresh times. |

## Related docs

- [Quota Monitor reference]({{ site.baseurl }}/user-guide/lifecycle-investigation/quota-monitor/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
- [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/)

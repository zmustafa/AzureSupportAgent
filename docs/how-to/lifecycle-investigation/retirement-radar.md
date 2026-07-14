---
layout: default
title: Triage lifecycle risk with Retirement Radar
parent: Lifecycle and investigation
grand_parent: How-to guides
nav_order: 1
description: Refresh retirement signals, map impact and ownership, track migration state, and create safe handoffs.
permalink: /how-to/lifecycle-investigation/retirement-radar/
---

# Triage lifecycle risk with Retirement Radar

**Exact route:** `/radar`.

![Retirement Radar event and workload view]({{ site.baseurl }}/assets/retirement-coverage.png)

## Prerequisites

- Product permission `radar.read` and the appropriate write permission for state changes when your role model separates them.
- An Azure connection that can read Service Health, Advisor, and inventory at the selected scope.
- Current workload ownership; AI only for runbook drafting; Jira/ServiceNow only for ticketing.

## Route

**Exact route:** `/radar`.

## How to refresh and prioritize retirement events

1. Open `/radar` and choose **Workload** or **Subscription** scope plus connection.

2. Check generated time, cache age, and `never loaded` state.
3. Select **Refresh** when the decision requires live Service Health and Advisor signals.
4. Filter retirement/breaking-change type, lifecycle status, text, or **Unowned only**.
5. Open an event and confirm source, service/feature, deadline, severity, impacted resources, and mapped owner.
6. Validate the announcement and affected resource inventory in Azure.

**Expected result:** A prioritized list of lifecycle events mapped to known resources and owners.

**Verification:** Confirm tracking ID/source, deadline, and at least one representative impacted resource. Public/reference-feed items may lag Azure notices.

## How to track migration work

1. Assign an accountable owner.

2. Move status from `new` to `acknowledged`, then `migration_planned`, `done`, or `waived` as evidence supports.
3. For a waiver, enter a defensible, non-sensitive reason and approval reference.
4. Generate a draft runbook if AI is configured; validate every step, dependency, date, and rollback.
5. Register a finding or create a ticket for tracked execution.
6. Refresh after migration and confirm affected resources no longer depend on the retiring feature.

**Expected result:** An auditable event state, owner, migration plan, and handoff.

**Verification:** Open the destination ticket/finding, inspect state history, and verify the Azure resource state after remediation.

## How to preview a lifecycle digest

1. Apply the intended scope and filters.

2. Select digest preview and review deadlines, ownership, and destinations.
3. Remove sensitive details and duplicates.
4. Route through approved automation/notification settings only after validating recipients.

**Expected result:** A reviewable summary; preview alone sends nothing.

**Verification:** Confirm every listed event remains current and recipients belong to the correct tenant/team.

## Safety and rollback

Refresh is read-only. Assignment, status, waiver, ticket, and finding actions write local or external records but do not migrate Azure resources. Status can be corrected by another state transition; history remains. A waiver does not cancel a retirement deadline. Correct or close an erroneous external ticket in its destination.

### Freshness and partial results

Snapshots are cached and age visibly. Source feeds can be delayed, resource matching depends on current inventory, and missing ownership produces `Unowned`. AI runbooks are proposals. An empty snapshot is not proof of no lifecycle risk when collectors failed or scope was incomplete.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No events and never loaded | Verify scope/connection and select **Refresh**. |
| No impacted resources | Refresh inventory and check workload/resource matching. |
| Owner is missing | Update ownership mapping, then refresh/reopen the event. |
| Runbook generation fails | Verify AI provider and retry with a narrower, sanitized event. |
| Ticket action fails | Verify connector health and destination configuration. |

## Related docs

- [Retirement Radar reference]({{ site.baseurl }}/user-guide/lifecycle-investigation/retirement-radar/)
- [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/)
- [Evidence Locker]({{ site.baseurl }}/how-to/lifecycle-investigation/evidence-locker/)

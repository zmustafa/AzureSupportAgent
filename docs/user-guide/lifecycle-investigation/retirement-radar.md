---
layout: default
title: Retirement Radar
parent: Lifecycle & Investigation
grand_parent: User guide
nav_order: 1
description: Track Azure service retirements, breaking changes, impacted resources, owners, and migration work.
permalink: /user-guide/lifecycle-investigation/retirement-radar/
---

# Retirement Radar

**Permission:** `radar.read`

## Purpose

**App route:** `/radar`
Retirement Radar combines cached Service Health and Advisor signals with an administrator-maintained classification and model-lifecycle reference. It maps announcements to workload resources, deadlines, owners, and action state.
![Retirement and lifecycle coverage dashboard]({{ site.baseurl }}/assets/retirement-coverage.png)

## Prerequisites and data sources

### Prerequisites

- An enabled Azure connection able to read the selected scope's Service Health, Advisor, and inventory data.
- Current workload inventory and ownership assignments for useful impact mapping.
- An AI provider only when drafting migration guidance.
- A configured Jira or ServiceNow connector only when creating an external ticket.

## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Workflow

1. Choose **Workload** or **Subscription** scope and the intended connection.
2. Inspect cache age; use **Refresh** to collect live signals when needed.
3. Filter by retirement or breaking change, lifecycle status, text, or **Unowned only**.
4. Open an event to review service, feature, deadline, impacted resources, owner, source, and migration context.
5. Assign or acknowledge it, mark migration planned or done, or waive it with a defensible reason.
6. Draft a migration runbook, register a finding, or send a ticket when follow-up is required.
7. Preview the digest before routing lifecycle updates through automation.

Statuses are `new`, `acknowledged`, `migration_planned`, `done`, and `waived`. A waiver records disposition; it does not remove the underlying Azure deadline.

## Interpretation of results

### Interpret

Countdown and red/amber/grey indicators prioritize time, but source quality and resource matching still matter. **Unowned** means no mapped owner was available. **Models at risk** comes from the model-lifecycle reference rather than a direct Azure resource retirement match.

## Exports, history, scheduling, and integrations

### Exports and handoffs

The view can draft migration guidance, register reliability findings, create supported tickets, and preview a digest. Review generated guidance and destination fields before sending. It does not perform the migration.

## Safety and limitations

### Safety

Refresh is explicit to avoid unnecessary Azure calls. Treat public-feed items as supplementary and potentially delayed. State changes, ticketing, and reference edits are auditable; reference editing is an administrator workflow described in [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/).

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| No events and never loaded | Confirm the connection and scope, then select **Refresh**. |
| Snapshot is stale | Compare cache age with configured TTL and refresh. |
| Event has no impacted resources | Refresh inventory and verify workload scope/resource matching. |
| Runbook generation fails | Verify an enabled AI provider, then retry with a narrower event context. |
| Ticket action is unavailable | Configure and enable Jira or ServiceNow and verify connector health. |

## Related pages

### Related docs

- [Case Files]({{ site.baseurl }}/user-guide/lifecycle-investigation/case-files/)
- [Scheduled Tasks]({{ site.baseurl }}/user-guide/automations/scheduled-tasks/)
- [Retirement Radar administration]({{ site.baseurl }}/admin/reference-sets-change-requests/)

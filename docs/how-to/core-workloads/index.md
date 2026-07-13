---
layout: default
title: Core and workload operations
parent: How-to guides
nav_order: 4
description: Task recipes for the Dashboard, Chat, Deep Investigation, workloads, Autopilot, groups, overlaps, and Mission Control.
permalink: /how-to/core-workloads/
has_children: true
---

# Core and workload operations

Use these recipes to move from estate orientation to a workload-scoped investigation or coordinated mission.

## Prerequisites

- Sign in and select only Azure connections and workloads you are authorized to inspect.
- Product permissions vary by task: `chat.use`, `workloads.read`, `workloads.write`, `missions.read`, and `missions.run`.
- Live checks require suitable Azure data-plane or control-plane read access; AI tasks require an active provider.

## Route

Start at `/dashboard`, `/chat`, `/workloads`, or `/mission-control`.

## How to choose a workflow

1. Use [Dashboard and Chat]({{ site.baseurl }}/how-to/core-workloads/dashboard-chat/) to orient, ask a focused question, or convene a Deep Investigation War Room.
2. Use [Workload fleet and manual creation]({{ site.baseurl }}/how-to/core-workloads/workload-fleet/) to review the portfolio or define a known boundary.
3. Use [Autopilot discovery]({{ site.baseurl }}/how-to/core-workloads/autopilot/) to survey a broad Azure scope and review proposed workload boundaries.
4. Use [Workload detail, groups, and overlaps]({{ site.baseurl }}/how-to/core-workloads/workload-detail-groups/) to validate membership, compare related environments, and resolve duplicate attribution.
5. Use [Mission Control]({{ site.baseurl }}/how-to/core-workloads/mission-control/) for a repeatable multi-system sweep.
6. Use [Proactive Support, Monitor, and Stats]({{ site.baseurl }}/how-to/core-workloads/proactive-monitor-stats/) to choose a specialist area or inspect application summaries.

**Expected result:** The selected workflow matches the decision: orientation, investigation, scope definition, portfolio organization, or coordinated analysis.

**Verification:** Confirm the route, Azure connection, workload, data timestamp, and permissions before interpreting or changing application records.

## Task index

| Task | Guide |
| --- | --- |
| Orient and investigate | [Dashboard and Chat]({{ site.baseurl }}/how-to/core-workloads/dashboard-chat/) |
| Create and review workloads | [Workload fleet]({{ site.baseurl }}/how-to/core-workloads/workload-fleet/) |
| Discover workload boundaries | [Autopilot]({{ site.baseurl }}/how-to/core-workloads/autopilot/) |
| Manage detail, groups, and overlaps | [Workload detail and groups]({{ site.baseurl }}/how-to/core-workloads/workload-detail-groups/) |
| Run coordinated analysis | [Mission Control]({{ site.baseurl }}/how-to/core-workloads/mission-control/) |
| Select proactive features and inspect health | [Proactive Support, Monitor, and Stats]({{ site.baseurl }}/how-to/core-workloads/proactive-monitor-stats/) |

## Safety and rollback

Dashboard, Chat evidence gathering, discovery survey, and most analyses are read-oriented. Saving workloads and groups changes the application registry, not Azure. Normal workload deletion is recoverable from Trash; merge, purge, and empty-trash require extra care. Mission cancellation stops remaining orchestration but does not undo completed reads or generated records.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| A page is empty | Check product permission, selected connection, workload existence, and whether a first scan is required. |
| Data looks old | Open the owning feature and inspect freshness; do not treat Dashboard or fleet cache reads as live scans. |
| An action is unavailable | Check the write/run permission and any Azure or AI capability required by that specific action. |

## Related docs

- [Core Experience reference]({{ site.baseurl }}/user-guide/core/)
- [Workloads reference]({{ site.baseurl }}/user-guide/workloads/)
- [Mission Control reference]({{ site.baseurl }}/user-guide/mission-control/)

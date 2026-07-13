---
layout: default
title: Workload Detail
parent: Workloads
grand_parent: User guide
nav_order: 3
description: Inspect one workload's membership, composition, health, risk, and analysis freshness.
permalink: /user-guide/workloads/workload-detail/
---

# Workload detail

**Route:** `/workloads/{id}`

## Purpose

Workload detail is the command center for one application boundary. It combines registry metadata and resources with cached composition, health, risk, and activity signals, then links to the tools that produced those signals.
![Workload command center with health, coverage, risk, and next actions]({{ site.baseurl }}/assets/workload-detail.png)

### When to use it

- Before an assessment, investigation, or mission to confirm scope.
- To understand why a fleet score is good, warning, poor, or unknown.
- To inspect resource types, locations, subscriptions, classifications, and risk.
- To refresh an Autopilot-origin workload after resources change.

## Prerequisites and data sources

### Prerequisites and permissions

- `workloads.read` to view, analyze, and refresh.
- `workloads.write` to edit membership or metadata.
- A usable connection for refresh and live feature scans.
- Feature-specific permissions for linked analysis pages.

## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Workflow

1. Open a workload from `/workloads`.
2. Confirm the name, environment, criticality, workload type, data classification, connection, and group.
3. Review the **Resources** tab. Verify that scope nodes, explicit resources, and exclusions represent the intended application.
4. Review composition by category, resource type, location, and subscription.
5. Inspect each health component and its freshness. Do not rely on the aggregate alone.
6. Review retirement, critical-finding, and assessment-gap counts.
7. If signals are missing or stale, select **Analyze**. The action requests relevant monitoring, telemetry, backup/DR, radar, ownership, and other refreshes available to the user.
8. For Autopilot-origin workloads, use **Refresh** to reconcile children of tracked resource groups and inspect what was added or removed.
9. Follow next-action links to assessments, architectures, Chat, or Mission Control.

## Interpretation of results

### Interpret results

The overall health score uses only present signals. A missing component is represented as unknown and excluded from weighting rather than treated as zero. Consequently:

- **Not analyzed** means there are no usable component scores.
- A good aggregate with only one present component is not equivalent to comprehensive health.
- A component's age can make an otherwise precise score unsuitable for a current decision.
- Risk counts are prompts to inspect the source feature, not full finding records.

Resource summary counts are derived from workload membership and refresh results. Scope nodes may imply many resources even when only a few nodes are stored.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety

- Confirm membership before running expensive or broad analyses; downstream tools inherit workload scope.
- Refresh can add newly discovered resources and remove deleted ones under tracked resource groups. Review the reported delta.
- Current refresh logic is focused on resource-group children; manually review higher-level scope changes.
- Editing nodes or exclusions can invalidate profiles and alter assessment/coverage results.
- Use soft delete for decommissioning. Purge only after confirming dependent documents and retention needs.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| **Not analyzed** remains after Analyze | Check each feature call, connection access, and application permissions; some signals may be unavailable |
| Aggregate looks better than expected | Inspect which components are absent; scoring renormalizes over present signals |
| Resources are missing after refresh | Verify origin and tracked resource groups; manually edit scopes that refresh does not reconcile |
| Deleted Azure resource remains | Run Refresh for an eligible workload or remove the explicit node manually |
| Analyze produces authorization errors | Verify both feature permission and Azure data access |
| Detail cannot open | Workload may be in Trash or purged; return to fleet/Trash and verify the ID |

## Related pages

- [Workload fleet]({{ site.baseurl }}/user-guide/workloads/fleet/)
- [Discovery and Autopilot]({{ site.baseurl }}/user-guide/workloads/discovery-autopilot/)
- [Groups and overlaps]({{ site.baseurl }}/user-guide/workloads/groups-overlaps/)
- [Chat and Deep Investigation]({{ site.baseurl }}/user-guide/core/chat-deep-investigation/)
- [Mission Control]({{ site.baseurl }}/user-guide/mission-control/)

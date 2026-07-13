---
layout: default
title: Inspect workload detail, groups, and overlaps
parent: Core and workload operations
grand_parent: How-to guides
nav_order: 4
description: Validate workload scope, refresh analysis, organize application families, and resolve shared-resource overlap.
permalink: /how-to/core-workloads/workload-detail-groups/
---

# Inspect workload detail, groups, and overlaps

![Workload detail command center]({{ site.baseurl }}/assets/workload-detail.png)

## Prerequisites

- `workloads.read`; `workloads.write` for membership, group, and overlap-removal changes.
- A usable Azure connection for Analyze, eligible refresh, and deep overlap expansion.

## Route

Detail: `/workloads/{id}`. Groups: `/workloads/groups` and `/workloads/groups/{id}`. Overlaps: `/workloads/overlaps`.

## How to validate and analyze workload detail

1. Open a workload from `/workloads`.
2. Confirm connection, environment, criticality, type, data classification, group, and description.
3. Inspect **Resources** for explicit nodes, broad scopes, and exclusions.
4. Review composition by category, type, location, and subscription.
5. Inspect each health component and freshness before the aggregate score.
6. Review retirement, critical-finding, assessment-gap, and next-action indicators.
7. Select **Analyze** when signals are missing/stale; follow links to the source feature for errors.
8. For an Autopilot-origin workload, use **Refresh** to reconcile children under tracked resource groups and review added/removed items.
9. Open Architecture, Assessments, Mission Control, Graph, or Chat as required.

**Expected result:** Scope, component health, risk, and freshness are understood for one workload.

**Verification:** Confirm source-feature timestamps and compare the Resources tab with current Azure inventory.

## How to create and compare workload groups

1. Open `/workloads/groups` and create a group with name, description, owner, color, and non-sensitive tags, or review naming-based suggestions.
2. Add related workloads such as production, staging, and development. Grouping does not merge them.
3. Open group detail and inspect rollup health, resources, criticality, retirements, and findings.
4. Open **Compare** and review resource-type, category, environment, and health-signal differences.
5. Use drift-only display to isolate differences, then decide whether each is intentional.
6. Remove a member or delete the group when the family model is no longer valid; members remain workloads.

**Expected result:** Related workloads remain separate but gain a reusable application-family comparison.

**Verification:** Confirm each workload's `group_id` association and that deleting a group detaches rather than deletes members.

## How to investigate and resolve overlaps

1. Open `/workloads/overlaps` and select all connections or one connection.
2. Run the instant scan for explicit duplicate membership.
3. Enable **Deep scan** to expand subscription/resource-group scope-implied membership.
4. Group by resource, workload pair, or type; inspect membership provenance such as explicit or via broader scope.
5. Export CSV for an owner review when needed.
6. Classify each overlap as intentional shared service or unintended duplicate attribution.
7. For unintended overlap, edit nodes/exclusions or use the supported remove-from-others action only for removable explicit membership.
8. Rerun the same scan to verify.

**Expected result:** Intentional sharing is documented and unintended duplicate membership is removed.

**Verification:** The resource disappears from unintended pairs while remaining in the authoritative workload; downstream scopes reflect the change.

## Safety and rollback

- Analyze can issue multiple read scans; confirm scope first.
- Workload Refresh currently focuses on resource-group children; review higher-level scope changes manually.
- Group rollups are cached aggregations, not scans. A highlighted difference is not automatically a defect.
- Removing membership changes downstream analyses. Shared resources must not be deduplicated without owner review.
- Group deletion is non-destructive; workload membership edits can be reversed by restoring the prior node/exclusion manually.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Aggregate seems too healthy | Check absent components; scoring reweights only analyzed signals. |
| Analyze is unauthorized | Verify feature permissions and Azure access for each underlying scan. |
| Group rollup is unknown | Analyze member workloads and check freshness. |
| Expected overlap is absent | Run Deep scan and verify the selected connection. |
| Scope-implied overlap cannot be removed inline | Edit the broad scope or add an exclusion in the workload editor. |

## Related docs

- [Workload detail reference]({{ site.baseurl }}/user-guide/workloads/workload-detail/)
- [Groups and overlaps reference]({{ site.baseurl }}/user-guide/workloads/groups-overlaps/)
- [Mission Control recipes]({{ site.baseurl }}/how-to/core-workloads/mission-control/)

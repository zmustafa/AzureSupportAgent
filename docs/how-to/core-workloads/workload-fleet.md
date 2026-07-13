---
layout: default
title: Operate the workload fleet and create workloads
parent: Core and workload operations
grand_parent: How-to guides
nav_order: 2
description: Triage fleet health and create a workload manually from explicit Azure scopes and resources.
permalink: /how-to/core-workloads/workload-fleet/
---

# Operate the workload fleet and create workloads

![Workload fleet cockpit]({{ site.baseurl }}/assets/workloads-fleet.png)

## Prerequisites

- `workloads.read` for fleet/profile views; `workloads.write` for create, edit, merge, delete, and grouping.
- A readable Azure connection for resource selection.
- Prior source scans for meaningful health, freshness, and risk values.

## Route

Open `/workloads`.

## How to triage the fleet

1. Review the fleet cockpit, cards, or table.
2. Search by name, description, tag, or classification; filter environment, criticality, and data class.
3. Sort critical production, worst-health, oldest/stale, or **Not analyzed** workloads first.
4. Compare health with resource composition and risk indicators.
5. Open a workload rather than comparing aggregate scores alone.
6. Use **Analyze** from detail for unknown signals, **Autopilot** for uncovered resources, **Overlaps** for duplicate attribution, or **Mission Control** for a coordinated sweep.

**Expected result:** High-priority workloads are separated from workloads that are merely unscanned.

**Verification:** Open each priority workload and confirm component availability and freshness; fleet health reweights only present signals.

## How to create a workload manually

1. On `/workloads`, select **+ New workload**.
2. Enter a unique **Name** and useful **Description**.
3. Select the Azure connection.
4. Set workload type, **Environment**, **Criticality**, and **Data class** when known; leave unknown values unset rather than guessing.
5. In the resource picker, browse or search management groups, subscriptions, resource groups, and resources.
6. Select the narrowest maintainable boundary. Use explicit resources when only part of a resource group belongs to the workload; use exclusions where the picker supports “scope minus resource.”
7. Add non-sensitive tags used for application organization.
8. Save the workload and open its detail page.

**Expected result:** One active application-registry record contains the selected scope nodes, resources, exclusions, connection, and classifications.

**Verification:** On `/workloads/{id}`, inspect **Resources** and confirm the intended resources are included and unrelated resources are absent.

## How to edit, delete, restore, or merge workloads

1. Use **Edit** to change metadata or membership, then recheck downstream scope.
2. Use normal delete to move a workload to Trash; restore it if removal was accidental.
3. To merge, select at least two workloads and choose **Merge … → 1**.
4. Name the merged workload, review the union, and note that source workloads move to Trash.
5. Purge or empty Trash only after checking architectures, Know-Me, FMEA, assessments, missions, ownership, and retention needs.

**Expected result:** Registry lifecycle reflects the intended active boundary without unintended Azure changes.

**Verification:** Confirm active/trash lists, merged membership, and downstream links before permanent deletion.

## Safety and rollback

- Fleet and profile views are cache reads, not live scans.
- Membership edits change downstream assessment, coverage, cost, ownership, and investigation scope.
- Manual save changes only the application registry; it does not move or tag Azure resources.
- Normal delete is reversible. Merge has no one-click undo; sources remain in Trash until purged. Purge and empty-trash are permanent.
- Never include secrets in names, descriptions, or tags.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Fleet is empty | Verify permission, create manually, or run Autopilot. |
| Health is unknown | Open detail and Analyze; missing signals are not zero. |
| Picker returns no resources | Check connection, scope, Reader access, and Resource Graph capability. |
| Resource count is surprising | Inspect broad scope nodes and exclusions, then run overlap analysis. |
| Merge is wrong | Inspect the merged record and restore sources from Trash before purging anything. |

## Related docs

- [Workload fleet reference]({{ site.baseurl }}/user-guide/workloads/fleet/)
- [Autopilot recipes]({{ site.baseurl }}/how-to/core-workloads/autopilot/)
- [Workload detail and groups recipes]({{ site.baseurl }}/how-to/core-workloads/workload-detail-groups/)

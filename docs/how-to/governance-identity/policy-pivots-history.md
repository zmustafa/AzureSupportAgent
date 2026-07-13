---
layout: default
title: Analyze policy pivots and history
parent: Governance and identity
grand_parent: How-to guides
nav_order: 2
description: Analyze assignments by person, subscription, time, custom dimensions, and saved history.
permalink: /how-to/governance-identity/policy-pivots-history/
---

# Analyze policy pivots and history

## Prerequisites

- Product permission `policy.read` and a loaded policy inventory.
- `policy.write` only when saving or deleting local simulations or snapshots.
- Enough source metadata to resolve assignment creator and time; blanks remain unknown.

## Route

`/policy/byperson`, `/policy/bysubscription`, `/policy/timeline`, `/policy/pivot`, and `/policy/history`.

## How to find policy assignments by person

1. Open `/policy/byperson`.

2. Filter to the workload or scope under review.
3. Expand a person/author group and inspect its policies, scopes, and assignment dates.
4. Export the bounded pivot if a review record is needed.

**Expected result:** Assignments grouped by available author metadata.

**Verification:** Open representative assignments in Azure. Missing author metadata does not prove that no person created the assignment.

## How to compare subscriptions

1. Open `/policy/bysubscription`.

2. Select the management-group or workload boundary.
3. Compare assignment counts and policy coverage across subscriptions.
4. Drill into an outlier and verify inherited assignments and exemptions.

**Expected result:** A scope-oriented comparison that highlights differences for investigation.

**Verification:** Confirm outliers against `/policy/assignments` and Azure; counts can differ because inherited and excluded scopes are represented differently.

## How to analyze timeline and custom pivots

1. Open `/policy/timeline` and choose the relevant time slice.

2. Inspect assignment activity using only records with known dates.
3. Open `/policy/pivot` and select row and column dimensions such as person, management group, subscription, policy, or date.
4. Apply slicers, then export CSV or Excel.
5. Record dimensions, filters, generated time, and source cache age with the result.

**Expected result:** A reproducible grouped analysis over the currently loaded assignment snapshot.

**Verification:** Recreate one pivot total from filtered assignment rows. Timeline is not an Azure Activity Log and should not be used as sole change evidence.

## How to review saved policy history

1. Open `/policy/history`.

2. Separate saved rollout simulations from coverage runs.
3. Open the relevant record and check workload, creation time, inputs, and output.
4. Compare it with current inventory before reusing any recommendation.
5. Delete a local record only when retention policy permits and `policy.write` is available.

**Expected result:** A traceable local record of prior analysis, not proof that Azure was changed.

**Verification:** Confirm current Azure state independently; saved runs are point-in-time artifacts.

## Safety and rollback

Pivots and exports are read-only. Saving and deleting history writes only local application records. Deletion may not be recoverable; export or preserve the record first when required. Do not include real user IDs or sensitive assignment parameters in shared examples.

### Freshness and partial results

All pivots derive from the loaded inventory and inherit its age, workload filter, missing metadata, and Resource Graph truncation. History does not auto-refresh its old inputs. A blank date or author is unknown data, not absence of activity.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Person is `Unknown` | Verify source metadata and Azure visibility; use scope/policy pivots instead. |
| Timeline is sparse | Assignment creation data may be absent; consult Azure Activity Log or IaC history. |
| Pivot totals disagree | Align slicers, workload scope, inheritance handling, and empty-value buckets. |
| Old simulation looks current | Check creation time and rerun analysis against a fresh inventory. |
| Cannot delete history | Confirm `policy.write` and retention requirements. |

## Related docs

- [Inventory Azure Policy and assignments]({{ site.baseurl }}/how-to/governance-identity/policy-inventory-assignments/)
- [Rollout Planner and AI tools]({{ site.baseurl }}/how-to/governance-identity/policy-rollout-ai/)
- [Azure Policy reference]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)

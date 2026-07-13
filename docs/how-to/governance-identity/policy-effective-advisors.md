---
layout: default
title: Resolve effective policy and governance risks
parent: Governance and identity
grand_parent: How-to guides
nav_order: 3
description: Resolve effective policy, review exemptions, and turn governance advisors into verified actions.
permalink: /how-to/governance-identity/policy-effective-advisors/
---

# Resolve effective policy and governance risks

## Prerequisites

- Product permission `policy.read` and a current inventory; compliance is needed for compliance-based advice.
- Exact target resource, resource group, subscription, or management-group scope.
- Change approval outside the app for any Azure remediation.

## Route

`/policy/governance`, `/policy/exemptions`, `/policy/effective`, and `/policy/advisors`.

## How to resolve effective policy at a scope

1. Open `/policy/effective`.

2. Enter or select the exact target scope.
3. Resolve assignments and inspect inherited scope, effect, enforcement mode, parameters, and `notScopes`.
4. Review applicable exemptions, reference IDs, categories, and expiry.
5. Repeat at a representative child resource when inheritance or exclusions may differ.

**Expected result:** A calculated set of assignments applicable after scope inheritance, exclusions, and known exemptions.

**Verification:** Compare selected rows with Azure Policy assignments and exemptions at every parent scope. This resolver is not an Azure authorization decision trace.

## How to review exemption hygiene

1. Open `/policy/exemptions`.

2. Filter expired, expiring, never-expiring, or weakly justified records.
3. Open each candidate and confirm assignment, scope, category, expiry, references, and owner.
4. Decide whether to renew, narrow, replace, or remove it through the approved change process.
5. Refresh inventory and resolve effective policy again after the change.

**Expected result:** A verified exemption action list with owners and deadlines.

**Verification:** Confirm the exemption in Azure and test one affected resource. An expired exemption in cache may already have changed.

## How to create or update an exemption safely

1. Open `/policy/exemptions` and select **Add exemption**, or select **Edit** on an existing row.

2. Choose the exact target assignment and scope, then enter category, expiry, and a non-sensitive justification.
3. Respect configured guardrails such as required justification, maximum expiry, and blocked never-expiring records.
4. Select **Preview & validate** and review the diff and generated Azure CLI.
5. On a read-only connection, copy the CLI into the approved external change process. On a write-enabled connection, select **Create exemption** or **Apply update** only after approval.
6. Refresh inventory, reopen the exemption, and resolve effective policy at an affected resource.

**Expected result:** The approved exemption is created or updated in Azure, or a reviewed CLI plan is produced without applying it.

**Verification:** Confirm assignment, scope, category, expiry, and justification in Azure. Test that only the intended resources are exempt.

## How to remove an exemption safely

1. Open the exemption and confirm the assignment, scope, owner, expiry, and reason for removal.

2. Select **Remove** and read the warning: the policy assignment will apply again to previously exempt resources.
3. On a read-only connection, copy the generated delete CLI for approved external execution. On a write-enabled connection, confirm **Remove exemption** only after impact review.
4. Refresh inventory and resolve effective policy at representative affected resources.

**Expected result:** The exemption is removed and the underlying assignment becomes applicable again, subject to other exclusions or exemptions.

**Verification:** Confirm deletion in Azure and test representative deployment/update paths before declaring success.

## How to use Governance and Advisors

1. Open `/policy/governance`, then `/policy/advisors`.

2. Review promotion candidates, remediation gaps, conflicts, exemption hygiene, and baseline coverage separately.
3. For a promotion candidate, confirm fresh compliance, representative deployment tests, exclusions, and false positives.
4. For Modify or DeployIfNotExists gaps, verify assignment identity, location, least-privilege role definitions, and remediation-task design.
5. For conflicts, compare definition IDs, parameters, scopes, inheritance, and effects before labeling a duplicate.
6. For a coverage proposal, validate the selected baseline and applicability to the workload.
7. Record accepted work in a ticket or rollout plan; do not treat an advisor card as approval.

**Expected result:** Prioritized, source-checked governance work rather than automatic changes.

**Verification:** Re-run the relevant advisor after external remediation and confirm the underlying assignment, compliance, identity, or exemption state changed.

## Safety and rollback

Governance, Effective policy, and Advisors are analytical. The Exemptions tab can create, update, or delete Azure exemptions when the selected connection is write-enabled; a read-only connection produces CLI for external review instead. Exemption removal can immediately restore policy enforcement, while a broad exemption can weaken governance. Use narrow scope, expiry, justification, owner, approval, and representative tests. Roll back a mistaken create by removing it; roll back an update or removal by recreating the previously approved exemption values. Deny promotion, role grants, and remediation tasks remain external changes and need their own Azure/IaC rollback.

### Freshness and partial results

Promotion advice depends on available compliance and is unsafe when compliance is stale, absent, sampled, or scoped too narrowly. Conflicts can be intentional. Baseline coverage is a gap heuristic. Resource Graph truncation and inaccessible subscriptions can hide assignments and exemptions.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Effective assignment is unexpected | Trace parent scopes, `notScopes`, exemption scope, expiry, and workload filter. |
| Apply action is unavailable | The connection is read-only; use the generated CLI through the approved external process or have an administrator review connection write settings. |
| Exemption validation is blocked | Supply required justification/expiry and comply with configured maximum-expiry and never-expire guardrails. |
| Safe-to-promote looks wrong | Refresh compliance and test representative create/update paths in audit. |
| Remediation gap persists | Verify managed identity, assignment location, role-definition IDs, and scope. |
| Conflict is intentional | Document distinct parameters, effect, ownership, or scope; do not remove it blindly. |
| Coverage run is incomplete | Check baseline, workload scope, inventory truncation, and inaccessible subscriptions. |

## Related docs

- [Rollout Planner and AI tools]({{ site.baseurl }}/how-to/governance-identity/policy-rollout-ai/)
- [Policy drift and IaC]({{ site.baseurl }}/how-to/governance-identity/policy-drift-iac/)
- [Azure Policy reference]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)

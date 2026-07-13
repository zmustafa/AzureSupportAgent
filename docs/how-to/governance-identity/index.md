---
layout: default
title: Governance and identity
parent: How-to guides
nav_order: 1
has_children: true
description: Task recipes for Azure Policy, Identity, PIM, app registrations, and RBAC reviews.
permalink: /how-to/governance-identity/
---

# Governance and identity how-to guides

Use these guides to turn cached governance and access data into verified review outcomes. The pages distinguish read-only analysis from local writes and Azure-side remediation.

| Goal | Guide |
| --- | --- |
| Scan and inventory policy | [Inventory and assignments]({{ site.baseurl }}/how-to/governance-identity/policy-inventory-assignments/) |
| Analyze policy ownership, scope, trends, and pivots | [Policy pivots and history]({{ site.baseurl }}/how-to/governance-identity/policy-pivots-history/) |
| Resolve effective policy and governance risks | [Effective policy and advisors]({{ site.baseurl }}/how-to/governance-identity/policy-effective-advisors/) |
| Plan a staged policy change | [Rollout Planner and AI tools]({{ site.baseurl }}/how-to/governance-identity/policy-rollout-ai/) |
| Reconcile observed policy with IaC | [Policy drift and IaC]({{ site.baseurl }}/how-to/governance-identity/policy-drift-iac/) |
| Triage identity, PIM, and applications | [Identity reviews and handoffs]({{ site.baseurl }}/how-to/governance-identity/identity-reviews/) |
| Review and export effective access | [RBAC access reviews]({{ site.baseurl }}/how-to/governance-identity/rbac-access-reviews/) |

## Common operating pattern

1. Select the intended tenant connection and the narrowest useful workload or scope.
2. Check generated time, cache age, collector status, and truncation or partial-result warnings.
3. Refresh only the collector needed for the decision.
4. Filter before interpreting totals or exporting.
5. Validate a candidate against Azure, Entra, Policy Insights, or the authoritative IaC repository.
6. Use an approved external change process, then refresh the affected data and preserve verification evidence.

Never put client secrets, access tokens, share tokens, real tenant IDs, object IDs, or user identifiers in prompts, exports used as examples, tickets, or documentation.

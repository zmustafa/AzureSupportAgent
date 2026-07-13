---
layout: default
title: Inventory Azure Policy and assignments
parent: Governance and identity
grand_parent: How-to guides
nav_order: 1
description: Scan policy inventory, review assignment details, and export a bounded register.
permalink: /how-to/governance-identity/policy-inventory-assignments/
feature_ids: [PROACTIVE_NAV:policy, POLICY_NAV:overview]
---

# Inventory Azure Policy and assignments

## Prerequisites

- Product permission `policy.read`.
- An ARM/Resource Graph connection with Reader visibility over every intended scope.
- Policy Insights read access when compliance is required.
- A current workload definition when using workload scope.

## Route

`/policy/overview`, `/policy/inventory`, and `/policy/assignments`.

## How to build a current policy inventory

1. Open `/policy/overview` and select the connection and workload scope.

2. Read the generated time, age, cache state, and any `never loaded` message.
3. Select **Refresh** for cached inventory behavior, or **Scan Compliance** when a live Azure pull and compliance summaries are required.
4. Open `/policy/inventory` and review definitions, initiatives, assignments, exemptions, and the scope tree.
5. Confirm that expected management groups and subscriptions appear before using totals.

**Expected result:** A scope-bounded snapshot containing the policy objects visible to the selected connection, plus compliance only when requested and available.

**Verification:** Compare a known assignment and its scope with Azure Policy. Confirm generated time is after the scan and inspect any Resource Graph truncation warning.

## How to review and export assignments

1. Open `/policy/assignments` after selecting the same connection and workload.

2. Filter the register by policy, scope, effect, enforcement mode, or search text.
3. Open a row and verify definition or initiative, parameters, assignment identity, `notScopes`, and scope.
4. Export the filtered rows with **CSV** or **Excel**.
5. Open the file locally and confirm the row count and filters match the UI.

**Expected result:** A reviewable assignment register whose export reflects the selected data set.

**Verification:** Spot-check one direct assignment, one inherited assignment, and any `DoNotEnforce` assignment against Azure. Treat missing compliance as unknown, not compliant.

## Safety and rollback

Inventory, compliance collection, filtering, and export do not change Azure. A scan can be slow and can consume API quota. Narrow scope before rescanning. Downloaded files can contain resource and identity metadata; store and delete them under organizational data-handling rules. There is no Azure rollback because no Azure write occurs.

### Freshness and partial results

Policy cache is persistent and does not expire automatically. A page visit does not prove the data is current. Resource Graph response-size limits can truncate large inventories, and Policy Insights may be unavailable because of permissions or API failure. Workload filtering can intentionally hide assignments outside the workload.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Never loaded | Run the explicit live scan for the selected connection and scope. |
| Assignment is missing | Check workload filtering, Azure visibility, inherited scope, cache age, and truncation warnings. |
| Compliance is blank | Verify Policy Insights permission and subscription coverage, then scan with compliance enabled. |
| Export has fewer rows | Clear unintended filters and verify whether export is scoped to the current register. |
| Scan is slow | Reduce workload/subscription scope and avoid concurrent rescans. |

## Related docs

- [Azure Policy reference]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
- [Policy pivots and history]({{ site.baseurl }}/how-to/governance-identity/policy-pivots-history/)
- [Effective policy and advisors]({{ site.baseurl }}/how-to/governance-identity/policy-effective-advisors/)

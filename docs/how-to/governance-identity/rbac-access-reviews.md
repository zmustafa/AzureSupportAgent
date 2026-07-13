---
layout: default
title: Review, scan, export, and investigate RBAC
parent: Governance and identity
grand_parent: How-to guides
nav_order: 7
description: Use every RBAC tab to review effective access, exposure, scopes, roles, insights, diagnostics, refreshes, exports, and investigations.
permalink: /how-to/governance-identity/rbac-access-reviews/
feature_ids: [PROACTIVE_NAV:rbac, RBAC_NAV:overview]
---

# Review, scan, export, and investigate RBAC

## Prerequisites

- Product permission `rbac.read`.
- ARM Reader visibility at every intended management-group, subscription, resource-group, and resource scope.
- Approved Graph read access for names, groups, Entra roles, ownership, and transitive paths.
- An approved external process for access changes; this feature is read-only.

## Route

`/rbac/overview`, `/rbac/effective`, `/rbac/privileged`, `/rbac/scopes`, `/rbac/roles`, `/rbac/insights`, and `/rbac/diagnostics`.

![RBAC effective-access review]({{ site.baseurl }}/usecase-assets/rbac.png)

## How to scan RBAC scopes and directory context

1. Open `/rbac/overview`, select the connection, and inspect KPI and freshness badges.

2. Open `/rbac/scopes` to identify stale, failed, or unauthorized scope slices.
3. Refresh one scope for a bounded Azure assignment update, **Directory** for principal/group/Entra context, or **All** only when necessary.
4. Follow background progress; the job can continue after navigation.
5. Open `/rbac/diagnostics` and resolve collector-specific errors.

**Expected result:** Current scope slices and directory context with explicit status per collector.

**Verification:** Generated times advance for the intended slices, Diagnostics is understood, and a known assignment/principal resolves correctly.

## How to review effective access

1. Open `/rbac/effective`.

2. Narrow workload/scope, surface, and principal type before entering search text.
3. Inspect principal, effective principal, role, role definition, assignment scope, assignment type, and access path.
4. Expand group/transitive or owner paths and note stale directory context.
5. Confirm candidate access directly in Azure and Entra.

**Expected result:** A bounded list of known direct, group-derived, ownership, Azure, and Entra access paths.

**Verification:** Check assignment ID/scope, group chain, principal state, role actions/data actions, and inheritance. This is not a complete authorization-engine simulation.

## How to investigate privileged and data-plane exposure

1. Open `/rbac/privileged` and separate privileged classification from roles containing data actions.

2. Prioritize broad scopes, cross-scope principals, standing users, external/unresolved principals, and nested groups.
3. Open `/rbac/roles` to inspect role definitions and available principal records.
4. Use `/rbac/insights` for pivots by role, principal, scope, surface, principal type, group inheritance, ownership, Entra role, eligibility, cross-scope, and orphaned state.
5. Establish business owner and intended use before proposing least-privilege or PIM changes.

**Expected result:** A source-verified access-review candidate with impact and ownership.

**Verification:** Validate role permissions, scope, deny/conditional controls, service ACLs, and recent use through authoritative systems.

## How to export and hand off an RBAC investigation

1. Apply all intended filters in `/rbac/effective`.

2. Use the available CSV, JSON, or workbook export control and record filter/scope/generated-time metadata.
3. Open the file and confirm row and column completeness.
4. Redact unnecessary object IDs, UPNs, group chains, and resource names before sharing.
5. Create an approved case or ticket externally and attach only the minimum evidence.
6. After remediation, refresh both the affected Azure scope and Directory when group or principal state changed.

**Expected result:** A reproducible review artifact and tracked investigation without an in-app access mutation.

**Verification:** The refreshed row disappears or changes as intended, while required emergency, deployment, and service-managed access remains intact.

## Safety and rollback

Scanning and analysis are read-only but can be expensive at scale. The app does not add or remove role assignments. Azure/Entra rollback must be prepared externally before revocation: restore the prior assignment or group membership using approved tooling. Never remove break-glass, deployment, inherited, or service-managed access without impact review.

### Freshness and partial results

Page visits read disk-backed caches and never scan. Azure scopes and directory context age independently. Partial or unauthorized collectors remain visible in Diagnostics. Row/page caps, server-side filtering, Graph gaps, unsupported deny/conditional assignments, classic administrators, and service ACLs can make results incomplete.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Overview is empty | Inspect Diagnostics and refresh the correct scope; page load is cache-only. |
| Principal/group path is stale | Refresh Directory and verify Graph consent. |
| Subscription is missing | Verify connection visibility and Reader at parent/subscription scope. |
| Search is slow | Filter scope, surface, and principal type before typing. |
| Export differs from UI | Confirm format, filters, paging/row caps, and snapshot time. |
| Expected access is absent | Check inheritance, nested groups, conditions, deny assignments, service ACLs, and collector errors. |

## Related docs

- [RBAC reference]({{ site.baseurl }}/user-guide/governance-identity/rbac/)
- [Identity reviews and handoffs]({{ site.baseurl }}/how-to/governance-identity/identity-reviews/)
- [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/)

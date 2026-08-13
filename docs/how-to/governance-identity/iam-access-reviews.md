---
layout: default
title: Review, scan, export, and investigate IAM
parent: Governance and identity
grand_parent: How-to guides
nav_order: 7
description: Use every IAM tab to review effective access, exposure, scopes, roles, insights, diagnostics, refreshes, granted-versus-used analysis, exports, and investigations.
permalink: /how-to/governance-identity/iam-access-reviews/
redirect_from:
  - /how-to/governance-identity/rbac-access-reviews/
feature_ids: [PROACTIVE_NAV:iam, ROUTE:iam, IAM_NAV:overview, IAM_NAV:effective, IAM_NAV:privileged, IAM_NAV:accessmap, IAM_NAV:leastprivilege, IAM_NAV:pim, IAM_NAV:roles, IAM_NAV:insights, IAM_NAV:scopes, IAM_NAV:diagnostics]
---

# Review, scan, export, and investigate IAM

## Prerequisites

- Product permission `iam.read`.
- ARM Reader visibility at every intended management-group, subscription, resource-group, and resource scope.
- Approved Graph read access for names, groups, Entra roles, ownership, and transitive paths.
- An approved external process for access changes; this feature is read-only.

## Route

`/iam` and `/iam/:tab` — including `/iam/overview`, `/iam/effective`, `/iam/privileged`, `/iam/leastprivilege`, `/iam/scopes`, `/iam/roles`, `/iam/insights`, and `/iam/diagnostics`. The former `/rbac` URLs redirect to their `/iam` equivalents, keeping the tab segment and query string.

![IAM access review overview showing grant, principal and privileged counts with per-scope freshness]({{ site.baseurl }}/assets/iam.png)

## How to scan RBAC scopes and directory context

1. Open `/iam`, select the connection, and read the freshness indicator in the page header. It reports the **newest** collection across every scope, and names how many scopes lag when they genuinely disagree.

2. Open `/iam/scopes` to identify stale, failed, or unauthorized scope slices.
3. Refresh at the right granularity: one scope for a bounded Azure assignment update, **↻ Refresh directory** on Overview for principal/group/Entra context, or **↻ Rescan** in the page header for everything. Rescan is in the header, so it is reachable from whichever tab exposed the stale data.
4. Follow background progress; the job can continue after navigation.
5. Open `/iam/diagnostics` and resolve collector-specific errors.

**Expected result:** Current scope slices and directory context with explicit status per collector.

**Verification:** Generated times advance for the intended slices, the header indicator drops back to a recent age with no stale split, Diagnostics is understood, and a known assignment/principal resolves correctly.

## How to review effective access

1. Open `/iam/effective`.

2. Narrow workload/scope, surface, and principal type before entering search text.
3. Inspect principal, effective principal, role, role definition, assignment scope, assignment type, and access path.
4. Expand group/transitive or owner paths and note stale directory context.
5. Confirm candidate access directly in Azure and Entra.

**Expected result:** A bounded list of known direct, group-derived, ownership, Azure, and Entra access paths.

**Verification:** Check assignment ID/scope, group chain, principal state, role actions/data actions, and inheritance. This is not a complete authorization-engine simulation.

## How to trace access paths and PIM eligibility

1. Open `/iam/accessmap` and select the intended principal or resource scope.
2. Follow each rendered edge back to its assignment, group, ownership, or eligible-role source; do not infer a grant from the diagram label alone.
3. Open `/iam/pim` for eligible and active assignment context, including schedule and activation state available in the cached scan.
4. Compare the path with `/iam/effective` and the source assignment in Azure or Entra.
5. Refresh the affected scope and directory context after an external access change.

**Expected result:** The effective principal, source assignment, transitive path, and PIM state are distinguished rather than collapsed into one grant.

**Verification:** The source assignment and group or eligible-role chain agree across Access Map, Effective Access, and the authoritative portal.

## How to investigate privileged and data-plane exposure

1. Open `/iam/privileged` and separate privileged classification from roles containing data actions.

2. Prioritize broad scopes, cross-scope principals, standing users, external/unresolved principals, and nested groups.
3. Open `/iam/roles` to inspect role definitions and available principal records.
4. Use `/iam/insights` for pivots by role, principal, scope, surface, principal type, group inheritance, ownership, Entra role, eligibility, cross-scope, and orphaned state.
5. Establish business owner and intended use before proposing least-privilege or PIM changes.

**Expected result:** A source-verified access-review candidate with impact and ownership.

**Verification:** Validate role permissions, scope, deny/conditional controls, service ACLs, and recent use through authoritative systems.

## How to measure granted access against access actually used

1. Open `/iam/leastprivilege`. If it reports that usage was not measured, treat that as "we have not looked", not as "nothing is over-privileged".

2. Open the window picker beside **Scan usage** and choose a preset — 7, 14, 30, 60, or 90 days — or enter a custom value between 1 and 90. The window is a lookback ending now; 90 days is the ceiling because that is what the Azure Activity Log retains.
3. Run **Scan usage**. It reads the Activity Log per subscription, is slow, and is independent of every access refresh control.
4. Read the window stated beside the figures, not the one in the picker. The picker sets what the *next* scan will read.
5. Work the recommendations by confidence. Each states both numbers — actions used out of actions granted — and every narrower proposal names the residual risk it gives up.

**Expected result:** A confidence-ranked list of over-privileged assignments, each with a narrower proposal and the capability that proposal removes.

**Verification:** Confirm the principal's recent activity against the Azure activity log for the subscription in question, and confirm the proposed role's actions cover the operations the owner says are required. "Unused" is a statement about the measured window only.

## How to export and hand off an RBAC investigation

1. Apply all intended filters in `/iam/effective`.

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
| The header reads `scanned just now` but the data looks old | The headline is the newest collection across all scopes. Check for the `N of M scopes stale` split, then read per-scope ages on Overview. |
| Least Privilege reports that usage was not measured | No usage scan has run for this connection. Pick a window and use **Scan usage**. |
| A usage window over 90 days will not apply | Azure Activity Log retention is the ceiling; the request is refused rather than silently shortened. |
| Principal/group path is stale | Refresh Directory and verify Graph consent. |
| Subscription is missing | Verify connection visibility and Reader at parent/subscription scope. |
| Search is slow | Filter scope, surface, and principal type before typing. |
| Export differs from UI | Confirm format, filters, paging/row caps, and snapshot time. |
| Expected access is absent | Check inheritance, nested groups, conditions, deny assignments, service ACLs, and collector errors. |

## Related docs

- [IAM reference]({{ site.baseurl }}/user-guide/governance-identity/iam/)
- [Work the IAM scanner inbox]({{ site.baseurl }}/how-to/governance-identity/iam-scanner-inbox/)
- [Run an IAM escalation review]({{ site.baseurl }}/how-to/governance-identity/iam-escalation-review/)
- [Find what access changed and who changed it]({{ site.baseurl }}/how-to/governance-identity/iam-compare-attribute-changes/)
- [Identity reviews and handoffs]({{ site.baseurl }}/how-to/governance-identity/identity-reviews/)
- [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/)

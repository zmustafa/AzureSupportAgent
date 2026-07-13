---
layout: default
title: Review identity, PIM, and app registrations
parent: Governance and identity
grand_parent: How-to guides
nav_order: 6
description: Refresh identity snapshots, triage findings, review PIM and app registrations, and create safe handoffs.
permalink: /how-to/governance-identity/identity-reviews/
feature_ids: [PROACTIVE_NAV:identity, IDENTITY_NAV:overview]
---

# Review identity, PIM, and app registrations

## Prerequisites

- Product permission `identity.read`.
- A Microsoft Graph-capable connection with approved user, application, and role-management read permissions.
- ARM discovery and Key Vault data-plane list/get access for vault expiry checks.
- Jira or ServiceNow only for ticket creation; an enabled AI/chat path for investigation handoff.

## Route

`/identity/overview`, `/identity/pim`, and `/identity/app-registrations`.

![Identity findings grouped by urgency]({{ site.baseurl }}/assets/identity.png)

## How to refresh and triage identity findings

1. Open `/identity/overview`, select the connection, and choose a 30-, 60-, 90-, or custom-day window.

2. Check generated time, `never loaded`, errors, sampled counts, and workload mapping.
3. Select **Refresh** once and wait; the slow collection does not run automatically.
4. Filter by severity and mapped-only status.
5. Prioritize expired/near-expiry credentials, privileged MFA evidence gaps, ownerless apps, Conditional Access review candidates, and Key Vault expiry.
6. Validate each item in Entra or Key Vault before remediation.
7. Refresh after the external correction.

**Expected result:** A prioritized, point-in-time set of identity posture findings with collector limitations visible.

**Verification:** Confirm subject, expiry, owner, policy state, and workload in the authoritative service. `Without MFA` is sampled evidence, not a tenant-wide authentication-method audit.

## How to review PIM and JIT posture

1. Open `/identity/pim` and inspect the PIM snapshot age.

2. Run **Refresh** when absent or stale.
3. Review standing access, stale eligible, stale active, and recent activation records.
4. Check principal, role, assignment age, last activation, and justification.
5. Validate the candidate in Entra PIM and with the business owner.
6. Move standing privilege to approved eligibility/JIT externally where appropriate.

**Expected result:** A verified list of privileged-access review candidates.

**Verification:** Confirm assignment type and activation history in Entra PIM, then refresh this tab independently.

## How to review and export app registrations

1. Open `/identity/app-registrations` and select **Refresh** if never loaded or stale.

2. Follow background progress; navigating away does not cancel the job.
3. Filter by owner, permission, audience, risk indicator, or credential state.
4. Open a row to inspect secret/certificate expiry, owners, delegated/application permissions, and portal link.
5. Export the filtered view to CSV or use the Excel workbook export where shown.
6. Verify the export count and protect it as sensitive governance metadata.

**Expected result:** A bounded app inventory and review artifact without secret values.

**Verification:** Spot-check owners, credential expiry, audience, and high-impact application permissions in Entra.

## How to investigate or create a ticket

1. From a validated overview finding, select **Investigate** for a contextual chat handoff or **Create Ticket** for a configured connector.

2. Review and redact the generated context.
3. Add impact, owner, due date, and source link without secrets or unnecessary personal data.
4. Submit and record the returned ticket reference.

**Expected result:** A traceable handoff; no credential, MFA, policy, or directory object is changed by the app.

**Verification:** Open the destination and confirm tenant, subject, severity, and link are correct.

## Safety and rollback

Feature collection is read-only; ticket creation writes to an external system. Exports and handoffs can disclose identity metadata. Credential rotation, owner changes, Conditional Access, and PIM changes occur externally and require overlap/testing or approved rollback. A mistaken ticket can be corrected or closed in the destination; an exported file must be securely deleted according to policy.

### Freshness and partial results

Overview, PIM, and app registrations use separate caches and refreshes. Partial collector failure can show last-known-good groups beside errors. App enumeration is capped, privileged MFA checks are sampled, Key Vault probes are best-effort, and Graph changes are eventually consistent.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Names appear as IDs | Fix Graph permission/token resolution and refresh the affected tab. |
| Apps or owners are missing | Check tenant, Graph consent, enumeration cap, errors, and job completion. |
| Vault findings are absent | Verify ARM discovery and data-plane access on each vault. |
| Refresh appears stuck | Check progress/job state and Graph throttling; do not start duplicates. |
| Ticket action fails | Verify connector health, destination configuration, and minimum required fields. |

## Related docs

- [Identity reference]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [RBAC access reviews]({{ site.baseurl }}/how-to/governance-identity/rbac-access-reviews/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

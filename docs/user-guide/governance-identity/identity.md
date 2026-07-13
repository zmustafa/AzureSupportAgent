---
layout: default
title: Identity
parent: Governance & Identity
grand_parent: User guide
nav_order: 2
description: Triage Entra and Key Vault security findings, PIM/JIT posture, and application-registration credentials, permissions, and ownership.
permalink: /user-guide/governance-identity/identity/
feature_ids: [PROACTIVE_NAV:identity, IDENTITY_NAV:overview]
---

# Identity

**Product permission:** `identity.read`.

## Purpose

**App routes:** `/identity` and `/identity/:tab`
Identity is a read-only posture view over Microsoft Graph and selected Azure/Key Vault evidence. It surfaces urgent credential, ownership, Conditional Access, MFA, PIM, and application-registration issues without rotating credentials or changing directory objects.
![Identity dashboard showing grouped security findings and urgency]({{ site.baseurl }}/assets/identity.png)

## Prerequisites and data sources

### Prerequisites

- A connection capable of obtaining a Microsoft Graph token.
- Graph application permissions/consent appropriate to the enabled collectors, commonly user, application, and directory-role read permissions (for example, `User.Read.All`, `Application.Read.All`, and `RoleManagement.Read.Directory` or the exact equivalents configured for your Graph integration).
- ARM Resource Graph access to discover Key Vaults and Key Vault data-plane list/get access for secret/certificate-expiry checks.
- A Jira or ServiceNow connector for ticket creation.

Use least privilege and follow organizational consent review. Missing Graph or vault access produces partial findings rather than granting broader access automatically.

## Tabs and actions

### Tabs

- **Security Findings** groups expiring app/service-principal credentials, ownerless apps, disabled/report-only Conditional Access gaps, sampled privileged users without observed MFA evidence, and Key Vault secret/certificate expiry.
- **PIM / JIT** reviews standing access, stale eligible/active assignments, and recent activation-review records.
- **App Registrations** inventories owners, secret/certificate counts and expiry, delegated/application permissions, audience, risk indicators, and detail drawers; it supports CSV and Excel workbook export.

Security Findings can be filtered by look-ahead window, severity, and workload mapping. Finding actions include **Investigate** (chat handoff) and **Create Ticket** when a connector is configured.

## Freshness and scope behavior

### Refresh and freshness

Opening each tab reads cache; it does not automatically launch a slow Graph aggregation. The Overview cache is keyed by tenant and look-ahead days. PIM and App Registrations have their own caches. Use the tab's refresh action when `never loaded`, stale, or older than a relevant identity change.

App-registration refresh can run as a long background/SSE job and may take many minutes in a large directory. A per-tenant lock prevents duplicate refreshes. Partial collector failure can leave last-known-good groups visible alongside errors—check the generated time and error metadata.

## Workflow overview

### Triage workflow

1. Select the intended connection and look-ahead window.
2. Refresh when needed and wait for completion; do not repeatedly start jobs.
3. Prioritize expired/near-expiry credentials and privileged MFA/PIM exposure.
4. Validate each finding in Entra/Key Vault because sampling, cache, and eventual consistency apply.
5. Assign a human owner and create a ticket or investigation handoff with minimum necessary context.
6. Remediate outside the app:
   - rotate credentials with overlap, validate consumers, then remove the old credential;
   - assign at least two accountable app owners according to policy;
   - review Conditional Access in report-only before enforcement;
   - move standing privilege toward approved PIM eligibility/JIT;
   - rotate Key Vault material and verify dependent applications.
7. Refresh and confirm the finding is resolved.

## Interpretation of results

### Interpret findings

- Severity and days-left prioritize work but do not know application criticality or rotation complexity.
- **Ownerless** means no owner was observed with available Graph data.
- **Without MFA** is based on the collector's available evidence and sampled privileged population; it is not an authoritative tenant-wide authentication-method audit.
- Disabled/report-only Conditional Access policies are review candidates, not automatically security defects.
- PIM stale/standing classifications use configured age windows and available activation history.
- Unmapped findings remain important; they simply could not be linked to a local workload.

## Exports, history, scheduling, and integrations

### App-registration review and export

Filter by owner, audience, or permission characteristics, then open the row detail to inspect each credential and permission. Export CSV for the current client-side data or Excel from the workbook endpoint. Exports contain identity metadata and should be handled as sensitive governance data; they never include secret values.

A large permission count is not necessarily excessive, and a low count is not necessarily safe. Evaluate application purpose, consent type, resource audience, owner, credential type, and actual use.

## Safety and limitations

- All feature operations are read-only except local ticket/investigation integration records.
- No secret value is retrieved for display and no credential is rotated by this UI.
- Findings are capped/sampled to control Graph throttling; large tenants may not be exhaustive.
- App enumeration has a configurable maximum.
- Key Vault probing is best-effort and resource-specific; inaccessible vaults create blind spots.
- Graph permissions and consent can take time to propagate.
- Ticket exports should avoid live object IDs unless operationally necessary.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| Never loaded | Start the correct tab's refresh; Overview, PIM, and App Registrations use separate caches. |
| Partial failure | Read collector errors and capability matrix; last-known-good data may still be shown. |
| Apps or owners are missing | Verify Graph consent, app-enumeration cap, tenant/connection, and refresh completion. |
| Key Vault findings are absent | Verify ARM discovery plus data-plane list/get RBAC on each relevant vault. |
| Names appear as IDs | Graph resolution failed or lacks permission; correct access and refresh. |
| App refresh seems stuck | Check the background job state and Graph throttling; do not launch concurrent refreshes. |

## Related pages

- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
- [RBAC]({{ site.baseurl }}/user-guide/governance-identity/rbac/)
- [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/)

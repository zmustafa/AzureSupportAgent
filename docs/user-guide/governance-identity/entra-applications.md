---
layout: default
title: "Entra: applications and consent"
parent: Governance & Identity
grand_parent: User guide
nav_order: 8
description: Risk-ranked application registrations and enterprise applications, per-application detail covering granted Graph permissions and credential expiry, and the tenant consent posture.
permalink: /user-guide/governance-identity/entra-applications/
feature_ids: [PROACTIVE_NAV:entra, ENTRA_NAV:applications]
---

# Entra: applications and consent

**Product permission:** `entra.read` for every view on this tab; `entra.admin` to start a collection from the freshness badge.

## Purpose

**App route:** `/entra/applications`

Applications answers which non-human identities exist in the tenant, what they have actually been granted, who is accountable for them, when their credentials expire, and what the tenant's consent settings allow users to hand out unsupervised. The distinction the whole screen is built around is *requested* versus *granted*: a manifest entry is what an application asks for, an app-role assignment is what it holds. Only the granted set is risk, and the two are never conflated.

## Prerequisites and data sources

- `entra.read` to view; `entra.admin` to refresh. Nothing on this tab writes to the directory.
- Consent tier 1: `Application.Read.All` and `Directory.Read.All` for registrations, service principals, owners, credentials and granted app-role assignments. Without these the tab is blind.
- Consent tier 2: `Policy.Read.PermissionGrant` for the consent posture and permission-grant policies, and `Synchronization.Read.All` for provisioning jobs. Missing either narrows the Consent sub-tab rather than emptying the grid.
- No Entra ID P1 or P2 licence is required for the application inventory itself. Conditional Access relevance on the detail view depends on the Conditional Access collection, which does need P1.
- An Azure ARM connection with a completed RBAC scan for the Azure reach block. It reads the existing RBAC cache and starts no new Azure collection.

Permission names and their risk tiers are resolved from the live Microsoft Graph service principal rather than a hard-coded list, so a new permission is named automatically and an unrecognised identifier is reported as unknown rather than silently dropped.

See [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/).

## Tabs and actions

| Sub-tab | What it answers |
| --- | --- |
| Inventory | Every application and enterprise application, ranked by risk, with the detail drawer |
| Consent | What the tenant lets users consent to, and every tenant-wide delegated grant |
| Registrations | Credential expiry hygiene for app registrations — the operational view rather than the risk view |

**Inventory** is one grid over both planes of the same object: local app registrations joined to their service principal, plus enterprise applications that have no local registration, which are third-party apps someone consented into the tenant. Microsoft first-party service principals are excluded. Filters are a free-text search over display name and application ID, an **Ownerless only** toggle, and a **Risk ≥** threshold; the API additionally accepts a maximum-permission-tier filter and offset/limit paging, defaulting to 200 rows and capped at 2000. Rows sort by risk descending, then by name. Columns are risk, application, permissions, credentials, owners, and assigned principals. Inline markers call out Azure-managed identities, enterprise-app-only entries, orphaned service principals, multi-tenant registrations, external publishers, self-grant capability, and tenant-wide content access.

Risk is a weighted score out of 100, and the components are published by the API so the number is never a black box:

| Component | Weight |
| --- | --- |
| Granted permission tier | 30 |
| Can grant itself permissions | 15 |
| Credential hygiene | 15 |
| Ownership | 10 |
| Exposure (multi-tenant, publisher, redirect URIs) | 10 |
| Azure control-plane reach | 10 |
| Usage and assignment breadth | 5 |
| Conditional Access coverage | 5 |

A component can be marked not applicable with a stated reason — an Azure-managed identity is not scored on credentials or ownership, because the platform owns both.

**Selecting a row** opens the application detail panel, which carries: the risk breakdown component by component with points against weight; owners resolved to names, with an explicit callout when there are none; granted application permissions as tier-coloured chips; delegated consent separated into tenant-wide and per-user; requested-but-not-granted permissions, labelled as not being risk today; credentials with their name or identifier, their kind, and their expiry state; federated identity credentials with issuer and a flag for a wildcard subject; Conditional Access relevance, meaning which enforced policies target this application or all applications; Azure reach from the RBAC cache with a staleness note; provisioning jobs with their status and any quarantine; and the findings raised against this object. Redirect URIs are collected and feed the exposure component of the score. Sign-in audience, single sign-on mode, assignment requirement, and the owning tenant of an external application are shown as facts alongside the identifiers.

**Consent** reports the tenant posture — whether user consent is unrestricted, disabled, or restricted to low-risk permissions; whether the admin consent request workflow is enabled; and where guest invitations may come from — followed by a table of every delegated grant consented for all principals, with the client application, the resource, the scopes, and the highest tier among them. Permission grant policies are returned with the same payload.

## Freshness and scope behavior

One collection builds one snapshot per tenant, and that snapshot serves every Entra tab. Refresh from the freshness badge in the page header. Sub-tabs never collect on their own; opening Inventory or Consent reads what is already cached, and the detail panel is served from the same snapshot rather than a live Graph call.

Granted permissions are collected by querying the handful of resource service principals that matter — Microsoft Graph, the legacy Azure AD Graph, Exchange Online and SharePoint Online — for everyone holding one of their application permissions, rather than fanning a call out across every principal in the tenant. A consent granted minutes ago will not appear until the next collection.

## Interpretation of results

- **A large permission count is not automatically bad, and a small one is not automatically safe.** One `RoleManagement.ReadWrite.Directory` grant outranks forty read scopes. Read the tier and the self-grant marker, not the number.
- **Self-grant is the tenant-takeover primitive.** An application able to assign app roles or write applications, directory objects, or permission-grant policy can give itself everything else. Treat that marker as the top of the queue.
- **Tenant-wide delegated grants behave like application permissions.** A grant consented for all principals applies to everyone who signs in, not only to the person who accepted it.
- **Requested is not granted.** Permissions in the manifest with no matching assignment carry no access. They are worth reviewing before the next consent, not today.
- **No owner is an operational finding.** Nobody is accountable for rotating the credentials or retiring the application, which is usually why the credential is the one that expires unnoticed.
- **A multi-decade credential lifetime is itself the finding.** It is reported as an approximate lifetime in years rather than a raw day count, because a secret that cannot be rotated on any sensible schedule is not a neutral fact.
- **Unverified publisher and external publisher are context, not verdicts.** Plenty of legitimate internal applications have neither.

## Safety and limitations

- Every collector is read-only. No application, service principal, credential, consent grant, or policy is modified.
- **No secret or certificate value is ever retrieved or displayed.** Only the credential's identifier or display name, its kind, and its expiry are collected. There is no code path that reads a secret value, and there could not be one — Microsoft Graph does not return it after creation.
- **Credential rotation happens outside this product.** This screen tells you what is about to expire and who owns it; creating, replacing, and removing credentials is done in the Microsoft Entra admin center through your change process.
- Microsoft first-party service principals are excluded from the grid, so the counts are about the tenant's own and its third-party applications.
- Permission tiering is a judgement about blast radius, not a Microsoft classification. An unrecognised write-scoped permission is treated as high rather than ignored.
- Conditional Access relevance is derived from the snapshot's enforced policies and application targeting; it is not a Microsoft what-if evaluation.
- Azure reach comes from the RBAC cache and can be older than the Entra snapshot. The panel says so when it is.
- Exports and screenshots of this tab contain application identifiers and consent detail. Handle them as governance material.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Grid is empty and the badge says `never loaded` | Start a collection from the freshness badge; sub-tabs never collect on their own. |
| Owners column reads `unknown` rather than a number | The owners read failed or was not consented. Ownership is not scored when it is unknown. |
| Risk components show `n/a` | The component does not apply to that identity — most often an Azure-managed identity. |
| Consent posture fields are blank | Grant `Policy.Read.PermissionGrant` and re-collect. |
| Provisioning block never appears | Grant `Synchronization.Read.All`; without it provisioning jobs are not collected. |
| Azure reach is missing for a service principal you expect | No RBAC scan exists, or the principal holds no Azure role in the scanned scope. |
| An app consented today is absent | Consent is eventually consistent and the grid reads a snapshot. Refresh. |
| A permission chip shows an identifier instead of a name | The resource service principal could not be resolved; the permission is reported rather than dropped. |
| Refresh returns a permission error | Collection requires `entra.admin`, not `entra.read`. |

## Related pages

- [Entra ID]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/)
- [Entra: privileged access]({{ site.baseurl }}/user-guide/governance-identity/entra-privileged/)
- [Review identity, PIM, and app registrations]({{ site.baseurl }}/how-to/governance-identity/identity-reviews/)
- [RBAC]({{ site.baseurl }}/user-guide/governance-identity/rbac/)

---
layout: default
title: RBAC
parent: Governance & Identity
grand_parent: User guide
nav_order: 3
description: Review effective Azure and Entra access, privileged/data-plane exposure, scope hierarchy, roles, insights, and collection diagnostics.
permalink: /user-guide/governance-identity/rbac/
feature_ids: [PROACTIVE_NAV:rbac, RBAC_NAV:diagnostics, RBAC_NAV:effective, RBAC_NAV:insights, RBAC_NAV:overview, RBAC_NAV:privileged, RBAC_NAV:roles, RBAC_NAV:scopes]
---

# RBAC

**Product permission:** `rbac.read`.

## Purpose

**App routes:** `/rbac` and `/rbac/:tab`
RBAC composes Azure role assignments, role definitions, scope hierarchy, and available Entra directory/group/ownership context into effective-access rows. It is an access-review tool and does not add or remove assignments.
![RBAC review showing effective and privileged access analysis]({{ site.baseurl }}/assets/identity.png)

## Prerequisites and data sources

### Prerequisites

- An ARM-capable connection with Reader access to role assignments/definitions at all intended management-group, subscription, resource-group, and resource scopes.
- Graph capability and appropriate directory-role/group/application read consent for resolved principals, transitive group paths, Entra roles, and application ownership.
- Product `rbac.read` access.

Azure Reader is enough to inspect many control-plane assignments but does not imply data-plane visibility into every service. Missing Graph access leaves Azure assignment IDs usable while reducing names and inherited/group context.

## Tabs and actions

### Tabs

- **Overview**: unique-principal, privileged/data-plane, scope, and freshness KPIs.
- **Effective Access**: server-filtered, paged, virtualized normalized rows with principal, role, scope, surface, assignment type, and access path.
- **Privileged & Exposure**: roles classified as privileged and/or containing data actions.
- **Scopes**: management-group → subscription → resource-group hierarchy with grant counts and per-scope freshness.
- **Roles & Principals**: role definitions and available directory principals.
- **Insights**: pivots by role, principal, scope, surface, principal type, privilege, data plane, group inheritance, ownership, Entra roles, eligibility, cross-scope access, and orphaned identities.
- **Diagnostics**: collector status, unauthorized/failed scopes, directory status, and partial errors.

Search/filter controls include text, scope/workload, principal type, surface, access category, and privileged-only. Results are server-paged; filtering first is more reliable and efficient than browsing a large unfiltered estate.

## Freshness and scope behavior

### Refresh and freshness

Page visits read disk-backed caches and never trigger Azure scans. Scope slices and the directory cache are refreshed independently. Header actions can refresh a single scope, directory context, or all. Refresh is a non-blocking background job with progress; it can continue if the browser closes.

Check per-scope age and status. A fresh subscription slice combined with a stale directory cache can show current assignments with unresolved principals or outdated group paths. Refreshing directory alone does not refresh Azure assignments.

## Workflow overview

### Access-review workflow

1. Select the correct connection and inspect Overview/Scopes freshness.
2. Refresh stale failed scopes and directory context as needed.
3. On **Effective Access**, narrow scope, principal type, and surface before searching.
4. Inspect role name and definition, assignment scope, effective principal, and access path:
   - **Direct** is assigned to the principal;
   - **Group/transitive** is inherited through group membership;
   - **Owner** reflects an application/service-principal ownership path where modeled.
5. Use **Privileged & Exposure** to prioritize Owner/admin-style roles and roles with data actions.
6. Use **Insights** to find cross-scope, group-derived, orphaned, and unusually broad access.
7. Verify each candidate against source Azure/Entra state and business ownership.
8. Remediate through the organization's approved Azure/Entra/PIM process, then refresh the relevant scope and directory.

## Interpretation of results

### Interpret results

- **Privileged** is a classification based on role metadata/name and should be reviewed, not blindly revoked.
- **Has data actions** means the role definition can authorize data-plane operations; actual access still depends on scope, deny assignments, service controls, and conditions.
- **Effective row** describes a known access path. It is not a full authorization-engine simulation.
- Group expansion depends on directory collection and can become stale independently.
- An orphaned/unresolved principal may be deleted, inaccessible to Graph, or simply unresolved; confirm before removing assignments.
- Grant counts are rows/known grants, not unique people.

## Exports, history, scheduling, and integrations

### Export, remediation, and safety

RBAC is read-only. There is no built-in assignment-change, approval, or IaC remediation flow and no general access-grid Excel endpoint. Use available client-side CSV where presented, or an approved external process, and verify the export scope/filters.

Apply least privilege, but do not remove emergency access, deployment identities, inherited group access, or service-managed assignments without ownership and impact review. Prefer PIM/JIT and narrowly scoped roles where supported. Keep break-glass identities under separate controls.

## Safety and limitations

### Limitations

- Cache composition and broad searches can be expensive at scale; use scope and principal filters.
- Text search can query the server as typed; avoid pasting sensitive content.
- Server page/row caps can limit broad results. Use pivots and scoped queries.
- Data-plane authorization, deny assignments, conditional role assignments, classic administrators, and service-specific ACLs may not be fully represented.
- Graph failure degrades principal names, group chains, PIM/Entra, and ownership context without necessarily failing Azure RBAC collection.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| Overview is empty | Inspect Diagnostics, then refresh scope/all; page load is cache-only. |
| Principal names or groups are stale | Run Directory refresh and verify Graph consent/capability. |
| A subscription is missing | Verify connection visibility and Reader at management-group/subscription scope; inspect scope diagnostics. |
| Search is slow | Filter scope, surface, and principal type first; use Insights pivots. |
| Expected access path is absent | Check nested group collection, cache ages, role scope, assignment conditions, and unsupported authorization surfaces. |
| Remediation action is unavailable | Expected: RBAC does not mutate Azure. Use an approved external/PIM/IaC workflow. |

## Related pages

- [Identity]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

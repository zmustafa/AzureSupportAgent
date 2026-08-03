---
layout: default
title: IAM
parent: Governance & Identity
grand_parent: User guide
nav_order: 3
description: Review effective Azure and Entra access, privileged/data-plane exposure, scope hierarchy, roles, insights, and collection diagnostics.
permalink: /user-guide/governance-identity/iam/
redirect_from:
  - /user-guide/governance-identity/rbac/
feature_ids: [PROACTIVE_NAV:iam, IAM_NAV:diagnostics, IAM_NAV:effective, IAM_NAV:insights, IAM_NAV:overview, IAM_NAV:privileged, IAM_NAV:roles, IAM_NAV:scopes]
---

# IAM

**Product permission:** `iam.read`.

## Purpose

**App routes:** `/iam` and `/iam/:tab` (the former `/rbac` URLs redirect here)
IAM composes Azure role assignments, role definitions, scope hierarchy, and available Entra directory/group/ownership context into effective-access rows. It is an access-review tool and does not add or remove assignments.
![IAM review showing effective and privileged access analysis]({{ site.baseurl }}/assets/identity.png)

## Prerequisites and data sources

### Prerequisites

- An ARM-capable connection with Reader access to role assignments/definitions at all intended management-group, subscription, resource-group, and resource scopes.
- Graph capability and appropriate directory-role/group/application read consent for resolved principals, transitive group paths, Entra roles, and application ownership.
- Product `iam.read` access.

Azure Reader is enough to inspect many control-plane assignments but does not imply data-plane visibility into every service. Missing Graph access leaves Azure assignment IDs usable while reducing names and inherited/group context.

## Tabs and actions

### Tabs

- **Overview**: unique-principal, privileged/data-plane, scope, and freshness KPIs.
- **Effective Access**: server-filtered, paged, virtualized normalized rows with principal, role, scope, surface, assignment type, and access path.
- **Access Map**: the same access drawn as a flow — principal ▸ via group ▸ role ▸ scope. See below.
- **Privileged**: roles classified as privileged and/or containing data actions.
- **Scopes**: management-group → subscription → resource-group hierarchy with grant counts and per-scope freshness.
- **Roles**: role definitions and available directory principals.
- **Insights**: pivots by role, principal, scope, surface, principal type, privilege, data plane, group inheritance, ownership, Entra roles, eligibility, cross-scope access, and orphaned identities.
- **Diagnostics**: collector status, unauthorized/failed scopes, directory status, and partial errors.

Search/filter controls include text, scope/workload, principal type, surface, access category, and privileged-only. Results are server-paged; filtering first is more reliable and efficient than browsing a large unfiltered estate.

### The Access Map

The grid lists grants. The Access Map draws them as paths — subject, then how, then verb, then object — so a question like "how does this person reach that subscription, and what would I have to change to stop them" is answered by following one ribbon rather than by reading and joining rows.

The columns are configurable rather than fixed. "Who can do what", "who can reach this workload" and "who can touch this resource" are one question asked from three ends, so they are presets of a single column chain rather than three separate screens. Pick a preset, or set the columns yourself from: principal, principal type, via group, role, role category, privileged, surface, access path, active/eligible, management group, subscription, resource group, resource type, resource, scope level, and whether the assignment carries an ABAC condition. Saved views keep the columns, the weighting and the filters together, because restoring only the columns would reload half a view and look broken.

Use the scope and workload rail on the left to focus the map. Tenant-wide with every principal in one column is legible only after the long tail is folded (see below); narrowing to a subscription, a workload or a search term is what makes individual people visible.

**The group column is not decoration.** Access held through a group cannot be revoked from the person — you remove them from the group. A chain of principal ▸ role ▸ scope renders perfectly well and would prescribe a fix that does not work, so "Via group" is in the default chain.

**Ribbon width has a stated unit.** *Grants* counts assignment rows and conserves across columns. *Distinct principals* answers "how many people flow along this ribbon" and deliberately does not add up across columns, because one person crossing two ribbons is still one person. The subtitle above the diagram always says which is in use.

Four things the diagram cannot express on its own are therefore reported beside it rather than drawn:

| Reported separately | Why it is not a ribbon |
| --- | --- |
| PIM-eligible grants | Eligibility is permission to ask for a role, not access anyone currently holds. Excluded by default; tick **Include PIM-eligible** to see what could be activated |
| Deny assignments | A deny *removes* access and a ribbon *adds* it. Drawing them together would state the opposite of the truth |
| Groups whose membership could not be read | Shown as the group itself rather than dropped, so the access stays visible even when the people are not. "We could not enumerate the group" must never render as "nobody has this" |
| The long tail of each column | Folded into one labelled "N more" bar. Every grant is still counted; raise **Per column** or narrow the focus to open it up |

Selecting any bar lists the principals and roles behind it and links through to the Effective Access evaluator and the access grid, so the picture is a starting point rather than the end of the trail.

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
5. Use **Privileged** to prioritize Owner/admin-style roles and roles with data actions.
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
- The Access Map draws no deny assignments and, by default, no PIM-eligible grants. Both are counted and reported beside the diagram. Read a ribbon as "this access exists", never as "this is the complete set of things that decide the outcome".
- Ribbon width weighted by *distinct principals* does not sum across columns. Do not read column totals off a principal-weighted map.
- A folded "N more" bar carries a real total but no names. Narrow the focus before concluding that a specific person does or does not hold access.

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

- [Entra ID]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

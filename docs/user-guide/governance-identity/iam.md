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
feature_ids: [PROACTIVE_NAV:iam, IAM_NAV:accessmap, IAM_NAV:bypass, IAM_NAV:compare, IAM_NAV:diagnostics, IAM_NAV:effective, IAM_NAV:escalation, IAM_NAV:evaluate, IAM_NAV:findings, IAM_NAV:insights, IAM_NAV:leastprivilege, IAM_NAV:overview, IAM_NAV:pim, IAM_NAV:privileged, IAM_NAV:reviews, IAM_NAV:roles, IAM_NAV:scanners, IAM_NAV:scopes, IAM_NAV:simulator]
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

### The page header

The header sits above the tab strip and stays on screen whichever tab is open. It carries the title, the freshness indicator, the full rescan control, and the connection picker.

**Freshness** reads as `scanned 6h ago`, turning amber once anything is past the refresh window and red once anything is more than a day old. Two things about that number matter:

- **The headline is the newest collection across every scope and the directory.** On a large estate one scope refreshing a minute ago would otherwise render "scanned just now" while everything else is days old. When the scopes genuinely disagree — some past the refresh window and some not — the indicator also names how many lag, as `N of M scopes stale`. When every scope is equally old the headline already tells the whole truth and no split is shown. Hover for the newest and oldest ages and the size of the refresh window.
- **It reports when data was *collected*, not when it was *verified*.** A delta refresh that skips an unchanged scope records a verification time and leaves the collection time alone, precisely so "four days old, verified two minutes ago" stays distinguishable from "collected two minutes ago". This indicator is the collection.

Before the overview resolves, and while a query is failing, the indicator renders nothing rather than asserting "never scanned" — no data yet is not the same as never scanned. A tenant that genuinely has no collection says `never scanned`.

**↻ Rescan** re-collects every scope and the directory. It lives in the header, so it is reachable from every tab; a reader on Findings or PIM who sees stale data no longer has to navigate to Overview to act on it. The two narrower controls — **⚡ Quick refresh** and **↻ Refresh directory** — remain on the **Overview** tab beside the per-scope table they operate on.

### Tabs

- **Overview**: unique-principal, privileged/data-plane, scope, and freshness KPIs, the per-scope table, and the narrow refresh controls.
- **Findings**: access findings raised against the collected estate.
- **Scanners**: named selections of those signals, with their cadence and last run.
- **Access**: server-filtered, paged, virtualized normalized rows with principal, role, scope, surface, assignment type, and access path.
- **Effective Access**: evaluates the effective permissions a principal holds at a scope.
- **Access Map**: the same access drawn as a flow — principal ▸ via group ▸ role ▸ scope. See below.
- **Escalation**: paths by which one grant leads to a broader one.
- **Shadow Access**: the doors that are not Azure RBAC — keys, tokens, and service-specific authorization that would still work if every role assignment were revoked.
- **Least Privilege**: granted versus actually used, and narrower role proposals. See below.
- **Simulator**: models a proposed access change before it is made anywhere.
- **Compare**: what changed since the previous collection, and who did it where the Activity Log can attribute it.
- **Reviews**: access-review and attestation state held locally.
- **PIM**: eligible and active assignment state.
- **Scopes**: management-group → subscription → resource-group hierarchy with grant counts and per-scope freshness.
- **Roles**: role definitions and available directory principals.
- **Insights**: pivots by role, principal, scope, surface, principal type, privilege, data plane, group inheritance, ownership, Entra roles, eligibility, cross-scope access, and orphaned identities.
- **Diagnostics**: collector status, unauthorized/failed scopes, directory status, and partial errors.

`/iam/privileged` remains a working URL and renders the Access grid with the privileged filter applied. It is no longer a separate tab in the strip.

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

### Least Privilege: granted versus used

This tab compares the actions a principal's roles *can* authorize against the actions they were actually observed performing, and proposes a narrower grant. Four things about it are structural:

- **Not measured is a wall, not an empty list.** A tenant that has never run a usage scan does not see "0 over-privileged principals", because that is the most reassuring possible rendering of "we have not looked".
- **Usage carries its own age, separate from the access snapshot.** The access data can be minutes old while usage is weeks old, and a stale denominator changes what "unused" means. The usage collection time is stated beside the figures.
- **Both numbers travel together, never the ratio alone.** "Used 12 of 8,000" is a fact; a bare percentage is a number designed to be quoted out of context.
- **A proposal is never shown without its residual risk.** Every narrower proposal states what it gives up.

**The usage window is a lookback ending now.** The picker beside **Scan usage** is a popover with presets of 7, 14, 30, 60, and 90 days plus a custom field accepting 1 to 90. The ceiling is Azure Activity Log retention: nothing older than 90 days can be measured, and the popover says so rather than silently clamping a larger request — "unused in 90 days" is a weaker claim than "unused in 180", and a control that quietly substituted one for the other would misstate the evidence behind an access removal.

Absolute start and end dates are deliberately not offered. The refresh takes a **day count ending now**, not a date range, so any absolute range would have to collapse to a lookback from today — the control would name one window while the scan read another.

The picker opens on the window the data on screen was actually measured over, not on a fixed default, so the number beside the figures and the number in the control always agree. Selecting a window only sets what the *next* scan will read; the window the current figures came from is stated to the left. **Scan usage** reads the Activity Log per subscription, is slow, and is separate from the access refresh.

## Freshness and scope behavior

### Refresh and freshness

Page visits read disk-backed caches and never trigger Azure scans. Scope slices and the directory cache are refreshed independently. **↻ Rescan** in the page header re-collects everything and is available from every tab; the Overview tab additionally offers **⚡ Quick refresh**, **↻ Refresh directory**, and per-scope refresh. Refresh is a non-blocking background job with progress; it can continue if the browser closes.

Check per-scope age and status. A fresh subscription slice combined with a stale directory cache can show current assignments with unresolved principals or outdated group paths. Refreshing directory alone does not refresh Azure assignments.

Usage data on the Least Privilege tab has its own freshness and is not affected by any of the access refresh controls. It advances only when **Scan usage** runs.

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
- The usage window cannot exceed 90 days, because the Azure Activity Log does not retain more. "Unused" is always a statement about the measured window, never about all time.
- The header freshness figure is the newest collection across all scopes. Read the split indicator, or the per-scope table on Overview, before treating the whole estate as current.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| Overview is empty | Inspect Diagnostics, then refresh scope/all; page load is cache-only. |
| Principal names or groups are stale | Run Directory refresh and verify Graph consent/capability. |
| A subscription is missing | Verify connection visibility and Reader at management-group/subscription scope; inspect scope diagnostics. |
| Search is slow | Filter scope, surface, and principal type first; use Insights pivots. |
| Expected access path is absent | Check nested group collection, cache ages, role scope, assignment conditions, and unsupported authorization surfaces. |
| Remediation action is unavailable | Expected: RBAC does not mutate Azure. Use an approved external/PIM/IaC workflow. |
| The header says `scanned just now` but a tab shows old data | The headline is the newest collection across all scopes. Check for the `N of M scopes stale` split beside it, then open Overview for the per-scope ages. |
| The header shows nothing where freshness should be | The overview has not resolved, or its query failed. That is not the same as never scanned, so nothing is asserted. Check Diagnostics. |
| Least Privilege says usage was not measured | No usage scan has run for this connection. Set the window and use **Scan usage**; it is separate from every access refresh control. |
| A usage window longer than 90 days will not apply | Azure Activity Log retention is the ceiling. The popover states it; the request is not silently shortened. |
| The usage window in the picker differs from the window in the figures | Expected. The picker sets the window for the *next* scan; the figures state the window they were measured over. |

## Related pages

- [Entra ID]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

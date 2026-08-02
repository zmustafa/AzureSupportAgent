---
layout: default
title: "Entra: privileged access"
parent: Governance & Identity
grand_parent: User guide
nav_order: 7
description: Standing versus eligible directory privilege, per-role PIM configuration health, merged Entra and Azure activation sessions, and the cross-plane join between directory power and Azure RBAC power.
permalink: /user-guide/governance-identity/entra-privileged/
feature_ids: [PROACTIVE_NAV:entra, ENTRA_NAV:privileged]
---

# Entra: privileged access

**Product permission:** `entra.read` for every view on this tab; `entra.admin` to start a collection from the freshness badge.

## Purpose

**App route:** `/entra/privileged`

Privileged access answers who can do what in the directory, whether that power is permanent or has to be switched on, what the tenant requires before it can be switched on, who actually switched it on, and what they did while it was on. It reads the Entra snapshot and — where an Azure RBAC scan exists — joins it with Azure control-plane power for the same principal. Nothing here activates, assigns, or removes a role.

## Prerequisites and data sources

- `entra.read` to view; `entra.admin` to refresh. No write action on this tab touches the directory.
- Consent tier 1 (`Directory.Read.All`, `RoleManagement.Read.Directory`) for role definitions and assignments. Without it the whole tab is blind.
- Consent tier 3 for PIM depth: `PrivilegedAccess.Read.AzureAD` and `RoleAssignmentSchedule.Read.Directory` for eligibility, permanence and activation history; `RoleManagementPolicy.Read.Directory` for the per-role activation policy; `PrivilegedAccess.Read.AzureADGroup` for PIM for groups.
- **Entra ID P2.** PIM does not exist below P2. On a P1 tenant the PIM domain reports `unlicensed`, permanence cannot be resolved, and there is no eligibility or activation data to read.
- Activation **history** and PIM **configuration** are separate collections behind separate scopes. `RoleManagementPolicy.Read.Directory` fills the PIM config grid; it does not produce a single activation row. Grant `RoleAssignmentSchedule.Read.Directory` for that.
- An Azure ARM connection with a completed RBAC scan for the cross-plane view and for the Azure half of activation sessions. The join reads the existing RBAC cache; it starts no new Azure collection.

See [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/) for the full scope list and the coverage states.

## Tabs and actions

| Sub-tab | What it answers |
| --- | --- |
| Overview | Headline privilege counts, the Azure-link state, and the privileged-access findings for this snapshot |
| Assignments | Every role assignment with its permanence, its path, its tier, and when it was last activated |
| PIM config | Per privileged role, what activation actually requires |
| Activations | Merged Entra and Azure activation sessions, with a drawer for what each principal did |
| JIT hygiene | Privilege meant to be just-in-time that quietly became permanent, and eligible roles nobody activates |
| Cross-plane | Directory power beside Azure control-plane power, one row per principal |

**Overview** shows six counts: global admins, privileged principals, standing privileged assignments, eligible assignments, PIM policies fully configured as a fraction of all PIM policies, and cross-plane principals. Global admins are toned as a problem when there are more than five or fewer than two. A banner states either that cross-plane analysis is unavailable and why, or that the Azure RBAC cache is older than the Entra snapshot and the join is indicative rather than current.

**Assignments** filters by kind — `standing`, `eligible`, or `all` — and by a free-text search over principal name, UPN, and role name. The API additionally accepts a `tier` and a `principal_type` filter. Columns are principal, principal type, role, tier, kind, permanence, and last activation. `kind` distinguishes `active`, `group-derived` (annotated with the group it came through), and `eligible`. Permanence reads `yes`, `time-bound`, or `unknown`; when PIM schedule data was unavailable the grid says so in a banner rather than guessing, because an active assignment and a live activation are indistinguishable without it.

**PIM config** lists privileged roles worst-configured first with a score out of 100 and one column per activation control: MFA on activation (satisfied by an authentication context), approval, justification, ticket, bounded duration (treated as satisfied at eight hours or less), notifications, and the raw maximum activation duration. If no policies were collected the tab reports the PIM domain state — `unlicensed`, `blind`, or genuinely clean — instead of an empty grid.

**Activations** merges what the current snapshot can see with a durable local ledger. Six tiles — sessions, Entra ID, Azure, tier-0, out of hours, no reason given — double as filters, alongside a search over person, role, scope and reason, a plane selector (both, Entra ID, Azure resources), a tier selector (any, tier-0, tier-1), and a window of 7, 30, 90 days or everything recorded. The browser sends its own UTC offset so the working-day judgement is made in tenant-local time; the endpoint takes that offset plus a business-hours start and end hour, and returns facets for plane, tier-0, out-of-hours, missing and weak justification, activations granted by someone other than the requestor, and distinct principals. Selecting a session opens a drawer with its start, end, granted duration, outcome and stated reason.

Below the filters, an **activation window** bins every loaded session across the real span it covers, with tier-0 stacked in red. Drag either handle, click a column, or use the relative presets to brush a sub-window; the table follows it. The brush is a client-side slice of what was already returned, so it costs no round trip — widening past the loaded range is what the days selector above is for. Changing any filter clears the brush rather than silently hiding rows from the new result set.

Inside that drawer, **actions** answers what the principal actually did during the window. It is the only call in this feature that reaches Microsoft, it runs for one session on demand, and its result is cached. Entra directory audits and the Azure Activity Log are read for the activation window plus a two-minute pad, and every action is classified rather than blamed: `required_activation` when the principal holds no standing role that covers it, `possible_without` when a standing role already allowed it, and `unclassified` when the standing picture is unreadable.

Two further datasets are served by the API without a control on this tab. `GET /api/entra/privileged/activations-export` returns an evidence pack: every session in the window with the ledger timestamps and a `provenance` block naming the exact Graph and ARM endpoints each claim came from. `GET /api/entra/privileged/principal/{principal_id}` returns a per-principal dossier — effective role names, every assignment, recent activations, Azure reach, and the findings raised against that object.

## Freshness and scope behavior

One collection builds one snapshot per tenant, and that snapshot serves every Entra tab. Refresh from the freshness badge in the page header. Sub-tabs never collect on their own; opening Assignments or PIM config reads what is already cached.

The activation ledger is the exception to snapshot-only reading. Graph retains directory audit history for about 30 days, so each refresh folds the sessions it can see into an append-only per-tenant record. A session already on record is refined, never duplicated, and history therefore reaches past the retention cliff — but only back to the first collection this product ever ran for the tenant.

The Azure half of cross-plane and of activation sessions comes from the RBAC cache, which is refreshed by its own scan. It can be older than the Entra snapshot, and every consumer carries the `stale` flag and the cache timestamp.

## Interpretation of results

- **Standing privilege is the finding, not the count.** A permanent tier-0 assignment is materially different from an eligible one that is activated twice a month with approval and a ticket. The Assignments grid separates them; the headline count does not.
- **Tier is derived from the role name**, so tier-0 means a role on the tenant-takeover list, tier-1 a role with broad but bounded power, tier-2 everything else.
- **A PIM score of 100 means every control is on**, not that the role is safe to hold. A bounded duration with no approval and no MFA still scores partially.
- **Out of hours is a question, not a verdict.** It is computed from the offset the browser reported and the configured working day; a distributed operations team will generate them legitimately.
- **Attribution is deliberately conservative.** `possible_without` does not mean nothing happened — it means the elevation was not what made it possible.
- **Cross-plane rows are the concentration risk.** A principal with both directory power and Azure Owner or User Access Administrator is a single point of total compromise, and no Microsoft surface shows that pairing in one place.

## Safety and limitations

- Every view is read-only. No role is activated, assigned, extended, or removed, and no PIM policy is changed.
- The ledger and the cached action results are stored locally per tenant and are never written back to Entra.
- Activation history before the first collection cannot be recovered; the ledger starts when you start.
- The action window is padded by two minutes because audit ingestion lags. Actions that landed outside that pad are not shown.
- Azure actions are recovered per subscription and are bounded per session; a very busy window is truncated rather than paged indefinitely.
- Grids are capped — assignments at 2000 rows and activation sessions at 500 per page — with the true total reported alongside.
- Exports and dossiers contain identity metadata. Treat them as governance material and do not paste live tenant, object, or user identifiers into tickets or prompts.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Permanence shows `unknown` for every row | PIM schedule data was not collected. Grant tier 3 and confirm the tenant has Entra ID P2. |
| PIM config grid is empty | Read the state on the empty panel: `unlicensed` is a licence problem, `blind` is a consent problem. |
| PIM config is populated but there are no activations | Grant `RoleAssignmentSchedule.Read.Directory`; configuration and history are separate collections. |
| Activation list stops about 30 days back | That is Graph retention. The ledger extends it forward from the first collection, not backwards. |
| Cross-plane says unavailable | No Azure RBAC scan exists for this tenant. The banner carries the reason. |
| Cross-plane shows a stale warning | The RBAC cache predates the Entra snapshot. Re-run the RBAC scan for a current join. |
| Out-of-hours counts look wrong | The judgement uses the browser's UTC offset. Check that the machine's time zone matches the tenant's working day. |
| Names appear as raw object IDs | The people collector failed or lacks permission; fix consent and refresh. |
| Refresh returns a permission error | Collection requires `entra.admin`, not `entra.read`. |

## Related pages

- [Entra ID]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/)
- [Entra: applications and consent]({{ site.baseurl }}/user-guide/governance-identity/entra-applications/)
- [Review identity, PIM, and app registrations]({{ site.baseurl }}/how-to/governance-identity/identity-reviews/)
- [IAM]({{ site.baseurl }}/user-guide/governance-identity/iam/)

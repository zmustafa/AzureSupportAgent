---
layout: default
title: Review privileged access and activations
parent: Governance and identity
grand_parent: How-to guides
nav_order: 11
description: Compare standing and eligible privilege, review per-role PIM configuration, analyze activation sessions and what was done during them, and produce an evidence pack.
permalink: /how-to/governance-identity/review-privileged-activity/
feature_ids: [PROACTIVE_NAV:entra, ENTRA_NAV:privileged]
---

# Review privileged access and activations

## Prerequisites

- Product permission `entra.read` for every view on this page. `entra.admin` is only needed to start a collection or change finding state.
- Tier 1 consent for directory role definitions and assignments.
- Tier 3 consent for PIM depth: per-role configuration, eligibility schedules, and activation history. Activation *history* is a separate scope from PIM *configuration*, so PIM can be measured while activations remain blind.
- Appropriate P2/ID Governance licensing for PIM. For native activation detail, use `RoleManagement.Read.Directory` for schedule instances and `AuditLog.Read.All` for the PIM audit log; adding Graph write scopes is not the read-only solution.
- An Azure ARM connection for the cross-plane view. Without it the Azure side is reported as unavailable with a reason, rather than shown as zero.
- A completed collection for the tenant.

## Route

`/entra/privileged`, with the **Overview**, **Assignments**, **PIM config**, **Activations**, **JIT hygiene** and **Cross-plane** sub-tabs.

## How to compare standing versus eligible privilege

1. Open `/entra/privileged` on the **Overview** sub-tab and read the KPI row: global admins, privileged principals, standing privileged, eligible, how many roles are fully configured in PIM, and how many principals hold power in both planes.

2. Treat the standing privileged figure as the headline. Standing privilege is permanent power that never passes through an approval, a justification or an expiry.
3. Open Assignments and switch standing/eligible/all, then search by principal or role. Tier and principal-type filters are API parameters, not current UI selectors. Inspect permanence: the standing lens alone does not prove every active row is permanent.
4. Search for a principal or a role to narrow the list. Each row carries the role, the assignment kind — active, group-derived or eligible — and the last activation recorded for that principal and role.
5. Pay particular attention to group-derived assignments. Privilege inherited through a group is the form most often missed in a manual review.
6. Use JIT hygiene only with its separate source/error notes and `identity.read`. Its bundled legacy live pipeline has standing candidates but no live schedule-only groups. Use native PIM config/Activations for collected schedule evidence instead of treating demo-only hygiene rows as real history.

**Expected result:** A candidate list of standing assignments to convert to eligibility, and eligible assignments to remove because they are never used.

**Verification:** Confirm the assignment type and activation history for each candidate in Microsoft Entra PIM before proposing a change. Graph data here is a cached snapshot.

## How to review per-role PIM configuration health

1. Open the **PIM config** sub-tab. Each privileged role is a row and each control is a column: MFA on activation, approval, justification, ticket, bounded duration and notifications.

2. Read the per-role score alongside the controls. A role that is eligible-only but activates with no MFA, no approval and no duration limit is standing privilege with extra steps.
3. Prioritize tier-0 roles. A weak activation policy on a highly privileged role is worth more than a missing notification on a minor one.
4. Check the domain state shown with the grid. If PIM configuration is blind or unlicensed, an empty grid means "could not look", not "nothing to fix".
5. Record the roles whose activation requirements need to change, with the specific control that is missing.

**Expected result:** A per-role list of missing activation controls, ordered by role tier.

**Verification:** Open the role setting in Microsoft Entra PIM and confirm the control state matches before raising a change.

## How to review activations and drill into one session

1. Open the **Activations** sub-tab. The tiles summarize the window: total sessions, Entra ID versus Azure, tier-0 activations, activations outside the working day, and activations with no reason given.

2. Set the window with the day selector — 7, 30, 90 days, or everything recorded. History reaches past the retention Graph offers because sessions are merged with a durable local ledger; the source banner states what came from where.
3. Filter by plane to separate directory elevation from Azure resource elevation, and by tier to isolate tier-0.
4. Select a tile to filter to that cohort — for example out-of-hours activations. Out-of-hours is judged against your browser's timezone offset, which is shown, rather than against UTC.
5. Search across person, role, scope, reason and ticket number to follow one thread.
6. Open a session to read who elevated, the role and scope, when it started and ended, how long it was granted for, the outcome, the justification quality, and whether it was self-service or granted by someone else.
7. Select **what they did** to open the drawer and start its action read immediately. It uses a two-minute padded window and, for Azure activations, the session's own subscription. Read error notes before interpreting an empty result. Attribution checks broad standing-role presence, not exact historical permission for every operation.

**Expected result:** A short list of activations that need an explanation, each with the actions taken during the elevated window.

**Verification:** Confirm the activation and its justification in Microsoft Entra PIM, and confirm the actions against the Azure activity log for the subscription in question.

The following browser fixtures illustrate the session-to-action review, not a live Microsoft Graph read or a computed assessment of privileged activity.

{% include screenshot.html file="identity-activation-ledger.png" title="Privileged activity: review recorded activation sessions" caption="Start with the role, principal and time window on the two modeled Entra sessions. Aggregate facet counts are not populated by this fixture, so the tiles must not be treated as measured review totals." %}

{% include screenshot.html file="identity-activation-actions.png" title="Activation drawer: actions and elevation attribution" caption="Open a session to compare its actions with the attribution labels, which are leads rather than causal proof. The on-demand action read shown here is answered by a fixture, not Microsoft Graph." %}

## How to check cross-plane power and produce the evidence pack

1. Open the **Cross-plane** sub-tab. Each row is a principal with its Entra roles and permissions beside its Azure roles, broad scopes and subscriptions.

2. Read the availability and age of the Azure side first. If the ARM link is unavailable or stale, the page says so with a reason — the correlation is only as current as its weaker half.
3. Prioritize principals holding power in both planes. A principal who can grant themselves directory roles and also controls subscriptions is a single point of total compromise, and this correlation does not exist in any Microsoft surface.
4. Use the principal's Investigate link with `investigate.read`; applicable non-Azure activity may load automatically under `investigate.activity`. The API-only privilege dossier is a separate endpoint, not the link's destination.
5. Use `/api/entra/privileged/activations-export` for retained sessions in the review window, or Posture's workbook. The Activations tab itself has no export button. The table loads at most 500 sessions, Cross-plane 1,000; neither page count guarantees export completeness.
6. Store the export as governance material. It contains identity metadata; it contains no secret or certificate value, because none is ever retrieved.

**Expected result:** A dated evidence pack for the review window, plus a named list of cross-plane principals to reduce.

**Verification:** Spot-check several exported rows against Microsoft Entra PIM and the Azure portal, and confirm the export's window and generation time match the review period.

## Safety and rollback

These views do not activate roles, approve requests or change assignments/PIM policies. Action enrichment reads Microsoft and stores a local cache; native refresh also updates the activation ledger, retained up to 100,000 sessions. A cached action result can be older than later events and needs an API `refresh=true` to bypass it. Preserve raw source evidence for consequential conclusions.

Removing standing privilege, converting an assignment to eligible, or tightening an activation policy happens in Microsoft Entra PIM through your approved change process. Plan the rollback before the change: keep at least two confirmed emergency access accounts, stage the removal of standing privilege one role at a time, and re-collect afterwards so the next review sees the new state. Exports and dossiers name real people and real roles — never paste tenant IDs, object IDs, user principal names or justification text into tickets, prompts or shared examples.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| PIM configuration is measured but activations are blind | Activation history needs its own scope. Grant it from `/entra/setup`, re-check permissions, then collect. |
| The PIM grid or activation list is empty | Check the domain state on the grid. Blind or unlicensed means "could not look", not "nothing found". |
| The Azure side of cross-plane is missing | No ARM connection is linked, or the link is stale. The page names the reason. |
| Out-of-hours counts look wrong | The judgment uses your browser's timezone offset, which is shown beside the tile. Confirm it matches the tenant's working day. |
| A justification reads as unknown rather than missing | The source for that session cannot carry a justification, so no judgment is made. |
| Names appear as raw object identifiers | The resolving collector failed or lacks permission. Fix consent and collect again. |
| Activation history is shorter than expected | Graph retention is limited; the durable ledger only holds what was collected while the product was watching. |

## Related docs

- [Entra ID]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [Entra privileged access]({{ site.baseurl }}/user-guide/governance-identity/entra-privileged/)
- [Entra blast radius]({{ site.baseurl }}/user-guide/governance-identity/entra-blast-radius/)
- [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/)

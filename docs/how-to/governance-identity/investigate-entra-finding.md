---
layout: default
title: Investigate and close an Entra finding
parent: Governance and identity
grand_parent: How-to guides
nav_order: 9
description: Filter the Entra findings inbox to a working set, investigate a finding against its signal definition, apply workflow state individually or in bulk, and confirm closure in the next collection.
permalink: /how-to/governance-identity/investigate-entra-finding/
feature_ids: [PROACTIVE_NAV:entra, ENTRA_NAV:findings, ENTRA_NAV:investigate]
---

# Investigate and close an Entra finding

## Prerequisites

- Product permission `entra.read` to read the inbox, a finding and its signal definition.
- Product permission `entra.admin` to change finding state, apply a bulk action, or run a scanner.
- A completed collection for the tenant. The inbox reads the snapshot; it never collects on its own.
- Tier 1 consent produces findings across posture, applications and directory roles. Tier 2 and tier 3 consent, plus Entra ID P1/P2, are what make the risk, PIM and governance signals measurable rather than blind.

## Route

`/entra/findings` for the inbox, scanners and identity hygiene; the deep-dive tabs at `/entra/conditional-access`, `/entra/privileged`, `/entra/applications`, `/entra/signals` and `/entra/governance` for context.

## How to filter the inbox to a working set

1. Open `/entra/findings` and stay on the **Findings inbox** sub-tab.

2. Use the severity chips to narrow to **Critical** or **High**. Each chip carries its own count, so you can see the size of the working set before you commit to it.
3. Use the state chips to separate **Open** from **Snoozed**. An expired snooze returns to open on its own; nobody has to remember to un-snooze it.
4. Add **Unassigned** to find work with no owner, and **Ageing > 30d** to surface findings that have been open long enough to be a process failure rather than a new event.
5. Use the search box to narrow to an object name or finding title.
6. Read the summary line: total findings, how many are suppressed, and how many were resolved automatically because the condition stopped appearing.

**Expected result:** A short, ordered list — worst severity first, then oldest — that one person can actually work through.

**Verification:** The row count in the summary line matches the filter chips you selected, and the **Age** column shows a first-seen age rather than a collection timestamp.

## How to investigate one finding and record an owner

1. Select a row to open the finding drawer.

2. Read the title and detail, then the **Why this matters** block. That text comes from the signal definition in the registry, not from the individual finding, so it explains the class of problem.
3. Read the **Evidence** list. These are the exact values the signal fired on and they are what make the score verifiable rather than a black box.
4. Read the **Remediation** section, including the numbered steps where the signal provides them.
5. Follow the portal link to validate the object in the Microsoft Entra admin center, and the documentation link where the signal carries one. Graph data is cached and eventually consistent — validate before you act.
6. Record the owner. Each finding carries an assignee in its workflow state, shown under the state column and filterable with the **Unassigned** chip. Set it where the app offers the assignee field.
7. Select **Acknowledge** to mark the finding as being worked. Use **Reopen** to move it back to open if the investigation shows it was acknowledged in error.

**Expected result:** A finding in the acknowledged state with an owner, and a validated understanding of what has to change in Entra.

**Verification:** The row's state changes in the inbox and the assignee appears beneath it. Confirm the underlying object still matches the evidence in the Microsoft Entra admin center.

## How to investigate one principal across identity evidence

1. Open `/entra/investigate` from a finding or select the principal directly.
2. Confirm the principal identity and object type before reading access, group, application, sign-in, or activity evidence.
3. Separate structural access from behavioural history: activity requires its additional product permission and may be unavailable even when identity detail is visible.
4. Follow handoffs to IAM or Conditional Access only when the destination preserves the same principal or scope.
5. Validate significant conclusions against the named source record and the current Entra portal state.

**Expected result:** One principal's identity, structural reach, and available activity evidence are correlated without treating missing domains as clean results.

**Verification:** The selected object remains the same across each handoff, timestamps are current enough for the decision, and every conclusion cites source evidence.

## How to snooze or suppress deliberately

1. Select one or more rows with the row checkboxes. The bulk bar appears with the selection count.

2. To defer, choose **Snoozed**, set the number of days, and add a reason. A snooze must have a duration so it can expire on its own and return the finding to open.
3. To close without fixing, choose **Suppressed** and enter a reason. The reason is mandatory: a suppression is a decision that this finding does not apply to your tenant, not evidence that the risk was removed.
4. Understand the consequence before suppressing. Suppressions persist across refreshes, are never rewritten by a collection, and are excluded from the posture score — so a suppressed finding stops costing points while the underlying configuration is unchanged.
5. Select **Apply**. Use **Clear** to abandon a selection.
6. Review suppressions periodically. The summary line always shows how many exist.

**Expected result:** The selected findings carry the chosen state with a durable reason, and snoozes carry an expiry date.

**Verification:** Filter by **Snoozed** to see the deferred set. The suppressed count in the summary line increases by exactly the number you suppressed, and the posture score changes only because those signals were excluded.

## How to close the loop after remediation

1. Remediate outside this app, in the Microsoft Entra admin center, through your approved change process. Nothing in this feature writes to the directory.

2. Return to `/entra` and select **Refresh** to collect a new snapshot, then wait for the progress strip to finish.
3. Open `/entra/findings` and read the recently resolved entries in the summary line. A finding resolves when the condition stops appearing in a collection, not when someone ticks a box.
4. Read the posture headline on `/entra` for the change since the previous run, and use the pillar breakdown to confirm the points came back where you expected.
5. Re-run the relevant scanner from the **Scanners** sub-tab if you want a targeted re-check. Scanners read the current snapshot and never call Microsoft Graph, so running one repeatedly during an investigation is safe.
6. If the finding is still present, compare the evidence against the change you made before assuming the collection is wrong.

**Expected result:** The remediated finding is absent from the new snapshot and appears in the resolved set.

**Verification:** The finding no longer appears under an unfiltered view, the score delta is consistent with the signal's severity weight, and the Entra portal shows the corrected configuration.

## Safety and rollback

Reading the inbox, opening a finding, and viewing scanner results are all read-only, and viewing scanner results deliberately does not record a run — otherwise merely looking would consume the new-versus-resolved baseline. Acknowledging, snoozing, suppressing, running a scanner and applying a bulk action are local writes stored per tenant by this product, recorded in the audit trail, and never pushed to Entra. Every one of them is reversible: reopen the finding, or apply a different state to the same selection.

The remediation itself always happens in Entra through your change process, with its own approval and rollback. Treat a bulk suppression as the highest-risk action on this page, because it removes findings from the score for everyone who reads it afterwards. Never paste real tenant IDs, object IDs, user principal names or evidence values into tickets, prompts or exported examples.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| The inbox is empty and says nothing is loaded | Run a collection from the freshness badge; the inbox never collects on its own. |
| A state change returns a permission error | Workflow actions require `entra.admin`, not `entra.read`. |
| Suppress is rejected | A suppression requires a reason; enter one and re-apply. |
| A snooze is rejected in the bulk bar | A snooze requires a number of days so it can expire on its own. |
| A remediated finding is still listed | The snapshot predates the fix. Refresh, then re-read the inbox. |
| A scanner says it cannot run | Its domain is blind or unlicensed; reporting zero findings would be indistinguishable from having looked. Fix coverage first. |
| The score moved but no finding changed | Coverage changed. Compare measured pillars rather than the headline number. |

## Related docs

- [Entra ID]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [Entra findings and scanners]({{ site.baseurl }}/user-guide/governance-identity/entra-findings-scanners/)
- [Entra posture and score]({{ site.baseurl }}/user-guide/governance-identity/entra-posture/)
- [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/)

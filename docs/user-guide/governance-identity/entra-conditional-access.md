---
layout: default
title: "Entra: Conditional Access"
parent: Governance & Identity
grand_parent: User guide
nav_order: 6
description: Read the Conditional Access coverage matrix, the normalized policy list, detected conflicts, break-glass candidates, the policy-as-code export and the offline change simulator.
permalink: /user-guide/governance-identity/entra-conditional-access/
feature_ids: [PROACTIVE_NAV:entra, ENTRA_NAV:conditional-access]
---

# Entra: Conditional Access

**Product permission:** `entra.read` for coverage, policies, conflicts, break-glass and saved simulations; `entra.admin` to confirm a break-glass candidate, to run a simulation and to re-run a saved one.

## Purpose

**App route:** `/entra/conditional-access`

The Microsoft Entra admin center shows Conditional Access one policy at a time. Every question that matters is a join across policies: who is protected by nothing, which exclusion defeats which control, which policy is shadowed by another, and whether an emergency account is about to be locked out. This page performs that join over the cached snapshot and answers it per cohort, per application class and per control.

The analysis is a pure function of the snapshot: deterministic, testable and repeatable. Nothing on this page writes a policy, an exclusion or an assignment to the tenant.

## Prerequisites and data sources

- A connection that can obtain a Microsoft Graph application token, and at least one completed collection for the tenant.
- Product permission `entra.read` to view; `entra.admin` for the break-glass confirmation and for any simulator run, both of which are written to the audit trail.
- Read-only Graph consent. Conditional Access policies, named locations and authentication strengths come from the tier 1 policy scope; resolving a policy's effective users through groups and roles, and knowing whether a user has an MFA method registered, depend on tier 2 group, membership, device and authentication-method scopes. See [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/).
- Entra ID P1 for Conditional Access itself. Without it the domain reports as unlicensed rather than empty.

Policy resolution follows rules that are easy to get wrong by hand: exclusions win at every level, `All users` includes guests unless a guest filter narrows it, role-scoped policies expand to eligible role holders and not only active ones, and nested groups are expanded transitively.

## Tabs and actions

The page is organised into five sub-tabs, selected within the page.

| Sub-tab | What it shows |
| --- | --- |
| Coverage | The headline gap sentence and the cohort × application-class × control matrix |
| Policies | Every policy normalised, with resolved user counts, controls and application scope |
| Conflicts | Set-logic detections across policies, each with an explanation |
| Break-glass | Emergency-account candidates with a local confirm or reject decision |
| Simulate | The offline change simulator and the saved simulation list |

**Coverage** opens with the sentence the page exists for: how many enabled users and how many enterprise applications are matched by no enforced policy, how many of those principals hold privileged roles, and out of what totals. An expandable note states how the count was made — enabled policies only, exclusions applied, role-scoped policies including eligible holders, disabled accounts not counted.

Below it, one matrix per application class — All cloud apps, Microsoft Admin Portals, Office 365 and Azure Management — crosses the cohorts against the controls MFA, Phishing-resistant, Compliant device, Hybrid joined and Session limits. The cohorts are Global Administrators, all privileged roles, break-glass accounts, members that are not privileged, guests, likely service accounts, and users with no MFA method registered. Each cell reports the strongest state any policy achieves for that combination: enforced for the whole cohort, enforced for part of it, only a report-only policy applies, or nothing applies. Selecting a cell opens a drill-down listing the policies that produced it and the members of the cohort left uncovered, flagging those with no MFA method. The uncovered list is a capped sample and says so.

**Policies** is a filterable table of policy name, state, effective user count, excluded user count, controls and application scope. Opening a policy shows its resolved detail together with a sample of the users it effectively covers, a sample of the users excluded from it, and any conflicts that involve it. The samples are capped; large identifier lists are deliberately not sent to the grid.

**Conflicts** lists every detection with a kind, the policies involved, an explanation and the number of principals affected.

**Break-glass** ranks candidate emergency accounts by a heuristic score with the reasons that produced it, whether the account holds Global Administrator, which security policies cover or exclude it, and whether it has an MFA method registered. Confirm or reject each candidate; the page warns when a confirmed emergency account is covered by a control it cannot satisfy.

**Simulate** is described under its own heading below.

A policy-as-code export is served by the API at `/api/entra/ca/export` in either JSON or Markdown. The Markdown form is a policy book with one section per policy giving effective and excluded user counts, applications, client app types, controls, the grant operator and a stable fingerprint; the JSON form resolves names alongside the raw identifiers so the artifact is both readable and re-applyable.

### The simulator

The simulator answers "if I make this change, what changes" — never "here is what happens". It computes a baseline result and a proposed result per principal per sign-in context and reports only the difference, in five categories: newly blocked, protection lost, newly challenged, newly granted, and unchanged. Break-glass impact is rendered first and is never collapsed.

A run posts to `/api/entra/ca/simulate` with a list of changes, an optional list of sign-in contexts, an optional list of cohorts, a sample size that defaults to 400 and is clamped between 50 and 5000, and optional save and label fields. The change vocabulary is closed: enable, disable, set to report-only, delete, add and modify. Anything else is rejected rather than ignored. The page builds single-policy enable, disable, report-only and delete changes.

Contexts and cohorts are published by `/api/entra/ca/simulate/contexts`, which also returns the cohorts that are always evaluated in full and the model's limitations. The default contexts cover browser on an unmanaged device, desktop client on a compliant device, Exchange ActiveSync, other legacy clients, browser from a trusted location, Microsoft admin portals, Azure management, and a high-risk sign-in. Break-glass, break-glass candidates, Global Administrators and all privileged principals are always evaluated in full regardless of sample size; the remainder is a seeded sample so that repeated runs stay comparable.

Saving a run stores it locally with its input and result. The saved list shows the label, timestamp, headline counts, break-glass impact and a staleness marker when the run predates the current snapshot. Re-running a saved simulation evaluates the same input against the current snapshot; if the change refers to a policy that no longer exists, the re-run is refused with an explanation rather than silently returning a different answer.

## Freshness and scope behavior

One snapshot per tenant serves every Entra tab, so a single refresh from the freshness badge updates Conditional Access alongside posture, privileged access, applications, signals, governance and blast radius. Opening a sub-tab reads the cached snapshot; no tab collects on its own.

If Conditional Access has never been collected for the tenant, the simulator and the analysis endpoints report that plainly instead of returning an empty result. Coverage and the simulator both report a blind or unlicensed Conditional Access domain with a route to Setup & coverage rather than showing a clean matrix.

## Interpretation of results

Report-only policies protect nobody. They are visible in the policy list and can produce a report-only cell in the matrix, but they never count towards coverage, never appear in the headline sentence and never block anyone in a simulation.

Conflict kinds each mean something specific:

| Kind | Meaning |
| --- | --- |
| Privileged exclusion | A privileged principal is excluded from a security control |
| Block contradicts grant | An unconditional block always wins, so an overlapping grant can never be satisfied |
| Exclusion sprawl | A large share of the targeted population is excluded |
| No effect | The policy resolves to zero users, or to no application |
| Unreachable condition | Every included platform or location is also excluded |
| Redundant | Fully subsumed by another policy — same or narrower users, applications and controls |
| Duplicate | Identical conditions and grants to another policy |

Break-glass detection is a heuristic and is labelled as one everywhere it appears. It scores signals such as not being covered by any enforced security policy, being explicitly excluded from one, holding Global Administrator, being cloud-only, matching an emergency naming pattern, having no department or job title, and having no recent interactive sign-in. Guests are never candidates. The decision to accept a candidate is always the operator's, and it is a local annotation: confirming or rejecting an account is stored with the finding state for this product and is never written to Entra.

In the simulator, the distinction that carries the value is between a challenge and an effective block. "Requires MFA" is friction for a user with a registered method and a hard block for a service account that has none, and the model computes that from each principal's capability profile rather than assuming everyone can satisfy a control. Where MFA registration could not be read, the result says how many principals are unknown instead of guessing. Where the run was sampled, it states how many principals were evaluated out of how many exist.

Every result carries a confidence label and the published limitations. Read them: the model does not cover Continuous Access Evaluation revocation timing, app-enforced session restrictions inside the application, per-application authentication context inside workloads, live device compliance evaluation, or guest MFA satisfaction from a home tenant beyond what the cross-tenant access policy states. Risk levels are hypothetical inputs, not predictions, and only enabled policies are evaluated.

## Safety and limitations

- The simulator is an offline model of the snapshot, not a Microsoft what-if evaluation. It must never be the sole evidence for enabling, disabling or deleting a policy. Use it to find the population a change would break, then validate in the Microsoft Entra admin center and roll out through report-only mode and your approved change process.
- Nothing on this page writes to the directory. No policy is created, enabled, disabled or deleted, and no exclusion is added.
- Break-glass confirmations and saved simulations are local annotations only. They never reach Entra and are not a substitute for a documented emergency access procedure.
- Coverage, conflict and simulation results are only as current as the snapshot. A policy changed after the last collection is not reflected until the next refresh.
- Uncovered lists, effective user samples and excluded user samples are capped and the page states the cap. Treat them as evidence of a gap, not as a complete membership export.
- Service-account identification is a heuristic based on naming, missing MFA registration and absent interactive sign-in. Verify before excluding anything from a rollout on that basis.
- Exports and drill-downs contain identity metadata. Handle them as governance material and avoid pasting live tenant, object or user identifiers into tickets or prompts.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The page says nothing has been collected | Start a collection from the freshness badge; sub-tabs never collect on their own. |
| The Conditional Access domain reports blind | Grant the missing policy read scope on Setup & coverage, then re-collect. |
| The domain reports unlicensed | The tenant lacks Entra ID P1; more consent will not help. |
| A policy shows far fewer users than expected | Exclusions are applied at every level; open the policy and read the excluded sample. |
| A role-scoped policy covers more users than the portal suggests | Eligible role holders are included, not only active assignments. |
| The matrix shows a report-only cell | Only a report-only policy applies, which protects nobody. |
| The simulator refuses to run | Conditional Access has not been collected, or no change was supplied. |
| A saved simulation cannot be re-run | It references a policy that no longer exists in the current snapshot. |
| A saved simulation is marked as based on older data | It predates the current snapshot; re-run it before relying on the numbers. |
| A simulation or break-glass action returns a permission error | Both require `entra.admin`, not `entra.read`. |

## Related pages

- [Entra ID]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [Entra: posture and score]({{ site.baseurl }}/user-guide/governance-identity/entra-posture/)
- [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/)
- [Entra: findings and scanners]({{ site.baseurl }}/user-guide/governance-identity/entra-findings-scanners/)
- [Entra: privileged access]({{ site.baseurl }}/user-guide/governance-identity/entra-privileged/)
- [Review Entra ID posture end to end]({{ site.baseurl }}/how-to/governance-identity/entra-first-refresh/)

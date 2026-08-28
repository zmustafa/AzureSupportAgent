---
layout: default
title: Operate Recovery Readiness
parent: Coverage operations
grand_parent: How-to guides
nav_order: 6
description: Run a recovery analysis, read a breach, set objectives, tune restore rates and export an audit-ready report.
permalink: /how-to/coverage/recovery-readiness/
feature_ids: [PROACTIVE_NAV:resiliency, ROUTE:resiliency, RESILIENCY_NAV:overview, RESILIENCY_NAV:matrix, RESILIENCY_NAV:analysis, RESILIENCY_NAV:resources, RESILIENCY_NAV:targets, RESILIENCY_NAV:workloads]
---

# How to use Recovery Readiness

Concept and vocabulary live in the [user guide]({{ site.baseurl }}/user-guide/coverage/recovery-readiness/). This page is the task list.

## Prerequisites

* `resiliency.read` for every task on this page. Setting objectives and editing restore rates additionally needs `resiliency.admin`.
* An Azure connection with a workload or subscription in scope.
* **A Backup Manager analysis of the same scope.** Recovery Readiness joins Backup Manager's snapshot, which is operator-triggered. Without it every protection fact honestly reports `unknown` — see the first recipe below.

## Route

**Proactive Support → Coverage → Recovery Readiness** (`/resiliency`), with six tabs: Overview, Recovery matrix, Analysis, Resources, Targets & breaches, and Workloads.

## How to run your first analysis

1. Open **Proactive Support → Coverage → Recovery Readiness**.
2. Pick a **workload** or **subscription**. Nothing is fetched until you ask.
3. Click **Analyze recovery posture**.

Nothing runs automatically, and the numbers do not move while you are working a decision. Re-run it when you want fresh figures.

### Do this first, or half the screen says "unknown"

Recovery Readiness reads the **backup estate from Backup Manager's analysis**, which is itself operator-triggered. If Backup Manager has never analyzed this scope, every protection fact honestly reports `unknown`.

So: run **Backup Manager → Analyze backups** for the same scope first, then run this one.

## How to find what cannot be recovered at all

1. Go to **Overview**.
2. Look at **No recovery path**.
3. Click any ✖ cell to open the resource and see why.

This is the highest-value output of the module. It is not "slower than you'd like" — it is that no mechanism exists for that failure.

The **Recovery matrix** tab is the same grid over the whole scope, filterable by name or type; **Resources** is the flat list with each resource's worst class and portal link.

## How to read a breach

**Targets & breaches** lists everything that misses its objective, worst consequence first.

Each row shows the derived value, the objective, the tier that set it, and the evidence. If a row looks wrong, check the **tier** column first — a wrong tier is the most common cause of a disputed finding.

## How to set your own objectives

Requires `resiliency.admin`.

1. Open **Targets**, then switch to **Objectives & rates**.
2. Objectives live per criticality tier, per scenario. Pick a target RTO class and a target RPO in minutes for each row.
3. **Save objectives.**

A resource inherits its tier from its workload's criticality, so set that first in the workload registry rather than overriding resources one at a time.

Two things will not go through, by design:

* `unknown` is not offered as a target RTO. It means a source could not be read; an objective nothing can ever breach is not an objective.
* Structurally invalid values — an unknown tier, a scenario that does not exist, a rate outside its bounds — come back **named** in a *Some values were refused* panel rather than being silently dropped, so you never leave believing a number you did not set.

Then **Acknowledge these objectives** on the Targets tab. Until you do, an export that quotes them is refused — a number in an auditor's PDF has to have been agreed by a person.

## How to tune the restore rates

Requires `resiliency.admin`. Same place: **Targets → Objectives & rates**, lower panel.

Duration bands are computed from throughput rates in the reference registry. The shipped values are **starting points, not truths**. If you have measured your own restore throughput, set it — every band names the rate that produced it, so the change is visible in the report.

The fixed mechanism overheads next to them (failover time, vault restore overhead, detect-and-decide) are editable for the same reason. Anyone with read access can *see* every one of these constants even without `resiliency.admin`, because a band derived from a number nobody can inspect is not reviewable.

Bands widen and confidence drops when the data volume is unknown. That width is deliberate; it is the honest signal that the estimate is weak.

## How to find the fix that moves the most resources

Open **Analysis**. Two tables, both ranked by consequence:

1. **RTO and RPO by resource type** — start at the top. The *Dominant reason* column names the one thing that explains the most resources in that row.
2. **Why — the reasons that explain the most** — the same reasons across the whole estate, with how many resources each accounts for.

Reading these columns correctly matters:

* **Undet.** is resources whose verdict could not be determined. It is counted separately from everything else, never folded into a rate.
* **Median RPO** covers only resources whose recovery point could be measured. **RPO excl.** is how many it left out; a median quoted without it describes fewer resources than the reader will assume.
* A resource type that cannot experience a scenario is **absent from that scenario**, not shown as meeting its objective.

The panel above the tables lists redundant resources whose corruption/deletion answer is at least two RTO classes worse than their infrastructure answer. Those are the rows no zone-centric tool flags.

## How to export a report

Requires `resiliency.read`. Both formats are refused while the objectives are still the shipped defaults; acknowledge them first.

* **⬇ Excel** — sixteen sheets, every row. Use this to pivot, cross-check, or hand over. The **Reasoning** sheet is one row per fact, so you can filter and count the evidence; the **Truncation** sheet says whether anything was dropped.
* **📄 PDF** — the readable report: executive summary, trend, per-type analysis, dominant reasons, unrecoverable resources, breaches, workload roll-up and appendices. Long sections are capped and state how many rows they omitted.
* **🗄 Evidence** — freezes the analysis as an immutable Evidence Locker snapshot with a SHA, so it can be diffed against a later capture.

Both files are named `recovery-readiness-<scope>-<date>`, so exporting three subscriptions in a row leaves three distinguishable files.

Read the Provenance sheet, or Appendix C in the PDF, before drawing conclusions from an empty section. "No findings" and "could not look" are opposite facts.

## How to roll it up per workload

Open **Workloads**. Each card gives the workload's worst class per scenario and names the **weakest link** — the single component that set the number. Every component is treated as required, so the roll-up is the pessimistic answer; a genuinely redundant pair recovers faster than shown.

## How to find what a parent deletion would destroy

The deletion column answers "can this resource be recovered". It does **not**, on its own, answer "what if someone deletes the thing that contains it" — and for several Azure services those are different answers.

1. Open the **Accidental deletion** column and look for cells carrying a `!`.
2. Open the drawer and read the **Does not cover** note. It names the radius the recovery path stops at.
3. Act on the ones that say *unrecoverable*:
   * **Azure SQL** — configure long-term retention. It is the only backup that survives deleting the logical server.
   * **PostgreSQL / MySQL flexible server** — five days is the whole window, and recovery is a management API call with no portal path. Enrolling the server in a Backup vault removes that cliff, because vaulted backups are held outside the subscription.
   * **Storage accounts** — soft delete does not protect the account. Either apply a lock or use vaulted blob backup, which does survive account deletion.

A resource already protected by a Backup vault shows a softer note naming the tier, because vaulted and operational backups differ on exactly this question and the tier is not visible from Resource Graph.

In the workbook the same information is the **Recovery limit** and **What it does not cover** columns of the Recovery matrix sheet, so you can filter the whole estate by severity.

## How to ask the agent

With `resiliency.read` you can ask in chat:

* *"What is the recovery posture for the payments workload?"*
* *"What has no recovery path for accidental deletion?"*
* *"Which databases would be lost if someone deleted their server?"*
* *"What misses its recovery objectives?"*

Answers carry the configuration that produced them. Quote the basis, not just the number. For deletion the agent also returns `does_not_cover`, which is the radius the answer stops at.

## How to add it to a mission

Recovery Readiness is a Mission Control system (**♻️ Recovery Readiness**). Add it to a mission to have it re-derived on the normal sweep. Its headline leads with the count of resources that cannot be recovered rather than a score, because a count is actionable and a score invites comparison.

## How to count it against the Reliability score

Off by default. To turn it on, set `assessments_include_recovery` in **Admin → Settings**.

Once enabled, an Assessments run that includes the Reliability pillar picks up three extra controls from the latest Recovery Readiness snapshot for that workload: resources with no recovery path, redundant resources with no point-in-time copy, and resources breaching their objectives.

Two things to expect:

* **Existing scores will move down.** That is why it is opt-in — announce it, or somebody watching a trend line will read it as a regression that nobody caused.
* **A workload that has never been analyzed contributes nothing.** The controls report *not applicable* and drop out of the score entirely. Run the analysis first, or the pillar is simply quiet about recovery.

## Safety and rollback

Recovery Readiness is **analysis only**. It never changes an Azure resource, never starts a failover and never restores anything — remediation deep-links into Backup Manager, which owns its own approvals. There is nothing here to roll back in Azure.

Two things it does write, both local to this application and both reversible:

* **Objectives and restore rates.** Saving snapshots the outgoing version first, so a previous set can be restored from the reference registry. Acknowledgement is a separate, deliberate act.
* **Analysis snapshots and trend points.** Re-running replaces the stored snapshot for that scope and appends one counts-only trend point.

Two cautions that are not about rollback but will cost you if missed:

* **Enabling the Reliability contribution lowers existing scores.** Announce it before turning `assessments_include_recovery` on, or a watcher reads it as a regression nobody caused.
* **Exports name real resources.** Treat the workbook, the PDF and the Evidence Locker snapshot as governance material.

## Related docs

* [Recovery Readiness (feature reference)]({{ site.baseurl }}/user-guide/coverage/recovery-readiness/) — the vocabulary, the five scenarios, and how each verdict is derived.
* [Backup Manager]({{ site.baseurl }}/how-to/coverage/backup-manager/) — run this first; it supplies the protection facts.
* [Backup & DR Coverage]({{ site.baseurl }}/how-to/coverage/backup-dr-coverage/) — is the estate protected at all, as opposed to how fast it recovers.
* [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/) — where the Reliability-pillar contribution lands.
* [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/) — where a frozen analysis is stored and diffed.
* [Permissions]({{ site.baseurl }}/reference/permissions/) — `resiliency.read` and `resiliency.admin`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Everything says `unknown` | Backup Manager has not analyzed this scope. Run that first. |
| A resource shows `unknown` protection | Its type is not mapped to a backup datasource. It is not unprotected — we did not look. |
| Export returns a 409 | The objectives are still the shipped defaults. Acknowledge them on the Targets tab. |
| A band looks implausible | Check the restore rate in the reference registry, and whether the resource's data size is known. |
| A workload's RTO looks pessimistic | Every component is treated as required. A genuinely redundant pair recovers faster than the figure shown. |
| Saving objectives lists refused values | The value is structurally invalid (unknown tier or scenario, or a rate outside its bounds). Fix the named field — nothing was silently accepted. |
| Reliability score did not change after enabling the contribution | Recovery Readiness has not analyzed that workload, so its controls are *not applicable*. |
| Export returns nothing and shows an error | Read it: the objectives are un-agreed (409) or the scope was never analyzed (400). Both are explained on screen. |
| The Trend panel says there is no direction | Fewer than two analyses have been recorded for this scope, or the scope is demo data, which is never recorded. |
| Trend improved but the caveat says otherwise | "No recovery path" fell while "undetermined" rose — the same resources probably became unreadable. That is a collection problem, not a recovery improvement. |
| The PDF omits rows the workbook has | Deliberate. Long sections are capped so the report stays readable; each says how many it omitted. The workbook is unbounded. |

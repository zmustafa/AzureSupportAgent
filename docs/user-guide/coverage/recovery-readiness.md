---
layout: default
title: Recovery Readiness
parent: Coverage
grand_parent: User guide
nav_order: 6
description: Recover from what, in how long, losing how much — per-scenario RTO and RPO derived from redundancy, backup frequency and replication, measured against your objectives.
permalink: /user-guide/coverage/recovery-readiness/
feature_ids: [PROACTIVE_NAV:resiliency, ROUTE:resiliency, RESILIENCY_NAV:overview, RESILIENCY_NAV:matrix, RESILIENCY_NAV:analysis, RESILIENCY_NAV:resources, RESILIENCY_NAV:targets, RESILIENCY_NAV:workloads, PERMISSION:resiliency.read, PERMISSION:resiliency.admin]
---

# Recovery Readiness

**Product permissions:** `resiliency.read` to view; `resiliency.admin` to set recovery objectives and tune the restore-rate reference.

## Purpose

**App routes:** `/resiliency` and `/resiliency/:tab`

Every other view in this area answers a neighbouring question. Backup Manager answers *is backup working*. Backup & DR Coverage answers *is it configured*. Assessments answers *does it meet best practice*. FMEA answers *what could fail and how bad*.

Recovery Readiness answers the one an application owner actually asks:

> **Recover from what, in how long, losing how much?**

It is read-only and makes no changes to Azure.

## The idea you need before anything else on this screen makes sense

"RTO 4 hours" is not a fact. It is an answer to an unstated question — recover from *what?*

* A zone-redundant virtual machine survives a zone outage untouched, and is completely unrecoverable once somebody deletes its resource group.
* A storage account with geo-redundant storage has an excellent region story, and for ransomware it has a fifteen-minute head start on replicating the encryption to the paired region.
* A database with a nightly backup and no replica is fine after a corrupt migration, and gone after a region failure.

The same resource has **different** answers for each. So this module reports recovery per **failure scenario**, never as a single number.

## The five scenarios

| Scenario | What fails | What actually saves you |
|---|---|---|
| Instance loss | one instance, node or replica | instance redundancy, platform self-healing |
| Zone loss | one availability zone | zone redundancy |
| Region loss | a whole Azure region | geo-replication, a paired region, Site Recovery |
| **Data corruption** | a bad deployment, a bad migration, ransomware | **point-in-time recovery only** |
| **Accidental deletion** | a resource or its data is deleted | **backup, soft delete or a resource lock only** |

### Redundancy is not a control for the last two

This is the single most important thing on the screen.

Zone-redundant storage, geo-redundant storage and multi-region writes **replicate the damage**, usually within seconds. A corrupted blob is corrupted in all three zones. A deleted row is deleted in every replica.

So a resource can be flawlessly redundant — green on every redundancy check, green in every other tool — and have **no recovery path at all** from a bad deployment. Recovery Readiness is the view that says so.

## Reading the heatmap

| Mark | Meaning |
|---|---|
| ● | Meets the objective for that scenario |
| ▲ | Breaches the objective |
| ✖ | **No recovery path exists.** Not slow — none |
| ? | **Unknown.** A source could not be read |
| · | The scenario does not apply to this resource |

A row that is healthy on the left and red on the right is a resource every redundancy check calls resilient.

### `unknown` is not `not protected`

They are opposite facts and the screen never merges them.

`unknown` means a source could not be read — most often that Backup Manager has not analyzed this scope yet, because its analysis is explicitly operator-triggered. It is **not** a statement that the resource is unprotected. If you see a lot of `unknown`, run a Backup Manager analysis for the same scope and re-run this one.

### `none` is not a degree of "slow"

`✖ No recovery path` means there is no mechanism that recovers this resource from that failure. It is a different kind of finding from "slower than you wanted", and it is usually a surprise.

## RTO is a class, not a promise

You cannot measure a recovery time without performing a recovery. Everything here is inferred from configuration, so recovery time is reported as a **capability class**:

| Class | Meaning |
|---|---|
| Automatic | failover with no action and no data movement |
| Minutes | automated failover, warm standby |
| Hours | orchestrated failover or geo-restore |
| A day or more | restore from backup — depends on data volume |
| No recovery path | nothing recovers this resource from this failure |
| Unknown | not enough was readable to say |

Where a duration is shown it is a **range with its assumptions printed**, for example `6–14h · assumes 120 GB at 40 MB/s · unverified`. The word *unverified* is doing real work: no drill has confirmed it.

**Derived is not proven.** Nothing on this screen is evidence until a recovery drill confirms it.

## RPO is computed, not estimated

Recovery point objective comes from configuration and is usually exact:

* a policy that runs daily has a **24-hour** worst case — not twelve; at 01:59 you are 23 hours 59 minutes from the 02:00 recovery point;
* an hourly policy that only runs 08:00–18:00 has a **16-hour** worst case, because nothing runs overnight;
* Site Recovery reports its own measured replication lag;
* platform backup (Cosmos DB, Azure SQL, PostgreSQL, blob point-in-time restore) is read directly from the service.

Where the configured schedule and the newest recovery point disagree, **reality wins** and the gap is itself reported — a daily policy whose job has failed for six days has a 24-hour design and a 144-hour reality.

## Objectives and breaches

Objectives are set per criticality tier **per scenario**, because a single number across all failure modes is meaningless — nobody demands fifteen-minute recovery from ransomware, and nobody accepts a day of data loss from a zone blip.

The product ships sensible defaults so the screen is useful immediately. They are labeled as defaults, and **an export that quotes them is refused until somebody acknowledges them**: a number handed to an auditor has to have an owner.

Breaches are ordered by consequence — no recovery path first, then total data loss, then the size of the miss weighted by tier. A mission-critical database missing its objective by an hour outranks forty low-tier machines missing theirs by a day.

### Changing the objectives, and the constants behind them

The Targets tab has a second view, **Objectives & rates**, and it exists because of one rule this feature will not bend: *every number the screen prints must be traceable to a constant you can see and change.* A duration band derived from an invisible throughput figure is not reviewable, and the first thing a sceptical engineer asks is where it came from.

So the view shows two things together:

* **The objectives** — a target RTO class and a target RPO in minutes, per tier, per scenario. `unknown` is deliberately not offered as a target: it means a source could not be read, and an objective of "we do not know" is one nothing can ever breach.
* **The restore rates and fixed mechanism overheads** — the throughput assumptions and the per-mechanism minutes that produce every duration band. These are **starting points, not truths**. If you have measured your own restore throughput, put it here; every band names the rate that produced it.

Everybody with read access can see these constants. Changing them needs `resiliency.admin`. Values that are structurally wrong — a zero restore rate, an unknown tier, a scenario that does not exist — are **refused by name** rather than quietly sanitized away, so you never end up believing a number you did not set.

## Workload roll-up

Per-resource answers do not tell you what your application's recovery time is. The Workloads tab aggregates them and, more usefully, **names the component that sets the number**.

*"Contoso Hotels: a day or more, because of one un-backed-up legacy virtual machine"* is a work item. *"Contoso Hotels: a day or more"* is a statistic.

Two assumptions are stated on the screen rather than hidden:

* **every component is treated as required** — a genuinely redundant pair would recover faster, so the figure is conservative;
* **components are assumed to recover in parallel** — an ordered recovery can take longer than the figure shown.

Undetermined components are excluded from the aggregate and counted next to it, so a quarter-measured application cannot look fully assessed.

## Analysis: which resource types are weak, and why

Per-resource answers tell you what is broken. They do not tell you what to *do*, because the same misconfiguration usually recurs across dozens of resources.

The **Analysis** tab groups the estate two ways:

* **RTO and RPO by resource type, per scenario** — ranked by consequence, with a *dominant reason* column. *"42 of 44 storage accounts have no region recovery, all because their vault is locally redundant"* is one change; forty-two rows is a backlog.
* **The reason index** — every distinct reason a verdict was reached, ranked by how much of the estate it explains. Working down this list moves more resources than working down a resource list.

Three renderings on this screen are deliberate and will not be softened:

* **There is no average RTO.** `unknown` is not a point on the scale, so a mean over it is undefined — and it would be the single most quotable number on the page. Worst class and the distribution behind it are shown instead.
* **A median RPO always shows how many resources it excluded.** A median over 41 of 44, presented as the answer for a type, is a lie of omission.
* **`undetermined` is its own column**, never folded into a rate. A type with three unreadable resources must not render as "94% fine".

Above the table sits the finding the whole feature exists for: **redundant resources whose answer for corruption or deletion is at least two RTO classes worse than their answer for infrastructure loss.** A Cosmos account with multi-region writes recovers from a region loss *automatically* and needs *a day or more* to recover from a bad deployment. Every redundancy check calls it resilient.

## Trend

Each analysis appends one small point — counts only, never resource rows — so a later report can say whether this is getting better or worse.

Two rules keep it honest:

* **One measurement is not a direction.** A single analysis shows no trend at all rather than a line through one point.
* **An improvement caused by losing visibility is flagged, not celebrated.** If "no recovery path" falls while "undetermined" rises, that is very often the same resources becoming unreadable, and the screen says so.

Gaps are real: a month with no analysis leaves a gap rather than an interpolated point. Demo data is never recorded, because a synthetic trend line printed beside real numbers is the kind of thing that gets quoted.

## Contributing to the Reliability score

A tenant should not score well on Well-Architected **Reliability** while holding resources that cannot be recovered at all. Recovery Readiness can therefore contribute three controls to the Reliability pillar in Assessments:

| Control | Fails when |
| --- | --- |
| Every resource has a recovery path for each failure it can experience | Any applicable scenario resolves to *no recovery path* |
| Redundant resources also have point-in-time recovery | A zone- or geo-redundant resource has no point-in-time copy, so corruption and deletion replicate with it |
| Resources meet their recovery objectives | A derived RPO or RTO is worse than the objective for the resource's tier |

Two design points are deliberate:

* **They are aggregate controls.** One finding per affected resource explodes on a real estate, so each control is a single finding carrying a bounded sample of the resources behind it.
* **They contribute nothing when there is no analysis.** If Recovery Readiness has not been run for the workload, the controls report *not applicable* and are excluded from the score. Failing them would be reporting our own absence as your risk.

The contribution is **off by default**, under the `assessments_include_recovery` setting. Turning it on moves an existing tenant's Reliability score, which reads as a regression to anyone tracking a trend line — that is a change to announce, not one to ship silently.

## Reports

Two formats, with different jobs. Both are refused while the objectives are still the shipped defaults — a number handed to an auditor has to have an owner.

### Excel — the complete artifact

Sixteen sheets, grouped and color-coded, with an index. Everything is here because this is the file somebody pivots, checks and hands over:

| Group | Sheets |
| --- | --- |
| Overview | Summary, Trend |
| Analysis | RTO-RPO by type, RTO-RPO distribution, Reason index, Redundancy gap |
| Detail | Recovery matrix, Resources, **Reasoning**, Breaches, Workloads |
| Objectives | Objectives, Assumptions and rates |
| Trust | Provenance, Truncation |

Three of those exist purely so the file cannot mislead:

* **Reasoning** is one row per configuration fact — resource, scenario, evidence kind, detail, source. The matrix also carries the reasons joined into a single cell, which reads well but can be neither filtered nor counted, and reasoning nobody can group is reasoning nobody uses.
* **Truncation** says where the analysis stopped. The store caps rows; a file presented as complete that silently dropped its tail is worse than one that admits it, because only the second can be checked. When nothing was dropped it says so rather than showing an empty grid.
* **Assumptions and rates** carries the constants behind every duration band, so any figure in the workbook can be traced to a number somebody chose.

The **Objectives** sheet repeats an agreed/not-agreed status on every row. A caveat that appears only once is a caveat a filtered view can lose, and this one changes what every target in the file means.

### PDF — the readable report

Cover, contents, executive summary, how to read it, trend, recovery by scenario, RTO/RPO by type, the dominant reasons, resources that cannot be recovered, breaches, workload roll-up, and three appendices.

* **The headline is a count, not a score.** "Twelve resources have no recovery path" is a work item; a score out of 100 invites comparison between estates that share none of the same assumptions.
* **"How to read this report" comes before the numbers**, not in an appendix. Every misreading it prevents turns a cautious report into a falsely reassuring one.
* **Long sections are bounded and say how many rows they omitted**, pointing at the workbook. A bounded report that does not admit its bounds is just an incomplete one.

### Save to Evidence

Freezes the analysis as an immutable Evidence Locker snapshot — **content, not a rendered PDF**. A PDF's hash proves only that the file has not changed; the content behind it can be diffed against a later capture and re-rendered, which is what "evidence" has to mean when somebody disputes it a year later. The reasoning travels with every finding, and the objectives and restore rates in force at the time are pinned alongside, or the targets would be unverifiable later.

## What it does not do

* **No changes to Azure.** Analysis only. Remediation is Backup Manager's job, and the drawer links there.
* **No fault injection.** Chaos testing needs write access to production.
* **No drills.** It reports what your configuration implies, not what a rehearsal proved.

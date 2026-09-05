---
layout: default
title: Assessment & Performance
parent: User guide
nav_order: 2
description: Score workload posture, profile bottlenecks, and manage failure-mode risk.
permalink: /user-guide/assessment-performance/
has_children: true
---

# Assessment & Performance

Use this section to turn workload inventory and telemetry into prioritized engineering work. Assessments evaluate controls and framework mappings, Performance Profiler ranks metric bottlenecks, and FMEA records design failure modes and follow-up actions.

{% include screenshot.html file="core-assessment-overview.png" title="Assessment entry point — overall posture, pillars, and individual controls" caption="The synthetic Commerce PROD report shows a score alongside pillar results, evaluated controls, and owner/finding context. Use Assessments for this control-based view, Performance Profiler for metric bottlenecks, and FMEA for failure-mode analysis. These fixture scores and mappings are not an executed assessment, certification, or verified risk conclusion." %}

## In this section

| Guide | Use it to |
|---|---|
| [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/) | Run control catalogs, interpret scores, manage waivers, and export reports. |
| [Performance Profiler]({{ site.baseurl }}/user-guide/assessment-performance/performance-profiler/) | Compare Azure Monitor observations with AMBA-aligned thresholds and rank candidate bottlenecks. |
| [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/) | Score failure modes with severity, occurrence, detection, and RPN. |

## Recommended sequence

1. Confirm the workload scope and cached inventory.
2. Run an [assessment]({{ site.baseurl }}/user-guide/assessment-performance/assessments/) to establish a posture baseline.
3. Use [Performance Profiler]({{ site.baseurl }}/user-guide/assessment-performance/performance-profiler/) when telemetry suggests capacity or latency risk.
4. Build an [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/) from reviewed architecture memory and assign follow-up owners.
5. Re-run periodically and compare results rather than treating one run as permanent truth.

## Shared safety model

Results reflect the selected scope, available permissions, cached inventory, and source freshness. A passing control or healthy metric does not prove that a workload is risk-free. AI summaries and generated FMEA rows require human review. Exports may contain resource names and operational findings; handle them according to your organization's data-classification policy.

The trust models differ: an assessment can finish **succeeded** with errored controls and provisional completeness, while profiler **partial/failed** attempts do not replace its latest complete-success posture. FMEA **Published** is a document lifecycle marker, not automated proof of reviewed controls. See the [assessment catalog and limits]({{ site.baseurl }}/user-guide/assessment-performance/assessments/#registered-controls-packs-and-targets) before comparing totals or scores across these features.

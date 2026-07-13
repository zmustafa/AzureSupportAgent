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

## In this section

| Guide | Use it to |
|---|---|
| [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/) | Run control catalogs, interpret scores, manage waivers, and export reports. |
| [Performance Profiler]({{ site.baseurl }}/user-guide/assessment-performance/performance-profiler/) | Compare Azure Monitor metrics with baselines and rank bottlenecks. |
| [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/) | Score failure modes with severity, occurrence, detection, and RPN. |

## Recommended sequence

1. Confirm the workload scope and cached inventory.
2. Run an [assessment]({{ site.baseurl }}/user-guide/assessment-performance/assessments/) to establish a posture baseline.
3. Use [Performance Profiler]({{ site.baseurl }}/user-guide/assessment-performance/performance-profiler/) when telemetry suggests capacity or latency risk.
4. Build an [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/) from reviewed architecture memory and assign follow-up owners.
5. Re-run periodically and compare results rather than treating one run as permanent truth.

## Shared safety model

Results reflect the selected scope, available permissions, cached inventory, and source freshness. A passing control or healthy metric does not prove that a workload is risk-free. AI summaries and generated FMEA rows require human review. Exports may contain resource names and operational findings; handle them according to your organization's data-classification policy.

---
layout: default
title: Performance Profiler
parent: Assessment & Performance
grand_parent: User guide
nav_order: 2
description: Rank Azure Monitor bottlenecks with heatmaps, trends, findings, and evidence exports.
permalink: /user-guide/assessment-performance/performance-profiler/
---

# Performance Profiler

## Purpose

Performance Profiler queries Azure Monitor metrics for a workload or subscription, compares observations with metric baselines, and ranks potential bottlenecks. Its heatmap and resource drill-down help identify where investigation should start; they do not establish root cause on their own.

**Application route:** `/performance`.

![Performance Profiler heatmap showing workload bottleneck scores]({{ site.baseurl }}/assets/performance-profiler.png)

## Common use cases

- Identify highly utilized resources before an incident or release.
- Compare workload performance over one, seven, or thirty days.
- Review bottleneck posture across the workload fleet.
- Register a metric concern as a finding or external ticket.
- Preserve a profiler result as evidence and export a PDF report.

## Prerequisites, permissions, and data

- `perfprofile.read` is required. Profiling reads metrics; the application does not require a separate profiler write permission.
- Select a workload or subscription and an Azure connection with access to Azure Monitor metrics for the resources.
- Current workload inventory determines what can be scanned.
- Metric definitions and thresholds use the supported baseline/reference set, including AMBA-aligned definitions where available.
- AI narrative requires a configured AI provider.
- Default settings commonly use a one-day window, 15-minute aggregation interval, six-hour server cache, and a bounded resource scan cap; administrators can change these values.

## Tabs and actions

### Profiler

Choose a workload or subscription, then select the metric time window. **Run** starts a streamed refresh with progress.

The **Analysis** sub-tab displays metrics as rows and resources as columns. Color indicates baseline state, and resources can be ordered by bottleneck score. Select a cell or resource to inspect metric detail and supporting values.

The **All Resources** sub-tab provides a searchable, filterable, virtualized table of resource, type, region, current value, threshold, utilization, and trend where supplied.

The AI narrative can summarize the whole result or a selected resource. Use it to form hypotheses, then inspect metric and service evidence.

### Fleet

Review the latest available score, status, resource count, and trend for each workload. Select a workload to return to its focused profile. Fleet comparisons can be misleading when workloads have different metric coverage or run times.

### Cleanup

Manage retained or soft-deleted runs. Restore required records before purge; permanent deletion cannot be undone.

## Workflow

1. Select the correct connection and a workload or subscription—not both.
2. Confirm inventory and choose a window appropriate to the issue. Short windows reveal spikes; longer windows can hide them through aggregation.
3. Run a refresh and monitor streamed progress.
4. Start with the highest bottleneck score, then inspect red and amber heatmap cells.
5. Compare current value, threshold, trend, region, and resource type.
6. Correlate with incidents, deployments, scaling events, logs, and dependencies.
7. Register a validated finding, create a ticket, or preserve evidence.
8. Re-profile after remediation using a comparable window.

## Interpret results

- **Red/critical** means a metric crossed its configured critical condition.
- **Amber/warning** indicates elevated risk or approaching threshold.
- **Green/OK** means the observed metric did not cross its baseline; it does not prove that the service is healthy.
- **Bottleneck score** is a prioritization aid across available metrics, not a probability of failure.
- **No data** can mean an unsupported metric, provider delay, inaccessible resource, wrong time grain, or no observations.
- **Trend** reflects stored comparable runs or time-series points and should be checked for window consistency.

## Exports, history, and integrations

- Download a PDF for a focused run or supported fleet report.
- View run history and score trend for the selected scope.
- Create an evidence snapshot from a run or scoped profile.
- Register a bottleneck as a product finding, create a ticket through configured connectors, or pin it into an investigation/War Room workflow where available.
- The profiler consumes Azure Monitor and baseline reference data but does not change Azure resources or thresholds.

## Safety and limitations

- Metrics are delayed, aggregated, and subject to Azure Monitor retention and throttling.
- A threshold can be unsuitable for a particular workload. Validate against SLOs and service guidance.
- Correlation is not causation; the highest score may be a symptom of a downstream issue.
- Cached profiles can be up to the configured TTL old. Run refresh when current evidence is required.
- Scan caps can omit resources from very large scopes; inspect completion and resource counts.
- AI narrative may invent causal explanations. Treat it only as a hypothesis.

## Troubleshooting

| Symptom | Checks |
|---|---|
| No resources appear | Verify scope, inventory freshness, connection, and metric-read access. |
| Many cells show no data | Check metric support for each resource type, time window, aggregation grain, and provider delay. |
| Refresh fails or is slow | Narrow scope/window, check Azure throttling, and inspect streamed error detail before retrying. |
| Result looks stale | Compare run time and cache TTL, then start an explicit refresh. |
| Score conflicts with user experience | Review service-level metrics, logs, dependencies, and baseline suitability. |
| PDF or evidence is unavailable | Open a completed persisted run and verify permission/storage health. |

## Related docs

- [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/)
- [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/)
- [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/)
- [AI Insight Packs]({{ site.baseurl }}/user-guide/design-ownership/ai-insight-packs/)

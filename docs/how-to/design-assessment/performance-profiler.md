---
layout: default
title: Run Performance Profiler
parent: Design and assessment operations
grand_parent: How-to guides
nav_order: 6
description: Profile workload or subscription metrics, analyze heatmaps and resources, create findings and evidence, export PDF, and manage history.
permalink: /how-to/design-assessment/performance-profiler/
---

# Run Performance Profiler

![Performance Profiler heatmap]({{ site.baseurl }}/assets/performance-profiler.png)

## Prerequisites

- `perfprofile.read` and access to Azure Monitor metrics for the selected connection/scope.
- Current workload inventory for workload mode; optional AI provider for narrative and connector for tickets.

## Route

Open `/performance`; top-level views are **🔥 Profiler**, **🚀 Fleet**, and **🧹 Cleanup**.

## How to profile a workload or subscription and choose a window

1. Open **Profiler** and choose workload or subscription scope, not both.
2. Confirm connection and inventory.
3. Select a preset ISO-duration window such as one or seven days, or the supported custom range. Use short windows for spikes and longer windows for recurring patterns.
4. Select **Run profile** and monitor streamed progress; the run persists and can continue across navigation.
5. Select the completed row in **Profile history** when it is not already displayed.

**Expected result:** A persisted profile contains resource metrics, baseline states, bottlenecks, scorecard, narrative when available, and exact run window.

**Verification:** Confirm scope, run time, displayed start/end window, profiled/resource counts, scan-cap warnings, and connection status.

## How to analyze the heatmap and all resources

1. Open **Heatmap** and start with highest bottleneck scores and red/amber cells.
2. Inspect metric value, threshold/baseline, resource, type, region, and available trend/detail.
3. Filter resource types or choose a resource to narrow the matrix.
4. Open **All Resources** for the full searchable/filterable virtualized resource list.
5. Correlate with deployments, scaling, logs, dependencies, and user-impact telemetry.
6. Treat green as “did not cross this baseline,” not proof of service health.

**Expected result:** A small set of candidate bottlenecks is supported by metric observations.

**Verification:** Reproduce important values in Azure Monitor for the same resource, aggregation, and time window.

## How to use narrative, findings, tickets, evidence, and PDF

1. Read the AI narrative as a hypothesis and compare every claim with the matrix.
2. Select **🛡️ Register findings** to create Performance-pillar findings from current bottlenecks.
3. For a specific bottleneck, choose **🎫 Ticket** and the intended connector.
4. Select **🗄 Evidence** to capture the currently viewed run as an immutable Evidence Locker snapshot.
5. Select **📄 PDF** for the current or historical run; wait for generation or cancel the request.
6. Open Assessments/Evidence/external ticket and confirm the handoff.

**Expected result:** Validated bottlenecks have traceable findings, ticket/evidence records, or a report.

**Verification:** Match scope, run ID/time, resource, metric, threshold, and window in each handoff.

## How to operate fleet profiling

1. Open **🚀 Fleet** and review latest score, breaches, top bottleneck, staleness, and failed/never-profiled rows.
2. Filter/sort, select a bounded set, and launch supported mass profiling.
3. Let background runs continue; do not submit duplicates while a row is running.
4. Retry failed rows after checking throttling/access, then open each workload's profile.

**Expected result:** Fleet rows update with terminal latest profiles and clear stale/error state.

**Verification:** Confirm each selected workload's profile time/window and drill-down result.

## How to use history and cleanup

1. Use **Profile history** to select comparable runs and download a run-specific PDF.
2. Move obsolete runs to Trash first; restore if required.
3. Open **🧹 Cleanup** for bulk retention review.
4. Purge individual or all trashed runs only after evidence/report retention is satisfied.

**Expected result:** Useful history remains available and obsolete data follows recoverable-then-permanent deletion.

**Verification:** Restored runs reopen; purged runs do not; evidence snapshots remain separate records.

## Safety and rollback

- Profiling is read-only against Azure; it does not change thresholds/resources.
- Metrics are delayed/aggregated and scan caps may omit resources.
- AI causality is untrusted. Correlate before ticket/remediation.
- Findings/tickets/evidence are records; correct them in their owning systems.
- Trash is rollback for run deletion; purge is irreversible.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No resources | Check scope, inventory, connection, and metric-read access. |
| Many no-data cells | Check metric support, window, aggregation, provider delay, and permissions. |
| Refresh is slow/fails | Narrow scope/window, inspect streamed error, and allow Azure backoff. |
| Result is stale | Select current scope and run an explicit profile. |
| Score conflicts with experience | Review service-level telemetry, dependencies, and baseline suitability. |
| Evidence/PDF unavailable | Select a completed persisted run and verify storage/permission. |

## Related docs

- [Performance Profiler reference]({{ site.baseurl }}/user-guide/assessment-performance/performance-profiler/)
- [Assessments recipes]({{ site.baseurl }}/how-to/design-assessment/assessments/)
- [Evidence Locker reference]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)

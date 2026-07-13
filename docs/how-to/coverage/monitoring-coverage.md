---
layout: default
title: Operate Monitoring Coverage
parent: Coverage operations
grand_parent: How-to guides
nav_order: 1
description: Scan AMBA alert coverage, triage gaps, generate IaC, operate fleet scans, and verify remediation.
permalink: /how-to/coverage/monitoring-coverage/
feature_ids: [MONITORING_COVERAGE_LOCAL_TABS:coverage, MONITORING_COVERAGE_LOCAL_TABS:all]
---

# Operate Monitoring Coverage

![Monitoring Coverage dashboard]({{ site.baseurl }}/assets/monitoring-coverage.png)

## Prerequisites

- Product permission `coverage.read`; `coverage.manage` is required only to curate the AMBA reference.
- An ARM-capable connection with Reader access to resources and `Microsoft.Insights/metricAlerts` in the selected scope.
- A workload definition for workload mode, or access to the selected subscription.

## Route

Open `/coverage`. The top-level views are **Coverage**, **Fleet**, and **Cleanup**.

## How to scan one scope and interpret its score

1. Open **Coverage** and select the connection and workload or subscription.
2. Check **Updated**, age, and the stale marker. Opening the page reads the saved result and does not start Azure collection.
3. Select **Refresh now** when the result is absent, stale, or predates a deployment.
4. Wait for the shielded collection to finish; it can save the run even if the browser disconnects.
5. Review overall coverage, then filter the matrix by status, severity, category, or resource type.
6. Open a row and compare the expected metric, aggregation, threshold, window, dimensions, target, and enabled state with the observed rule.

**Expected result:** Each deployable static AMBA expectation is classified as present, missing, or misconfigured. Thresholdless guidance and explicitly nondeployable metrics are not scored as deployable gaps.

**Verification:** Confirm the generated timestamp, evaluated recommendation count, partial/truncation warnings, and that the denominator matches the current AMBA reference.

## How to generate remediation IaC

1. Filter to **Missing** or **Misconfigured**.
2. Open each gap and confirm that the baseline applies to the resource.
3. Select **Generate IaC** and choose Bicep or Terraform.
4. Download the artifact and replace reviewed placeholders, especially Action Group and scope references.
5. Validate naming, metric support, dimensions, aggregation, threshold, frequency, window, cost, and routing in the normal repository pipeline.
6. Deploy outside Monitoring Coverage through the organization's approved IaC process.
7. Return to `/coverage`, refresh the same scope, and confirm that the expectation becomes present.

**Expected result:** A reviewable artifact is downloaded; this page does not change Azure.

**Verification:** Validate the artifact in its native toolchain, then use a fresh coverage scan rather than the download event as proof of deployment.

## How to operate fleet coverage

1. Open **Fleet**. It reads only the latest saved workload results and does not scan Azure on page load.
2. Search or sort by coverage, missing count, stale age, or failed state.
3. Select workloads, or select all visible rows.
4. Start the background mass scan. At most three workloads run in parallel with staggered starts.
5. Leave the page if needed; queue and running state survive navigation.
6. Retry failed rows, then open a workload to inspect its cached detailed report.

**Expected result:** Fleet rows update with recommendation, present, missing, misconfigured, age, environment, and connection data.

**Verification:** Confirm each selected row reaches a terminal state and that drill-down retains the workload's connection.

## How to preserve or clean up a run

1. From the current result, use the available PDF, finding, ticket, Approval Inbox, or Evidence Locker handoff only after checking scope and timestamp.
2. Open **Cleanup** to review saved runs.
3. Trash obsolete runs first; restore them if needed.
4. Purge only after retention requirements are satisfied.

**Expected result:** Evidence handoffs represent the loaded run; trashed runs remain recoverable until purged.

**Verification:** Open the saved evidence or restored run and compare its scope and timestamp.

## Safety and rollback

- Scanning and IaC generation are read-only. Roll back a deployed artifact through the same reviewed IaC system that applied it.
- A coverage percentage is present expectations divided by evaluated deployable expectations, not the percentage of all resources monitored.
- Review alert cost and routing before deployment. The generator cannot infer the correct on-call destination.
- Purge is irreversible.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Never scanned | Select a valid scope and use **Refresh now**. |
| Rule is unexpectedly missing | Compare exact resource ID, metric namespace/name, dimensions, enabled state, and target scope. |
| Score falls after refresh | Check inaccessible scopes, reference revisions, new resources, and newly misconfigured rules. |
| Fleet row is stale | Run a selected fleet scan or refresh that workload directly. |
| Artifact fails validation | Resolve placeholders and verify the metric catalog, aggregation, dimensions, region, and Action Group. |

## Related docs

- [Monitoring Coverage reference]({{ site.baseurl }}/user-guide/coverage/monitoring-coverage/)
- [Alerts Manager recipes]({{ site.baseurl }}/how-to/coverage/alerts-manager/)
- [Connection Capability recipes]({{ site.baseurl }}/how-to/coverage/connection-capability/)

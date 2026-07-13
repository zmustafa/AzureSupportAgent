---
layout: default
title: Monitoring Coverage
parent: Coverage
grand_parent: User guide
nav_order: 1
description: Measure Azure Monitor alert coverage against the AMBA baseline and generate reviewable IaC for gaps.
permalink: /user-guide/coverage/monitoring-coverage/
feature_ids: [MONITORING_COVERAGE_LOCAL_TABS:coverage, MONITORING_COVERAGE_LOCAL_TABS:all]
---

# Monitoring Coverage

**Product permission:** `coverage.read`; baseline-management actions require `coverage.manage`.

## Purpose

**App route:** `/coverage`
Monitoring Coverage compares alert rules discovered in Azure with the configured Azure Monitor Baseline Alerts (AMBA) reference. It classifies each expected alert as present, missing, or misconfigured and preserves scans for trend and fleet views.
![Monitoring Coverage dashboard showing AMBA alert coverage and remediation artifacts]({{ site.baseurl }}/assets/monitoring-coverage.png)

## Prerequisites and data sources

### Prerequisites

- An enabled Azure connection that can acquire an ARM token.
- Reader access at every subscription or workload scope to inspect resources and `Microsoft.Insights/metricAlerts`.
- A workload definition for workload-scoped analysis, or access to the selected subscription scope.
- `coverage.manage` only when curating the shared reference set; ordinary scans and artifact generation do not change Azure.

Use [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/) first if subscriptions or rules unexpectedly disappear.

## Tabs and actions

### Views and controls

- **Coverage** shows scorecards, trend, filters, the resource-by-alert matrix, gaps, and all resources.
- **Fleet** compares the latest saved result for each workload. It does not launch fleet-wide Azure collection.
- **Cleanup** manages saved runs through trash, restore, and permanent purge.
- Scope, category, severity, and status filters narrow the matrix without changing the underlying scan.
- A resource detail view explains expected alert status and observed configuration.

## Freshness and scope behavior

### Scan and freshness behavior

Opening the page returns the latest cached snapshot, including `generated_at`, age, and a stale indicator. A missing cache produces an empty/never-scanned state rather than an implicit Azure call. Results become stale after the configured cache TTL (commonly six hours), but stale data remains visible.

**Refresh now** starts an explicit, shielded collection: the server can finish and save the run even if the browser disconnects. The completed result becomes a trend point and a run-history entry. Confirm scope and connection before refreshing because a partial-permission scan can lower the apparent coverage.

## Workflow overview

### Assess and remediate

1. Open `/coverage` and select a connection and scope.
2. Check the snapshot timestamp and refresh when needed.
3. Review the overall percentage, then filter by severity or status.
4. Open missing and misconfigured rows. Confirm that the resource is in the intended baseline and that the observed rule targets the expected resource.
5. Choose **Generate IaC**, then select Bicep or Terraform.
6. Download the artifact, add the intended action group and organization-specific naming, tags, scopes, thresholds, and deployment controls.
7. Optionally create workload findings, create a connector-backed ticket, save the scan to Evidence Locker, download a PDF report, or send generated Bicep to the Approval Inbox. These are separate handoffs; approval does not itself prove Azure deployment.
8. Validate and deploy through the normal repository and pipeline review process.
9. Refresh Monitoring Coverage and confirm that gaps moved to present.

Generated Bicep uses Azure Monitor metric-alert resources; Terraform uses AzureRM metric alerts. Generation is not deployment and does not manage Terraform state. Artifacts may contain explicit placeholders or TODOs, especially for action-group wiring.

Finding registration is available only for workload scope. PDF and Evidence Locker actions capture the currently loaded result, so verify its scope and timestamp first. Ticket creation requires a configured supported connector.

## Interpretation of results

### Interpret results

- **Present** means a matching enabled rule was observed.
- **Missing** means no matching rule was observed for a baseline expectation.
- **Misconfigured** means a rule exists but material fields such as threshold or evaluation window differ from the reference. Whether it reduces coverage is configurable.
- **Coverage percentage** is based on expectations counted as present versus all evaluated expectations. It is not the percentage of all Azure resources monitored.
- **No data/unreadable** should be investigated as a collection problem, not treated as a clean result.

Small threshold differences can fall inside the configured tolerance. The AMBA reference is locally curated, so a reference revision can change the score even if Azure does not change.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

- The scan is read-only. IaC is generated as text and is never applied by this view.
- Review alert cost, frequency, dimensions, regional support, and action-group routing before deployment.
- The generated rule cannot infer the correct on-call destination.
- Resource Graph/API throttling, unsupported metrics, inaccessible subscriptions, or scan limits can produce partial results.
- Purging a saved run is irreversible; trash first when retention policy permits.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| Never scanned | Select a valid connection and use **Refresh now**. |
| Result remains stale | Confirm the refresh completed, then reload; inspect connection and Azure throttling errors. |
| Expected rule is missing | Compare target resource ID, metric namespace/name, enabled state, dimensions, and scope. |
| Score fell after refresh | Look for inaccessible scopes, a changed AMBA reference, newly discovered resources, or rules now classified as misconfigured. |
| Generated template is not deployable as-is | Resolve TODOs, especially action groups and scope references, then validate in the normal IaC toolchain. |

## Related pages

- [Alerts Manager]({{ site.baseurl }}/user-guide/coverage/alerts-manager/)
- [Telemetry Coverage]({{ site.baseurl }}/user-guide/coverage/telemetry-coverage/)
- [Backup & DR Coverage]({{ site.baseurl }}/user-guide/coverage/backup-dr-coverage/)

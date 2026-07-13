---
layout: default
title: Telemetry Coverage
parent: Coverage
grand_parent: User guide
nav_order: 3
description: Audit Azure diagnostic settings, recommended categories, destinations, and retention, then generate remediation artifacts.
permalink: /user-guide/coverage/telemetry-coverage/
---

# Telemetry Coverage

**Product permission:** `coverage.read`; reference and approved-workspace management require `coverage.manage`.

## Purpose

**App route:** `/telemetry`
Telemetry Coverage compares discovered diagnostic settings with a resource-type-specific reference. It identifies resources with no diagnostics, incomplete categories, destination drift, or destinations that could not be read.
![Telemetry Coverage dashboard showing diagnostic-setting gaps and remediation options]({{ site.baseurl }}/assets/telemetry-coverage.png)

## Prerequisites and data sources

### Prerequisites

- An enabled ARM-capable Azure connection with Reader access to the selected scope and permission to read `Microsoft.Insights/diagnosticSettings`.
- A selected workload or subscription scope.
- At least one approved Log Analytics workspace configured by an administrator when destination compliance is required.
- Write permissions and an appropriate Azure role are needed only when an exported remediation is later deployed outside this view.

## Tabs and actions

### Views

- **Coverage** provides scorecards, trend, a resource/category matrix, gap details, workspace selection, and all-resources detail.
- **Fleet** compares the latest saved workload snapshots, including any-settings, all-categories, and unreadable-destination indicators.
- **Cleanup** supports trash, restore, and purge for saved runs.
- The resource drawer shows available/recommended categories, enabled categories, destination, and retention information that the collector could observe.

## Freshness and scope behavior

### Scan and freshness

Page load reads the latest saved snapshot and never launches a surprise estate scan. Results include generated time, age, and stale state; the common default TTL is six hours. An explicit refresh can stream start, progress, completion, and error events while resources are inspected.

A configurable per-scope scan cap protects Azure and the service. If the result indicates truncation or unreadable resources, do not interpret the percentage as whole-estate coverage. Fleet reads saved results and does not itself refresh every workload.

## Workflow overview

### Workflow

1. Open `/telemetry`, choose the connection and scope, and inspect freshness.
2. Choose the approved destination used for generated artifacts.
3. Refresh if the snapshot is missing, stale, or predates a relevant deployment.
4. Review **no settings**, **partial**, **drift**, and **unreadable** groups separately.
5. Open a resource and verify that missing categories are actually supported by that resource type.
6. Generate Bicep for explicit diagnostic settings or a policy-oriented artifact for broad governance.
7. Review resource scopes, categories, destination, identity/RBAC, retention expectations, and rollout approach.
7. Optionally create workload findings, create a connector-backed ticket, save the result to Evidence Locker, download a PDF, or send Bicep to the Approval Inbox. Approval is a handoff, not proof of deployment.
8. Deploy through the approved IaC pipeline, then re-scan to verify.

## Interpretation of results

### Interpret results

- **None**: no diagnostic setting was observed.
- **Partial**: a setting exists, but one or more recommended categories were not enabled.
- **Compliant**: observed settings satisfy the active reference and destination checks.
- **Drift**: settings point somewhere other than an approved/selected destination or differ from the expected configuration.
- **Unreadable/unknown destination**: the collector could not verify destination details. Treat this as missing evidence, not automatically as a missing setting.

Category availability differs by Azure resource type and API version. A category expected by a stale custom reference may no longer be supported, while a new category may not yet be in the reference.

## Exports, history, scheduling, and integrations

### Remediation, policy, and approval

Generated Bicep uses diagnostic-setting resources and can require a placeholder resource reference. Policy-oriented output is intended for a DeployIfNotExists design; assignment requires an identity and suitable role assignment at the target scope. The app generates or proposes artifacts—it does not silently deploy them.

Administrators curate the approved-workspace list and telemetry reference. Reference changes affect future classifications. If local governance uses change requests, review the proposed reference diff before approval; reference approval is not approval to modify Azure resources.

Finding registration requires workload scope. Ticketing requires a configured supported connector. PDF and evidence actions use the currently loaded scan; confirm its freshness before preserving or sharing it.

## Safety and limitations

- Diagnostic data can contain sensitive operational information. Select destinations and retention according to data classification and residency policy.
- A generated setting may increase ingestion and retention cost.
- Not every resource supports diagnostic settings or the same log/metric categories.
- Generated policy requires external validation, managed identity, and RBAC before remediation can succeed.
- Destination existence does not prove that ingestion, table routing, or downstream alerting works.
- Purge is permanent.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| No approved workspace appears | Ask an administrator to curate approved workspaces and verify connection visibility. |
| Many resources are unreadable | Verify diagnostic-settings read access, destination access, scan cap, and connection scope. |
| A supported category is shown missing everywhere | Refresh the reference and confirm exact category names/API support. |
| Policy remediation does nothing | Check assignment identity, role assignment, definition parameters, evaluation delay, and remediation task state. |
| Bicep has placeholders | Replace resource and workspace references with reviewed IaC symbols before validation/deployment. |

## Related pages

- [Monitoring Coverage]({{ site.baseurl }}/user-guide/coverage/monitoring-coverage/)
- [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

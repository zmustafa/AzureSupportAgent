---
layout: default
title: Operate Telemetry Coverage
parent: Coverage operations
grand_parent: How-to guides
nav_order: 3
description: Audit diagnostic settings, destinations, categories, fleet posture, and generated remediation artifacts.
permalink: /how-to/coverage/telemetry-coverage/
---

# Operate Telemetry Coverage

![Telemetry Coverage dashboard]({{ site.baseurl }}/assets/telemetry-coverage.png)

## Prerequisites

- Product permission `coverage.read`; reference and approved-workspace management require `coverage.manage`.
- ARM Reader access to the scope and permission to read `Microsoft.Insights/diagnosticSettings`.
- An approved Log Analytics workspace when destination compliance is evaluated.

## Route

Open `/telemetry`. Use **Coverage**, **Fleet**, or **Cleanup**.

## How to audit diagnostic settings

1. Open **Coverage** and select the connection and workload or subscription.
2. Check the saved scan's timestamp, age, stale state, truncation, and unreadable counts.
3. Select the approved destination used for comparison and generated artifacts.
4. Refresh explicitly; page load and Fleet do not launch surprise scans.
5. Review **None**, **Partial**, **Drift**, and **Unreadable** separately.
6. Open a resource and compare available/recommended categories, enabled categories, destinations, and observed retention.

**Expected result:** Resources are classified against the active resource-type reference and destination rules.

**Verification:** Confirm that the category exists for the resource's API version and that unreadable evidence is not being counted as proof of absence.

## How to generate and verify remediation

1. Select a reviewed gap and destination.
2. Generate Bicep for explicit settings or policy-oriented output for broad governance.
3. Review resource references, categories, workspace ID, retention, data residency, ingestion cost, and target scope.
4. For DeployIfNotExists, add an assignment identity and suitable RBAC before creating a remediation task.
5. Validate and deploy outside this view through the approved pipeline.
6. Refresh Telemetry Coverage and verify categories and destination.
7. Confirm ingestion or table routing separately; destination existence alone does not prove data arrival.

**Expected result:** A deployable-after-review artifact is produced; no diagnostic setting is silently changed by this view.

**Verification:** Use a new scan for configuration proof and a destination query for ingestion proof.

## How to compare fleet posture and retain evidence

1. Open **Fleet** to compare the latest saved workload snapshots by any-setting, all-category, and unreadable-destination status.
2. Drill into stale or weak rows and refresh them individually.
3. Save a current PDF or Evidence Locker snapshot only after checking scope and timestamp.
4. Use **Cleanup** to trash, restore, or permanently purge saved runs.

**Expected result:** Fleet comparison remains cache-only and evidence reflects a fixed saved scan.

**Verification:** Match the workload, connection, generated time, and evaluated-resource count.

## Safety and rollback

- Generated settings can increase ingestion and retention cost and can move sensitive operational data.
- Roll back through the IaC deployment that created or changed the setting; then re-scan and verify destination behavior.
- Category support differs by resource type. Do not deploy a stale custom reference blindly.
- Purge is permanent.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No approved workspace | Ask an administrator to curate workspaces and verify connection visibility. |
| Many unreadable rows | Check diagnostic-settings and destination read access, scope, scan cap, and throttling. |
| Category is missing everywhere | Verify the exact category name and current API support, then review the reference. |
| Policy remediation does nothing | Check assignment identity, role assignment, parameters, evaluation delay, and remediation task. |
| Data does not arrive | Query the destination and inspect routing/retention; a compliant setting is not ingestion proof. |

## Related docs

- [Telemetry Coverage reference]({{ site.baseurl }}/user-guide/coverage/telemetry-coverage/)
- [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
- [Connection Capability recipes]({{ site.baseurl }}/how-to/coverage/connection-capability/)

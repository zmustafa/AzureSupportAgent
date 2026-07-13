---
layout: default
title: Operate Backup and DR Coverage
parent: Coverage operations
grand_parent: How-to guides
nav_order: 4
description: Assess backup and recovery evidence, generate runbooks, verify recovery, and manage saved runs.
permalink: /how-to/coverage/backup-dr-coverage/
---

# Operate Backup and DR Coverage

![Backup and disaster recovery coverage dashboard]({{ site.baseurl }}/assets/backup-coverage.png)

## Prerequisites

- Product permission `coverage.read`; `coverage.manage` is required to curate the protection reference.
- ARM Reader access to the estate and access to the Recovery Services, Backup, and Site Recovery state used by the collector.
- A workload or subscription scope and relevant provider registrations.

## Route

Open `/backupdr`. Use **Coverage**, **Fleet**, or **Cleanup**.

## How to assess protection posture

1. Open **Coverage**, select the connection and scope, and inspect the saved timestamp.
2. Refresh when the scan is absent, stale, or predates a protection change.
3. Review red failed checks before amber degraded evidence.
4. Open a resource and inspect backup enablement, policy/retention, latest job, redundancy, encryption, soft delete, restore testing, PITR/persistence, and DR-pair evidence where applicable.
5. Distinguish **N/A** from failed: N/A means the active reference does not apply that check to the resource type.
6. Confirm critical findings in the source Azure service.

**Expected result:** Applicable checks are classified green, amber, red, or N/A, with recency evaluated against configured thresholds.

**Verification:** Check current Azure job/replication state and the scan timestamp. Green configuration is not proof that data can be restored.

## How to prepare and verify remediation

1. Generate Bicep or the PowerShell-oriented remediation runbook for reviewed gaps.
2. Add owners, RPO/RTO, data classification, target region, vault policy, cost, validation, rollback, and the approved change window.
3. Review every placeholder and service-specific command.
4. Execute through the organization's Azure/IaC process; this view never enables protection or triggers failover.
5. Perform an approved restore or test-failover exercise when required.
6. Capture recovery evidence and refresh the same scope.

**Expected result:** The artifact becomes an organization-specific, approved recovery change or runbook.

**Verification:** Require successful restore/failover evidence plus a fresh green coverage check; neither alone is sufficient.

## How to compare fleet results and manage retention

1. Open **Fleet** to compare saved protected, off-site, recent-job, and DR-pair percentages.
2. Drill into stale or weak workloads and refresh them directly.
3. Preserve a current PDF, finding, ticket, or Evidence Locker snapshot when needed.
4. In **Cleanup**, trash obsolete scans, restore when necessary, and purge only after retention approval.

**Expected result:** Fleet remains cache-only and retained evidence identifies its scope and collection time.

**Verification:** Compare workload, connection, generated time, applicable-check count, and any partial warnings.

## Safety and rollback

- Never trigger production failover solely to clear a finding.
- Enrollment, retention, replication, and failover can affect cost, residency, and recovery-point availability.
- Roll back with a service-specific approved plan; validate that rollback does not remove required recovery points or protection.
- Purge is irreversible.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Protected item appears unprotected | Check vault access, provider registration, resource-ID mapping, and selected scope. |
| Latest job is stale | Check schedule, timezone, paused protection, job status, and configured recency threshold. |
| DR pair is unhealthy | Inspect replication errors and lag before changing configuration. |
| Check should be N/A | Review the exact resource-type mapping in the Backup/DR reference. |
| Runbook is generic | Add service-specific objectives, owners, commands, validation, approvals, and rollback. |

## Related docs

- [Backup & DR Coverage reference]({{ site.baseurl }}/user-guide/coverage/backup-dr-coverage/)
- [Inventory recipes]({{ site.baseurl }}/how-to/estate-intelligence/inventory/)
- [Connection Capability recipes]({{ site.baseurl }}/how-to/coverage/connection-capability/)

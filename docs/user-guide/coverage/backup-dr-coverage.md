---
layout: default
title: Backup & DR Coverage
parent: Coverage
grand_parent: User guide
nav_order: 4
description: Assess backup, recovery, resilience, and disaster-recovery posture and generate a manual remediation runbook.
permalink: /user-guide/coverage/backup-dr-coverage/
---

# Backup & DR Coverage

**Product permission:** `coverage.read`; reference management requires `coverage.manage`.

## Purpose

**App route:** `/backupdr`
Backup & DR Coverage assesses resources against the configured protection reference. It combines backup enablement and policy evidence with recency, redundancy, encryption, soft delete, restore testing, point-in-time recovery, persistence, and DR-pair checks where applicable.
![Backup and disaster recovery dashboard showing protection checks and gaps]({{ site.baseurl }}/assets/backup-coverage.png)

## Prerequisites and data sources

### Prerequisites

- An ARM-capable connection with Reader access to the selected estate and rights to inspect Recovery Services/Backup and Site Recovery state used by the collector.
- A workload or subscription scope.
- Provider registration and service availability for the relevant backup/DR products.
- `coverage.manage` only to curate what each resource type is expected to satisfy.

## Tabs and actions

### Views

- **Coverage** shows protection scorecards, trend, per-resource checks, gaps, and all resources.
- **Fleet** compares saved workload results: protected, off-site, recent-job percentages, and DR-pair health.
- **Cleanup** manages saved scan runs with recoverable trash and irreversible purge.
- Resource detail uses green, amber, red, and not-applicable states for checks supported by the reference.

## Freshness and scope behavior

### Scan and freshness

Opening the page reads a saved snapshot. The configured TTL—commonly six hours—drives the stale indicator but does not erase the last-known result. Refresh explicitly collects current protection and job evidence, saves a run, and adds a trend point. Job recency and restore-test/drill age are evaluated against configurable thresholds, so a score can change as time passes even without a configuration change.

## Workflow overview

### Workflow

1. Select the connection and workload/subscription.
2. Check generated time and refresh if needed.
3. Start with critical red gaps, then amber degraded evidence.
4. Open each resource and distinguish unsupported/not-applicable checks from failed checks.
5. Generate Bicep or the PowerShell-oriented remediation runbook.
6. Review ownership, recovery objective, data criticality, target region, vault policy, cost, and change window.
7. Optionally create Reliability findings, create a connector-backed ticket, save the scan to Evidence Locker, download a PDF, or send Bicep/runbook material to the Approval Inbox.
8. Execute approved steps through the organization's Azure/IaC process.
9. Test recovery or failover where required, capture evidence, and refresh the scan.

Generated material includes Bicep and a downloadable PowerShell-oriented runbook. It is never applied by this coverage view. Review every scope, dependency, placeholder, destructive implication, and service-specific command before external execution because backup enrollment, retention, replication, and failover can have cost and data-protection consequences. Approval Inbox submission is a governed handoff, not deployment confirmation.

## Interpretation of results

### Interpret checks

- **Backup enabled**: an active protection relationship was observed.
- **Policy/retention**: an attached policy and its observed retention meet the reference.
- **Last job**: the latest job succeeded within the configured service-level window.
- **Geo redundancy/off-site region**: protection is not confined to the same failure boundary.
- **DR pair/geo-DR pair**: replication or paired-service evidence is healthy and current.
- **Encryption/soft delete**: the observed vault/service controls meet the baseline.
- **Restore test**: recovery/failover testing is recent enough for the configured drill threshold.
- **PITR/persistence**: service-specific data recovery controls are enabled where applicable.
- **N/A**: the reference does not apply that check to the resource type; it is not a pass or failure.

A green configuration check does not prove recoverability. Successful restore evidence and exercised procedures remain essential.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

- The feature audits and writes local scan records only; it does not enable backup, trigger failover, delete recovery points, or change retention.
- Azure job and replication states may lag. Confirm critical findings in the source service.
- Some services expose incomplete protection evidence or use service-specific recovery models.
- A stale successful job can become amber/red based solely on elapsed time.
- Cross-region protection must satisfy residency, sovereignty, and cost requirements.
- Never run a production failover merely to clear a dashboard finding; use an approved test plan.
- Finding registration requires workload scope; ticketing requires a configured supported connector. PDF and evidence actions preserve the currently loaded result, so check its timestamp first.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| Protected resource appears unprotected | Verify vault access, provider registration, datasource/resource ID mapping, and selected scope. |
| Latest job is stale | Confirm schedule, timezone, job status, paused protection, and collector threshold. |
| DR pair is unhealthy | Inspect replication errors and lag in Site Recovery/service-specific controls before acting. |
| A check should be N/A | Review the admin reference mapping for that exact resource type. |
| Runbook is too generic | Add service-specific RPO/RTO, owners, validation, rollback, and approvals before execution. |

## Related pages

- [Inventory]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/)
- [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)

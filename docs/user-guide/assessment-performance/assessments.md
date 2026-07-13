---
layout: default
title: Assessments
parent: Assessment & Performance
grand_parent: User guide
nav_order: 1
description: Run workload controls, interpret posture scores, manage findings and waivers, and export evidence.
permalink: /user-guide/assessment-performance/assessments/
---

# Assessments

## Purpose

Assessments evaluate a workload against selected control packs and pillars, calculate posture scores, map controls to frameworks, and retain runs for trend and portfolio review. They help teams prioritize findings; they are not a certification or substitute for an auditor.

**Application routes:** `/assessments` and `/assessments/:id`.

![Assessment report with pillar scores and control findings]({{ site.baseurl }}/assets/assessment.png)

## Common use cases

- Establish a Well-Architected or security posture baseline.
- Review control failures by pillar, severity, framework, or resource.
- Compare a workload with a pinned baseline or previous run.
- Assign findings, create time-bound waivers, and hand off tickets.
- Review the latest score across a workload portfolio.
- Add organization-specific custom checks.

## Prerequisites, permissions, and data

- `assessments.read` permits catalog, run, waiver, trend, and portfolio viewing.
- `assessments.run` is required to enqueue runs and perform mutating assessment actions, including finding state, waivers, and custom checks.
- A workload and current resource inventory are required.
- Controls can use Azure Resource Graph, policy compliance, RBAC, identity posture, and manual attestations. Each source requires the relevant connection access.
- AI summaries and AI-generated custom checks require a configured provider; the underlying deterministic control results remain the primary evidence.

## Landing-page tabs

### Run

Select one or more workloads, the desired pillars or packs, and whether to generate an AI summary. Enqueued runs continue in the background. The history table shows status, score, failed count, pillars, time, and trend, with filters for date window, latest-per-workload, grouping, and sorting. In-flight runs can be cancelled; completed runs can be moved to Trash.

### Portfolio

The portfolio shows the latest completed result per workload: overall score, pillar scores, failed controls, trend, and last-run time. Select a row to open the run, but compare run scope and catalog version before ranking workloads.

### Custom

Create organization-specific controls manually or from a natural-language AI draft. Review title, description, pillar, severity, query, and framework mappings before enabling the check. Test custom queries against a limited scope first.

### Trash

Restore soft-deleted runs or permanently purge them. Emptying Trash is irreversible.

## Run-detail tabs and actions

The header shows workload, pillars, AI status, trigger, evidence completeness/confidence, and buttons to **Re-run**, **Set baseline**, and export **PDF**, **CSV**, or **JSON**.

### Controls

Search and filter by pillar, status, and framework. Sort findings, expand a row for description, AI impact, flagged resources, remediation text, waiver reason, or error. Bulk actions can update state, assign, waive, or create tickets. Available states include pass, fail, error, manual, waived, and not applicable.

A remediation command is a suggestion. Review scope, syntax, side effects, and rollback before execution outside the product.

### Compliance

Review framework coverage for available mappings such as CIS, NIST, ISO, Microsoft Cloud Security Benchmark, and PCI. Coverage indicates mapped control outcomes, not formal certification.

### Resources

Inspect the resources scanned in the run, including type, group, region, and Azure portal link. A displayed list may be capped even when the total scanned count is larger.

## Workflow

1. Confirm workload inventory, connection, and required source permissions.
2. Select workloads and only the relevant packs/pillars.
3. Enqueue the run and monitor queued, running, succeeded, failed, or cancelled status.
4. Open a completed result and check completeness before the score.
5. Review critical and high-severity failures, errors, and manual controls.
6. Assign findings or create tickets; use waivers only with justification, approver, and expiry.
7. Pin a reviewed baseline and compare later runs.
8. Export evidence and re-run after remediation.

## Interpret scores and findings

The overall and pillar scores summarize evaluated controls. **N/A** controls do not apply; **manual** controls require attestation; **error** means the control could not be evaluated and must not be read as pass. **Waived** records an accepted exception rather than remediation. A score delta is meaningful only when scope, controls, and source availability are comparable.

The baseline diff highlights new failures and resolved findings. Verify that apparent improvements were not caused by missing resources, permission loss, catalog changes, or a narrower scope.

## Exports, history, and integrations

- **PDF** is suitable for a rendered stakeholder report.
- **CSV** supports tabular finding analysis.
- **JSON** preserves structured run data for automation or evidence processing.
- Trend and Portfolio use historical completed runs; a pinned baseline gives a deliberate comparison point.
- Ticket connectors can hand findings to configured external systems.
- Action-plan handoff can prepare Azure Policy enforcement context; it does not remove the need for staged policy review.
- Manual control attestations and waiver history form part of the run's governance context.

## Safety and limitations

- Results are point-in-time and bounded by scope, permissions, catalog, and source freshness.
- Framework mappings are informational and do not provide certification.
- AI summaries and custom-check drafts can be wrong; verify against control evidence.
- Waivers reduce visible failure treatment but do not remove technical risk.
- Resource or remediation links may expose sensitive identifiers.
- Purge is permanent; preserve required evidence before deleting.

## Troubleshooting

| Symptom | Checks |
|---|---|
| Run remains queued | Check worker health and queue load; avoid enqueuing duplicates. |
| Controls show error | Expand the error, verify source permission/query support, and rerun after correction. |
| Score changed unexpectedly | Compare scope, catalog/packs, N/A counts, permissions, and baseline. |
| Missing resources | Refresh workload inventory and verify subscription/resource-group scope. |
| PDF export is slow | Keep the export dialog open, allow report generation to finish, and retry after run completion. |
| Custom check fails | Validate the Resource Graph query, supported fields, result shape, and limited-scope behavior. |

## Related docs

- [Assessment & Performance overview]({{ site.baseurl }}/user-guide/assessment-performance/)
- [Performance Profiler]({{ site.baseurl }}/user-guide/assessment-performance/performance-profiler/)
- [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/)
- [Ownership]({{ site.baseurl }}/user-guide/design-ownership/ownership/)

---
layout: default
title: Ownership
parent: Design & Ownership
grand_parent: User guide
nav_order: 4
description: Manage owners, assignments, coverage, suggestions, estates, and attestations.
permalink: /user-guide/design-ownership/ownership/
---

# Ownership

## Purpose

Ownership records accountable people, teams, and services and associates them with subscriptions, resource groups, resources, workloads, and architectures. It also measures coverage, proposes candidates from directory and RBAC evidence, presents owner-centric estates, and records attestations.

**Application routes:** `/ownership` and `/ownership/:tab`.

## Common use cases

- Find who owns a workload or Azure resource.
- Bulk-import an owner directory and assignments.
- Identify unowned scopes before an audit or migration.
- Review evidence-based assignment suggestions.
- Apply approved ownership metadata as Azure tags.
- Ask owners to attest workload responsibility or status.

## Prerequisites, permissions, and data

| Requirement | Detail |
|---|---|
| Read | `ownership.read` to view directory, assignments, coverage, suggestions, estates, and attestations. |
| Write | `ownership.write` to create or change records and accept mutating actions. |
| Directory | Local owner records; optional Entra, OIDC, SSO, and RBAC-backed people-picker results depend on configured identity access. |
| Azure scope | Connection and subscription/workload scope for scans, subjects, coverage, and suggestions. |
| Tagging | Azure write permission is required only when applying ownership records as resource tags. |

The owner directory and **My Estate** are shared owner-centric views. **Assignments**, **Coverage**, **Suggestions**, and **Attestation** use the selected connection and scope as applicable.

## Tabs and actions

### Directory

Create people, teams, or service owners manually or through the federated people picker. Search, filter by kind, sort, edit, soft-delete, restore, and purge records. Use **Import** for bulk data, export the directory, and review Trash.

**Apply as tags** is a separate Azure mutation. Preview the target set and values, confirm permissions and naming policy, then review tag revision history so an approved change can be traced or reverted where supported.

### Assignments

Link an owner to a subscription, resource group, resource, workload, or architecture. Filter by current scope and use bulk operations carefully. Assignments in this registry do not grant Azure RBAC permissions; they record accountability.

### Coverage

Review owned versus unowned subjects, coverage percentage, gaps, and trend. A high percentage can still hide critical unowned systems, so inspect the gap list and scope denominator.

### Suggestions

Review proposed owners derived from available signals such as RBAC assignments, directory groups, and existing workload context. Accept only after confirming the candidate is accountable—not merely technically capable or historically active.

### My Estate

Select an owner to see the subjects attributed to that owner. Use this view for handoff, review, and contact validation; it is not an authorization boundary.

### Attestation

Record owner sign-off for scoped items. Check evidence and due dates before attesting. Attestation proves that a user recorded a decision at a time; it does not prove current Azure compliance.

## Workflow

1. Build or import the owner directory and remove duplicates.
2. Select the correct Azure connection and narrow scope.
3. Create explicit assignments for high-criticality workloads first.
4. Review **Coverage** and prioritize unowned critical subjects.
5. Inspect **Suggestions**, accepting or rejecting each with human validation.
6. Ask owners to review **My Estate** and complete attestations.
7. Export the current records for review; optionally apply approved tags through previewed changes.
8. Re-run coverage after organizational or workload changes.

## Interpret results

- **Assigned** means an ownership record exists; it does not mean the owner has RBAC access.
- **Unowned** means no assignment resolved through current records and scope.
- **Suggested** is evidence-based but unapproved.
- **Coverage** depends on the selected subject set and current inventory.
- **Attested** is a historical declaration and should be checked for expiry or changed conditions.

## Exports, history, and integrations

- Import and export directory/assignment data in supported tabular formats such as CSV; validate a small sample before large imports.
- Tag-application revision history records proposed/applied ownership-tag changes and supports the available review or revert flow.
- Assignment changes are available to workload, architecture, inventory, and graph experiences that resolve ownership.
- Directory lookups can integrate with configured Entra, OIDC/SSO, and RBAC sources.

## Safety and limitations

- Ownership records do not modify RBAC or confer authority.
- RBAC-derived suggestions can identify administrators rather than accountable service owners.
- Tag application writes to Azure and can conflict with policy, casing rules, inherited tags, or automation. Always preview.
- Import can create duplicates or overwrite intended values if identifiers are inconsistent.
- Purging Trash is permanent.
- Directory exports contain personal and organizational information; handle them under applicable privacy rules.

## Troubleshooting

| Symptom | Checks |
|---|---|
| People picker returns no results | Verify identity connector configuration, Graph permissions, query spelling, and connection health. |
| Coverage is unexpectedly low | Confirm scope, inventory freshness, assignment subject type, and deleted/duplicate owner records. |
| Suggestion seems wrong | Inspect the underlying RBAC/group evidence; reject it and create an explicit assignment. |
| Assignment is not visible elsewhere | Confirm subject identifier and tenant, then refresh the consuming view. |
| Tag apply fails | Check Azure write rights, locks, policy denials, tag limits, and the previewed resource set. |
| Import reports errors | Validate required columns, kinds, stable identifiers, encoding, and duplicate rows. |

## Related docs

- [Design & Ownership overview]({{ site.baseurl }}/user-guide/design-ownership/)
- [Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/)
- [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/)
- [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/)

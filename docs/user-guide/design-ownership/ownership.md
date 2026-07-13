---
layout: default
title: Ownership
parent: Design & Ownership
grand_parent: User guide
nav_order: 4
description: Manage owners, assignments, coverage, suggestions, estates, and attestations.
permalink: /user-guide/design-ownership/ownership/
feature_ids: [PROACTIVE_NAV:ownership, OWNERSHIP_NAV:assignments, OWNERSHIP_NAV:attestation, OWNERSHIP_NAV:coverage, OWNERSHIP_NAV:directory, OWNERSHIP_NAV:estate, OWNERSHIP_NAV:suggestions]
---

# Ownership

**App routes:** `/ownership` and `/ownership/:tab`

**Product permissions:** `ownership.read`; mutations require `ownership.write`.

## Purpose

Ownership records accountable people, teams, and services and associates them with subscriptions, resource groups, resources, workloads, and architectures. It also measures coverage, proposes candidates from directory and RBAC evidence, presents owner-centric estates, and records attestations.

## Prerequisites and data sources

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

Create people, teams, or service owners manually or through the federated people picker. The owner editor has **From directory** and **Manual** nested tabs and supports temporary delegate cover. Search, filter by kind, sort, edit, soft-delete, restore, permanently purge, or empty Trash. Use **Import** for CSV/Excel bulk data, download a template, choose workbook sheets, review inferred mappings, export CSV/Excel, and review Trash.

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

Record owner sign-off for scoped assignments. Items are **never**, **stale** at 90 days, or **fresh**; the implemented action attests one assignment at a time. Attestation proves that a user recorded confirmation at a time; it does not prove current Azure compliance.

## Freshness and scope behavior

Coverage page loads are cache-only; an explicit refresh performs a read-only Azure scan, stores a trend point, and becomes stale after six hours. Suggestions are cache-only and combine inventory owner tags, RBAC evidence, and orphan-tag promotion signals. Directory and estate are tenant-wide; assignments, coverage, suggestions, and attestation honor tenant, subscription, or workload scope as implemented.

## Workflow overview

### Implementation-grounded usage scenarios

1. **Onboard accountable owners from a workbook:** open `/ownership/directory`, upload CSV or Excel, choose a sheet when required, review the AI-suggested column mapping and row preview, then import owners and any resolvable assignments with `ownership.write`.
2. **Close an unowned workload gap:** refresh `/ownership/coverage` for the workload, inspect cached tag/RBAC evidence in `/ownership/suggestions`, accept a verified candidate to create an explicit assignment, and refresh coverage to verify the denominator and gap changed.
3. **Publish ownership as Azure tags:** from Directory, open **Apply as tags**, preview conflicts and skipped unowned resources, confirm the live write, inspect per-resource results in **Revisions**, and revert the saved recovery copy only after checking for later tag changes.

### Maintain the ownership lifecycle

1. Build or import the owner directory and remove duplicates.
2. Select the correct Azure connection and narrow scope.
3. Create explicit assignments for high-criticality workloads first.
4. Review **Coverage** and prioritize unowned critical subjects.
5. Inspect **Suggestions**, accepting or rejecting each with human validation.
6. Ask owners to review **My Estate** and complete attestations.
7. Export the current records for review; optionally apply approved tags through previewed changes.
8. Re-run coverage after organizational or workload changes.

## Interpretation of results

- **Assigned** means an ownership record exists; it does not mean the owner has RBAC access.
- **Unowned** means no assignment resolved through current records and scope.
- **Suggested** is evidence-based but unapproved.
- **Coverage** depends on the selected subject set and current inventory.
- **Attested** is a historical declaration and should be checked for expiry or changed conditions.

## Exports, history, scheduling, and integrations

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
- Import preview accepts files up to 8 MB; confirmed imports are bounded to 10,000 mapped rows, while tag-plan Resource Graph collection is bounded to 5,000 rows.
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

## Related pages

- [Design & Ownership overview]({{ site.baseurl }}/user-guide/design-ownership/)
- [Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/)
- [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/)
- [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/)

---
layout: default
title: Tag Intelligence
parent: Estate Intelligence
grand_parent: User guide
nav_order: 2
description: Audit tag usage, hygiene, compliance, cost allocation, and drift, then preview, export, approve, apply, and revert controlled changes.
permalink: /user-guide/estate-intelligence/tag-intelligence/
feature_ids: [TAGINTEL_NAV:cost, TAGINTEL_NAV:coverage, TAGINTEL_NAV:drift, TAGINTEL_NAV:generate, TAGINTEL_NAV:hygiene, TAGINTEL_NAV:policy, TAGINTEL_NAV:remediate]
---

# Tag Intelligence

**Product permissions:** `tagintel.read`; catalog, snapshot, saved-change, apply, and revert operations require `tagintel.write` (currently admin-gated).

## Purpose

**App routes:** `/tagintel` and `/tagintel/:tab`
Tag Intelligence analyzes tags already present in Inventory, turns conventions into a catalog and policy proposal, and provides the scope's most consequential write workflow: previewed, explicitly approved tag remediation with a revision for recovery.
![Tag Intelligence showing census, coverage, drift, and remediation analysis]({{ site.baseurl }}/assets/tag-intelligence.png)

## Prerequisites and data sources

### Prerequisites

- A recently refreshed Inventory for the same connection and tenant/subscription/management-group/workload scope.
- Resource Graph/Reader access for analysis.
- Cost Management Reader for Cost allocation.
- For apply/revert: `tagintel.write`, a connection that is not read-only, and Azure `Microsoft.Resources/tags/write` rights such as Tag Contributor at every target scope.
- An approved change and rollback process for bulk metadata updates.

## Tabs and actions

### Tabs

- **Census** inventories keys/values and supports drill-down and plain-English questions.
- **Hygiene** finds near-duplicate keys, casing drift, value variants, and workload-inference opportunities; findings can be queued for remediation.
- **Coverage** evaluates required tags from the catalog or selected requirements and highlights high-ROI resources missing one tag.
- **Cost** allocates available spend by workload, owner, or billing code.
- **Drift** captures tag snapshots and compares key/value/coverage changes; revision history records applied changes.
- **Policy** generates policy definitions/initiative material and presents a staged rollout ladder.
- **AI Generate** turns a plain-English intent into a concrete proposed change set for real resources.
- **Remediate** previews diffs, generates scripts/IaC and rollback material, applies approved writes, and exposes revisions for revert.

## Freshness and scope behavior

### Freshness

Census and dependent analyses use cached estate data. The UI warns when data is old (commonly after 24 hours), but it does not guarantee a live refresh. Refresh Census explicitly and, when resources themselves have changed, refresh Inventory first. Cost and drift snapshots have separate timestamps.

A remediation preview can become stale between preview and apply. The server validates against current known inventory, but concurrent Azure changes are still possible; keep the approval window short and re-preview material batches.

## Workflow overview

### Analyze tags

1. Open **Census**, select scope, and refresh.
2. Drill from key to value, subscription, resource type, and resource.
3. Use **Hygiene** to choose canonical keys/values; do not normalize values until consumers and owners confirm semantics.
4. Maintain the catalog of canonical, aliased, and required keys.
5. Use **Coverage** to prioritize missing requirements.
6. Load **Cost** only when the required billing data/permissions exist; unallocated spend can indicate missing or unmapped billing tags.
7. Capture a **Drift** snapshot before and after a tagging campaign.

## Interpretation of results

### Policy behavior

Policy generation produces definitions/initiative JSON for audit, append/inherit, or deny-oriented governance and provides a safe rollout ladder. It does not assign policy in Azure. Start in audit at a test scope, analyze existing exemptions and effects, then progress only through reviewed stages. Deny can break deployments; modify/DeployIfNotExists requires an identity and RBAC.

### Interpret results

- Near-duplicate keys/values are lexical signals; two values can look similar and have different business meaning.
- Required-tag coverage reflects the active catalog/override, not a universal Azure standard.
- Cost allocation is only as complete as cost data and tag presence at the billing grain.
- Drift compares captured states; it does not prove who made a change.
- Applied/failed counts are per resource. A partially applied batch must not be described as successful without reconciliation.

## Exports, history, scheduling, and integrations

### Governed remediation

1. Queue fixes from Hygiene/Coverage or use **AI Generate**. Treat AI output as an untrusted proposal.
2. In **Remediate**, inspect every operation, target count, and before/after diff.
3. Generate validation, PowerShell, Azure CLI, Bicep, and rollback artifacts as needed. Export does not apply anything.
4. Review least-privilege advice, ownership, locks, policy interactions, inherited tags, and downstream billing/automation dependencies.
5. Re-preview, obtain organizational approval, and invoke Apply with explicit approval. The endpoint rejects apply without `approved=true` and a writable authorized connection.
6. Monitor the streaming per-resource results. Partial success is possible; investigate each skipped/failed item.
7. Preserve the revision record and verify via Census/Drift and downstream systems.
8. If rollback is approved, inspect the revision before invoking revert. Revert performs another Azure write and can overwrite legitimate changes made after the original run.

Saved change sets can be grouped, duplicated, imported, and exported as JSON. Inspect imported content and scope before previewing it.

## Safety and limitations

- Read-only analysis is the default; apply and revert are explicit writes with audit/revision records.
- Tag updates can trigger policy, automation, chargeback, access, or lifecycle behavior.
- Azure tag limits, reserved prefixes, unsupported resource types, locks, policy denies, and inheritance can block updates.
- Apply/revert is atomic per resource, not across the entire batch.
- Generated scripts can contain resource identifiers; store exports as operational data and never add credentials.
- Estate caps can truncate analysis; check result metadata before bulk remediation.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| Census misses recent tags | Refresh Inventory, then Census for the same connection/scope. |
| Apply is disabled/rejected | Verify `tagintel.write`, `approved=true`, writable connection, Tag Contributor rights, locks, and policy. |
| Some resources failed | Review each streamed error; do not blindly retry successful resources. Re-preview a reduced set. |
| Revert would remove later changes | Do not run it unchanged; derive a new reviewed change set from current state and the revision. |
| Cost is unallocated | Confirm cost permissions, billing-data freshness, selected dimension, and canonical billing tag. |
| AI proposal targets too much | Narrow the intent/scope, inspect resolved operations, and require a fresh preview. |

## Related pages

- [Inventory]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/)
- [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
- [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/)

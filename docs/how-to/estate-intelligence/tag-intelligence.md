---
layout: default
title: Operate Tag Intelligence
parent: Estate intelligence operations
grand_parent: How-to guides
nav_order: 12
description: Use every tag-analysis tab and safely preview, apply, verify, export, import, and revert tag changes.
permalink: /how-to/estate-intelligence/tag-intelligence/
feature_ids: [TAGINTEL_NAV:cost, TAGINTEL_NAV:coverage, TAGINTEL_NAV:drift, TAGINTEL_NAV:generate, TAGINTEL_NAV:hygiene, TAGINTEL_NAV:policy, TAGINTEL_NAV:remediate]
---

# Operate Tag Intelligence

![Tag Intelligence workspace]({{ site.baseurl }}/assets/tag-intelligence.png)

## Prerequisites

- Product permission `tagintel.read`; catalog, snapshot, saved-change, apply, and revert operations require `tagintel.write`.
- A recently refreshed Inventory for the same connection and scope.
- Cost Management Reader for Cost.
- For apply/revert: a non-read-only connection and `Microsoft.Resources/tags/write` rights such as Tag Contributor at every target.
- An approved bulk metadata change and recovery process.

## Route

Open `/tagintel` or a tab route: **Census**, **Hygiene**, **Coverage**, **Cost**, **Drift**, **Policy**, **AI Generate**, or **Remediate**.

## How to use Census and the plain-English console

1. Open `/tagintel/census`, select scope, and check the freshness warning.
2. Load from cache for the current Inventory state or refresh from Azure; a background refresh survives navigation.
3. Drill from key to value, subscription, resource type, and resource. Drill requests use cached resources and do not call Azure.
4. Use the question console for tag questions and inspect its explanation, Resource Graph query, and matching rows.
5. Use the key search or `?key=` deep link to share a focused drill view.

**Expected result:** Every observed key/value and untagged resource can be traced to concrete cached resources.

**Verification:** Compare counts with Inventory for the same connection/scope and inspect a sample in Azure.

## How to normalize Hygiene findings into the catalog

1. Open **Hygiene** and review near-duplicate keys, casing drift, value variants, and inferred workload clusters.
2. Confirm business semantics with owners; lexical similarity does not prove equivalence.
3. Create or edit catalog entries with canonical key, aliases, category, purpose, required/inherited state, scope, allowed values, owner, and description.
4. Optionally seed a limited draft catalog from commonly used discovered keys.
5. Queue reviewed rename/normalize operations to Remediate.

**Expected result:** The tenant catalog records intentional conventions and the remediation cart contains only reviewed operations.

**Verification:** Reopen the catalog and inspect queued operation type, source key/value, destination, and target scope.

## How to prioritize Coverage gaps

1. Open **Coverage** and choose catalog-required keys or a reviewed override.
2. Read **Evaluated**, exempt count, and **Compliant / all required tags**. This differs from Census **Any-tag coverage** by design.
3. Review resources missing all or several requirements.
4. Prioritize **Missing only one tag — fix queue** and send selected rows to Remediate.
5. Validate shared/platform exceptions rather than treating them as compliant resources.

**Expected result:** Required-tag coverage and a high-ROI queue are calculated against the active requirement set.

**Verification:** Open a queued resource and confirm current tags, missing key, exception status, and intended value.

## How to allocate Cost by tags

1. Open **Cost**.
2. If empty, select **Load cost data**; this refreshes the shared Inventory cost cache.
3. Choose workload, owner, billing code, environment, or another supported dimension.
4. Review allocated and unallocatable spend and shared-resource splits.
5. Trace missing allocation back to Census/Coverage before proposing a billing tag.

**Expected result:** Available trailing cost is grouped by observed tag-derived dimensions.

**Verification:** Check period, currency, cost freshness, missing billing tags, and Cost Management permissions.

## How to capture Drift and inspect revisions

1. Open **Drift** and capture a tag snapshot before a campaign.
2. Capture another snapshot after refresh and compare key, value, and coverage deltas.
3. Review revision history for applied Tag Intelligence or ownership tag changes.
4. Open a revision to inspect per-resource before/after state.

**Expected result:** Snapshots show tag-state drift; revisions show recoverable applied changes.

**Verification:** Compare snapshot timestamps and exact resource IDs. Snapshot drift does not prove who changed Azure.

## How to generate policy safely

1. Open **Policy** and select reviewed catalog keys, values, and modes.
2. Generate audit, append/inherit, or deny definitions and initiative material.
3. Inspect parameters, effects, exclusions, inheritance behavior, and the staged rollout ladder.
4. Start in audit at a test scope; analyze compliance and exemptions.
5. Advance only through reviewed stages. Assign externally through the approved policy/IaC process.

**Expected result:** Policy JSON is generated but not assigned in Azure.

**Verification:** Validate definitions and initiative with policy tooling and test-scope compliance before stronger effects.

## How to use AI Generate without granting it authority

1. Open **AI Generate** and describe a narrow tagging intent.
2. Review every grounded operation and targeted real resource.
3. Remove incorrect or overbroad operations.
4. Send the proposal to Remediate for deterministic preview.

**Expected result:** AI creates an untrusted proposed change set, not an Azure change.

**Verification:** Match each target, operation, key, and value against current Census and owner intent.

## How to preview and export a remediation plan

1. Open **Remediate** from a queued fix, AI proposal, or saved change set.
2. Review supported operations: rename key, set/add/inherit/delete tag, or normalize value.
3. Run the dry-run preview and inspect every before/after diff, overwrite count, flag, and target.
4. Generate PowerShell, Azure CLI, Resource Graph validation, Bicep, and rollback artifacts as needed.
5. Review least-privilege advice, locks, policy, inheritance, Azure tag limits, reserved prefixes, unsupported resource types, billing, automation, and lifecycle effects.
6. Export artifacts or save the change set. No Azure write occurs yet.

**Expected result:** A reproducible plan and rollback material are generated from cached current state.

**Verification:** Re-preview material batches immediately before approval and compare target count and overwrites.

## How to apply tags safely and verify partial results

1. Keep the approval window short and refresh/re-preview if the estate may have changed.
2. Obtain organizational approval and select the explicit approval control.
3. Apply. The endpoint rejects requests without `approved=true`, `tagintel.write`, a writable connection, and Azure tag-write rights.
4. Monitor streamed per-resource start/result events.
5. Separate applied, skipped, and failed resources; the batch is atomic per resource, not across the estate.
6. Preserve the generated revision recovery copy.
7. Refresh Census/Inventory, capture Drift, and verify downstream billing, policy, and automation.

**Expected result:** Authorized resources are updated and every result plus a revertible revision is recorded.

**Verification:** Compare applied/failed counts, revision before/after, current Azure tags, and downstream behavior. Never call a partial batch fully successful.

## How to revert without overwriting later legitimate changes

1. Open the exact revision and inspect its captured prior tag sets.
2. Refresh current tags and compare them with the revision's after-state.
3. If any resource changed later, do not run the original revert unchanged; build a new reviewed change set from current state and the recovery copy.
4. Obtain approval and invoke revert with explicit approval.
5. Monitor per-resource results; revert performs an ARM replace and records an inverse revision.
6. Refresh and verify Azure, Census, Drift, billing, policy, and automation.

**Expected result:** Reviewed prior tag state is restored where safe, with another auditable recovery revision.

**Verification:** Confirm current tags and the new inverse revision. Revert is another write, not an automatic undo.

## How to manage, export, and import saved change sets

1. Save a reviewed change set and organize it into a group.
2. Duplicate when a new variation is needed rather than overwriting historical intent.
3. Export selected change sets as a portable JSON bundle.
4. Inspect imported JSON for operations, scopes, resource IDs, and group names.
5. Import; records are added as new items and referenced groups are matched by name or created.
6. Preview every imported change set against the current estate before applying.

**Expected result:** Portable definitions are managed without automatically executing them.

**Verification:** Compare imported count/content, then run a fresh dry-run and inspect every target.

## Safety and rollback

- Analysis, policy generation, AI generation, preview, and script generation are read-only.
- Apply and revert can trigger policy, chargeback, automation, access, or lifecycle behavior.
- Scripts and bundles contain resource identifiers; handle as operational data and never include credentials.
- Concurrent Azure changes can make a preview or revert unsafe.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Census misses tags | Refresh Inventory, then Census for the same connection/scope. |
| Coverage seems lower than Census | Census measures any-tag presence; Coverage requires all selected keys. |
| Cost is empty/unallocated | Load cost, check permissions/freshness, and verify the canonical billing dimension. |
| Apply disabled or rejected | Check `tagintel.write`, explicit approval, writable connection, Tag Contributor, locks, and policy. |
| Some resources failed | Review each error and re-preview only the unresolved set. |
| Revert would remove later work | Derive a new reviewed change set from current state and the revision. |
| AI targets too much | Narrow intent/scope and remove operations before deterministic preview. |

## Related docs

- [Tag Intelligence reference]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/)
- [Inventory recipes]({{ site.baseurl }}/how-to/estate-intelligence/inventory/)
- [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)

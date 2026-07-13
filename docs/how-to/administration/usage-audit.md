---
layout: default
title: Review usage and audit history
parent: Administration tasks
grand_parent: How-to guides
nav_order: 61
description: Interpret model usage estimates and investigate administrative actions without overstating evidence.
permalink: /how-to/administration/usage-audit/
---

# Review usage and audit history

## Prerequisites

- Access to the Usage admin area (`settings.write` in the current implementation).
- The provider invoice or Azure billing view when cost reconciliation is required.
- Product permission `audit.read`.
- A UTC time window and, when available, actor, action, or target identifiers.

## Route

- Open `/admin/usage`.
- Open `/admin/audit`.

## How to review AI model usage

1. Review totals and rows grouped by provider and model.
2. Compare request count, prompt tokens, completion tokens, and estimated USD cost.
3. Treat a tilde-marked cost as a fallback estimate for a model absent from the current price table.
4. Compare unusual growth with provider/model changes, scheduled tasks, assessment runs, or investigations in the same period.
5. Reconcile financial decisions against the provider's authoritative invoice.

**Expected result:** The operator identifies which provider/model combinations account for recorded requests and token estimates.

**Verification:** Generate one bounded, non-sensitive request and confirm its usage appears after refresh; do not expect exact invoice parity because pricing, caching, and billing timing can differ.

## How to investigate an administrative action

1. Set the narrowest available time window, filter, search, or page range.
2. Locate the initiating entry and record timestamp, actor, action, target, provider/model, and non-secret metadata.
3. Follow related identifiers through approval, settings, connection, connector, access-control, backup, demo, or feature records.
4. Correlate external writes with Azure Activity Log or destination records.
5. Preserve decision-grade evidence through an approved Evidence Locker or SIEM process.

**Expected result:** A defensible timeline distinguishes an application-recorded action from external execution and delivery evidence.

**Verification:** Cross-check timestamps and identifiers in at least one independent system for external operations. Audit metadata should not contain plaintext credentials.

## Safety and rollback

Audit entries are evidence, not an undo mechanism. Redact sensitive metadata before sharing. Roll back the underlying change through its owning admin page and verify the compensating action is also audited.

This page is read-only. Reduce future use through approved model, schedule, or runtime-setting changes; there is no rollback for tokens already consumed.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Audit page is unavailable | Confirm `audit.read` in the effective role. |
| No matching entry appears | Broaden pagination/time assumptions and verify the action actually completed. |
| Entry says success but Azure differs | Correlate managed-change status and Azure Activity Log; app audit alone does not prove Azure completion. |
| Cost differs from invoice | Use provider billing as authoritative and check price-table coverage, caching, and delayed records. |
| Expected request is absent | Confirm the active provider/model and refresh after the operation completes. |

## Related docs

- [Auditing]({{ site.baseurl }}/security/auditing/)
- [Evidence Locker]({{ site.baseurl }}/how-to/lifecycle-investigation/evidence-locker/)
- [AI provider recipe]({{ site.baseurl }}/how-to/administration/ai-providers/)
- [Usage and audit reference]({{ site.baseurl }}/admin/usage-audit/)

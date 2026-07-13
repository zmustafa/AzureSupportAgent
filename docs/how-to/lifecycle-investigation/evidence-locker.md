---
layout: default
title: Capture and manage investigation evidence
parent: Lifecycle and investigation
grand_parent: How-to guides
nav_order: 5
description: Capture immutable snapshots, verify hashes, compare evidence, export or share safely, and manage Trash.
permalink: /how-to/lifecycle-investigation/evidence-locker/
feature_ids: [EVIDENCE_CONTENT_TABS:inventory, EVIDENCE_CONTENT_TABS:properties, EVIDENCE_CONTENT_TABS:changes, EVIDENCE_CONTENT_TABS:metrics, EVIDENCE_CONTENT_TABS:findings, EVIDENCE_CONTENT_TABS:architecture, EVIDENCE_CONTENT_TABS:memory, EVIDENCE_CONTENT_TABS:activity]
---

# Capture and manage investigation evidence

**Exact route:** `/evidence`.

## Prerequisites

- Product permissions `evidence.read` and `evidence.write` for capture and lifecycle actions.
- Source permissions for every selected section and a defined retention purpose.
- A same-tenant case/ticket when attaching evidence.

## Route

**Exact route:** `/evidence`.

## How to capture and verify a snapshot

1. Open `/evidence` and select **New snapshot**.

2. Enter a non-sensitive name and choose workload, subscription, or explicit-resource scope.
3. Include only required sections: inventory, properties, changes, metrics, findings, architecture, memory, or activity.
4. Choose **standard** or **audit** retention and add non-sensitive tags.
5. Select **Capture snapshot**.
6. Record snapshot ID, creation time, creator, size, section counts, retention class, and SHA-256 digest.
7. Open the snapshot; integrity is verified when content is read.

**Expected result:** An immutable point-in-time bundle with a recorded digest.

**Verification:** Confirm scope/sections and successful integrity check. Empty sections can reflect source permission or freshness gaps.

## How to compare and export snapshots

1. Select two snapshots with comparable scope and sections.

2. Run **Snapshot diff** and review added, removed, and changed inventory/findings.
3. Validate material changes against source systems.
4. Open a snapshot and select **Export** for its JSON bundle where authorized.
5. Verify exported ID, digest, generated time, and section counts, then store securely.

**Expected result:** A point-in-time comparison and portable evidence bundle.

**Verification:** Reopen both snapshots and reproduce key differences; a diff is not meaningful when scope/section selection differs.

## How to attach or share evidence safely

1. Attach the snapshot to a same-tenant case or supported ticket.

2. If external read-only access is necessary, select **Share read-only link**.
3. Copy the time-limited token only into an approved secure channel.
4. Record recipient, purpose, and expiry; do not place the token in public tickets or docs.
5. After expiry, create a new short-lived link only if access is still justified.

**Expected result:** A traceable pointer or time-limited share; the immutable snapshot content is unchanged.

**Verification:** Confirm the intended recipient can access only the expected snapshot and that expiry is correct.

## How to trash, restore, or permanently delete evidence

1. Move an active snapshot to **Trash** when retention policy permits.

2. Use **Restore** to reverse a mistaken soft deletion.
3. Use **Delete forever** for one item or **Empty Trash** only after retention/legal-hold review.
4. Read the confirmation carefully; permanent deletion removes the hash-stamped blob and cannot be undone.

**Expected result:** Soft deletion remains reversible; purge is permanent.

**Verification:** Restored items reappear with the same metadata/digest. Purged items no longer appear and must not be assumed recoverable.

## Safety and rollback

Snapshots are writes, but content is not edited in place. Capture can preserve sensitive metrics, identifiers, memory, and activity. Minimize scope and sections. Trash is rollback for soft delete; purge and Empty Trash have no rollback. A share token is a bearer credential until expiry—handle it like a secret.

### Freshness and partial results

A snapshot never refreshes; it preserves what collectors could see at capture time. Source caches, API failures, telemetry retention, and permission gaps can make sections partial. Digest integrity proves content consistency after capture, not completeness or truth of upstream data.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Integrity verification fails | Stop using the bundle, preserve audit details, and capture from authoritative sources again. |
| Diff is empty | Confirm distinct IDs, times, scopes, and included sections. |
| Expected section is empty | Check source permission, freshness, scope, and capture selection. |
| Attach fails | Verify same tenant, target existence, and write permission. |
| Share expired | Create a new short-lived share; never reuse or publish the old token. |

## Related docs

- [Evidence Locker reference]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)
- [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/)
- [Auditing]({{ site.baseurl }}/security/auditing/)

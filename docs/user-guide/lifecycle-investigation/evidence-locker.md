---
layout: default
title: Evidence Locker
parent: Lifecycle & Investigation
grand_parent: User guide
nav_order: 5
description: Capture immutable, SHA-256-stamped investigation snapshots, compare them, share them, and attach them to cases.
permalink: /user-guide/lifecycle-investigation/evidence-locker/
feature_ids: [EVIDENCE_CONTENT_TABS:inventory, EVIDENCE_CONTENT_TABS:properties, EVIDENCE_CONTENT_TABS:changes, EVIDENCE_CONTENT_TABS:metrics, EVIDENCE_CONTENT_TABS:findings, EVIDENCE_CONTENT_TABS:architecture, EVIDENCE_CONTENT_TABS:memory, EVIDENCE_CONTENT_TABS:activity]
---

# Evidence Locker

**Permissions:** `evidence.read`, `evidence.write`

## Purpose

**App route:** `/evidence`
Evidence Locker creates write-once point-in-time bundles and records a SHA-256 digest. Available sections include inventory, properties, changes, metrics, findings, architecture, memory, and activity. The digest is verified when content is read, making unexpected modification detectable.

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Create a snapshot

1. Select **New snapshot** and give it a non-sensitive name.
2. Choose workload, subscription, or explicit-resource scope.
3. Include only sections needed for the investigation; metrics may contain sensitive operational data.
4. Select **standard** or **audit** retention and add non-sensitive tags.
5. Create the snapshot and record its ID, digest, generated time, size, and section counts in the case.

Standard and audit retention periods are administrator-configurable. Audit class is longer-lived; it is not a substitute for an external legal-hold system.

### Review, diff, and hand off

Content tabs preserve the collected sections. Compare two snapshots to see added, removed, or changed inventory and findings. Attach a snapshot to a case or supported external ticket. Export JSON/ZIP where authorized.

A share link is read-only and time-limited, but possession of its token grants access until expiry. Use the shortest practical duration and an approved channel. Do not paste tokens into public tickets or documentation.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Immutability and deletion

Snapshots are not edited in place. Delete moves an item to Trash; restore returns it. Permanent purge is an administrator action and should follow retention and evidence policy. A digest mismatch means integrity verification failed and must be investigated rather than ignored.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Integrity verification fails | Stop relying on the bundle, preserve audit details, and create a new snapshot from authoritative sources. |
| Diff is empty | Confirm two distinct snapshot IDs, scopes, and times. |
| Share link expired | Generate a new short-lived link; old links remain invalid. |
| Attach fails | Verify the target case/ticket exists in the same tenant and you have write permission. |
| Expected section is empty | Check scope, source permissions, scan freshness, and section selection at creation. |

## Related pages

### Related docs

- [Case Files]({{ site.baseurl }}/user-guide/lifecycle-investigation/case-files/)
- [Data flow]({{ site.baseurl }}/security/data-flow/)
- [Auditing]({{ site.baseurl }}/security/auditing/)

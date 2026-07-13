---
layout: default
title: Run an investigation with Case Files
parent: Lifecycle and investigation
grand_parent: How-to guides
nav_order: 6
description: Create, enrich, progress, verify, close, reopen, and safely delete investigation cases.
permalink: /how-to/lifecycle-investigation/case-files/
---

# Run an investigation with Case Files

## Prerequisites

- Product permissions `cases.read` and `cases.write` for case changes.
- Valid same-tenant finding, change, evidence, architecture, workload, or investigation references.
- A named owner and measurable verification criteria.

## Route

`/cases` and `/cases/:id`.

## How to open and scope a case

1. Open `/cases` and create a case with a concise, non-sensitive title, summary, and severity.

2. Assign an owner and link workload, architecture, and investigation chat when available.
3. Open `/cases/:id` and confirm status `open`, risk/confidence, assignee, and scope.
4. Add an initial note stating impact, known facts, unknowns, and next step.
5. Move to `investigating`.

**Expected result:** A durable case with an append-only opening timeline.

**Verification:** Refresh the page and confirm case metadata and `opened`, assignment, note, and status events persist.

## How to build an investigation timeline

1. Attach validated finding UIDs, change-event IDs, and Evidence Locker snapshot IDs.

2. Link the investigation chat rather than copying sensitive transcripts.
3. Add timestamped notes for hypotheses, tests, decisions, and rejected explanations.
4. Correct mistakes with a new note; timeline events and notes are not edited in place.
5. Update severity, risk, confidence, summary, or assignee when evidence changes.

**Expected result:** A chronological record connecting evidence, decisions, ownership, and handoffs.

**Verification:** Open every reference and confirm same-tenant scope, relevance, and integrity.

## How to remediate and verify a case

1. Record the approved remediation and rollback reference.

2. Move to `remediating` only when execution ownership is clear.
3. Apply the change through the approved external system.
4. Capture fresh telemetry, inventory, quota, policy, identity, RBAC, or evidence as appropriate.
5. Move to `verifying` and record measurable expected versus observed results.
6. Resolve when success criteria are met; close after operational follow-up.

**Expected result:** A case whose resolution is supported by fresh verification evidence.

**Verification:** Confirm source symptoms are absent or controlled, no unacceptable regression exists, and attachments/timestamps postdate remediation.

## How to reopen or delete a case safely

1. Reopen or move status backward when new evidence invalidates resolution; status transitions are allowed in either direction and remain logged.

2. For an erroneous case, review references and external tickets before deletion.
3. Use the delete action only after retention approval and the warning that the case and timeline are permanently deleted.
4. Preserve required evidence elsewhere first; deleting a case does not delete external tickets or source records.

**Expected result:** Reopened work remains auditable; permanent deletion removes the case record only.

**Verification:** Reopened status and reason appear in the timeline. A deleted case is absent and cannot be restored through the UI.

## Safety and rollback

Case writes affect local records, not Azure resources. Notes are append-only and should contain no secrets, access tokens, share tokens, raw customer payloads, or unnecessary personal data. Status and metadata can be changed again; timeline history remains. Permanent case deletion has no rollback. External remediation uses its own approved rollback.

### Freshness and partial results

Case metadata and timeline are database-backed and current when loaded, but attached evidence is point-in-time and linked source objects can age, be removed, or remain partial. A case summary is not automatically synchronized with external tickets or Azure state.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Case is absent | Check ID and filters; confirm it was not permanently deleted. |
| Attachment fails | Confirm object type, same tenant, existence, and write permission. |
| Assignee is unavailable | Refresh access-control data and verify the user/role. |
| Resolution lacks confidence | Return to investigating/verifying and collect fresh evidence. |
| Timeline appears out of order | Compare absolute creation timestamps and client time zone. |

## Related docs

- [Case Files reference]({{ site.baseurl }}/user-guide/lifecycle-investigation/case-files/)
- [Evidence Locker]({{ site.baseurl }}/how-to/lifecycle-investigation/evidence-locker/)
- [Telemetry Intelligence]({{ site.baseurl }}/how-to/lifecycle-investigation/telemetry-intelligence/)

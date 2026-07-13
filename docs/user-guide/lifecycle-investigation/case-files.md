---
layout: default
title: Case Files
parent: Lifecycle & Investigation
grand_parent: User guide
nav_order: 6
description: Maintain durable incident records with append-only timelines, evidence, remediation, and verification.
permalink: /user-guide/lifecycle-investigation/case-files/
---

# Case Files

**Permissions:** `cases.read`, `cases.write`

## Purpose

**App routes:** `/cases` and `/cases/:id`
Case Files persist incident context beyond a chat or browser session. A case links workload and architecture context, investigation chat, findings, changes, evidence snapshots, remediation task, assignee, risk/confidence, and an append-only event timeline.

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Lifecycle

The supported statuses are **open**, **investigating**, **remediating**, **verifying**, **resolved**, and **closed**. Severities are **info**, **warning**, **error**, and **critical**.

1. Create a case from a finding or investigation; provide a concise title and non-sensitive summary.
2. Assign an owner and link the relevant workload, architecture, and investigation message when available.
3. Move to **investigating** and append notes rather than rewriting history.
4. Attach finding UIDs, change-event IDs, and Evidence Locker snapshot IDs.
5. Record the approved remediation and move to **remediating**.
6. Capture fresh verification evidence and move to **verifying**.
7. Resolve only when success criteria are met; close after operational follow-up is complete.

Status, severity, assignee, risk score, confidence, and summary can be updated. Each material transition creates actor/timestamp context in the timeline. Case deletion is soft deletion; it does not erase external tickets or attached source records.

## Interpretation of results

### Timeline interpretation

Events include opened, status, note, attach, investigation, handoff, assigned, resolved, and reopened. Timeline events are append-only. Correct an error with a new note; do not rely on deletion to conceal a mistaken decision.

## Exports, history, scheduling, and integrations

### Exports and handoffs

Case detail is available as structured API data. Evidence exports remain governed by Evidence Locker controls. External ticket references are pointers, so verify that both systems contain enough context without duplicating secrets or personal data.

## Safety and limitations



## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Case is absent | Check filters and ID; soft-deleted cases are excluded from the active list. |
| Attachment fails | Confirm the referenced object exists in the same tenant and the field type is correct. |
| Assignee cannot be selected | Confirm the user exists and has appropriate access; refresh Access Control data. |
| Case cannot be resolved | Add verification evidence and ensure the writer has `cases.write`. |
| Timeline appears wrong | Compare absolute timestamps; events are ordered by creation time and are never rewritten. |

## Related pages

### Related docs

- [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Auditing]({{ site.baseurl }}/security/auditing/)

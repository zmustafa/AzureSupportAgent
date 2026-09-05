---
layout: default
title: Lifecycle and investigation
parent: How-to guides
nav_order: 2
has_children: true
description: Task recipes for lifecycle, capacity, telemetry, evidence, and case investigations.
permalink: /how-to/lifecycle-investigation/
---

# Lifecycle and investigation how-to guides

Use these recipes to identify time-bound operational risk, investigate telemetry, preserve evidence, and maintain an auditable case record.

| Goal | Guide |
| --- | --- |
| Triage Azure retirements and breaking changes | [Retirement Radar]({{ site.baseurl }}/how-to/lifecycle-investigation/retirement-radar/) |
| Review reservation expiry and utilization | [Reservations Monitor]({{ site.baseurl }}/how-to/lifecycle-investigation/reservations-monitor/) |
| Scan capacity and throttling risk | [Quota Monitor]({{ site.baseurl }}/how-to/lifecycle-investigation/quota-monitor/) |
| Query, triage, and reconstruct telemetry | [Telemetry Intelligence]({{ site.baseurl }}/how-to/lifecycle-investigation/telemetry-intelligence/) |
| Capture, compare, share, and retain evidence | [Evidence Locker]({{ site.baseurl }}/how-to/lifecycle-investigation/evidence-locker/) |
| Open, investigate, verify, and close cases | [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/) |

## How to take a signal through investigation and verification

1. Choose the task above and select the correct connection and narrowest supported scope. Telemetry Intelligence chooses the first discovered component for most analyses; it does not offer a component picker.
2. Check generated time, retention window, cache age, collector errors, and partial-result indicators.
3. Use **Load** versus **Run scan** deliberately in Quota, **Refresh** for current Radar/Reservations collection, or **Load telemetry** to activate telemetry queries. Evidence capture creates a fixed bundle; a case note does not refresh its sources.
4. Validate AI narrative and derived risk against raw rows and authoritative Azure data.
5. Preserve minimum necessary evidence before remediation.
6. Verify externally applied changes with fresh data, then update the case timeline.

**Expected result:** A traceable progression from a scoped signal to reviewed evidence and an accountable case.

**Verification:** Confirm the actual destination record and that verification timestamps postdate remediation. Record collection limits rather than treating missing rows, a green completion message, or a resolved case status as proof of success.

{% include screenshot.html file="estate-case-timeline.png" title="Investigation handoff example — preserve scope and chronology in a case" caption="The synthetic checkout case keeps its workload and assignee beside timeline notes and linked findings, changes, and evidence. Use the Case Files and Evidence Locker recipes to understand those records and their separate permissions. The case is still Investigating; the image does not establish remediation, external verification, or a successful evidence capture." %}

## How to select a safe handoff or recovery action

1. Use Quota's filtered CSV or full-snapshot CSV/JSON, Reservations' filtered report formats, Radar's runbook download, or Evidence Locker's JSON bundle for the appropriate handoff. Case Files and Telemetry Intelligence have no dedicated download button.
2. Before creating a ticket, review its destination and data. Connector selection sends immediately, while digest preview sends nothing. `teleintel.read` can authorize ticket creation; `radar.manage` and `evidence.write` authorize their respective ticket paths. All three UI ticket pickers additionally require `connectors.manage` to list connectors.
3. Recheck uncertain creation outcomes before repeating requests. Case handoff is best-effort, findings create new runs, and tickets can be duplicated.
4. Use evidence **Trash → Restore** only for a reversible snapshot deletion. Evidence purge has no undo; Case Files soft deletion has no restore UI/API despite retaining storage records. Do not substitute either for an approved retention process.

**Expected result:** The selected export, handoff, or recovery action has a known effect and does not imply an unsupported retry, restore, or Azure rollback.

**Verification:** Confirm restored content/hash, new case/finding/ticket state, or exported row scope as applicable. Consult each feature's troubleshooting steps for cache replacement, lost streams, or delivery failures.

Treat exports, telemetry rows, evidence bundles, case notes, ticket content, and share links as sensitive. Never use real identifiers, secrets, access tokens, or customer payloads in examples.

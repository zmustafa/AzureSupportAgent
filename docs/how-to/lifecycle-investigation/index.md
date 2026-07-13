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

## Common operating pattern

1. Select the correct connection and the narrowest useful workload, subscription, resource, or component.
2. Check generated time, retention window, cache age, collector errors, and partial-result indicators.
3. Refresh or scan only when the decision requires newer evidence.
4. Validate AI narrative and derived risk against raw rows and authoritative Azure data.
5. Preserve minimum necessary evidence before remediation.
6. Verify externally applied changes with fresh data, then update the case timeline.

Treat exports, telemetry rows, evidence bundles, case notes, ticket content, and share links as sensitive. Never use real identifiers, secrets, access tokens, or customer payloads in examples.

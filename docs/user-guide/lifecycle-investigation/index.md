---
layout: default
title: Lifecycle & Investigation
parent: User guide
nav_order: 7
description: Track lifecycle risks, investigate telemetry, preserve evidence, and manage durable cases.
permalink: /user-guide/lifecycle-investigation/
has_children: true
---

# Lifecycle & investigation

These tools turn time-sensitive estate signals into an accountable investigation record. They are read-oriented by default: refresh or scan explicitly, interpret scope and freshness, then hand material evidence into a finding, ticket, or case.

| Guide | Use it to |
| --- | --- |
| [Retirement Radar]({{ site.baseurl }}/user-guide/lifecycle-investigation/retirement-radar/) | Map Azure retirements and breaking changes to affected workloads. |
| [Reservations Monitor]({{ site.baseurl }}/user-guide/lifecycle-investigation/reservations-monitor/) | Track reservation expiry, renewal mode, and utilization. |
| [Quota Monitor]({{ site.baseurl }}/user-guide/lifecycle-investigation/quota-monitor/) | Find regional capacity headroom before deployments fail. |
| [Telemetry Intelligence]({{ site.baseurl }}/user-guide/lifecycle-investigation/telemetry-intelligence/) | Translate questions to bounded KQL and correlate failures. |
| [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/) | Capture hash-stamped point-in-time evidence. |
| [Case Files]({{ site.baseurl }}/user-guide/lifecycle-investigation/case-files/) | Preserve investigation, remediation, and verification history. |

## Common operating model

1. Select the intended connection and workload, subscription, or component scope.
2. Check generated time, cache age, and any truncation or unreadable indicators.
3. Refresh only when current Azure state is required.
4. Validate AI narrative against displayed queries and source rows.
5. Preserve decision-grade evidence, attach it to a case, and record verification before closure.

Never place live tenant IDs, resource IDs, webhook URLs, tokens, or personal data in screenshots or examples.

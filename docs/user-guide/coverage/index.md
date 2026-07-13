---
layout: default
title: Coverage
description: Audit monitoring, telemetry, backup protection, alert operations, and connection reachability.
parent: User guide
nav_order: 4
permalink: /user-guide/coverage/
has_children: true
---

# Coverage

Coverage views compare the estate with operational baselines and expose the connection blind spots that can make an assessment incomplete. Opening a view reads its latest saved result; use the view's refresh or scan action when current Azure state is required.

| Guide | Use it to |
| --- | --- |
| [Monitoring Coverage]({{ site.baseurl }}/user-guide/coverage/monitoring-coverage/) | Compare metric alerts with the Azure Monitor Baseline Alerts (AMBA) reference. |
| [Alerts Manager]({{ site.baseurl }}/user-guide/coverage/alerts-manager/) | Triage fired alerts and safely manage rules, action groups, and proposed changes. |
| [Telemetry Coverage]({{ site.baseurl }}/user-guide/coverage/telemetry-coverage/) | Find missing diagnostic settings, categories, and approved destinations. |
| [Backup & DR Coverage]({{ site.baseurl }}/user-guide/coverage/backup-dr-coverage/) | Assess protection, recovery recency, resilience, and DR pairing. |
| [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/) | See which configured connections can reach each required Azure surface. |

## Shared operating model

1. Choose the intended Azure connection and workload or subscription scope.
2. Check the generated time, age, and stale indicator before interpreting a score.
3. Refresh explicitly if the saved result is absent or too old for the decision.
4. Investigate gaps and unreadable resources separately; an unreadable result is not proof of non-compliance.
5. Preview and review every generated artifact. Deployment remains an external, operator-controlled step unless a feature explicitly presents an approved write action.
6. Re-scan after remediation to verify the observed state.

> Coverage is evidence, not a guarantee. Azure permissions, scan caps, unsupported resource types, and connection capabilities can reduce the observed estate.

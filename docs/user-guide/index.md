---
layout: default
title: User guide
nav_order: 3
description: Learn every user-facing area of Azure Support Agent.
permalink: /user-guide/
redirect_from:
  - /USER_GUIDE/
feature_ids: [SHELL_NAV:chat, SHELL_NAV:workloads, SHELL_NAV:proactive, SHELL_NAV:automations]
has_children: true
---

# User guide

Use this guide to understand every major application area, its prerequisites, workflow, outputs, and safety boundaries. Each area also has numbered procedures in the [How-to guides]({{ site.baseurl }}/how-to/); the third column links straight to them.

![The Proactive Support tool directory, grouped by what each tool answers]({{ site.baseurl }}/assets/proactive-support.png)

| Area | What it covers | Procedures |
| --- | --- | --- |
| [Core experience]({{ site.baseurl }}/user-guide/core/) | Dashboard, Chat, Deep Investigation, the Proactive Support directory, Monitor, and Stats | [Core and workload recipes]({{ site.baseurl }}/how-to/core-workloads/) |
| [Workloads]({{ site.baseurl }}/user-guide/workloads/) | Fleet, Autopilot discovery, workload detail, groups, and overlaps | [Core and workload recipes]({{ site.baseurl }}/how-to/core-workloads/) |
| [Mission Control]({{ site.baseurl }}/user-guide/mission-control/) | Coordinated multi-system workload sweeps | [Run a Mission Control sweep]({{ site.baseurl }}/how-to/core-workloads/mission-control/) |
| [Design & Ownership]({{ site.baseurl }}/user-guide/design-ownership/) | AI Insight Packs, Architectures, Know-Me, Ownership, Estate Graph, sandbox diagnostics, and network/DNS diagnostics | [Design and assessment recipes]({{ site.baseurl }}/how-to/design-assessment/) |
| [Assessment & Performance]({{ site.baseurl }}/user-guide/assessment-performance/) | Assessments, Performance Profiler, and FMEA | [Design and assessment recipes]({{ site.baseurl }}/how-to/design-assessment/) |
| [Coverage]({{ site.baseurl }}/user-guide/coverage/) | Monitoring Coverage, Alerts Manager, Telemetry Coverage, Backup & DR Coverage, Backup Manager, and Connection Capability | [Coverage recipes]({{ site.baseurl }}/how-to/coverage/) |
| [Estate Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/) | Inventory, Tag Intelligence, and Change Explorer | [Estate intelligence recipes]({{ site.baseurl }}/how-to/estate-intelligence/) |
| [Governance & Identity]({{ site.baseurl }}/user-guide/governance-identity/) | Azure Policy; Entra ID posture, Conditional Access, privileged access, applications, guests, findings, and blast radius; IAM effective access, access paths, reviews and PIM, and change simulation | [Governance and identity recipes]({{ site.baseurl }}/how-to/governance-identity/) |
| [Lifecycle & Investigation]({{ site.baseurl }}/user-guide/lifecycle-investigation/) | Retirement Radar, Reservations Monitor, Quota Monitor, Telemetry Intelligence, Evidence Locker, and Case Files | [Lifecycle and investigation recipes]({{ site.baseurl }}/how-to/lifecycle-investigation/) |
| [Automations]({{ site.baseurl }}/user-guide/automations/) | Scheduled Tasks, Sub Agents, Workbooks, Playbooks, and Notifications | [Automations and connector recipes]({{ site.baseurl }}/how-to/automations-connectors/) |

Configuration, access control, reference sets, and observability live in [Administration]({{ site.baseurl }}/admin/).

## A reliable operating pattern

1. Select the intended Azure connection and workload or subscription scope.
2. Check data freshness, permissions, and any partial-result indicators.
3. Refresh only when current Azure state is required.
4. Validate AI narratives against displayed source evidence.
5. Preview generated changes and preserve approval gates.
6. Re-scan after remediation and record verification.

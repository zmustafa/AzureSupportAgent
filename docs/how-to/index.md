---
layout: default
title: How-to guides
nav_order: 4
description: Step-by-step procedures for every Azure Support Agent application area and operational use case.
permalink: /how-to/
feature_ids: [SHELL_NAV:chat, SHELL_NAV:workloads, SHELL_NAV:proactive, SHELL_NAV:automations]
has_children: true
---

# How-to guides

These recipes explain how to complete real tasks in Azure Support Agent. Each procedure identifies the application route, permissions and prerequisites, numbered actions, expected result, verification, safety or rollback considerations, and troubleshooting.

![Mission Control coordinating a multi-system workload sweep]({{ site.baseurl }}/assets/mission-control.png)

## Start with these

| Task | Recipe |
| --- | --- |
| Discover the estate and save the first workloads | [Run Workload Autopilot]({{ site.baseurl }}/how-to/core-workloads/autopilot/) |
| Collect Entra ID data for the first time | [First Entra collection]({{ site.baseurl }}/how-to/governance-identity/entra-first-refresh/) |
| Find and close alert gaps on a workload | [Monitoring Coverage]({{ site.baseurl }}/how-to/coverage/monitoring-coverage/) |
| Triage a failed or missing backup | [Backup Manager]({{ site.baseurl }}/how-to/coverage/backup-manager/) |
| Close a Conditional Access coverage gap | [Conditional Access coverage gaps]({{ site.baseurl }}/how-to/governance-identity/close-ca-coverage-gaps/) |
| Lock the application down to known IP ranges | [Restrict network access by IP]({{ site.baseurl }}/how-to/administration/network-access/) |

## Every recipe area

| Area | Recipes | Feature reference |
| --- | --- | --- |
| [Core and workload operations]({{ site.baseurl }}/how-to/core-workloads/) | Dashboard, Chat, Deep Investigation, Proactive Support, Monitor, Stats, workload fleet, Autopilot discovery, workload detail and groups, and Mission Control | [Core experience]({{ site.baseurl }}/user-guide/core/) |
| [Design and assessment operations]({{ site.baseurl }}/how-to/design-assessment/) | Insight Packs, Architectures and Know-Me, Ownership, Estate Graph, Assessments, Performance Profiler, FMEA, sandbox diagnostics, connectivity tests, and private DNS debugging | [Design & Ownership]({{ site.baseurl }}/user-guide/design-ownership/) |
| [Coverage operations]({{ site.baseurl }}/how-to/coverage/) | Monitoring Coverage, Alerts Manager, Telemetry Coverage, Backup & DR Coverage, Backup Manager, and Connection Capability | [Coverage]({{ site.baseurl }}/user-guide/coverage/) |
| [Estate intelligence operations]({{ site.baseurl }}/how-to/estate-intelligence/) | Inventory, Tag Intelligence, and Change Explorer | [Estate Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/) |
| [Governance and identity]({{ site.baseurl }}/how-to/governance-identity/) | Policy inventory, pivots, effective policy, rollout and drift; Entra collection, findings, Conditional Access gaps, privileged activity and guest reviews; IAM access reviews, scanner inbox, escalation review, disabled access, and attribute changes | [Governance & Identity]({{ site.baseurl }}/user-guide/governance-identity/) |
| [Lifecycle and investigation]({{ site.baseurl }}/how-to/lifecycle-investigation/) | Retirement Radar, Reservations Monitor, Quota Monitor, Telemetry Intelligence, Evidence Locker, and Case Files | [Lifecycle & Investigation]({{ site.baseurl }}/user-guide/lifecycle-investigation/) |
| [Automations and connectors]({{ site.baseurl }}/how-to/automations-connectors/) | Scheduled Tasks, Sub Agents, Workbooks, Playbooks, Notifications, connector lifecycle, and every implemented connector type | [Automations]({{ site.baseurl }}/user-guide/automations/) |
| [Administration tasks]({{ site.baseurl }}/how-to/administration/) | Providers, tenants, sandbox VMs, connectors, general settings, access control, security and sessions, network access, prompts and scoring, reference sets, usage and audit, MCP tools, durable batches, backup, and demo data | [Administration]({{ site.baseurl }}/admin/) |

## How to use a recipe

1. Confirm the route and scope before running a scan or editing a record.
2. Check both product permissions and Azure/Graph permissions.
3. Review freshness, cache, truncation, and partial-result indicators.
4. Verify generated or AI-authored content against source evidence.
5. Preview write operations, preserve approvals, and understand rollback before apply.
6. Re-query the owning system after a change and preserve verification evidence.

{: .warning }
Examples intentionally contain no live tenant identifiers, resource IDs, tokens, receiver addresses, or credentials. Keep operational exports and screenshots out of public documentation unless they are sanitized.

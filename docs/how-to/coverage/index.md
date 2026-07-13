---
layout: default
title: Coverage operations
parent: How-to guides
nav_order: 1
description: Task recipes for monitoring, alerts, telemetry, backup protection, and connection capability.
permalink: /how-to/coverage/
has_children: true
---

# Coverage operations

Use these recipes to collect current coverage evidence, close supported gaps through reviewed changes, and verify the result.

## Prerequisites

- Sign in with access to the intended Azure connection and scope.
- Confirm the product permission named by each guide.
- Use [Connection Capability]({{ site.baseurl }}/how-to/coverage/connection-capability/) before interpreting an unexpectedly empty scan.

## Route

Open the feature route listed in the selected guide.

## How to choose the right coverage workflow

1. Use [Monitoring Coverage]({{ site.baseurl }}/how-to/coverage/monitoring-coverage/) for AMBA metric-alert coverage and saved fleet results.
2. Use [Alerts Manager]({{ site.baseurl }}/how-to/coverage/alerts-manager/) for fired-alert triage, overlaps, rule and Action Group authoring, deployment plans, and approval-gated Azure changes.
3. Use [Telemetry Coverage]({{ site.baseurl }}/how-to/coverage/telemetry-coverage/) for diagnostic-setting categories and destinations.
4. Use [Backup & DR Coverage]({{ site.baseurl }}/how-to/coverage/backup-dr-coverage/) for backup, restore-test, replication, and resilience evidence.
5. Use [Connection Capability]({{ site.baseurl }}/how-to/coverage/connection-capability/) to explain collection blind spots or disabled writes.

**Expected result:** You start from the feature whose collector and reference match the control being investigated.

**Verification:** Confirm the route, selected connection, scope, and result timestamp before acting on a score.

## Safety and rollback

Coverage scans are read-only, but Alerts Manager can apply approved Azure changes. Generated IaC and runbooks are artifacts, not deployments. Preserve evidence before purging runs, and use each feature's verification procedure after an external or managed change.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| A page is empty | Check connection capability, scope, Azure Reader access, and whether an explicit first scan is required. |
| A score changed without an Azure change | Check reference revisions, age-based checks, newly discovered resources, and partial collection warnings. |
| A write control is missing | Check the action-specific product permission, connection read-only state, approval state, and Azure RBAC. |

## Related docs

- [Coverage feature reference]({{ site.baseurl }}/user-guide/coverage/)
- [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)

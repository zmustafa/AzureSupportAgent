---
layout: default
title: Reference Sets & Change Requests
parent: Administration
nav_order: 7
description: Curate AMBA, telemetry, Backup/DR, and retirement references and decide proposed changes.
permalink: /admin/reference-sets-change-requests/
---

# Reference sets and change requests

**Permissions:** `coverage.manage` and/or `settings.write` as enforced by the screen

## Purpose

**App routes:** `/admin/amba`, `/admin/ambachanges`, `/admin/telemetry`, `/admin/telemetrychanges`, `/admin/backupdr`, `/admin/backupdrchanges`, `/admin/radar`
Reference sets define what coverage and lifecycle features expect. Changes can alter scores and generated remediation without changing Azure directly.

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### AMBA

Curate recommended Azure Monitor alerts per resource type: signal/metric, operator, threshold/unit, aggregation/window, severity, and classification. Use revision/history/reset/import/export controls shown. **AMBA Change Requests** presents proposed before/after changes for approve or reject with rationale.

### Telemetry

Curate recommended diagnostic log/metric categories and destination expectations per resource type. Approved Log Analytics workspaces are maintained in General settings. **Telemetry Change Requests** governs proposed additions/removals before they affect coverage.

### Backup/DR

Curate protection checks by resource type, including backup, replication, recent job/test, and severity semantics. **Backup/DR Change Requests** governs proposals. The reference is an expectation; it does not configure vaults or replication.

### Retirement Radar

Maintain classification rules (keywords, change type, service, replacement, migration URL/date where shown) and model lifecycle rows (model/version/stage and GA/deprecation/retirement/replacement). Use revisions, restore, or built-in reset rather than silently erasing history.

### Review procedure

1. Confirm source and affected resource types.
2. Inspect before/after values and downstream score/remediation impact.
3. Check duplicate/conflicting rules and region/API support.
4. Approve or reject with a reason.
5. Re-run a representative coverage scan and document changed baselines.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations



## Troubleshooting


Use the checks below when results differ from expectations.

## Related pages

- [Monitoring Coverage]({{ site.baseurl }}/user-guide/coverage/monitoring-coverage/)
- [Telemetry Coverage]({{ site.baseurl }}/user-guide/coverage/telemetry-coverage/)
- [Backup & DR Coverage]({{ site.baseurl }}/user-guide/coverage/backup-dr-coverage/)
- [Retirement Radar]({{ site.baseurl }}/user-guide/lifecycle-investigation/retirement-radar/)

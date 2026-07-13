---
layout: default
title: Scheduled Tasks
parent: Automations
grand_parent: User guide
nav_order: 1
description: Create, preview, enable, run, and monitor recurring automation targets.
permalink: /user-guide/automations/scheduled-tasks/
---

# Scheduled Tasks

**Permissions:** `tasks.read`, `tasks.write`, `tasks.run`

## Purpose

**App route:** `/automations/tasks`
A scheduled task invokes one supported target: agent, assessment, workbook, or playbook. Schedules can be daily, weekly, or cron-based, with an IANA time zone. The scheduler checks due work periodically and limits concurrent execution.

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Create and validate

1. Choose the target type and existing target.
2. Supply only the target parameters required for its documented scope.
3. Build a daily/weekly recurrence or enter cron; set the intended time zone.
4. Preview upcoming occurrences, especially around daylight-saving transitions.
5. Choose connector destinations only when the result should leave the app.
6. Save disabled, use **Run now**, inspect output/history, then enable.

The list shows schedule, enabled state, last run, next run, and history. Runs are preserved as separate records rather than overwriting prior output. Archive obsolete tasks; restore or purge only under the appropriate retention process.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety

A task inherits target permissions, connection read-only policy, tool classification, and approval behavior. Never use a schedule to bypass human review of destructive operations. Limit concurrency and avoid overlapping scans against the same Azure scope.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Next run is unexpected | Check cron, IANA time zone, and daylight-saving boundary in Preview. |
| Task never runs | Verify enabled state, valid target, scheduler health, and next-run time. |
| Run is queued | The concurrent-run ceiling may be active; inspect other running tasks. |
| Delivery missing | Review run result, notification rule, destination connector, and delivery log. |
| Run now is hidden | `tasks.run` is required separately from read/write. |

## Related pages

### Related docs

- [Workbooks]({{ site.baseurl }}/user-guide/automations/workbooks/)
- [Playbooks]({{ site.baseurl }}/user-guide/automations/playbooks/)
- [Notifications]({{ site.baseurl }}/user-guide/automations/notifications/)
